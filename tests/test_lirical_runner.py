"""Running LIRICAL as a sibling ECS task.

The contract that matters is that this NEVER raises. A review must complete
whether or not the engine answered — losing a whole review because a Fargate
task failed to place would be a far worse outcome than a report with no engine
section. Every failure path below therefore asserts a returned reason, not an
exception.

No test touches AWS: the ECS client is injected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adoc.knowledge.lirical import LiricalRequest
from adoc.knowledge.lirical_runner import LIRICAL_DATA_DIR, EcsLiricalRunner

_TSV = """\
! LIRICAL TSV Output (v2.4.1)
! Sample: patient
rank\tdiseaseName\tdiseaseCurie\tpretestprob\tposttestprob\tcompositeLR
1\tSjogren syndrome\tOMIM:270150\t1/8621\t12.5%\t4.82
2\tRelapsing polychondritis\tORPHA:728\t1/8621\t3.1%\t2.10
"""


class FakeEcs:
    """Just enough of the ECS client, scripted per test."""

    def __init__(
        self,
        *,
        run_result: dict[str, Any] | None = None,
        describe_sequence: list[dict[str, Any]] | None = None,
        run_raises: Exception | None = None,
        writes_output_into: Path | None = None,
    ) -> None:
        # Stands in for the sidecar writing its TSV onto the shared EFS mount.
        self.writes_output_into = writes_output_into
        self.run_result = run_result or {"tasks": [{"taskArn": "arn:aws:ecs:::task/abc"}]}
        self.describe_sequence = describe_sequence or [
            {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 0}]}]}
        ]
        self.run_raises = run_raises
        self.run_calls: list[dict[str, Any]] = []
        self.stopped: list[str] = []

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.run_calls.append(kwargs)
        if self.run_raises is not None:
            raise self.run_raises
        if self.writes_output_into is not None:
            # The runner has already created its per-run directory.
            for created in self.writes_output_into.iterdir():
                if created.is_dir():
                    (created / "lirical.tsv").write_text(_TSV, encoding="utf-8")
        return self.run_result

    def describe_tasks(self, **_kwargs: Any) -> dict[str, Any]:
        if len(self.describe_sequence) > 1:
            return self.describe_sequence.pop(0)
        return self.describe_sequence[0]

    def stop_task(self, **kwargs: Any) -> dict[str, Any]:
        self.stopped.append(kwargs.get("task", ""))
        return {}


def _runner(tmp_path: Path, ecs: FakeEcs, **overrides: Any) -> EcsLiricalRunner:
    kwargs: dict[str, Any] = {
        "cluster": "a-doc",
        "task_definition": "a-doc-lirical",
        "subnets": ["subnet-1"],
        "security_groups": ["sg-1"],
        "container_work_dir": "/data/a-doc-data/work/lirical",
        "client": ecs,
        "sleep": lambda _s: None,
    }
    kwargs.update(overrides)
    return EcsLiricalRunner(tmp_path, **kwargs)


def _request() -> LiricalRequest:
    return LiricalRequest(observed=["HP:0000988", "HP:0002315"])


def test_a_successful_run_returns_a_parsed_ranking(tmp_path: Path) -> None:
    ecs = FakeEcs(writes_output_into=tmp_path)

    run = _runner(tmp_path, ecs).run(_request())

    assert run.ok, run.error
    assert run.result is not None
    assert [d.name for d in run.result.diseases] == [
        "Sjogren syndrome",
        "Relapsing polychondritis",
    ]
    assert run.terms_used == ["HP:0000988", "HP:0002315"]


def test_the_task_is_launched_with_the_expected_shape(tmp_path: Path) -> None:
    """Public subnets with no NAT gateway (ADR 0006), so a task without a
    public IP cannot pull its image and never starts."""
    ecs = FakeEcs()
    _runner(tmp_path, ecs).run(_request())

    call = ecs.run_calls[0]
    assert call["taskDefinition"] == "a-doc-lirical"
    assert call["launchType"] == "FARGATE"
    net = call["networkConfiguration"]["awsvpcConfiguration"]
    assert net["assignPublicIp"] == "ENABLED"
    override = call["overrides"]["containerOverrides"][0]
    assert override["name"] == "lirical"
    assert "prioritize" in override["command"]


def test_a_placement_failure_is_reported_not_raised(tmp_path: Path) -> None:
    ecs = FakeEcs(run_result={"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]})

    run = _runner(tmp_path, ecs).run(_request())

    assert not run.ok
    assert "placed nothing" in run.error
    assert "RESOURCE:MEMORY" in run.error


def test_a_boto_exception_is_reported_not_raised(tmp_path: Path) -> None:
    """An IAM misconfiguration must not take a review down with it."""
    ecs = FakeEcs(run_raises=PermissionError("AccessDeniedException: ecs:RunTask"))

    run = _runner(tmp_path, ecs).run(_request())

    assert not run.ok
    assert "run_task failed" in run.error


def test_a_nonzero_exit_is_reported(tmp_path: Path) -> None:
    ecs = FakeEcs(
        describe_sequence=[
            {
                "tasks": [
                    {
                        "lastStatus": "STOPPED",
                        "containers": [{"exitCode": 1}],
                        "stoppedReason": "Essential container exited",
                    }
                ]
            }
        ]
    )

    run = _runner(tmp_path, ecs).run(_request())

    assert not run.ok
    assert "exited 1" in run.error


def test_an_overrunning_task_is_stopped_and_reported(tmp_path: Path) -> None:
    """The timeout is ours, not a waiter's: a review hanging behind a stuck
    engine is worse than a review with no engine section, and the task is
    stopped rather than left running."""
    ecs = FakeEcs(describe_sequence=[{"tasks": [{"lastStatus": "RUNNING", "containers": [{}]}]}])
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])

    run = _runner(tmp_path, ecs, monotonic=lambda: next(clock, 10_000.0), timeout=60.0).run(
        _request()
    )

    assert not run.ok
    assert "timed out" in run.error
    assert ecs.stopped == ["arn:aws:ecs:::task/abc"]


def test_a_clean_exit_with_no_output_is_reported(tmp_path: Path) -> None:
    """The difference between "the engine ranked nothing" and "the engine
    never started" — a bootstrap failure leaves the directory empty."""
    ecs = FakeEcs()

    run = _runner(tmp_path, ecs).run(_request())

    assert not run.ok
    assert "wrote no output" in run.error


def test_an_unconfigured_runner_says_so(tmp_path: Path) -> None:
    """Local development has no cluster. That is a skip, not a crash."""
    run = _runner(tmp_path, FakeEcs(), cluster="", task_definition="").run(_request())

    assert not run.ok
    assert "not configured" in run.error


def test_a_request_with_no_valid_terms_is_reported(tmp_path: Path) -> None:
    """`build_prioritize_args` raises on an empty observed list; the runner
    must convert that into a reason rather than propagate it."""
    run = _runner(tmp_path, FakeEcs()).run(LiricalRequest(observed=["not-an-hpo-term"]))

    assert not run.ok
    assert "could not prepare run" in run.error


def test_the_data_dir_matches_the_sidecar_image() -> None:
    """Pins `LIRICAL_DATA_DIR` against the Dockerfile that builds the image.

    These are two artifacts that must agree and have no compiler between
    them. They disagreed — the image downloads to /opt/liricaldata, the
    runner passed /lirical-data — and every launched task exited 1 with
    "Missing required file `hp.json`".

    The build-time smoke test could not catch it: it invokes prioritize with
    the image's own `$LIRICAL_DATA`, so it exercised the correct path while
    the only real caller passed a different one.
    """
    import re
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parents[1] / "deploy" / "lirical" / "Dockerfile"
    match = re.search(r"^ENV LIRICAL_DATA=(\S+)", dockerfile.read_text(), re.M)

    assert match, "deploy/lirical/Dockerfile no longer sets ENV LIRICAL_DATA"
    assert match.group(1) == LIRICAL_DATA_DIR, (
        f"the image stores LIRICAL's data in {match.group(1)} but the runner passes "
        f"{LIRICAL_DATA_DIR}; every launched task will exit 1 on missing data files"
    )

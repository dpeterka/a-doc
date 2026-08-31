"""Run LIRICAL and get a parsed ranking back.

ADR 0029 put LIRICAL in its own image rather than a JRE in the app image: a
JRE is ~200MB and a second runtime on every always-on web task, for something
only the deep review uses, a few times a day, for about a second. The cost of
that choice is that invoking it is not a function call — it is launching a
sibling ECS task and waiting, with input and output exchanged on the shared
EFS mount.

Two runners implement the same protocol:

- `EcsLiricalRunner` — what production uses.
- `SubprocessLiricalRunner` — a local `lirical` binary, for development and
  for anyone running this outside AWS.

Neither raises. A review must complete whether or not the engine answered, so
failure is returned as `LiricalRun(ok=False, error=...)` and the caller
renders "the phenotype engine did not run this week" rather than losing the
review. That is the same contract the PubMed client follows.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from adoc.knowledge.lirical import (
    LiricalRequest,
    LiricalResult,
    build_prioritize_args,
    parse_lirical_tsv,
)

logger = logging.getLogger(__name__)

# LIRICAL itself takes about a second once the JVM is warm, but a Fargate task
# has to be placed, pull nothing (the image is cached per host, not
# guaranteed), mount EFS and start a JVM that loads the HPO ontology and
# phenotype.hpoa into heap. Measured cold starts for the jobs task in this
# account run 40-90s; this leaves room for a bad one without letting a review
# hang indefinitely behind it.
DEFAULT_TIMEOUT_SECONDS = 420.0
DEFAULT_POLL_SECONDS = 5.0

# Where the two containers meet. Both mount the same EFS access point, the app
# at its data dir and LIRICAL at /data, so a path under the data repo's `work/`
# is visible to both.
LIRICAL_WORK_RELDIR = "work/lirical"

# LIRICAL's own data directory inside its image (deploy/lirical/), baked in at
# build time so a running container has no network dependency.
# Where LIRICAL's data sits INSIDE the sidecar image. This must match
# `ENV LIRICAL_DATA` in deploy/lirical/Dockerfile exactly.
#
# It did not. The image downloads to /opt/liricaldata and this said
# /lirical-data, so every launched task died with "Missing required file
# `hp.json` in `/lirical-data`" and exit 1. The build-time smoke test could
# not catch it: that test invokes `prioritize -d "$LIRICAL_DATA"`, using the
# image's own env var, so it exercised the correct path while the only real
# caller passed a different one. A green build proved nothing about the call
# that matters.
#
# `tests/test_lirical_runner.py` now pins this against the Dockerfile so the
# two cannot drift again.
LIRICAL_DATA_DIR = "/opt/liricaldata"


class LiricalRun(BaseModel):
    """One invocation. `ok=False` carries the reason and is not an error."""

    ok: bool = False
    error: str = ""
    result: LiricalResult | None = None
    terms_used: list[str] = Field(default_factory=list)
    terms_excluded: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


class LiricalRunner(Protocol):
    def run(self, request: LiricalRequest) -> LiricalRun: ...


def _read_output(out_dir: Path, prefix: str) -> str | None:
    """The TSV LIRICAL wrote, if it wrote one.

    LIRICAL names its output `<prefix>.tsv`; a run that failed at bootstrap
    leaves the directory empty, which is the difference between "the engine
    ranked nothing" and "the engine never started".
    """
    candidate = out_dir / f"{prefix}.tsv"
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    # Be forgiving about the exact name across LIRICAL versions rather than
    # reporting a failure because a suffix moved.
    for path in sorted(out_dir.glob("*.tsv")):
        return path.read_text(encoding="utf-8")
    return None


class SubprocessLiricalRunner:
    """Runs a local `lirical` binary. Development and non-AWS use."""

    def __init__(
        self,
        work_dir: Path,
        *,
        binary: str = "lirical",
        data_dir: str = LIRICAL_DATA_DIR,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._work_dir = work_dir
        self._binary = binary
        self._data_dir = data_dir
        self._timeout = timeout

    def run(self, request: LiricalRequest) -> LiricalRun:
        if shutil.which(self._binary) is None:
            return LiricalRun(ok=False, error=f"{self._binary} is not on PATH")

        observed, negated = request.validated_terms()
        out_dir = self._work_dir / uuid.uuid4().hex[:12]
        started = time.monotonic()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            args = build_prioritize_args(request, data_dir=self._data_dir, out_dir=str(out_dir))
            subprocess.run(  # noqa: S603 - fixed binary, args built by code
                [self._binary, *args],
                check=True,
                capture_output=True,
                timeout=self._timeout,
            )
            text = _read_output(out_dir, "lirical")
            if text is None:
                return LiricalRun(ok=False, error="lirical produced no output file")
            return LiricalRun(
                ok=True,
                result=parse_lirical_tsv(text),
                terms_used=observed,
                terms_excluded=negated,
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired:
            return LiricalRun(ok=False, error=f"lirical timed out after {self._timeout:.0f}s")
        except Exception as exc:  # noqa: BLE001 - never raise into a review
            return LiricalRun(ok=False, error=f"{type(exc).__name__}: {exc}")


class EcsLiricalRunner:
    """Launches the `a-doc-lirical` task and waits for it.

    The IAM this needs is narrow and granted in `deploy/cfn/ecs.yaml`:
    `ecs:RunTask` on the `a-doc-lirical` family in this cluster only,
    `iam:PassRole` for the two roles that task definition names (conditioned
    on `ecs-tasks`, without which PassRole on a role is close to being that
    role), and `DescribeTasks`/`StopTask` to poll and to clean up a run that
    overran.

    Output is exchanged on EFS: both containers mount the same access point,
    so a directory under the data repo's `work/` is visible to both.
    """

    def __init__(
        self,
        work_dir: Path,
        *,
        cluster: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        container_work_dir: str,
        data_dir: str = LIRICAL_DATA_DIR,
        client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._work_dir = work_dir
        self._cluster = cluster
        self._task_definition = task_definition
        self._subnets = subnets
        self._security_groups = security_groups
        self._container_work_dir = container_work_dir.rstrip("/")
        self._data_dir = data_dir
        self._client = client
        self._timeout = timeout
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def _ecs(self) -> Any:
        if self._client is None:
            import boto3  # imported lazily: a local run never needs it

            self._client = boto3.client("ecs")
        return self._client

    def run(self, request: LiricalRequest) -> LiricalRun:
        if not self._cluster or not self._task_definition:
            return LiricalRun(ok=False, error="lirical task is not configured")

        observed, negated = request.validated_terms()
        run_id = uuid.uuid4().hex[:12]
        host_out = self._work_dir / run_id
        container_out = f"{self._container_work_dir}/{run_id}"
        started = self._monotonic()

        try:
            host_out.mkdir(parents=True, exist_ok=True)
            args = build_prioritize_args(request, data_dir=self._data_dir, out_dir=container_out)
        except Exception as exc:  # noqa: BLE001
            return LiricalRun(ok=False, error=f"could not prepare run: {type(exc).__name__}: {exc}")

        try:
            ecs = self._ecs()
            launched = ecs.run_task(
                cluster=self._cluster,
                taskDefinition=self._task_definition,
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": self._subnets,
                        "securityGroups": self._security_groups,
                        # Public subnets with no NAT gateway (ADR 0006), so a
                        # task without a public IP cannot pull its image.
                        "assignPublicIp": "ENABLED",
                    }
                },
                overrides={"containerOverrides": [{"name": "lirical", "command": args}]},
            )
            tasks = launched.get("tasks") or []
            if not tasks:
                failures = launched.get("failures") or []
                return LiricalRun(ok=False, error=f"run_task placed nothing: {failures}")
            task_arn = tasks[0]["taskArn"]
        except Exception as exc:  # noqa: BLE001 - never raise into a review
            return LiricalRun(ok=False, error=f"run_task failed: {type(exc).__name__}: {exc}")

        exit_code = self._await_exit(task_arn, started)
        if isinstance(exit_code, str):
            return LiricalRun(
                ok=False, error=exit_code, duration_seconds=self._monotonic() - started
            )

        text = _read_output(host_out, "lirical")
        if text is None:
            return LiricalRun(
                ok=False,
                error=f"lirical exited {exit_code} and wrote no output",
                duration_seconds=self._monotonic() - started,
            )
        try:
            result = parse_lirical_tsv(text)
        except Exception as exc:  # noqa: BLE001
            return LiricalRun(
                ok=False, error=f"could not parse output: {type(exc).__name__}: {exc}"
            )

        return LiricalRun(
            ok=True,
            result=result,
            terms_used=observed,
            terms_excluded=negated,
            duration_seconds=self._monotonic() - started,
        )

    def _await_exit(self, task_arn: str, started: float) -> int | str:
        """The container's exit code, or an error string.

        Polls rather than using a waiter so the timeout is ours: a review that
        hangs behind a stuck engine is worse than a review with no engine
        section, and an overrun task is stopped rather than left running.
        """
        ecs = self._ecs()
        while True:
            if self._monotonic() - started > self._timeout:
                try:
                    ecs.stop_task(
                        cluster=self._cluster, task=task_arn, reason="a-doc: review timeout"
                    )
                except Exception as exc:  # noqa: BLE001 - best effort
                    logger.warning("lirical: could not stop the overrunning task: %s", exc)
                return f"lirical timed out after {self._timeout:.0f}s"

            self._sleep(self._poll_seconds)
            try:
                described = ecs.describe_tasks(cluster=self._cluster, tasks=[task_arn])
            except Exception as exc:  # noqa: BLE001
                return f"describe_tasks failed: {type(exc).__name__}: {exc}"

            tasks = described.get("tasks") or []
            if not tasks:
                return "the launched task disappeared before it finished"
            task = tasks[0]
            if task.get("lastStatus") != "STOPPED":
                continue

            containers = task.get("containers") or [{}]
            code = containers[0].get("exitCode")
            if code is None:
                return f"lirical stopped without an exit code: {task.get('stoppedReason', '')}"
            if int(code) != 0:
                return f"lirical exited {code}: {task.get('stoppedReason', '')}"
            return int(code)

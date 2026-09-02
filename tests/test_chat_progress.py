"""Tests for the stage progress ticker — ADR 0046 (PAT-06).

PAT-06 says the UI "displays only a static loading spinner, causing patients
to believe the application has frozen". That is not what shipped: the page
already carries a labelled indicator saying the wait can take a few minutes
and that she can close the tab. What was missing is WHICH minute, and these
tests pin that without weakening the honest line it sits inside.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import BaseModel
from web_support import build_app, build_fake_client, exploding_transport, login

from adoc.reason.dag import Dag, Node, run
from adoc.reason.progress import (
    FALLBACK_LABEL,
    STAGE_LABELS,
    ProgressTracker,
)


class Payload(BaseModel):
    v: int = 0


def test_the_tracker_reports_the_current_stage() -> None:
    tracker = ProgressTracker()
    tracker.start(total=4)

    tracker.note("challenger", 2)
    progress = tracker.read()

    assert progress.label == STAGE_LABELS["challenger"]
    assert (progress.step, progress.total) == (2, 4)
    assert progress.visible is True


def test_a_finished_turn_reports_nothing() -> None:
    """A page that keeps showing a stage after the reply arrived is worse
    than one that showed nothing."""
    tracker = ProgressTracker()
    tracker.start(total=4)
    tracker.note("composer", 4)

    tracker.finish()

    assert tracker.read().finished is True
    assert tracker.read().visible is False


def test_an_idle_tracker_reports_nothing() -> None:
    """Nothing running means nothing shown — not "step 0 of 0".

    Both guards are asserted, because either alone is sufficient and a test
    on the visible outcome would pass with one of them removed."""
    progress = ProgressTracker().read()

    assert progress.visible is False
    assert progress.finished is True
    assert progress.step == 0


def test_an_unmapped_node_falls_back_rather_than_leaking_an_identifier() -> None:
    """A new DAG node must not put an internal name on the patient's page."""
    tracker = ProgressTracker()
    tracker.start(total=4)

    tracker.note("some_new_internal_node", 2)

    assert tracker.read().label == FALLBACK_LABEL


def test_a_note_after_finishing_is_ignored() -> None:
    """The DAG's `finally` calls `finish`; a late callback from a thread
    must not resurrect a completed turn's status line.

    The guard inside `note` is asserted directly, not only through `read`:
    `read` gates on `finished` too, so a test on the visible outcome alone
    passes with the guard removed."""
    tracker = ProgressTracker()
    tracker.start(total=4)
    tracker.note("challenger", 2)
    tracker.finish()

    tracker.note("composer", 4)

    assert tracker.read().visible is False
    # The late note left no residue on the object either.
    assert tracker._label == ""
    assert tracker._step == 0


def test_an_abandoned_turn_stops_reporting() -> None:
    """A turn that dies between stages would otherwise leave the page
    reporting a stage forever."""
    tracker = ProgressTracker()
    tracker.start(total=4)
    tracker.note("challenger", 2)

    import adoc.reason.progress as module

    original = module._STALE_AFTER_SECONDS
    module._STALE_AFTER_SECONDS = 0.01
    try:
        time.sleep(0.05)
        assert tracker.read().visible is False
    finally:
        module._STALE_AFTER_SECONDS = original


def test_the_dag_reports_every_node_as_it_starts() -> None:
    """Called BEFORE the node runs, because the point is to say what is
    happening now — not what just finished."""
    seen: list[tuple[str, int, int]] = []

    def node(name: str, depends_on: str) -> Node:
        return Node(
            name=name,
            fn=lambda _ctx: Payload(),
            input_model=Payload,
            output_model=Payload,
            depends_on=depends_on,
        )

    run(
        Dag([node("first", "seed"), node("second", "first")]),
        {"seed": Payload()},
        on_node_start=lambda name, step, total: seen.append((name, step, total)),
    )

    assert seen == [("first", 1, 2), ("second", 2, 2)]


def test_a_failing_progress_hook_cannot_fail_the_run() -> None:
    """A status line must never be able to stop a reasoning run."""

    def boom(_name: str, _step: int, _total: int) -> None:
        raise RuntimeError("the status line exploded")

    result = run(
        Dag(
            [
                Node(
                    name="only",
                    fn=lambda _ctx: Payload(v=1),
                    input_model=Payload,
                    output_model=Payload,
                    depends_on="seed",
                )
            ]
        ),
        {"seed": Payload()},
        on_node_start=boom,
    )

    assert [r.name for r in result.nodes] == ["only"]


# --- the endpoint --------------------------------------------------------------------------------


def test_the_progress_endpoint_is_empty_when_nothing_is_running(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    body = client.get("/chat/progress").text

    assert body.strip() == ""


def test_the_progress_endpoint_shows_the_running_stage(tmp_path: Path) -> None:
    from adoc.reason.progress import TRACKER

    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    TRACKER.start(total=4)
    TRACKER.note("ledger_maintainer", 1)
    try:
        body = client.get("/chat/progress").text
    finally:
        TRACKER.finish()

    assert STAGE_LABELS["ledger_maintainer"] in body
    assert "step 1 of 4" in body


def test_the_progress_endpoint_requires_a_login(tmp_path: Path) -> None:
    """It is a low-value fragment, but an unauthenticated endpoint that
    reveals when the patient is using the app is still a leak."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app, follow_redirects=False)

    response = client.get("/chat/progress")

    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers["location"]


def test_progress_never_carries_any_part_of_the_turn(tmp_path: Path) -> None:
    """Only stage labels are published, so a poll can carry no patient
    content even if the response were cached or logged."""
    from adoc.reason.progress import TRACKER

    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    TRACKER.start(total=4)
    TRACKER.note("composer", 4)
    try:
        body = client.get("/chat/progress").text
    finally:
        TRACKER.finish()

    assert body.strip() != ""
    for label in STAGE_LABELS.values():
        body = body.replace(label, "")
    # What remains is markup, the step counter, and nothing else.
    assert "patient" not in body.lower()


def test_the_waiting_indicator_keeps_its_honest_wording(tmp_path: Path) -> None:
    """PAT-06 read the existing indicator as a bare spinner and proposed
    replacing it. The line that sets the expectation is the valuable part;
    the ticker is additive."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    body = client.get("/chat").text

    assert "can take a few minutes" in body
    assert "you can leave this page open" in body
    assert 'hx-get="/chat/progress"' in body


def test_every_node_of_the_real_diagnostic_dag_has_a_label(tmp_path: Path) -> None:
    """Pinned against the DAG the application actually builds, not against a
    list written here — otherwise adding a stage silently ships `Working...`
    to the patient, and a test over a hardcoded list would still pass."""
    from adoc.casefile.repo import LEDGER_RELPATH
    from adoc.reason.stages import build_diagnostic_dag

    calls: list = []
    _app, repo, db, _c = build_app(tmp_path)
    client = build_fake_client(exploding_transport(calls), exploding_transport(calls))
    sink: dict[str, object] = {}
    dag = build_diagnostic_dag(client, repo, repo.root / LEDGER_RELPATH, db, sink)  # type: ignore[arg-type]

    unlabelled = [n.name for n in dag.nodes if n.name not in STAGE_LABELS]

    assert unlabelled == [], f"stages with no patient-facing label: {unlabelled}"
    assert all(label.endswith("...") for label in STAGE_LABELS.values())


def test_the_labels_say_what_is_happening_not_which_module_runs() -> None:
    """`ledger_maintainer` is an internal name. What a waiting person needs
    is what it is doing to her records."""
    for name, label in STAGE_LABELS.items():
        assert name not in label.lower().replace(" ", "_")
        assert "_" not in label

"""A release asks for a review when it has never touched the ledger.

Shipping a change to the review and RUNNING it are different events, and the
gap between them is up to the 7-day floor. In September 2026 that gap
swallowed a whole work track: ADRs 0044, 0045, 0049, 0050, 0051, 0052 and 0053
deployed green across two releases while the ledger's last write was still
`0.31.1`. The board stayed at 46 active leads for five days. The tick logged
`skipped full review this tick` every 30 minutes — the correct message, and
nothing distinguished it from a stalled pipeline.

Worse, with no review running the only way to see what the new code did was to
call it by hand, and a probe's output reads exactly like a deployed outcome.
Two changelog entries were written that way.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from adoc.casefile.repo import HISTORY_RELPATH, DataRepo
from adoc.reason.review import (
    last_ledger_writer,
    mark_review_wanted_on_version_change,
)
from adoc.reason.review_trigger import load_review_marker, mark_review_wanted

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _repo(tmp_path: Path, *, wrote: str | None = "0.31.1") -> DataRepo:
    repo = DataRepo(tmp_path)
    (tmp_path / "case").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    if wrote is not None:
        (tmp_path / HISTORY_RELPATH).write_text(
            json.dumps(
                {
                    "resulting_version": 18,
                    "resulting_updated": "2026-09-04T14:50:04.744496+00:00",
                    "diff": {"provenance": {"app_version": wrote}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return repo


def test_a_version_that_has_never_written_asks_for_a_review(tmp_path: Path) -> None:
    """The production state, undetected for five days."""
    repo = _repo(tmp_path, wrote="0.31.1")

    reason = mark_review_wanted_on_version_change(repo, running_version="0.33.5", at=_NOW)

    assert reason is not None
    assert "0.33.5" in reason and "0.31.1" in reason
    marker = load_review_marker(repo)
    assert marker is not None and marker.reasons


def test_a_version_that_already_wrote_asks_for_nothing(tmp_path: Path) -> None:
    """Once a review has run under this version there is nothing to exercise,
    and the 7-day floor is the right cadence again."""
    repo = _repo(tmp_path, wrote="0.33.5")

    assert mark_review_wanted_on_version_change(repo, running_version="0.33.5", at=_NOW) is None
    assert load_review_marker(repo) is None


def test_asking_twice_for_the_same_version_sets_one_reason(tmp_path: Path) -> None:
    """The tick runs every 30 minutes. A review that legitimately writes no
    diff leaves the last writer unchanged, so without this the marker re-arms
    on every tick and a full review — four to five frontier calls — runs every
    six hours forever."""
    repo = _repo(tmp_path, wrote="0.31.1")

    first = mark_review_wanted_on_version_change(repo, running_version="0.33.5", at=_NOW)
    second = mark_review_wanted_on_version_change(repo, running_version="0.33.5", at=_NOW)

    assert first is not None
    assert second is None
    marker = load_review_marker(repo)
    assert marker is not None and len(marker.reasons) == 1


def test_a_marker_set_for_another_reason_is_not_disturbed(tmp_path: Path) -> None:
    """Ingest and chat turns set this marker too. Appending must not drop what
    they recorded — the review's report says what prompted it."""
    repo = _repo(tmp_path, wrote="0.31.1")
    mark_review_wanted(repo, "ingest: 3 new document(s)", at=_NOW)

    mark_review_wanted_on_version_change(repo, running_version="0.33.5", at=_NOW)

    marker = load_review_marker(repo)
    assert marker is not None
    assert [r.reason for r in marker.reasons][0] == "ingest: 3 new document(s)"
    assert len(marker.reasons) == 2


def test_a_ledger_nothing_has_ever_written_asks_for_nothing(tmp_path: Path) -> None:
    """A fresh repo has no history. `should_run_full_review` already returns
    True for "no full review has ever run", so a marker here would be noise —
    and claiming a version regression against nothing would be a lie."""
    repo = _repo(tmp_path, wrote=None)

    assert mark_review_wanted_on_version_change(repo, running_version="0.33.5", at=_NOW) is None


def test_an_unreadable_history_line_does_not_fail_the_tick(tmp_path: Path) -> None:
    """The tick runs every 30 minutes and does real deterministic work. It
    must not die because a version could not be compared."""
    repo = _repo(tmp_path, wrote=None)
    (tmp_path / HISTORY_RELPATH).write_text("{ not json\n", encoding="utf-8")

    assert last_ledger_writer(repo) is None
    assert mark_review_wanted_on_version_change(repo, running_version="0.33.5", at=_NOW) is None


def test_only_the_last_line_decides(tmp_path: Path) -> None:
    """The history is append-only and long. An old first line must not make a
    current ledger look like it was written by an ancient version."""
    repo = _repo(tmp_path, wrote=None)
    (tmp_path / HISTORY_RELPATH).write_text(
        json.dumps({"diff": {"provenance": {"app_version": "0.10.0"}}})
        + "\n"
        + json.dumps({"diff": {"provenance": {"app_version": "0.33.5"}}})
        + "\n",
        encoding="utf-8",
    )

    assert last_ledger_writer(repo) == "0.33.5"


def test_the_tick_asks_before_it_reads_the_marker() -> None:
    """Order is the whole mechanism. Asking after the marker is read sets a
    flag nothing consults until the next tick — 30 minutes of latency for no
    reason, and one more thing that works only by accident."""
    import inspect

    from adoc.reason import review as module

    source = inspect.getsource(module.run_review_tick)
    asks = source.index("mark_review_wanted_on_version_change(")
    reads = source.index("marker = load_review_marker(repo)")

    assert asks < reads

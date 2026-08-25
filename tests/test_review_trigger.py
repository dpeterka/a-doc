"""Tests for adoc.reason.review_trigger: the "review wanted" marker
(docs/adr/0019-event-triggered-review.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from adoc.casefile.repo import DataRepo
from adoc.reason.review_trigger import (
    REVIEW_MARKER_RELPATH,
    ReviewMarker,
    clear_review_marker,
    load_review_marker,
    mark_review_wanted,
)


def _repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


def test_load_review_marker_is_none_when_never_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert load_review_marker(repo) is None


def test_mark_review_wanted_creates_and_persists_the_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    mark_review_wanted(repo, "ingest: 1 new document(s), 3 new lab row(s)", at=at)

    assert (repo.root / REVIEW_MARKER_RELPATH).exists()
    marker = load_review_marker(repo)
    assert marker is not None
    assert len(marker.reasons) == 1
    assert marker.reasons[0].reason == "ingest: 1 new document(s), 3 new lab row(s)"
    assert marker.reasons[0].at == at
    assert marker.first_set_at == at
    assert marker.last_set_at == at


def test_mark_review_wanted_is_not_git_committed(tmp_path: Path) -> None:
    """`work/` is gitignored — the marker is a derived scheduling signal,
    not part of the patient's case-file record."""
    repo = _repo(tmp_path)
    mark_review_wanted(repo, "chat turn applied a ledger diff (1 op(s))")

    from git import Repo as GitRepo

    git_repo = GitRepo(repo.root)
    assert git_repo.is_dirty(untracked_files=True) is False


def test_mark_review_wanted_accumulates_multiple_reasons_in_order(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    second = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)

    mark_review_wanted(repo, "ingest: 2 new document(s), 0 new lab row(s)", at=first)
    marker = mark_review_wanted(repo, "chat turn applied a ledger diff (2 op(s))", at=second)

    assert [r.reason for r in marker.reasons] == [
        "ingest: 2 new document(s), 0 new lab row(s)",
        "chat turn applied a ledger diff (2 op(s))",
    ]
    assert marker.first_set_at == first
    assert marker.last_set_at == second


def test_marker_summary_deduplicates_consecutive_identical_reasons(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mark_review_wanted(repo, "ingest: 1 new document(s), 0 new lab row(s)")
    mark_review_wanted(repo, "ingest: 1 new document(s), 0 new lab row(s)")
    mark_review_wanted(repo, "chat turn applied a ledger diff (1 op(s))")

    marker = load_review_marker(repo)
    assert marker is not None
    assert marker.summary() == (
        "ingest: 1 new document(s), 0 new lab row(s); chat turn applied a ledger diff (1 op(s))"
    )


def test_marker_reasons_cap_keeps_the_most_recent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for i in range(60):
        mark_review_wanted(repo, f"reason {i}")

    marker = load_review_marker(repo)
    assert marker is not None
    assert len(marker.reasons) == 50
    assert marker.reasons[0].reason == "reason 10"
    assert marker.reasons[-1].reason == "reason 59"


def test_clear_review_marker_removes_the_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mark_review_wanted(repo, "ingest: 1 new document(s), 1 new lab row(s)")
    assert load_review_marker(repo) is not None

    clear_review_marker(repo)

    assert load_review_marker(repo) is None
    assert not (repo.root / REVIEW_MARKER_RELPATH).exists()


def test_clear_review_marker_is_a_noop_when_nothing_is_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    clear_review_marker(repo)  # must not raise
    assert load_review_marker(repo) is None


def test_load_review_marker_treats_corrupt_json_as_absent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = repo.root / REVIEW_MARKER_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    assert load_review_marker(repo) is None


def test_empty_marker_summary() -> None:
    assert ReviewMarker().summary() == "no reasons recorded"

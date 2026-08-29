"""Tests for adoc.intake.replay — recovering turns whose facts were lost."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adoc.casefile.repo import DataRepo
from adoc.intake.replay import find_dropped_turns, replay_dropped_turns


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


def _log(repo: DataRepo, entries: list[dict[str, Any]]) -> None:
    path = repo.root / "logs" / "chat" / "2026-08-29.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_a_turn_followed_by_an_error_is_found(repo: DataRepo) -> None:
    """The production case: a long message answered only with an apology, so
    its facts never reached the case file."""
    _log(
        repo,
        [
            {"role": "patient", "timestamp": "2026-08-29T00:24:26", "text": "A long history..."},
            {
                "role": "assistant",
                "kind": "error",
                "timestamp": "2026-08-29T00:26:52",
                "text": "Sorry — I had trouble recording that one.",
            },
        ],
    )

    dropped = find_dropped_turns(repo)

    assert [d.timestamp for d in dropped] == ["2026-08-29T00:24:26"]
    assert dropped[0].text == "A long history..."


def test_a_turn_that_was_answered_is_not_replayed(repo: DataRepo) -> None:
    """Replaying an answered turn would double-apply its facts."""
    _log(
        repo,
        [
            {"role": "patient", "timestamp": "T1", "text": "hello"},
            {"role": "assistant", "kind": "reply", "timestamp": "T2", "text": "hi"},
        ],
    )

    assert find_dropped_turns(repo) == []


def test_an_error_belonging_to_a_later_message_is_not_attributed_backwards(
    repo: DataRepo,
) -> None:
    """A patient turn answered normally, followed by another turn that failed,
    must not make the FIRST one look dropped."""
    _log(
        repo,
        [
            {"role": "patient", "timestamp": "T1", "text": "answered fine"},
            {"role": "assistant", "kind": "reply", "timestamp": "T2", "text": "ok"},
            {"role": "assistant", "kind": "error", "timestamp": "T3", "text": "trouble"},
        ],
    )

    assert [d.timestamp for d in find_dropped_turns(repo)] == []


def test_replay_reruns_each_dropped_turn_verbatim(repo: DataRepo) -> None:
    """Nothing is invented: the text replayed is the text she sent."""
    _log(
        repo,
        [
            {"role": "patient", "timestamp": "T1", "text": "my thyroid failed in 2021"},
            {"role": "assistant", "kind": "error", "timestamp": "T2", "text": "trouble"},
        ],
    )
    seen: list[str] = []

    report = replay_dropped_turns(
        None,
        repo,
        None,
        find_dropped_turns(repo),
        runner=lambda _c, _r, _d, text: seen.append(text),
    )

    assert seen == ["my thyroid failed in 2021"]
    assert report.replayed == ["T1"]
    assert report.failed == []


def test_one_failing_replay_does_not_stop_the_others(repo: DataRepo) -> None:
    _log(
        repo,
        [
            {"role": "patient", "timestamp": "T1", "text": "first"},
            {"role": "assistant", "kind": "error", "timestamp": "T2", "text": "trouble"},
            {"role": "patient", "timestamp": "T3", "text": "second"},
            {"role": "assistant", "kind": "error", "timestamp": "T4", "text": "trouble"},
        ],
    )

    def runner(_c: Any, _r: Any, _d: Any, text: str) -> None:
        if text == "first":
            raise RuntimeError("still broken")

    report = replay_dropped_turns(None, repo, None, find_dropped_turns(repo), runner=runner)

    assert report.failed == ["T1"]
    assert report.replayed == ["T3"]

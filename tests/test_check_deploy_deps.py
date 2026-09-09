"""`scripts/check_deploy_deps.py`'s stale-review check.

Six releases of convergence work — ADRs 0044, 0045, 0049, 0050, 0051, 0052,
0053 — deployed green across 0.32.0 and 0.33.0 while the ledger's last write
was still `app_version 0.31.1`. The board stayed at 46 active leads and
nothing said so: the review tick logged `skipped full review this tick` every
30 minutes, which is the CORRECT message and, at a glance, indistinguishable
from a pipeline that has stopped.

This is the project's recurring shape one more time — a check that cannot fire
looks exactly like a check that fires and finds nothing — but arrived at from
the other side: the code was fine, and nothing had run it.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_deploy_deps", Path(__file__).parent.parent / "scripts" / "check_deploy_deps.py"
)
assert _SPEC is not None and _SPEC.loader is not None
check_deploy_deps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_deploy_deps)

STALE_REVIEW_DAYS = check_deploy_deps.STALE_REVIEW_DAYS
check_last_review = check_deploy_deps.check_last_review


def _history(tmp_path: Path, *, days_ago: int, app_version: str = "0.33.1") -> Path:
    written = datetime.now(UTC) - timedelta(days=days_ago, hours=1)
    path = tmp_path / "case" / "ledger-history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "resulting_version": 18,
                "resulting_updated": written.isoformat(),
                "diff": {"provenance": {"app_version": app_version}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_a_recent_write_passes(tmp_path: Path) -> None:
    _history(tmp_path, days_ago=1)

    assert check_last_review(tmp_path) == 0


def test_a_ledger_nothing_has_written_in_weeks_fails(tmp_path: Path) -> None:
    """The actual production state, undetected for six days and counting: the
    tick running, declining correctly, and the board never moving."""
    _history(tmp_path, days_ago=STALE_REVIEW_DAYS + 1)

    assert check_last_review(tmp_path) == 1


def test_one_skipped_week_is_not_an_alarm(tmp_path: Path) -> None:
    """The floor is 7 days, so a single missed window is ordinary. An alarm
    that fires on the ordinary case gets ignored, and then the real one is
    ignored too."""
    _history(tmp_path, days_ago=8)

    assert check_last_review(tmp_path) == 0
    assert STALE_REVIEW_DAYS > 7, "the threshold must sit above the review floor"


def test_a_ledger_never_written_fails(tmp_path: Path) -> None:
    """No history file at all. A fresh deployment reads the same as a broken
    one here, and both are worth saying out loud."""
    assert check_last_review(tmp_path) == 1


def test_an_empty_history_fails(tmp_path: Path) -> None:
    path = tmp_path / "case" / "ledger-history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    assert check_last_review(tmp_path) == 1


def test_an_unreadable_last_line_is_the_finding_not_a_crash(tmp_path: Path) -> None:
    """A verifier that raises on bad input tells you nothing about the thing
    you asked it to verify."""
    path = tmp_path / "case" / "ledger-history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json\n", encoding="utf-8")

    assert check_last_review(tmp_path) == 1


def test_the_writing_version_is_reported(tmp_path: Path, capsys) -> None:
    """The number that would have caught this. A ledger last touched by
    0.31.1 while 0.33.1 runs means every release in between has been theory —
    and that is visible in one line rather than in a log nobody reads."""
    _history(tmp_path, days_ago=2, app_version="0.31.1")

    check_last_review(tmp_path)

    assert "0.31.1" in capsys.readouterr().out


def test_only_the_last_line_decides(tmp_path: Path) -> None:
    """The history is append-only and long. An old first line must not make a
    current ledger look stale."""
    path = tmp_path / "case" / "ledger-history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = datetime.now(UTC) - timedelta(days=400)
    recent = datetime.now(UTC) - timedelta(days=1)
    path.write_text(
        json.dumps({"resulting_updated": old.isoformat(), "diff": {}})
        + "\n"
        + json.dumps({"resulting_updated": recent.isoformat(), "diff": {}})
        + "\n",
        encoding="utf-8",
    )

    assert check_last_review(tmp_path) == 0


def test_the_verifier_ships_in_the_image() -> None:
    """`--in-task` checks the reference indexes on disk and the age of the
    last ledger write — things that only exist inside a running task. The
    Dockerfile copied `src` and `models.yaml` but not this script, so that
    half of the verifier had never once run where it was meant to:

        /opt/venv/bin/python: can't open file
        '/app/scripts/check_deploy_deps.py': [Errno 2] No such file

    A checker that cannot be invoked is indistinguishable from a checker that
    passes, which is the shape it was written to catch.
    """
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "scripts/check_deploy_deps.py" in dockerfile

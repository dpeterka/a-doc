"""Recover patient turns whose facts never reached the case file.

An intake turn can fail after the patient has already spoken: the model's
structured output fails validation, the retry fails the same way, and the
turn is abandoned with an apology. The words are not lost — every turn is
appended to `logs/chat/<date>.jsonl` before anything else happens — but the
FACTS derived from them are, and nothing re-derives them.

That happened in production. A 6,775-character message describing a long
stretch of medical history was answered with "I had trouble recording that
one... Could you say it again, or put it a little differently?" Nothing was
wrong with what she wrote, and re-typing six thousand characters is not a
reasonable thing to ask of a patient.

This module finds those turns and replays them through the ordinary capture
path, so the facts land without her retyping anything.

**Nothing here invents a turn.** It replays text she actually sent, taken
verbatim from the transcript, and applies it through the same deterministic
op-application every other turn uses.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from adoc.casefile.repo import DataRepo
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient
from adoc.web.casefile_helpers import read_recent_chat

logger = logging.getLogger(__name__)

# The apology an abandoned turn shows the patient. A transcript entry with
# this shape is the marker that a turn was dropped, because the error is
# recorded in the log exactly as she saw it.
_ERROR_KINDS = frozenset({"error", "withheld"})


class DroppedTurn(BaseModel):
    """One patient message whose turn ended in an error."""

    timestamp: str
    text: str
    error_text: str = ""

    @property
    def preview(self) -> str:
        """First few words, for a log line — never the whole message."""
        head = " ".join(self.text.split()[:8])
        return f"{head}… ({len(self.text)} chars)"


class ReplayReport(BaseModel):
    found: list[str] = Field(default_factory=list)
    replayed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


def find_dropped_turns(repo: DataRepo, *, max_files: int = 7) -> list[DroppedTurn]:
    """Patient messages immediately followed by an error reply.

    "Immediately followed" is the whole test. A patient turn answered
    normally is fine; one whose next assistant entry is an error is one whose
    facts were never applied.
    """
    entries = read_recent_chat(repo, max_files=max_files, max_turns=2000)
    dropped: list[DroppedTurn] = []
    for index, entry in enumerate(entries):
        if entry.get("role") != "patient":
            continue
        following = entries[index + 1 : index + 3]

        def _is(entry: dict[str, Any], errored: bool) -> bool:
            if entry.get("role") != "assistant":
                return False
            return (entry.get("kind") in _ERROR_KINDS) is errored

        error = next((e for e in following if _is(e, True)), None)
        if error is None:
            continue
        # An intervening successful reply means this turn WAS answered and the
        # error belongs to a later message.
        replied_first = next((e for e in following if _is(e, False)), None)
        if replied_first is not None and following.index(replied_first) < following.index(error):
            continue
        text = str(entry.get("text") or "")
        if not text.strip():
            continue
        dropped.append(
            DroppedTurn(
                timestamp=str(entry.get("timestamp") or ""),
                text=text,
                error_text=str(error.get("text") or ""),
            )
        )
    return dropped


def replay_dropped_turns(
    client: LlmClient,
    repo: DataRepo,
    db: LabsDb,
    turns: list[DroppedTurn],
    *,
    runner: Any = None,
) -> ReplayReport:
    """Re-run each dropped turn through the capture path.

    `runner` is injected so this is testable without a model; it defaults to
    the ordinary post-intake capture pass.
    """
    if runner is None:  # pragma: no cover - the production wiring
        from adoc.intake.agent import run_visit_capture

        runner = run_visit_capture

    report = ReplayReport(found=[t.timestamp for t in turns])
    for turn in turns:
        logger.info("replay: re-running dropped turn %s %s", turn.timestamp, turn.preview)
        try:
            runner(client, repo, db, turn.text)
        except Exception as exc:  # noqa: BLE001 - one bad turn must not stop the rest
            logger.warning("replay: turn %s failed again: %s", turn.timestamp, exc)
            report.failed.append(turn.timestamp)
            continue
        report.replayed.append(turn.timestamp)
    return report


def today() -> date:
    return date.today()

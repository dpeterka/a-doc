"""Turning what the patient says into regimen entries.

`case/regimen.yaml` is only as good as its last update, and the list changes
in conversation — "I stopped the selenium last month", "I've added vitamin D
since the summer". Without this, the record is a snapshot of whatever the
backfill found and drifts from the truth every week.

The model proposes; this module decides. Two deterministic guards stand
between a chat turn and the case file:

**Grounding.** A proposed substance name must actually appear in the
patient's own message. A model that invents "magnesium" for a message that
never mentions it writes a fiction into a medical record, and no prompt
wording makes that impossible — a substring check does. This is the same
principle as citation checking: the claim must be traceable to its source.

**Dates stated no more precisely than they are known.** "Last month" becomes
a date plus a `month` precision, never a bare day (ADR 0027). An unparseable
time expression yields no date at all rather than a guess, because a wrong
stop date is worse than a missing one: it would place a substance outside a
lab draw it actually overlapped.

What this does NOT solve: a patient asking "should I take magnesium?"
mentions magnesium, so grounding passes and only the prompt separates a
question from a statement. That failure is visible in the record and
correctable by a later turn, which is the reason entries carry their source
ref.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from adoc.casefile.regimen import (
    REGIMEN_RELPATH,
    Regimen,
    RegimenEntry,
    load_regimen,
    merge_entries,
    save_regimen,
)
from adoc.casefile.repo import DataRepo
from adoc.intake.wizard import parse_approx_date_with_precision

RegimenAction = Literal["started", "stopped", "taking"]


class RegimenChange(BaseModel):
    """One statement the patient made about something she takes."""

    name: str
    action: RegimenAction = "taking"
    """`taking` is the weakest claim — she says she takes it, without saying
    since when. It attests the substance on today's date and nothing more."""
    dose: str | None = None
    frequency: str | None = None
    when_text: str | None = None
    """Her own words for the timing — "last month", "since spring", "in
    2021". Parsed here, never by the model, so precision survives."""


class RegimenUpdateReport(BaseModel):
    """What actually happened, for logging and for tests."""

    applied: list[str] = Field(default_factory=list)
    dropped_ungrounded: list[str] = Field(default_factory=list)
    """Names the model proposed that do not appear in the patient's message."""


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _is_grounded(name: str, message: str) -> bool:
    """Whether `name` actually occurs in the patient's own words.

    Normalized on both sides so "Vitamin D3" matches "vitamin d3" and
    "vitamin-d3". A multi-word name is grounded when its normalized form is a
    substring of the normalized message, which tolerates the punctuation and
    casing differences between what she typed and what the model returned.
    """
    key = _normalize(name)
    return bool(key) and key in _normalize(message)


def to_entries(
    changes: list[RegimenChange], *, message: str, today: date, source_ref: str
) -> tuple[list[RegimenEntry], list[str]]:
    """`changes` as entries, plus the names dropped for not being grounded."""
    entries: list[RegimenEntry] = []
    dropped: list[str] = []
    for change in changes:
        if not change.name.strip():
            continue
        if not _is_grounded(change.name, message):
            dropped.append(change.name)
            continue

        when: date | None = None
        precision = None
        if change.when_text:
            parsed = parse_approx_date_with_precision(change.when_text)
            if parsed is not None:
                when, precision = parsed

        entry = RegimenEntry(
            name=change.name.strip(),
            dose=change.dose,
            frequency=change.frequency,
            reported_on=today,
            sources=[source_ref],
        )
        if change.action == "started":
            if when is not None:
                entry.started = when
                entry.started_precision = precision
            else:
                # She said she started it but not when. Recording today as the
                # start would claim she began it during this conversation.
                # Attesting today says only what is true: she is on it now.
                entry.attested_on = [today]
        elif change.action == "stopped":
            entry.stopped = when or today
            entry.stopped_precision = precision or "day"
        else:
            # "I take X" — attests today and asserts nothing about the past.
            entry.attested_on = [today]
        entries.append(entry)
    return entries, dropped


def apply_regimen_changes(
    repo: DataRepo, changes: list[RegimenChange], *, message: str, today: date
) -> RegimenUpdateReport:
    """Fold a turn's regimen statements into `case/regimen.yaml`.

    Writes only when something changed, so an ordinary turn touches disk not
    at all. Never raises: this runs inside the silent post-turn capture pass,
    whose reply has already been delivered, and a failure here must not cost
    the patient her answer.
    """
    report = RegimenUpdateReport()
    if not changes:
        return report

    entries, dropped = to_entries(
        changes,
        message=message,
        today=today,
        source_ref=f"patient-report:{today.isoformat()}",
    )
    report.dropped_ungrounded = dropped
    if not entries:
        return report

    path = repo.root / Path(REGIMEN_RELPATH)
    before = load_regimen(path)
    after = merge_entries(before, entries)
    after.updated = today
    save_regimen(path, after)
    report.applied = [e.name for e in entries]
    return report


__all__ = [
    "Regimen",
    "RegimenChange",
    "RegimenUpdateReport",
    "apply_regimen_changes",
    "to_entries",
]

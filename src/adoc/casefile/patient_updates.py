"""Applying what the patient says about results and about the record itself.

Two payloads the visit-capture pass carries alongside its fact ops and
regimen changes, both applied by deterministic code here.

The guards mirror `regimen_chat`'s, because the failure modes are the same:

**A reported result must be grounded in her message.** A model that invents
"iron" for a message that never mentions it writes a fabricated lab result
into a medical record.

**A dispute must name an item that exists.** A dispute against a ref nothing
resolves would sit in the file forever matching nothing, and — worse — would
look like an unaddressed patient objection when there is no such item. The
target is validated against the encounters and documents actually on disk.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from adoc.casefile.disputes import (
    DISPUTES_RELPATH,
    Dispute,
    DisputeKind,
    add_dispute,
    load_disputes,
    save_disputes,
)
from adoc.casefile.repo import DataRepo
from adoc.casefile.reported import (
    REPORTED_RESULTS_RELPATH,
    Direction,
    ReportedResult,
    load_reported_results,
    merge_reported,
    save_reported_results,
)
from adoc.intake.wizard import parse_approx_date_with_precision

logger = logging.getLogger(__name__)

ENCOUNTERS_RELDIR = "case/encounters"


class ReportedResultClaim(BaseModel):
    """A result the patient remembers, as the model reports it."""

    analyte: str
    direction: Direction = "unknown"
    value: float | None = None
    unit: str | None = None
    when_text: str | None = None
    """Her words for the timing — parsed here, never by the model."""
    note: str = ""


class DisputeClaim(BaseModel):
    """A patient objection to something on file."""

    target: str
    """Copied verbatim from a ref shown in the context pack."""
    kind: DisputeKind = "wrong-detail"
    statement: str = ""


class PatientUpdateReport(BaseModel):
    reported: list[str] = Field(default_factory=list)
    disputes: list[str] = Field(default_factory=list)
    dropped_ungrounded: list[str] = Field(default_factory=list)
    dropped_unknown_target: list[str] = Field(default_factory=list)


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _is_grounded(name: str, message: str) -> bool:
    key = _normalize(name)
    return bool(key) and key in _normalize(message)


def _known_targets(repo: DataRepo) -> set[str]:
    """Refs a dispute may legitimately name."""
    targets: set[str] = set()
    directory = repo.root / ENCOUNTERS_RELDIR
    if directory.is_dir():
        targets |= {f"encounter:{p.name}" for p in directory.glob("*.md")}
    return targets


def apply_patient_updates(
    repo: DataRepo,
    *,
    reported: list[ReportedResultClaim],
    disputes: list[DisputeClaim],
    message: str,
    today: date,
    known_document_refs: set[str] | None = None,
) -> PatientUpdateReport:
    """Fold a turn's reported results and disputes into the case file.

    Never raises: this runs inside the silent post-turn capture pass, whose
    reply has already been delivered.
    """
    report = PatientUpdateReport()

    entries: list[ReportedResult] = []
    for claim in reported:
        if not _is_grounded(claim.analyte, message):
            report.dropped_ungrounded.append(claim.analyte)
            continue
        when = None
        precision = None
        if claim.when_text:
            parsed = parse_approx_date_with_precision(claim.when_text, today=today)
            if parsed is not None:
                when, precision = parsed
        entries.append(
            ReportedResult(
                analyte=claim.analyte.strip(),
                direction=claim.direction,
                value=claim.value,
                unit=claim.unit,
                when=when,
                when_precision=precision,
                reported_on=today,
                note=claim.note,
                sources=[f"patient-report:{today.isoformat()}"],
            )
        )

    if entries:
        path = repo.root / Path(REPORTED_RESULTS_RELPATH)
        merged = merge_reported(load_reported_results(path), entries)
        merged.updated = today
        save_reported_results(path, merged)
        report.reported = [e.analyte for e in entries]

    valid_targets = _known_targets(repo) | (known_document_refs or set())
    current = load_disputes(repo.root / Path(DISPUTES_RELPATH))
    changed = False
    for objection in disputes:
        if objection.target not in valid_targets:
            # ADR 0028's posture: an unresolvable ref costs the claim, not the
            # turn. Logged loudly because a dropped dispute is a patient
            # correction going unheard, which is worse than a dropped citation.
            report.dropped_unknown_target.append(objection.target)
            logger.warning(
                "patient-updates: dropping a dispute against %r - no such item on file",
                objection.target,
            )
            continue
        current, added = add_dispute(
            current,
            Dispute(
                target=objection.target,
                kind=objection.kind,
                statement=objection.statement.strip() or message.strip(),
                reported_on=today,
            ),
        )
        if added:
            changed = True
            report.disputes.append(objection.target)

    if changed:
        save_disputes(repo.root / Path(DISPUTES_RELPATH), current)
    return report


__all__ = [
    "DisputeClaim",
    "PatientUpdateReport",
    "ReportedResultClaim",
    "apply_patient_updates",
]

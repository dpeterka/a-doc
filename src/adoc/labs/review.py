"""Human review decisions as SOURCE data (ADR 0026).

`labs.sqlite` is a derived artifact — rebuildable from `labs-export.jsonl`
(PLAN.md "State"). But the outcomes of a person reviewing the confirm queue
were stored only inside it, entangled with the extractor's output in that
same export. A human's judgement is not derived from anything; it is
primary data, and storing it as a by-product of extraction made "rebuild
from sources" a destructive operation instead of a routine one.

`case/review-decisions.jsonl` fixes that: one committed, diffable record per
decision, written by nothing but a human's action. A rebuild exports these,
wipes, re-ingests, and replays them.

**Matching survives a rename.** The `labs` UNIQUE key is
`(date, name, specimen, source_doc)` and `name` is in it — but the ADR 0025
fixes deliberately change `name` on some rows, so matching on it would miss
exactly the rows we touched. Identity here normalizes the name and sheds
trailing connectives, the same shape PR #151 uses to resolve citations
against renamed rows.

**An unmatched decision is reported, never guessed onto a neighbour.**
Silently attaching someone's correction to a different measurement than the
one they reviewed is worse than losing it, because it then looks
authoritative.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from adoc.labs.db import LabsDb
from adoc.labs.models import ExtractionStatus, LabResult

REVIEW_DECISIONS_RELPATH = Path("case") / "review-decisions.jsonl"

# Statuses that represent a HUMAN decision. `auto` is the extractor's own
# output and carries no judgement, so it is never exported or replayed.
HUMAN_STATUSES = frozenset(
    {
        ExtractionStatus.CONFIRMED,
        ExtractionStatus.CORRECTED,
        ExtractionStatus.REJECTED,
    }
)

# Fields a correction can carry. Deliberately the measurement values a human
# would retype — never provenance (`source_doc`, `raw_json`) or identity
# (`date`, `name`), which would let a replay move a decision onto a
# different row than the one reviewed.
CORRECTABLE_VALUE_FIELDS = (
    "value",
    "comparator",
    "value_text",
    "ucum_unit",
    "ref_low",
    "ref_high",
    "ref_text",
    "flag",
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_TRAILING_CONNECTIVES = ("is", "of", "was", "are", "were", "shows", "at", "to")


def normalized_name(name: str) -> str:
    """Identity for a row's analyte name that survives the ADR 0025 renames.

    Sheds trailing connectives, then strips case and punctuation, so
    "10-year probability of hip fracture is" and
    "10-year probability of hip fracture" are the same row.
    """
    words = name.split()
    while words and words[-1].lower().strip(":,;") in _TRAILING_CONNECTIVES:
        words.pop()
    return _NON_ALNUM_RE.sub("", " ".join(words).lower())


class ReviewDecision(BaseModel):
    """One human decision about one lab row."""

    source_doc: str
    date: str
    specimen: str
    name_key: str
    """`normalized_name` of the row's name when the decision was made."""
    name_at_decision: str
    """The name as it read then — for the operator's report, not matching."""
    status: ExtractionStatus
    values: dict[str, Any] = Field(default_factory=dict)
    """The corrected field values, for a `corrected` decision. Empty
    otherwise: confirming or rejecting sets no values."""

    def key(self) -> tuple[str, str, str, str]:
        return (self.source_doc, self.date, self.specimen, self.name_key)


def decision_from_row(row: LabResult) -> ReviewDecision:
    values: dict[str, Any] = {}
    if row.extraction_status == ExtractionStatus.CORRECTED:
        for field in CORRECTABLE_VALUE_FIELDS:
            value = getattr(row, field)
            if value is not None:
                values[field] = value.value if hasattr(value, "value") else value
    return ReviewDecision(
        source_doc=row.source_doc,
        date=row.date.isoformat(),
        specimen=row.specimen,
        name_key=normalized_name(row.name),
        name_at_decision=row.name,
        status=row.extraction_status,
        values=values,
    )


def export_decisions(db: LabsDb) -> list[ReviewDecision]:
    """Every human decision currently in the store, oldest row first.

    Includes REJECTED rows: "a person looked at this and said it is wrong"
    is exactly the judgement a rebuild must not throw away and then
    re-present as a fresh row to review.
    """
    decisions = [
        decision_from_row(row) for row in db.all_rows() if row.extraction_status in HUMAN_STATUSES
    ]
    return sorted(decisions, key=lambda d: (d.date, d.name_at_decision))


def write_decisions(path: Path, decisions: list[ReviewDecision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for decision in decisions:
            fh.write(decision.model_dump_json() + "\n")


def read_decisions(path: Path) -> list[ReviewDecision]:
    if not path.exists():
        return []
    decisions: list[ReviewDecision] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            decisions.append(ReviewDecision.model_validate_json(line))
    return decisions


class ReplayReport(BaseModel):
    applied: int = 0
    unmatched: list[ReviewDecision] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unmatched


def replay_decisions(db: LabsDb, decisions: list[ReviewDecision]) -> ReplayReport:
    """Re-apply human decisions to freshly-ingested rows.

    A decision whose row no longer exists is REPORTED, not guessed onto a
    neighbouring row — see the module docstring. That happens legitimately
    when the ADR 0025 gate retires a row a human had reviewed.
    """
    index: dict[tuple[str, str, str, str], LabResult] = {}
    for row in db.all_rows():
        if row.id is None:  # pragma: no cover - persisted rows always have one
            continue
        index[(row.source_doc, row.date.isoformat(), row.specimen, normalized_name(row.name))] = row

    report = ReplayReport()
    for decision in decisions:
        matched = index.get(decision.key())
        if matched is None or matched.id is None:
            report.unmatched.append(decision)
            continue
        if decision.status == ExtractionStatus.REJECTED:
            db.reject_row(matched.id)
        elif decision.status == ExtractionStatus.CONFIRMED:
            db.confirm_row(matched.id)
        elif decision.values:
            db.correct_row(matched.id, **decision.values)
        else:
            # A `corrected` decision that recorded no values cannot be
            # replayed as a correction; confirming it would assert a
            # judgement the person did not make, so leave it for review.
            report.unmatched.append(decision)
            continue
        report.applied += 1
    return report


def format_replay_report(report: ReplayReport) -> str:
    lines = [f"replay: applied {report.applied} human decision(s)"]
    if report.unmatched:
        lines.append(
            f"replay: {len(report.unmatched)} decision(s) had no matching row and were NOT "
            "applied — review these, they are not lost (they stay in "
            f"{REVIEW_DECISIONS_RELPATH}):"
        )
        for decision in report.unmatched[:20]:
            lines.append(
                f"  {decision.date} {decision.name_at_decision!r} "
                f"({decision.status.value}) from {decision.source_doc[:12]}"
            )
        if len(report.unmatched) > 20:
            lines.append(f"  ... and {len(report.unmatched) - 20} more")
    return "\n".join(lines)

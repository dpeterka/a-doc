"""A dated, versioned count of the board's shape, written once per review
(ADR 0052).

The convergence track (PLAN.md) is a series of releases whose whole purpose is
to make the differential *smaller and better directed*. Every one of them was
measured by hand, once, against a number recalled from the previous release —
and twice that recollection was wrong: an "8 emerging" projection that predated
the corroboration rule, and a "187 of 2079" denominator that was never the
denominator the change acted on.

A hand-measured before/after is not a boundary. It cannot be re-read, it cannot
be diffed by anything but a person's memory, and it is gone as soon as the
terminal scrolls. So each review now appends one line here: what the board
looked like, and the app version that made it look that way. The next review
subtracts. Attribution is then a fact on disk rather than a claim.

Nothing here is derived from a model. Every field is a count over the ledger
the review just committed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from adoc.casefile.emerging import split_emerging
from adoc.casefile.ledger import ACTIVE_STATUSES
from adoc.casefile.retirement import Retirement
from adoc.casefile.schema import Hypothesis, Ledger

SNAPSHOT_RELPATH = "case/convergence.jsonl"


class ConvergenceSnapshot(BaseModel):
    """One review's boundary marker."""

    review_date: date
    app_version: str

    # The board.
    active: int = 0
    differential: int = 0
    emerging: int = 0
    by_tier: dict[str, int] = Field(default_factory=dict)
    ruled_out_total: int = 0
    parked_total: int = 0
    off_board_this_review: int = 0
    """Leads this review took off the differential, by whichever mechanism."""
    by_cause: dict[str, int] = Field(default_factory=dict)
    """`off_board_this_review` split by `RetirementCause`. The split is the
    point: three separate rules write `parked`, so a bare count says the board
    shrank without saying what shrank it, and the convergence track exists to
    answer exactly that."""

    # What the review had to work with.
    open_questions: int = 0
    criteria_sets: int = 0
    criteria_met_items: int = 0
    engine_terms: int = 0
    engine_terms_lab_derived: int = 0

    def delta(self, previous: ConvergenceSnapshot | None) -> dict[str, int]:
        """Every integer field that moved since `previous`, as a signed delta.

        An absent previous snapshot yields `{}` rather than a delta against
        zero: the first review of a new counter did not *add* its whole board,
        and reporting that it did would be the same kind of false before/after
        this file exists to stop.
        """
        if previous is None:
            return {}
        moved: dict[str, int] = {}
        for name, field in type(self).model_fields.items():
            if field.annotation is not int:
                continue
            change = getattr(self, name) - getattr(previous, name)
            if change:
                moved[name] = change
        return moved


def measure(
    ledger: Ledger,
    *,
    app_version: str,
    today: date,
    retirements: Iterable[Retirement] = (),
    open_questions: int = 0,
    criteria_sets: int = 0,
    criteria_met_items: int = 0,
    engine_terms: int = 0,
    engine_terms_lab_derived: int = 0,
) -> ConvergenceSnapshot:
    """Count the committed ledger. Counts only — no judgement, no model."""
    proposed = list(retirements)
    by_cause: dict[str, int] = {}
    for item in proposed:
        by_cause[item.cause] = by_cause.get(item.cause, 0) + 1
    active: list[Hypothesis] = [h for h in ledger.hypotheses if h.status in ACTIVE_STATUSES]
    differential, emerging = split_emerging(active, today=today)
    by_tier: dict[str, int] = {}
    for hypothesis in active:
        by_tier[hypothesis.tier] = by_tier.get(hypothesis.tier, 0) + 1
    return ConvergenceSnapshot(
        review_date=today,
        app_version=app_version,
        active=len(active),
        differential=len(differential),
        emerging=len(emerging),
        by_tier=by_tier,
        ruled_out_total=sum(1 for h in ledger.hypotheses if h.status == "ruled-out"),
        parked_total=sum(1 for h in ledger.hypotheses if h.status == "parked"),
        off_board_this_review=len(proposed),
        by_cause=by_cause,
        open_questions=open_questions,
        criteria_sets=criteria_sets,
        criteria_met_items=criteria_met_items,
        engine_terms=engine_terms,
        engine_terms_lab_derived=engine_terms_lab_derived,
    )


def load_snapshots(path: Path) -> list[ConvergenceSnapshot]:
    """Every snapshot on file, oldest first.

    A malformed line is skipped rather than raised on. This is a measurement
    log: one bad line must never be able to stop a review from running, and a
    review that cannot record its boundary is still a review worth having.
    """
    if not path.exists():
        return []
    snapshots: list[ConvergenceSnapshot] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snapshots.append(ConvergenceSnapshot.model_validate(json.loads(line)))
        except Exception:
            continue
    return snapshots


def latest_snapshot(path: Path) -> ConvergenceSnapshot | None:
    snapshots = load_snapshots(path)
    return snapshots[-1] if snapshots else None


def append_snapshot(path: Path, snapshot: ConvergenceSnapshot) -> None:
    """Append-only. A snapshot is a record of what was true on a date; a
    re-run that overwrote it would erase the boundary it came to mark."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(snapshot.model_dump_json() + "\n")


_LABELS: dict[str, str] = {
    "active": "leads on the board",
    "differential": "in the differential",
    "emerging": "emerging",
    "ruled_out_total": "ruled out to date",
    "parked_total": "parked to date",
    "off_board_this_review": "taken off the board this review",
    "open_questions": "open questions",
    "criteria_sets": "criteria sets tracked",
    "criteria_met_items": "criteria items met",
    "engine_terms": "terms sent to the engines",
    "engine_terms_lab_derived": "of those, derived from labs",
}


def render(snapshot: ConvergenceSnapshot, previous: ConvergenceSnapshot | None) -> list[str]:
    """The boundary section of the review, in markdown.

    Written for the reader who wants to know whether the last release did
    anything, which is the question a hand-measured number kept failing to
    answer.
    """
    lines = ["## What moved since the last review", ""]
    if previous is None:
        lines += [
            "This is the first review to record a boundary, so there is nothing "
            "to subtract from yet. The counts below are the starting line.",
            "",
        ]
    else:
        span = (snapshot.review_date - previous.review_date).days
        versions = (
            f"still on {snapshot.app_version}"
            if snapshot.app_version == previous.app_version
            else f"{previous.app_version} → {snapshot.app_version}"
        )
        lines += [
            f"Against {previous.review_date.isoformat()} ({span} day(s) ago, {versions}):",
            "",
        ]
        moved = snapshot.delta(previous)
        if not moved:
            lines += ["Nothing counted here changed.", ""]
        else:
            for name in moved:
                label = _LABELS.get(name, name.replace("_", " "))
                lines.append(f"- {label}: {getattr(previous, name)} → {getattr(snapshot, name)}")
            lines.append("")

    lines += [
        f"- {snapshot.active} lead(s) on the board — {snapshot.differential} in the "
        f"differential, {snapshot.emerging} emerging",
        f"- {snapshot.off_board_this_review} taken off the board this run"
        + (f" ({_render_causes(snapshot)})" if snapshot.by_cause else "")
        + f"; {snapshot.ruled_out_total} ruled out and {snapshot.parked_total} parked in total",
        f"- {snapshot.open_questions} open question(s); {snapshot.criteria_met_items} "
        f"criteria item(s) met across {snapshot.criteria_sets} set(s)",
        f"- {snapshot.engine_terms} term(s) sent to the engines, "
        f"{snapshot.engine_terms_lab_derived} of them derived from labs",
        "",
    ]
    return lines


_CAUSE_WORDS: dict[str, str] = {
    "unsupported": "nothing supported it",
    "outweighed": "outweighed by evidence against",
    "stale": "gone stale",
    "definitive-exclusion": "definitively excluded",
    "rule-out-met": "its own rule-out condition was met",
    "tier-fold": "folded to fit the tier cap",
}


def _render_causes(snapshot: ConvergenceSnapshot) -> str:
    return ", ".join(
        f"{count} {_CAUSE_WORDS.get(cause, cause)}"
        for cause, count in sorted(snapshot.by_cause.items())
    )


def render_tier_line(snapshot: ConvergenceSnapshot) -> str:
    if not snapshot.by_tier:
        return "no leads on the board"
    parts = [f"{count} {tier}" for tier, count in sorted(snapshot.by_tier.items())]
    return ", ".join(parts)


def snapshot_dates(snapshots: Iterable[ConvergenceSnapshot]) -> list[str]:
    return [s.review_date.isoformat() for s in snapshots]

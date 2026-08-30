"""Retiring hypotheses that have stopped earning their place (ADR 0035).

Measured on the live ledger at version 12: 50 hypotheses, every one `active`,
none ever retired across twelve versions. `ruled-out` appeared in no prompt
and no logic — the status was reachable in the type system and unreachable in
practice. One stage added hypotheses and no stage subtracted, so the ledger
could only grow.

This is the subtracting stage. Plain code, never a model call, per CLAUDE.md's
rule that deterministic logic is not delegated.

Two exclusions are absolute and are the reason this can be automatic at all:

**`cant-miss` is never auto-retired.** The entire point of that tier is that
the cost of missing one is catastrophic and asymmetric. A rule that could
silently drop a pulmonary embolism to tidy a list is not a rule worth having.

**Patient-origin hypotheses are never auto-retired.** ADR 0032 makes
patient-reported material first-class. Her own theory is hers to withdraw;
having the machine bin it quietly would be precisely the wrong behaviour.

Nothing is deleted. A retirement is a status change — visible in the diff,
present in git history, reversible by the next review that finds new support.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from adoc.casefile.schema import (
    Hypothesis,
    HypothesisStatus,
    Ledger,
    LedgerDiff,
    Provenance,
    UpdateHypothesis,
)

# How long a low-value hypothesis may sit untouched before it is parked. Only
# applies to `low`/`minimal` probability: a `high` or `moderate` lead that has
# gone quiet is a lead nobody has tested, which is a prompt for the
# test-chooser rather than grounds for retirement.
STALE_DAYS = 90


class Retirement(BaseModel):
    """One proposed status change, with the reason in plain words."""

    hypothesis_id: str
    hypothesis_name: str
    to_status: HypothesisStatus
    reason: str


class RetirementReport(BaseModel):
    """What a retirement pass would do, and what it deliberately left alone."""

    retirements: list[Retirement] = Field(default_factory=list)
    protected_count: int = 0
    """Active hypotheses excluded from consideration entirely because they are
    can't-miss or patient-origin. Counted so the report can say what was left
    alone rather than implying everything was assessed."""

    @property
    def count(self) -> int:
        return len(self.retirements)


def is_protected(hypothesis: Hypothesis) -> bool:
    """Whether this hypothesis may never be retired automatically."""
    return hypothesis.tier == "cant-miss" or hypothesis.origin == "patient"


def _no_supporting_evidence(hypothesis: Hypothesis) -> Retirement | None:
    """A hypothesis nothing supports.

    Not a judgement about the disease — a judgement about the entry. A
    differential is a set of claims about THIS patient, and a claim with no
    cited support is speculation that was never withdrawn. Eight of fifty on
    the live ledger, all `low` or `minimal`.
    """
    if hypothesis.evidence_for:
        return None
    return Retirement(
        hypothesis_id=hypothesis.id,
        hypothesis_name=hypothesis.name,
        to_status="parked",
        reason="nothing on file supports this",
    )


def _outweighed(hypothesis: Hypothesis) -> Retirement | None:
    """More against than for, counting strong evidence double.

    Weighted rather than counted flat because three weak observations do not
    outweigh one strong contradicting result, and treating them as equal would
    let volume beat quality.
    """
    if not hypothesis.evidence_for:
        return None

    def weigh(items: list) -> int:
        return sum(2 if e.strength == "strong" else 1 for e in items)

    against = weigh(hypothesis.evidence_against)
    if against <= weigh(hypothesis.evidence_for):
        return None
    return Retirement(
        hypothesis_id=hypothesis.id,
        hypothesis_name=hypothesis.name,
        to_status="ruled-out",
        reason="the evidence against outweighs the evidence for",
    )


def _stale(hypothesis: Hypothesis, *, today: date, stale_days: int) -> Retirement | None:
    """A low-value lead nobody has touched in a long time.

    Restricted to `low`/`minimal` on purpose. A `high` or `moderate`
    hypothesis that has gone quiet is one nobody has tested — that is work for
    the test-chooser, not grounds for dropping it.
    """
    if hypothesis.probability not in ("low", "minimal"):
        return None
    age = (today - hypothesis.first_proposed).days
    if age < stale_days:
        return None
    touched = hypothesis.last_challenged or hypothesis.first_proposed
    if (today - touched).days < stale_days:
        return None
    return Retirement(
        hypothesis_id=hypothesis.id,
        hypothesis_name=hypothesis.name,
        to_status="parked",
        reason=f"{hypothesis.probability} probability and untouched for {age} days",
    )


def propose_retirements(
    ledger: Ledger,
    *,
    today: date,
    stale_days: int = STALE_DAYS,
) -> RetirementReport:
    """What should stop being part of the active differential.

    Returns a proposal rather than mutating: the ledger is only ever changed
    through a diff that the invariants check, so this produces the list and
    the caller applies it.

    Rules are tried in order and the FIRST match wins, so a hypothesis is
    retired for one stated reason rather than an accumulated list. "Nothing
    supports this" is a more useful thing to read than three overlapping
    verdicts.
    """
    retirements: list[Retirement] = []
    protected = 0

    for hypothesis in ledger.hypotheses:
        if hypothesis.status != "active":
            continue
        if is_protected(hypothesis):
            protected += 1
            continue

        for proposal in (
            _no_supporting_evidence(hypothesis),
            _outweighed(hypothesis),
            _stale(hypothesis, today=today, stale_days=stale_days),
        ):
            if proposal is not None:
                retirements.append(proposal)
                break

    return RetirementReport(retirements=retirements, protected_count=protected)


def retirements_to_diff(report: RetirementReport, *, provenance: Provenance) -> LedgerDiff | None:
    """The retirements as a ledger diff, or `None` if there are none.

    Routed through a diff rather than mutating the ledger directly so the
    invariants in `casefile.ledger` still get to check it. Retirement is a
    status change like any other and does not get a private back door.
    """
    if not report.retirements:
        return None
    return LedgerDiff(
        provenance=provenance,
        rationale=(
            "Deterministic retirement pass (ADR 0035): "
            + "; ".join(f"{r.hypothesis_name} — {r.reason}" for r in report.retirements)
        ),
        ops=[UpdateHypothesis(id=r.hypothesis_id, status=r.to_status) for r in report.retirements],
    )


def render_retirements(report: RetirementReport) -> list[str]:
    """The report section. Says what was left alone as well as what was cut."""
    if not report.retirements:
        return ["_Nothing was retired from the differential this week._", ""]

    lines = [
        "These are no longer part of the working differential. Nothing is deleted — "
        "each one stays on file and comes back if new evidence turns up.",
        "",
    ]
    for item in report.retirements:
        verb = "Ruled out" if item.to_status == "ruled-out" else "Set aside"
        lines.append(f"- **{item.hypothesis_name}** — {verb}: {item.reason}.")
    lines.append("")
    if report.protected_count:
        lines.append(
            f"_{report.protected_count} were not assessed for retirement at all: "
            "anything in the can't-miss tier, and anything you raised yourself, "
            "stays until a person decides otherwise._"
        )
        lines.append("")
    return lines

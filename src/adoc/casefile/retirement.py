"""Retiring hypotheses that have stopped earning their place (ADR 0035).

Measured on the live ledger at version 12: 50 hypotheses, every one `active`,
none ever retired across twelve versions. `ruled-out` appeared in no prompt
and no logic — the status was reachable in the type system and unreachable in
practice. One stage added hypotheses and no stage subtracted, so the ledger
could only grow.

This is the subtracting stage. Plain code, never a model call, per CLAUDE.md's
rule that deterministic logic is not delegated.

Two exclusions protect `cant-miss` and patient-origin hypotheses from the
accumulating rules, and are the reason those can be automatic at all:

**`cant-miss` is never retired by accumulated model opinion.** The entire
point of that tier is that the cost of missing one is catastrophic and
asymmetric. A rule that could silently drop a pulmonary embolism because the
Challenger produced more counter-prose than support is not a rule worth
having.

**Patient-origin hypotheses are never retired by accumulated model opinion.**
ADR 0032 makes patient-reported material first-class. Her own theory is hers
to withdraw; having the machine bin it quietly would be precisely the wrong
behaviour.

ADR 0038 narrows that protection rather than removing it. TWO rules run
BEFORE the protection check, because both rest on something objective:

**A definitive exclusion** — one `evidence_against` item at
`strength="definitive-exclusion"`, from a source allowed to carry one
(`DEFINITIVE_EXCLUSION_SOURCES`). Clinical exclusion is not additive: a
negative serum metanephrines excludes pheochromocytoma however many
non-specific symptoms point at it.

**A met rule-out** — the hypothesis's own stated `rule_out_check`, evaluated
against stored lab rows. It said at creation what would end it; this is the
stage that checks.

Pheochromocytoma is a can't-miss lead AND the textbook case of a diagnosis one
negative test excludes. A protection that cannot tell those apart guarantees
the bloat it was meant to be worth paying for.

Nothing is deleted. A retirement is a status change — visible in the diff,
present in git history, reversible by the next review that finds new support.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field

from adoc.casefile.schema import (
    Evidence,
    Hypothesis,
    HypothesisStatus,
    Ledger,
    LedgerDiff,
    Provenance,
    RuleOutCheck,
    UpdateHypothesis,
)

LabLookup = dict[str, "LabFact"]
"""`{normalized analyte name: LabFact}` — the minimum a rule-out check needs.

Passed as a plain mapping, exactly as `knowledge.criteria` takes
`PhenotypeLookup`, so `casefile` gains no dependency on `labs` or `knowledge`.
The caller that has both (`reason.review`) builds it."""

# Which source schemes may carry `strength="definitive-exclusion"` (ADR 0038).
#
# The restriction IS the safety property. The Challenger writes
# `evidence_against`; without this it would have a one-word route to retiring
# a can't-miss lead. `pmid:` is refused because literature knows nothing about
# this patient, and `engine:` because a phenotype engine that never ranked
# something has not refuted it (ADR 0036's whole `neutral` argument).
DEFINITIVE_EXCLUSION_SOURCES = ("labs:", "doc:", "encounter:", "patient-report:")

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
    refused_exclusions: list[str] = Field(default_factory=list)
    """Evidence claiming `definitive-exclusion` from a source not permitted to
    make that claim (ADR 0038). Reported, never acted on: a model reaching for
    the one strength that bypasses the balance scale is worth seeing, and
    silently ignoring it would hide the attempt."""
    error: str = ""
    """Set only when a retirement was PROPOSED but the write to disk failed —
    never for the ordinary case of nothing needing retirement. Distinct from
    an empty `retirements` list on purpose: `render_retirements` used to
    print "nothing was retired" identically whether nothing was proposed or
    a proposal existed and the apply failed (a lock, an IO error), which
    reported a real operational failure as an ordinary clean week."""

    @property
    def count(self) -> int:
        return len(self.retirements)


class LabFact(BaseModel):
    """One stored lab result, reduced to what a rule-out check can answer on.

    Deliberately not `labs.models.LabResult`: `casefile` must not import
    `labs`. The caller flattens.
    """

    value: float | None = None
    value_text: str = ""
    flag: str = ""
    """The lab's own high/low/abnormal flag, lowercased, or empty."""
    unit: str = ""
    ref: str = ""
    """A `labs:<slug>:<date>` source ref, so a retirement can cite what ended
    the hypothesis rather than merely asserting it."""


_NEGATIVE_MARKERS = (
    "not detected",
    "non-reactive",
    "nonreactive",
    "negative",
    "none seen",
    "absent",
    "not present",
    "within normal limits",
    "normal",
)


def normalize_analyte(text: str) -> str:
    """Lowercase, non-alphanumerics stripped — the key shape a `LabLookup`
    uses.

    Exported and applied at lookup time because the two sides disagreed and
    the disagreement was silent. `review.build_lab_lookup` keys on
    `_normalize_analyte(name)`, so `Vitamin B12` is stored under
    `vitaminb12`; `evaluate_rule_out` looked up `check.analyte` RAW. Only a
    single lowercase word could ever match.

    Measured on the real case file: of 16 machine-checkable rule-outs
    proposed against 461 stored analytes, **15 answered "no result on file"
    and 1 matched** — the one whose analyte was `ferritin`. Every multi-word
    or capitalised name was unreachable.

    Same shape as `criteria._RA_RF`, which was `r"rheumatoid factor"` with a
    literal space matched against a space-stripped name: a check that cannot
    fire looks exactly like a check that fires and finds nothing. Here it
    would have made ADR 0038's evaluator and ADR 0047's writer both correct
    in isolation and jointly useless.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def evaluate_rule_out(check: RuleOutCheck, labs: LabLookup) -> tuple[bool, str]:
    """`(met, why)`. Not-met and cannot-tell are both `False` — deliberately.

    An analyte nobody has measured must never end a hypothesis. Absence of a
    test is the ordinary state of every differential and reads nothing like a
    negative result; conflating them is the one failure this evaluator must
    not have.

    `why` carries the reason either way, so a report can say what happened
    rather than only what changed.
    """
    fact = labs.get(normalize_analyte(check.analyte)) or labs.get(check.analyte)
    if fact is None:
        return False, f"no {check.analyte} result on file"

    if check.operator == "negative":
        text = fact.value_text.strip().lower()
        if not text:
            return False, f"{check.analyte} has no qualitative result to read"
        # Negation first: "not detected" contains "detected", and the
        # substring order is the whole correctness of this (same rule
        # `knowledge.criteria._is_positive` states).
        if any(marker in text for marker in _NEGATIVE_MARKERS):
            return True, f"{check.analyte} is negative ({fact.value_text.strip()})"
        return False, f"{check.analyte} is not negative ({fact.value_text.strip()})"

    if check.operator == "normal":
        flag = fact.flag.strip().lower()
        if flag in ("", "n", "normal"):
            return True, f"{check.analyte} is within the lab's reference range"
        return False, f"{check.analyte} is flagged {flag!r}"

    # below / above — both require a threshold, and a unit that matches.
    if fact.value is None:
        return False, f"{check.analyte} has no numeric value to compare"
    if check.unit and fact.unit and check.unit.lower() != fact.unit.lower():
        # The eosinophil bug in another form: `4.5 %` and `320 cells/uL` are
        # the same analyte and not comparable numbers.
        return False, (
            f"{check.analyte} is stored in {fact.unit!r}, not the {check.unit!r} the rule-out names"
        )
    assert check.threshold is not None  # guarded by RuleOutCheck's validator
    if check.operator == "below":
        met = fact.value < check.threshold
    else:
        met = fact.value > check.threshold
    comparison = "below" if check.operator == "below" else "above"
    return met, (
        f"{check.analyte} is {fact.value:g}{' ' + fact.unit if fact.unit else ''}, "
        f"{'' if met else 'not '}{comparison} {check.threshold:g}"
    )


def _definitive_exclusion(hypothesis: Hypothesis) -> Evidence | None:
    """The first `evidence_against` item that both claims a definitive
    exclusion AND comes from a source allowed to make that claim.

    A `definitive-exclusion` from a refused source is not an error and does
    not raise — it simply does not end anything, and
    `refused_definitive_exclusions` reports it so the discrepancy is visible
    rather than silent.
    """
    for item in hypothesis.evidence_against:
        if item.strength != "definitive-exclusion":
            continue
        if item.source.startswith(DEFINITIVE_EXCLUSION_SOURCES):
            return item
    return None


def refused_definitive_exclusions(hypothesis: Hypothesis) -> list[Evidence]:
    """Items claiming a definitive exclusion from a source that may not make
    one. Reported, never acted on (ADR 0038)."""
    return [
        item
        for item in hypothesis.evidence_against
        if item.strength == "definitive-exclusion"
        and not item.source.startswith(DEFINITIVE_EXCLUSION_SOURCES)
    ]


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


def _excluded_by_definitive_evidence(hypothesis: Hypothesis) -> Retirement | None:
    item = _definitive_exclusion(hypothesis)
    if item is None:
        return None
    return Retirement(
        hypothesis_id=hypothesis.id,
        hypothesis_name=hypothesis.name,
        to_status="ruled-out",
        reason=f"ruled out by a definitive result — {item.claim.strip()} ({item.source})",
    )


def _rule_out_met(hypothesis: Hypothesis, labs: LabLookup) -> Retirement | None:
    if hypothesis.rule_out_check is None:
        return None
    met, why = evaluate_rule_out(hypothesis.rule_out_check, labs)
    if not met:
        return None
    return Retirement(
        hypothesis_id=hypothesis.id,
        hypothesis_name=hypothesis.name,
        to_status="ruled-out",
        reason=f"its own rule-out condition is now met — {why}",
    )


def propose_retirements(
    ledger: Ledger,
    *,
    today: date,
    stale_days: int = STALE_DAYS,
    labs: LabLookup | None = None,
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
    lab_lookup: LabLookup = labs or {}
    retirements: list[Retirement] = []
    refused: list[str] = []
    protected = 0

    for hypothesis in ledger.hypotheses:
        if hypothesis.status != "active":
            continue

        for name in (e.claim.strip() for e in refused_definitive_exclusions(hypothesis)):
            refused.append(f"{hypothesis.name}: {name}")

        # BEFORE the protection check (ADR 0038). Both rest on something
        # objective — a result, or the hypothesis's own stated condition —
        # rather than on accumulated model opinion, which is what the
        # protection exists to guard against.
        objective = _excluded_by_definitive_evidence(hypothesis) or _rule_out_met(
            hypothesis, lab_lookup
        )
        if objective is not None:
            retirements.append(objective)
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

    return RetirementReport(
        retirements=retirements, protected_count=protected, refused_exclusions=refused
    )


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
    if report.error:
        # A retirement was PROPOSED and the write failed - distinct from, and
        # printed instead of, the ordinary "nothing was retired" line, which
        # used to cover this case too and read a real operational failure
        # (a lock, an IO error) as an unremarkable quiet week.
        return [f"_Retirement was proposed this week but could not be saved: {report.error}._", ""]
    lines: list[str] = []
    if report.retirements:
        lines += [
            "These are no longer part of the working differential. Nothing is deleted — "
            "each one stays on file and comes back if new evidence turns up.",
            "",
        ]
        for item in report.retirements:
            verb = "Ruled out" if item.to_status == "ruled-out" else "Set aside"
            lines.append(f"- **{item.hypothesis_name}** — {verb}: {item.reason}.")
        lines.append("")
    else:
        # Still falls through to the notes below: a week that retired nothing
        # can still have refused an exclusion or protected a lead, and both
        # are worth saying.
        lines += ["_Nothing was retired from the differential this week._", ""]
    if report.refused_exclusions:
        # A model reaching for the one strength that bypasses the balance
        # scale is worth seeing. Silently ignoring it would hide the attempt.
        lines.append(
            "_Something was marked as definitively ruling a lead out, from a source "
            "that cannot settle it (literature, or a phenotype engine). It was not "
            "acted on:_"
        )
        lines += [f"- {item}" for item in report.refused_exclusions]
        lines.append("")
    if report.protected_count:
        lines.append(
            f"_{report.protected_count} were not assessed for retirement at all: "
            "anything in the can't-miss tier, and anything you raised yourself, "
            "stays until a person decides otherwise._"
        )
        lines.append("")
    return lines

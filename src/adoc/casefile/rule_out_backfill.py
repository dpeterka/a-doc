"""Give the leads already on the board a way to end (ADR 0047).

ADR 0035 required every new hypothesis to state what would rule it out, and
`rule_out.strip_ops_missing_rule_out` enforces it — on the diagnostic chat
path only. The review path never applied it, and 43 of the ledger's 46
active hypotheses were created there. Measured in production on 2026-09-02:

    rule_out_check populated   0 / 54
    rule_out prose populated   0 / 54
    ever retired               0

The retirement pass has been evaluating a field nothing writes. ADR 0047
closes the writer for NEW leads; this closes it for the ones already there,
which would otherwise sit unfalsifiable forever.

## One model call per batch, and the result is proposed, not applied

The rule-outs are written by the challenger role, in batches, and land as an
ordinary `LedgerDiff` through `apply_and_save` — so the ledger invariants
check them like any other write and the change is visible in the history.

Nothing is invented for a lead the model will not commit on: an entry it
declines, or answers with one of `rule_out`'s empty phrases, is left alone
and counted. A wrong rule-out is worse than none, because a wrong one can
retire a live lead.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from adoc import __version__
from adoc.casefile.rule_out import is_usable_rule_out
from adoc.casefile.schema import (
    Hypothesis,
    Ledger,
    LedgerDiff,
    Provenance,
    RuleOutCheck,
    UpdateHypothesis,
)
from adoc.reason.client import LlmClient, Message

logger = logging.getLogger(__name__)

BATCH_SIZE = 8
"""Leads per model call. Small enough that one bad response costs little,
large enough that 46 leads do not become 46 calls."""

_SYSTEM = (
    "You are helping a single-patient diagnostic case file converge.\n\n"
    "Its differential holds leads that were added without stating what would "
    "take them off the board, so none of them can ever be ruled out and the "
    "list only grows. For each lead below, give the single result that would "
    "end it.\n\n"
    "Name a result someone could actually get back — 'a normal serum "
    "metanephrines', 'a negative anti-dsDNA', 'a temporal-bone CT showing no "
    "bone erosion'. Do NOT write 'further testing', 'more information', "
    "'clinical correlation' or anything else that names the wish for a "
    "result rather than a result: a requirement any hypothesis can satisfy "
    "is not a requirement.\n\n"
    "If you cannot honestly name one for a lead, return an empty string for "
    "it. That is a real answer and it is better than a wrong one — a wrong "
    "rule-out retires a live lead.\n\n"
    "SECOND, and separately: when — and only when — that result is one of "
    "the lab analytes listed below, also give the machine-checkable form: "
    "`analyte` copied EXACTLY from the list, and `operator` as one of "
    "`negative` (a qualitative result reading negative), `normal` (within "
    "the lab's own reference range), `below` or `above` (with a numeric "
    "`threshold` and its `unit`).\n\n"
    "Leave `analyte` empty when the rule-out is imaging, a biopsy, an "
    "examination finding, or anything else not in that list. Do not "
    "approximate a name to make it fit: an analyte nobody has measured "
    "cannot be evaluated, and an invented one is silently useless."
)


class RuleOutProposal(BaseModel):
    """One lead's proposed falsification condition, in both halves.

    Prose alone does not retire anything. `retirement._rule_out_met` returns
    immediately unless `rule_out_check` is set — it never reads the prose —
    so a backfill that wrote only `rule_out` would satisfy ADR 0035 and
    still retire nothing. Both halves or the exercise is decorative.
    """

    id: str
    rule_out: str = ""
    analyte: str = ""
    """The stored lab name this turns on, or empty when the rule-out is not
    a lab at all (imaging, biopsy, an examination finding). Validated
    against the analytes actually on file: an analyte nobody has measured
    makes the check unevaluable, and `evaluate_rule_out` treats
    cannot-tell as not-met, so an invented name is silently inert."""
    operator: Literal["negative", "normal", "below", "above", ""] = ""
    threshold: float | None = None
    unit: str = ""


class RuleOutProposals(BaseModel):
    proposals: list[RuleOutProposal] = Field(default_factory=list)


class BackfillReport(BaseModel):
    """What the backfill did, and what it declined to do."""

    considered: int = 0
    proposed: int = 0
    unusable: int = 0
    """Returned, but vacuous — `further testing` and friends."""
    declined: int = 0
    """The model returned nothing for this lead, deliberately."""
    checkable: int = 0
    """Of the proposed, how many also carry a `rule_out_check`. This is the
    number that decides whether anything can ever retire: prose alone
    satisfies ADR 0035 and `retirement._rule_out_met` never reads it."""
    unknown_analytes: list[str] = Field(default_factory=list)
    """Analytes named that are not on file — an unevaluable check, recorded
    rather than written."""
    unknown_ids: list[str] = Field(default_factory=list)
    applied: int = 0


def needs_rule_out(ledger: Ledger) -> list[Hypothesis]:
    """Active leads with nothing that could end them."""
    return [
        h
        for h in ledger.hypotheses
        if h.status in {"active", "monitoring"}
        and not (h.rule_out or "").strip()
        and h.rule_out_check is None
    ]


def _render_analytes(analytes: Sequence[str]) -> str:
    """The analytes actually on file, so a proposed check can be evaluated.

    Without this the model names textbook analytes and `evaluate_rule_out`
    answers "no X result on file" forever — not-met, safe, and inert."""
    if not analytes:
        return "## Lab analytes on file\n\n(none — leave `analyte` empty for every lead)\n"
    return "## Lab analytes on file (copy exactly)\n\n" + "\n".join(
        f"- {name}" for name in analytes
    )


def _render(batch: Sequence[Hypothesis]) -> str:
    lines: list[str] = []
    for h in batch:
        lines.append(f"### {h.id}")
        lines.append(f"Name: {h.name}")
        lines.append(f"Tier: {h.tier}   Probability: {h.probability}")
        if h.evidence_for:
            lines.append("Evidence for:")
            lines += [f"  - {e.claim}" for e in h.evidence_for[:4]]
        if h.evidence_against:
            lines.append("Evidence against:")
            lines += [f"  - {e.claim}" for e in h.evidence_against[:3]]
        lines.append("")
    return "\n".join(lines)


def _checkable(proposal: RuleOutProposal, known: dict[str, str]) -> RuleOutCheck | None:
    """The machine-checkable half, or `None` if it cannot be made evaluable.

    Refuses rather than approximates. `evaluate_rule_out` treats an analyte
    with no result on file as not-met, so a check naming an invented analyte
    is indistinguishable from a working one and will never fire — exactly
    the silent-absence shape this repository keeps hitting.
    """
    analyte = proposal.analyte.strip()
    if not analyte or not proposal.operator:
        return None
    stored = known.get(analyte.lower())
    if stored is None:
        return None
    if proposal.operator in {"below", "above"} and proposal.threshold is None:
        # `RuleOutCheck`'s own validator requires it; refusing here keeps the
        # failure a counted outcome rather than an exception mid-batch.
        return None
    return RuleOutCheck(
        analyte=stored,
        operator=proposal.operator,
        threshold=proposal.threshold,
        unit=proposal.unit.strip(),
    )


def propose_rule_outs(
    client: LlmClient,
    ledger: Ledger,
    *,
    batch_size: int = BATCH_SIZE,
    analytes: Iterable[str] = (),
) -> tuple[list[UpdateHypothesis], BackfillReport]:
    """Ops setting `rule_out` on every lead that has none, plus a report.

    Never raises for a bad batch: one unusable response must not cost the
    other five batches, the same posture every other stage here takes.
    """
    targets = needs_rule_out(ledger)
    known_analytes = sorted({a.strip() for a in analytes if a.strip()})
    lowered = {a.lower(): a for a in known_analytes}
    report = BackfillReport(considered=len(targets))
    by_id = {h.id: h for h in targets}
    ops: list[UpdateHypothesis] = []
    seen: set[str] = set()

    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        try:
            result = client.complete(
                "challenger",
                system=_SYSTEM,
                messages=[
                    Message(
                        role="user",
                        content=f"{_render_analytes(known_analytes)}\n\n{_render(batch)}",
                    )
                ],
                schema=RuleOutProposals,
            )
            payload = result.parsed
        except Exception as exc:  # noqa: BLE001 - a bad batch must not stop the rest
            logger.warning("rule-out backfill: batch starting at %d failed: %s", start, exc)
            continue
        if not isinstance(payload, RuleOutProposals):
            logger.warning("rule-out backfill: batch starting at %d returned no proposals", start)
            continue

        for proposal in payload.proposals:
            if proposal.id not in by_id:
                report.unknown_ids.append(proposal.id)
                continue
            if proposal.id in seen:
                continue
            seen.add(proposal.id)
            text = proposal.rule_out.strip()
            if not text:
                report.declined += 1
                continue
            if not is_usable_rule_out(text):
                report.unusable += 1
                continue
            report.proposed += 1
            check = _checkable(proposal, lowered)
            if check is not None:
                report.checkable += 1
            elif proposal.analyte.strip():
                # Named an analyte that is not on file. Recorded rather than
                # accepted: `evaluate_rule_out` would answer "no result on
                # file" forever, which is not-met, safe, and inert — a check
                # that looks like a check and can never fire.
                report.unknown_analytes.append(proposal.analyte.strip())
            ops.append(UpdateHypothesis(id=proposal.id, rule_out=text, rule_out_check=check))

    return ops, report


def backfill_diff(ops: Sequence[UpdateHypothesis], *, model_id: str) -> LedgerDiff:
    """Wrap the ops in a diff so the ledger invariants see them."""
    return LedgerDiff(
        provenance=Provenance(
            app_version=__version__,
            prompt_template_version="rule_out_backfill@v1",
            model_id=model_id,
            dag_node="rule_out_backfill",
            timestamp=datetime.now(UTC),
        ),
        rationale=(
            f"ADR 0047: gave {len(ops)} lead(s) a stated way to end. Leads created before "
            "the review path enforced ADR 0035 had no falsification condition, so the "
            "retirement pass could never evaluate them."
        ),
        ops=list(ops),
    )

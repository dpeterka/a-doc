"""Adjudicating what the phenotype engines say (PLAN.md phase 3, criterion 1).

LIRICAL and the similarity index have run inside the review since ADR 0029,
and until now their output was *rendered* and nothing more. Both nodes sit
after `apply_review_diff`, so nothing they found could reach the ledger: the
review got longer, and the differential did not get sharper. That is the
failure `docs/research/scoring-across-engines.md` predicted in as many words —
"they will add three more opinions to fifty and the report will get longer
rather than sharper".

## What this does, and what it refuses to do

It does **not** fuse scores. LIRICAL reports a likelihood ratio, the index
reports Resnik similarity, and the ledger carries uncalibrated probability
buckets. Averaging those is arithmetic on incommensurable units, and the
research note rejects it outright. Nothing here multiplies, weights or
combines a score with another score.

It combines at the level of **direction** — corroborates / opposes / neutral —
which is the one thing every unit can honestly state and which is comparable
across units. The model supplies the direction and the reasoning. What that
direction *does* to the ledger is plain code below, because that mapping is a
policy decision and policy decisions are not delegated to a model.

## The neutral verdict is the important one

A phenotype engine that has never heard of a hypothesis has not refuted it.
LIRICAL knows phenotype and nothing else, so a hypothesis rooted in serology,
imaging or exposure history can be entirely correct and still score zero. If
`ledger_only` were read as opposition, this node would spend every review
manufacturing counter-evidence against the hypotheses whose support happens to
live in a modality the engine cannot see — and the retirement pass, which
counts counter-evidence, would then start killing them.

So `neutral` is a first-class outcome with no ledger effect, and the prompt
pushes toward it whenever the engine is simply out of its depth.

## Why it can only add evidence

The ops this emits are `AddEvidence` and — for a genuinely missed candidate —
`AddHypothesis`. It never edits a probability or a tier. Those belong to the
stages that reason over the whole case; an engine that saw only the phenotype
should not be re-grading a differential it cannot fully see. Evidence is
enough to drive convergence on its own, because `casefile.retirement` already
retires on accumulated counter-evidence.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    Ledger,
    LedgerDiff,
    LedgerOp,
    Provenance,
)
from adoc.knowledge.lirical_divergence import LiricalComparison, LiricalFinding

logger = logging.getLogger(__name__)

EngineName = Literal["lirical", "semsim"]
Direction = Literal["corroborates", "opposes", "neutral"]

# Engine evidence is `moderate`, never `strong`.
#
# `casefile.retirement` counts strong evidence double when deciding whether a
# hypothesis is outweighed. A phenotype engine reproducing the phenotype it
# was given is real support but it is not independent confirmation, and
# letting it carry double weight would let two engines retire a hypothesis
# between them without a human or a lab ever weighing in.
ENGINE_EVIDENCE_STRENGTH = "moderate"

# A newly adopted engine candidate starts here. `expanded`, never
# `most-likely`: an engine ranking is a reason to look, not a reason to lead.
ADOPTED_TIER = "expanded"
ADOPTED_PROBABILITY = "low"

# Ceiling on hypotheses adopted from engine output in one review.
#
# `engine_only` findings are capped at the engines' own top-N, but two engines
# on a broad phenotype can still surface a dozen plausible-looking rare
# diseases at once. Adding twelve hypotheses in one pass is precisely the
# inflation this whole node exists to counteract, and it would swamp a
# differential a person has to read. The highest-ranked survive; the rest are
# reported as considered.
MAX_ADOPTIONS_PER_REVIEW = 3

_ID_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _ID_RE.sub("-", text.lower()).strip("-")


class EngineDivergence(BaseModel):
    """One thing an engine and the ledger disagree about, put to the model."""

    id: str
    engine: EngineName
    kind: Literal["engine_only", "ledger_only"]
    name: str
    hypothesis_id: str | None = None
    score_label: str = ""
    """The engine's own score, in the engine's own units and labelled with
    them — `LR 12.4`, `similarity 3.81`. Never normalised into a shared
    scale, because there isn't one."""
    rank: int | None = None


class EngineVerdictPayload(BaseModel):
    """What the model returns per divergence."""

    divergence: str
    direction: Direction
    rationale: str
    rule_out: str = ""
    """Required only when adopting an `engine_only` candidate: what would kill
    this hypothesis. A hypothesis with no stated way to die will not die
    (`docs/research/scoring-across-engines.md`, part 3a), and this node is not
    going to add ones that cannot."""


class EngineAdjudicationPayload(BaseModel):
    verdicts: list[EngineVerdictPayload] = Field(default_factory=list)


class EngineAdjudicationResult(BaseModel):
    ran: bool = False
    error: str = ""
    divergences: list[EngineDivergence] = Field(default_factory=list)
    verdicts: list[EngineVerdictPayload] = Field(default_factory=list)
    model_id: str = ""
    prompt_template_version: str = ""

    @property
    def by_direction(self) -> dict[str, int]:
        counts: dict[str, int] = {"corroborates": 0, "opposes": 0, "neutral": 0}
        for verdict in self.verdicts:
            counts[verdict.direction] = counts.get(verdict.direction, 0) + 1
        return counts


def collect_divergences(
    lirical: LiricalComparison, semsim: LiricalComparison
) -> list[EngineDivergence]:
    """Everything the two engines disagree with the ledger about.

    Agreements are deliberately excluded. They are handled without a model
    call by `agreement_evidence` below — an engine independently ranking a
    hypothesis the ledger already holds is a fact to record, not a judgement
    to make, and spending a reasoning call to be told "yes, they agree" would
    be waste.
    """
    divergences: list[EngineDivergence] = []
    for engine, comparison in (("lirical", lirical), ("semsim", semsim)):
        if not comparison.ran:
            continue
        for finding in comparison.findings:
            if finding.kind == "agreement":
                continue
            divergences.append(
                EngineDivergence(
                    id=f"{engine}:{finding.kind}:{_slug(finding.disease_name)}",
                    engine=engine,
                    kind=finding.kind,
                    name=finding.disease_name,
                    hypothesis_id=finding.ledger_hypothesis_id,
                    score_label=_score_label(engine, finding),
                    rank=finding.rank,
                )
            )
    return divergences


def _score_label(engine: str, finding: LiricalFinding) -> str:
    """The engine's score in its own units, or nothing.

    Both engines store their score in `composite_lr`, but it means completely
    different things: LIRICAL's is a likelihood ratio, the index's is a Resnik
    similarity. The field is shared; the unit is not. Labelling by engine is
    the whole reason this function exists — calling a similarity an "LR" is
    exactly the unit-blindness `render_semsim_comparison` refuses to commit
    and `docs/research/scoring-across-engines.md` argues against.
    """
    score = finding.composite_lr
    if score is None:
        return ""
    return f"LR {score:.3g}" if engine == "lirical" else f"similarity {score:.3g}"


def _cites_engine(hypothesis: Hypothesis, engine: str) -> bool:
    """Whether this hypothesis already carries evidence from `engine`.

    The engines run on every review and their findings are stable, so without
    this the same corroboration would be appended week after week until the
    hypothesis card was nothing but engine refs. Matched on the engine name
    rather than the whole ref because the ref carries the review's date and so
    differs every time.
    """
    prefix = f"engine:{engine}:"
    every = (*hypothesis.evidence_for, *hypothesis.evidence_against)
    return any(e.source.startswith(prefix) for e in every)


def agreement_evidence(
    lirical: LiricalComparison,
    semsim: LiricalComparison,
    ledger: Ledger,
    *,
    today: date,
) -> list[LedgerOp]:
    """Record corroboration. Deterministic — no model call.

    Where an independent engine ranks a hypothesis the ledger already holds,
    that is the best-supported kind of finding in the case and it was going
    entirely unrecorded: 24 of 25 hypotheses in production had an empty
    evidence list (see `DivergenceSet.panel_citations` for the same problem
    found in the blind panel). Agreement between units that work in genuinely
    different ways is exactly what should survive into the record.
    """
    by_id = {h.id: h for h in ledger.hypotheses}
    ops: list[LedgerOp] = []
    for engine, comparison in (("lirical", lirical), ("semsim", semsim)):
        if not comparison.ran:
            continue
        for finding in comparison.of_kind("agreement"):
            hypothesis_id = finding.ledger_hypothesis_id
            hypothesis = by_id.get(hypothesis_id) if hypothesis_id else None
            if hypothesis is None or _cites_engine(hypothesis, engine):
                continue
            score = _score_label(engine, finding)
            where = f" at rank {finding.rank}" if finding.rank else ""
            ops.append(
                AddEvidence(
                    id=hypothesis.id,
                    for_or_against="for",
                    evidence=Evidence(
                        claim=(
                            f"Independently ranked by {engine}{where} from this patient's "
                            f"phenotype alone{f' ({score})' if score else ''}."
                        ),
                        source=f"engine:{engine}:{today.isoformat()}",
                        strength=ENGINE_EVIDENCE_STRENGTH,
                    ),
                )
            )
    return ops


def verdicts_to_ops(
    divergences: list[EngineDivergence],
    verdicts: list[EngineVerdictPayload],
    ledger: Ledger,
    *,
    today: date,
) -> tuple[list[LedgerOp], list[str]]:
    """Turn directions into ledger ops. Plain code, deliberately.

    Returns the ops and a list of human-readable notes about what was NOT
    done and why — a verdict that changes nothing is a real outcome and
    silently dropping it would make the node look like it did less than it
    did.
    """
    by_id = {d.id: d for d in divergences}
    existing = {h.id: h for h in ledger.hypotheses}
    ops: list[LedgerOp] = []
    notes: list[str] = []
    adoptions = 0

    for verdict in verdicts:
        divergence = by_id.get(verdict.divergence)
        if divergence is None:
            notes.append(f"verdict for unknown divergence {verdict.divergence!r}, ignored")
            continue

        if verdict.direction == "neutral":
            # The common and correct outcome for `ledger_only`. No op.
            continue

        if divergence.kind == "ledger_only":
            if verdict.direction == "corroborates":
                # Incoherent: `ledger_only` means the engine did NOT rank it.
                # Treated as neutral rather than trusted, and said out loud.
                notes.append(
                    f"{divergence.name}: {divergence.engine} did not rank this, so a "
                    "'corroborates' verdict cannot be acted on — treated as neutral"
                )
                continue
            hypothesis = existing.get(divergence.hypothesis_id or "")
            if hypothesis is None or _cites_engine(hypothesis, divergence.engine):
                continue
            ops.append(
                AddEvidence(
                    id=hypothesis.id,
                    for_or_against="against",
                    evidence=Evidence(
                        claim=_counter_claim(divergence, verdict),
                        source=f"engine:{divergence.engine}:{today.isoformat()}",
                        strength=ENGINE_EVIDENCE_STRENGTH,
                    ),
                )
            )
            continue

        # engine_only
        if verdict.direction != "corroborates":
            notes.append(
                f"{divergence.name}: raised by {divergence.engine} and not adopted "
                f"({verdict.direction})"
            )
            continue
        if not verdict.rule_out.strip():
            notes.append(
                f"{divergence.name}: not adopted — no rule-out condition given, and a "
                "hypothesis with no stated way to die will not die"
            )
            continue
        if adoptions >= MAX_ADOPTIONS_PER_REVIEW:
            notes.append(
                f"{divergence.name}: ranked by {divergence.engine} but not adopted this "
                f"review — already at the cap of {MAX_ADOPTIONS_PER_REVIEW}"
            )
            continue
        new_id = _slug(divergence.name)
        if new_id in existing:
            continue
        ops.append(
            AddHypothesis(
                hypothesis=Hypothesis(
                    id=new_id,
                    name=divergence.name,
                    tier=ADOPTED_TIER,
                    probability=ADOPTED_PROBABILITY,
                    status="active",
                    origin="model",
                    first_proposed=today,
                    rule_out=verdict.rule_out.strip(),
                    evidence_for=[
                        Evidence(
                            claim=_support_claim(divergence, verdict),
                            source=f"engine:{divergence.engine}:{today.isoformat()}",
                            strength=ENGINE_EVIDENCE_STRENGTH,
                        )
                    ],
                )
            )
        )
        adoptions += 1

    return ops, notes


def _support_claim(divergence: EngineDivergence, verdict: EngineVerdictPayload) -> str:
    where = f" at rank {divergence.rank}" if divergence.rank else ""
    score = f" ({divergence.score_label})" if divergence.score_label else ""
    return (
        f"Raised by {divergence.engine}{where} from this patient's phenotype{score}, "
        f"and not previously on the differential. {verdict.rationale.strip()}"
    )[:600]


def _counter_claim(divergence: EngineDivergence, verdict: EngineVerdictPayload) -> str:
    return (
        f"{divergence.engine} did not support this on phenotype grounds. "
        f"{verdict.rationale.strip()}"
    )[:600]


def build_engine_diff(
    result: EngineAdjudicationResult,
    lirical: LiricalComparison,
    semsim: LiricalComparison,
    ledger: Ledger,
    *,
    today: date,
    provenance: Provenance,
) -> tuple[LedgerDiff | None, list[str]]:
    """The whole deterministic half: agreements plus adjudicated divergences.

    `None` when there is nothing to write, which is an ordinary review.
    """
    ops = agreement_evidence(lirical, semsim, ledger, today=today)
    verdict_ops, notes = verdicts_to_ops(result.divergences, result.verdicts, ledger, today=today)
    ops += verdict_ops
    if not ops:
        return None, notes
    return (
        LedgerDiff(
            provenance=provenance,
            rationale=(
                f"Phenotype-engine adjudication: {len(ops)} op(s) from "
                f"{len(result.verdicts)} verdict(s) and engine agreement."
            ),
            ops=ops,
        ),
        notes,
    )


def render_engine_adjudication(result: EngineAdjudicationResult, notes: list[str]) -> list[str]:
    """Report lines. Empty when there is nothing to say."""
    if not result.ran and not result.error:
        return []
    lines = ["### What the phenotype engines change", ""]
    if result.error:
        lines += [f"_Not adjudicated this review: {result.error}_", ""]
        return lines

    counts = result.by_direction
    lines.append(
        f"{len(result.verdicts)} divergence(s) adjudicated — "
        f"{counts['corroborates']} corroborating, {counts['opposes']} opposing, "
        f"{counts['neutral']} neutral."
    )
    lines.append("")
    by_id = {d.id: d for d in result.divergences}
    for verdict in result.verdicts:
        divergence = by_id.get(verdict.divergence)
        if divergence is None:
            continue
        lines.append(
            f"- **{divergence.name}** ({divergence.engine}, {divergence.kind}) — "
            f"**{verdict.direction}**. {verdict.rationale.strip()}"
        )
    if notes:
        lines += ["", "_Considered and not acted on:_"]
        lines += [f"- {note}" for note in notes]
    lines.append("")
    return lines

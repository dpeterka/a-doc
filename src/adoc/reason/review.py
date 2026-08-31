"""The weekly deep review (PLAN.md "Session loops (c)"), as a typed DAG.

`run_weekly_review` assembles and runs an explicit `reason.dag.Dag` (so the
contracts below are enforced by code, not hoped for by a prompt):

  trend_scan -> blind_panel_0..N-1 -> current_ledger -> divergence_diff
    -> adjudication -> challenge_sweep -> apply_review_diff -> test_chooser
    -> staleness_scan -> ops_metrics -> render_report

Contracts:
  - every `blind_panel_i` node carries TWO blindness preconditions (ADR 0002's
    "blind-reviewer rule", amended): `forbid_context_key("ledger")` — the
    ledger is never loaded into the DAG's run context until *after* every
    blind panel member has produced its de novo differential, so a caller
    who sneaks a `"ledger"` key into `initial` (the negative test in
    `tests/test_review.py`) trips it immediately — and
    `edge_payload_lacks_section("ledger")`, which inspects the panel node's
    actual validated input (the `ContextPack` under `"blind_context_pack"`)
    for a ledger section. The two checks are not redundant:
    `forbid_context_key` only ever sees the run-context *dict keys*, so it
    would NOT catch a real regression where `blind_context_pack` itself was
    built with `include_ledger=True` (that ledger content never becomes a
    `ctx["ledger"]` entry) — `edge_payload_lacks_section` is the
    content-aware check that does catch it (see `tests/test_review.py`'s
    dedicated regression test).
  - `adjudication`'s postcondition (`adjudication_covers_every_divergence`)
    requires an explicit accept/reject decision, with a substantive
    rationale (>= `MIN_SUBSTANTIVE_LENGTH` chars, not identical across every
    divergence), for every divergence the deterministic diff produced
    (PLAN.md "Anti-anchoring").
  - `challenge_sweep`'s postcondition (`challenge_sweep_covers_every_active_hypothesis`)
    requires a substantive note (same length/non-identical bar) for every
    currently-active hypothesis.
  - `apply_review_diff`'s postcondition requires the ledger version to have
    incremented.

`apply_review_diff` merges the accepted-divergence ops and the full
challenge-sweep's `RecordChallenge` ops into ONE `LedgerDiff`, applied in a
single `DataRepo.apply_ledger_diff` call (not the lock-free `casefile.
ledger.apply_and_save` primitive it wraps — this DAG runs from the weekly
scheduled task, which shares the same EFS-mounted data repo with the web
task's diagnostic turns, so it is exactly as exposed to the concurrent-
write durability defect `apply_ledger_diff` fixes; see that method's
docstring) — recording a challenge for every currently active hypothesis
in the same diff is what keeps `ledger.apply_diff`'s staleness invariant
(c) satisfied regardless of how stale any hypothesis was going into the
review; that is the entire point of a weekly full sweep.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from adoc import __version__
from adoc.casefile.ledger import ACTIVE_STATUSES, load_ledger
from adoc.casefile.phenotype import PHENOTYPE_RELPATH, load_phenotype, select_for_engine
from adoc.casefile.questions import (
    QUESTIONS_RELPATH,
    OpenQuestion,
    load_questions,
    merge_proposed,
    question_id,
    save_questions,
)
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH, DataRepo
from adoc.casefile.retirement import (
    RetirementReport,
    propose_retirements,
    render_retirements,
    retirements_to_diff,
)
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    EvidenceStrength,
    Hypothesis,
    Ledger,
    LedgerDiff,
    LedgerOp,
    ProbabilityBucket,
    Provenance,
    RecordChallenge,
    Tier,
    UpdateHypothesis,
    validate_source_ref,
)
from adoc.config import reference_path
from adoc.knowledge.criteria import CriteriaResult, score_all
from adoc.knowledge.icap import IcapReport, render_icap, scan_ana_patterns
from adoc.knowledge.lirical import LiricalRequest
from adoc.knowledge.lirical_divergence import (
    LiricalComparison,
    compare_semsim_to_ledger,
    compare_to_ledger,
    render_comparison,
    render_semsim_comparison,
)
from adoc.knowledge.lirical_runner import LIRICAL_WORK_RELDIR, EcsLiricalRunner, LiricalRunner
from adoc.knowledge.mondo import load_mondo_index
from adoc.knowledge.pubmed import PUBMED_CACHE_RELPATH, PubMedArticle, PubMedClient
from adoc.knowledge.semsim import load_index
from adoc.labs.db import LabsDb
from adoc.labs.queries import abnormal_summary
from adoc.labs.validate import canonicalize, trend_outlier
from adoc.reason.citations import check_evidence_citations
from adoc.reason.client import LlmClient, Message
from adoc.reason.context import ContextPack, build_context
from adoc.reason.dag import (
    Contract,
    Ctx,
    Dag,
    Node,
    edge_payload_lacks_section,
    forbid_context_key,
    run,
)
from adoc.reason.engine_adjudication import (
    EngineAdjudicationPayload,
    EngineAdjudicationResult,
    build_engine_diff,
    collect_divergences,
    render_engine_adjudication,
)
from adoc.reason.prompts import load_prompt
from adoc.reason.review_trigger import (
    ReviewMarker,
    clear_review_marker,
    load_review_marker,
)
from adoc.reason.tools import redact_gated_text
from adoc.reason.verify import (
    ENTAILMENT_CACHE_RELPATH,
    Claim,
    DefaultSourceTextResolver,
    EntailmentCache,
    SourceTextResolver,
    log_verification_report,
    pop_deferred_claims,
    verify_claims,
)

# --------------------------------------------------------------------------
# Stage-IO models
# --------------------------------------------------------------------------


class Marker(BaseModel):
    """A trivial initial-context payload for a node with no real input."""

    value: str = "start"


class TrendFinding(BaseModel):
    analyte: str
    date: date
    value: float | None
    message: str


class TrendScanResult(BaseModel):
    findings: list[TrendFinding] = Field(default_factory=list)


_EVIDENCE_STRENGTHS = frozenset({"strong", "moderate", "weak"})

_EVIDENCE_STRENGTH_SYNONYMS = {
    "supporting": "moderate",
    "supportive": "moderate",
    "suggestive": "weak",
    "possible": "weak",
    "definitive": "strong",
    "conclusive": "strong",
    "high": "strong",
    "medium": "moderate",
    "low": "weak",
}
"""Words panel members reach for instead of the three the schema allows.
`supporting` is the one observed in production; the rest are the obvious
neighbours, mapped so a near-miss keeps its meaning rather than flattening
to the default."""


_PROBABILITY_BUCKETS = frozenset({"high", "moderate", "low", "minimal"})

_PROBABILITY_BUCKET_SYNONYMS = {
    "possible": "low",
    "likely": "high",
    "probable": "high",
    "very high": "high",
    "very low": "minimal",
    "unlikely": "minimal",
    "remote": "minimal",
    "medium": "moderate",
    "intermediate": "moderate",
}
"""Words panel members reach for instead of the four the schema allows."""


class BlindEvidenceItem(BaseModel):
    """One cited claim supporting a blind panel member's hypothesis.

    Exists because the prompt has always asked the panel to cite source refs
    and the schema gave it nowhere to put them. The result, measured on a
    real review: 24 hypotheses reached the ledger with ZERO evidence items,
    and zero refs even in the prose — the panel cited *values* densely
    ("FSH 91.4 mIU/mL") but never a resolvable `labs:fsh:<date>`. Every
    hypothesis card in the UI therefore rendered an empty evidence section.

    `source` is deliberately an UNVALIDATED string. The first version of this
    model validated it against `casefile.schema.validate_source_ref` in a
    field validator, which raises — so a single invented ref failed the whole
    `BlindDifferentialPayload`, which failed the panel member, which failed
    the review. That is exactly what happened on the first real run: the
    panel finally cited densely, guessed four prefixes wrong
    (`other:monospot_(heterophile)_screen:2026-03-17` — a real analyte on a
    real date), and took a 14-node, 12-minute review down with it.

    A bad citation must cost the citation. Refs are filtered by
    `_resolvable_evidence` after the payload parses, which drops and logs
    what does not resolve — the strip-not-fail posture of ADR 0016. Nothing
    unresolvable reaches the ledger either way; the difference is whether the
    other 23 hypotheses survive.
    """

    claim: str
    source: str
    strength: EvidenceStrength = "moderate"

    @field_validator("strength", mode="before")
    @classmethod
    def _tolerate_unknown_strength(cls, value: object) -> object:
        """An unrecognised strength degrades to the default instead of failing
        the payload.

        The `source` fix above did not generalise, and the very next review
        died the same death one field over: a panel member wrote
        `strength: "supporting"` and the `Literal` rejected it, taking all 14
        nodes and 13 minutes with it. The rule is not "validate refs late", it
        is **no single field of one evidence item may fail the payload** —
        the schema exists to shape a hypothesis, not to referee an adjective.

        Synonyms map where the intent is unambiguous; anything else becomes
        `moderate`, which is the field's default and the honest reading of a
        vocabulary the model did not commit to.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _EVIDENCE_STRENGTHS:
                return normalized
            mapped = _EVIDENCE_STRENGTH_SYNONYMS.get(normalized)
            if mapped is not None:
                return mapped
            logger.warning(
                "review: panel used an unknown evidence strength %r; recording as 'moderate'",
                value,
            )
        return "moderate"


class BlindDifferentialItem(BaseModel):
    """One hypothesis in a blind panel member's de novo differential —
    the schema `reason/prompts/blind_reviewer.md` describes."""

    name: str
    probability_bucket: ProbabilityBucket
    why: str
    cant_miss: bool = False

    @field_validator("probability_bucket", mode="before")
    @classmethod
    def _tolerate_unknown_bucket(cls, value: object) -> object:
        """Same posture as `BlindEvidenceItem.strength`, for the same reason.

        This field is REQUIRED and a `Literal`, so an unrecognised word here
        is even more destructive than a bad strength — it fails the item, the
        payload, the panel member and the review. Two separate 13-minute runs
        have already been lost to a `Literal` refusing a near-miss.

        Unknown values fall to `low` rather than the middle: a bucket this
        code did not understand is not evidence of a high-probability
        hypothesis, and `cant_miss` carries urgency independently of it.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _PROBABILITY_BUCKETS:
                return normalized
            mapped = _PROBABILITY_BUCKET_SYNONYMS.get(normalized)
            if mapped is not None:
                return mapped
            logger.warning(
                "review: panel used an unknown probability bucket %r; recording as 'low'", value
            )
        return "low"

    evidence: list[BlindEvidenceItem] = Field(default_factory=list)
    """Cited support. Empty is tolerated rather than rejected: a panel member
    that cites nothing should still contribute its hypothesis to the
    divergence diff, and a missing citation is visible in the UI instead of
    failing a 12-minute review."""


class BlindDifferentialPayload(BaseModel):
    """What the `blind_panel` LLM call itself returns."""

    items: list[BlindDifferentialItem] = Field(default_factory=list)


class BlindDifferential(BaseModel):
    """A `BlindDifferentialPayload` stamped with which panel member (by
    binding index) and model produced it — stamped by code, mirroring
    `reason.stages._LedgerDiffPayload`/`Provenance`."""

    items: list[BlindDifferentialItem] = Field(default_factory=list)
    panel_index: int
    model_id: str = ""


DivergenceKind = Literal["panel_only", "probability_mismatch", "ledger_only"]


class Divergence(BaseModel):
    """One place the blind panel's pooled output disagrees with the
    current ledger, from the deterministic diff in `compute_divergences`."""

    id: str
    kind: DivergenceKind
    name: str
    ledger_hypothesis_id: str | None = None
    panel_probability_bucket: ProbabilityBucket | None = None
    ledger_probability_bucket: ProbabilityBucket | None = None
    panel_cant_miss: bool = False
    support_count: int = 1
    rationale_hint: str = ""
    panel_evidence: list[BlindEvidenceItem] = Field(default_factory=list)
    """Cited support pooled from every panel member that raised this
    hypothesis, deduplicated by `(claim, source)`. Carried through so an
    accepted divergence lands in the ledger WITH its citations instead of
    prose alone (see `_merge_review_ops`)."""


class DivergenceSet(BaseModel):
    divergences: list[Divergence] = Field(default_factory=list)

    panel_citations: dict[str, list[BlindEvidenceItem]] = Field(default_factory=dict)
    """Panel citations for ledger hypotheses the panel NAMED, keyed by ledger
    hypothesis id — including the ones it agreed with.

    A divergence, by definition, exists only where the panel and the ledger
    disagree. So citations used to survive exclusively on disagreement: an
    accepted `panel_only` divergence became a new hypothesis carrying its
    refs, and everything else dropped them. Where the panel *agreed* with a
    ledger hypothesis, `compute_divergences` recorded the name in
    `covered_norms` and discarded the evidence entirely — and a
    `probability_mismatch` pooled only the *mismatched* members' citations,
    throwing away those of members who happened to agree.

    That inverted the intent. The hypotheses both the ledger and an
    independent blind panel endorse are the best-supported ones in the case,
    and they were precisely the ones left uncited — 24 of 25 hypotheses in
    prod had an empty evidence list, which is what the patient's hypothesis
    cards render. Citations are a fact about the data, not a verdict on a
    probability, so they are collected for every named hypothesis and
    attached independently of what the adjudicator decides.
    """


def _resolvable_items(
    items: Iterable[BlindEvidenceItem], label: str, db: LabsDb, repo: DataRepo
) -> list[Evidence]:
    """`items`, keeping only citations whose refs are well-formed AND resolve.

    Shared by both citation paths: the divergence path (a new hypothesis
    carrying its refs) and the agreement path (`AddEvidence` onto a
    hypothesis the ledger already holds). `label` names the hypothesis in the
    log line only.
    """
    kept: list[Evidence] = []
    for ev in items:
        # Grammar first, and defensively: `Evidence.source` validates its ref
        # and RAISES, which is right for the ledger's own type but means the
        # citation check below cannot be reached with a malformed ref. The
        # panel emitted `other:<analyte>:<date>` — well-formed in shape, bad
        # in prefix — so this branch is a live path, not a theoretical one.
        try:
            validate_source_ref(ev.source)
        except (ValueError, ValidationError) as exc:
            logger.warning(
                "review: dropped a malformed panel citation for %r: %s (%s)", label, ev.source, exc
            )
            continue
        report = check_evidence_citations(
            [Evidence(claim=ev.claim, source=ev.source, strength=ev.strength)], db, repo
        )
        if report.failing:
            logger.warning(
                "review: dropped an unresolvable panel citation for %r: %s (%s)",
                label,
                ev.source,
                report.failing[0].reason,
            )
            continue
        kept.append(Evidence(claim=ev.claim, source=ev.source, strength=ev.strength))
    return kept


def _resolvable_evidence(divergence: Divergence, db: LabsDb, repo: DataRepo) -> list[Evidence]:
    """The divergence's pooled panel citations, keeping only refs that
    actually resolve.

    The review path has no citation-check DAG contract of its own (unlike a
    diagnostic turn, where `citation_check` gates `apply`), so a ref that
    resolves to nothing would otherwise become an uncheckable ledger entry —
    exactly the fabrication Phase 2 exists to prevent. Filtering here rather
    than failing the run follows ADR 0016's posture: strip the unsupported
    claim and proceed, because destroying a 12-minute review over one bad
    ref costs more than it protects.

    An unresolvable ref is LOGGED, never silently dropped.
    """
    return _resolvable_items(divergence.panel_evidence, divergence.name, db, repo)


def _pool_evidence(items: Iterable[BlindDifferentialItem]) -> list[BlindEvidenceItem]:
    """Every citation the panel members offered for one hypothesis, deduped
    on `(claim, source)`.

    Panel members work independently, so two of them citing the same lab row
    for the same claim is agreement, not two pieces of evidence — pooling
    without dedup would inflate a hypothesis's apparent support purely by
    panel size.
    """
    seen: set[tuple[str, str]] = set()
    pooled: list[BlindEvidenceItem] = []
    for item in items:
        for ev in item.evidence:
            key = (ev.claim.strip().lower(), ev.source)
            if key in seen:
                continue
            seen.add(key)
            pooled.append(ev)
    return pooled


class DivergenceDecisionPayload(BaseModel):
    divergence: str
    decision: Literal["accept", "reject"]
    rationale: str


logger = logging.getLogger(__name__)

_ADJUDICATION_KEY_RE = re.compile(r"[^a-z0-9]+")


def _adjudication_key(text: str) -> str:
    return _ADJUDICATION_KEY_RE.sub("", text.lower())


def resolve_adjudication_decisions(
    divergences: Sequence[Divergence],
    decisions: Sequence[DivergenceDecisionPayload],
) -> dict[str, DivergenceDecisionPayload]:
    """Map each divergence id to the decision the model made about it.

    A divergence id is a generated slug — `panel-only:` plus a
    punctuation-stripped hypothesis name, e.g. a 62-character unbroken run
    for "Premature ovarian insufficiency / menopause (autoimmune oophoritis
    subtype)". Requiring the model to echo that CHARACTER-FOR-CHARACTER cost
    a real scheduled review: it adjudicated the divergence, wrote a
    substantive rationale, and the run still failed because the id it
    returned did not match byte-for-byte.

    The contract's intent is "every divergence was adjudicated", not "the
    model can transcribe a slug". So an exact match wins; failing that, the
    id and the divergence's human-readable NAME are compared with case and
    punctuation removed. Ambiguity is never resolved by guessing — a key
    that would match more than one divergence is dropped, so the contract
    still fails rather than silently attaching a rationale to the wrong
    hypothesis.
    """
    resolved: dict[str, DivergenceDecisionPayload] = {}
    unclaimed = list(decisions)

    for decision in list(unclaimed):
        for div in divergences:
            if decision.divergence == div.id:
                resolved.setdefault(div.id, decision)
                unclaimed.remove(decision)
                break

    # Build the loose index only over divergences still needing a decision,
    # and only where the key is unambiguous across the whole set.
    counts: dict[str, int] = {}
    for div in divergences:
        for key in {_adjudication_key(div.id), _adjudication_key(div.name)}:
            counts[key] = counts.get(key, 0) + 1
    loose: dict[str, Divergence] = {}
    for div in divergences:
        if div.id in resolved:
            continue
        for key in {_adjudication_key(div.id), _adjudication_key(div.name)}:
            if key and counts.get(key) == 1:
                loose[key] = div

    for decision in unclaimed:
        matched = loose.get(_adjudication_key(decision.divergence))
        if matched is not None and matched.id not in resolved:
            div = matched
            logger.info(
                "adjudication: matched decision %r to divergence %r on a normalized key",
                decision.divergence,
                div.id,
            )
            resolved[div.id] = decision
    return resolved


class AdjudicationPayload(BaseModel):
    """What the `challenger` LLM call itself returns for divergence adjudication."""

    decisions: list[DivergenceDecisionPayload] = Field(default_factory=list)


class AdjudicationResult(BaseModel):
    decisions: list[DivergenceDecisionPayload] = Field(default_factory=list)
    model_id: str = ""
    prompt_template_version: str = ""


class HypothesisChallengeNote(BaseModel):
    id: str
    note: str
    plain_language: str = ""
    """One or two sentences saying what this condition IS, for a reader who
    has never heard the name.

    Sourced from the sweep because the sweep is the one stage that visits
    EVERY active hypothesis on every review — so it backfills the glosses of
    hypotheses created before the field existed, with no separate command and
    no extra model call. Only written when the hypothesis does not already
    have one; a gloss does not need rewriting every week."""


class ChallengeSweepPayload(BaseModel):
    """What the `challenger` LLM call itself returns for the full sweep."""

    notes: list[HypothesisChallengeNote] = Field(default_factory=list)


class ChallengeSweepResult(BaseModel):
    notes: list[HypothesisChallengeNote] = Field(default_factory=list)
    model_id: str = ""
    prompt_template_version: str = ""


class TestChooserItem(BaseModel):
    """One next-appointment item, in named short fields rather than one
    unbounded paragraph.

    The previous shape was a single free-text `text`, and the renderer emitted
    `- {text}` per item. A real review produced 22 items averaging a dense
    paragraph each — a page the patient described as unreadable and which, as
    she pointed out, no doctor could work through in an appointment either.
    The structure to fix that already half-existed (`hypothesis_ids` was
    always here); what was missing was anywhere to put a SHORT name, so the
    model put everything in the only field it had.
    """

    panel: str = ""
    """The test, referral or question itself, named in a few words — "Celiac
    screen: tTG-IgA + total IgA". This is what gets bulleted."""
    ask: str = ""
    """One sentence: what to actually ask for."""
    why: str = ""
    """Short rationale, folded away in the UI."""
    audience: Literal["doctor", "you"] = "doctor"
    """Who can answer this.

    `you` is the important one and it comes from a real observation: the list
    was telling the patient to ask her doctor what her own pelvic ultrasound
    showed, what supplements she takes, and whether she has bloating. Those
    are not appointment items — the system either already holds the document
    or can simply ask her. Delegating them to a fifteen-minute appointment
    wastes the appointment AND wastes the intake conversation this system
    exists to have.
    """
    hypothesis_ids: list[str] = Field(default_factory=list)

    text: str = ""
    """Legacy free-text field, kept only so an in-flight review that predates
    the restructure still parses. Folded into `ask` by the validator below."""

    @model_validator(mode="after")
    def _fill_from_legacy_text(self) -> TestChooserItem:
        """Degrade rather than fail when only the old shape arrives.

        ADR 0028's rule: no single field of one item may fail a payload. An
        item with neither `panel` nor `text` keeps an empty panel and is
        dropped by the renderer, which is visible; raising here would lose the
        other twenty.
        """
        if not self.ask and self.text:
            self.ask = self.text.strip()
        if not self.panel and self.ask:
            head = self.ask.split(".")[0].strip()
            self.panel = head[:80] if head else ""
        return self

    @field_validator("audience", mode="before")
    @classmethod
    def _tolerate_unknown_audience(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"doctor", "you"}:
                return normalized
            if normalized in {"patient", "self", "andrea", "user"}:
                return "you"
            logger.warning(
                "review: test chooser used an unknown audience %r; recording as 'doctor'", value
            )
        return "doctor"


class TestChooserPayload(BaseModel):
    items: list[TestChooserItem] = Field(default_factory=list)


class TestChooserResult(BaseModel):
    items: list[TestChooserItem] = Field(default_factory=list)
    questions_open_markdown: str = ""


class CriteriaScanResult(BaseModel):
    """Every registered classification-criteria scorer, run over the current
    labs and phenotype record.

    Deterministic and offline — no model call — so it sits with `trend_scan`
    at the front of the review rather than among the LLM stages.
    """

    results: list[CriteriaResult] = Field(default_factory=list)

    def applicable(self) -> list[CriteriaResult]:
        """Sets whose entry criterion is not explicitly failed.

        A set ruled out by its own entry criterion (this patient's ANA is
        negative, so the 2019 SLE criteria do not apply) is still computed and
        still reported — silently dropping it would hide the fact that it was
        checked — but it is not what a reader should look at first.
        """
        return [r for r in self.results if r.entry_met is not False]

    icap: IcapReport = Field(default_factory=IcapReport)
    """ANA-pattern mapping. Empty for a seronegative patient, which is the
    ordinary outcome and not a failure — see `knowledge.icap`."""


class StaleArtifact(BaseModel):
    hypothesis_id: str
    hypothesis_name: str
    dag_node: str
    model_id: str
    prompt_template_version: str
    generations_behind: int


class StalenessReport(BaseModel):
    stale: list[StaleArtifact] = Field(default_factory=list)


class DeferredVerificationFinding(BaseModel):
    """One deferred claim the sweep judged `not_entailed` — surfaced in the
    review report rather than silently dropped or (impossible, by schema
    design — see `sweep_deferred_entailment_claims`'s docstring)
    automatically stripped from an already-applied ledger."""

    hypothesis_id: str
    claim: str
    source: str
    rationale: str


class DeferredVerificationSweepResult(BaseModel):
    """Result of sweeping `reason.verify`'s deferred-entailment queue
    (PLAN.md latency "diagnostic-turn-latency": claims on `expanded`/
    `cant-miss` hypotheses are deferred out of a diagnostic turn's
    synchronous path; this is the batch venue that picks them up)."""

    checked: int = 0
    entailed: int = 0
    insufficient_source: int = 0
    not_entailed: list[DeferredVerificationFinding] = Field(default_factory=list)


class RoleCost(BaseModel):
    role: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_estimate: float


class OpsMetrics(BaseModel):
    role_costs: list[RoleCost] = Field(default_factory=list)
    total_cost_estimate: float = 0.0
    ledger_churn_tier_moves: int = 0
    hypothesis_ages_days: dict[str, int] = Field(default_factory=dict)
    challenger_kill_rate: float | None = None
    blind_panel_divergence_rate: float = 0.0
    stale_artifact_count: int = 0


class ReviewReport(BaseModel):
    """The weekly review's final, committed artifact — `run_weekly_review`'s
    return value."""

    review_date: date
    markdown_path: str
    commit_sha: str
    tag: str
    ledger_version_before: int
    ledger_version_after: int
    trend_findings: list[TrendFinding] = Field(default_factory=list)
    divergences: DivergenceSet
    adjudication: AdjudicationResult
    staleness: StalenessReport
    deferred_verification: DeferredVerificationSweepResult = Field(
        default_factory=DeferredVerificationSweepResult
    )
    metrics: OpsMetrics
    # docs/adr/0019-event-triggered-review.md: what made `run_review_tick`
    # decide to run THIS full review — a `reason.review_trigger.
    # ReviewMarker.summary()` rollup, or a floor/force-driven sentence when
    # there was no marker at all. "" for a review produced by calling
    # `run_weekly_review` directly (tests, scripts) rather than through
    # `run_review_tick` — `render_review_markdown` renders no section for
    # an empty string rather than a misleading placeholder.
    trigger_summary: str = ""


# --------------------------------------------------------------------------
# Deterministic helpers (no LLM)
# --------------------------------------------------------------------------


def deterministic_trend_scan(db: LabsDb) -> TrendScanResult:
    """Deterministic trend scan (PLAN.md loop (c) step (a)): every
    currently-flagged result plus each analyte's latest reading, checked
    against `labs.validate.trend_outlier`. No LLM call.

    One bulk fetch of every analyte's series up front
    (`LabsDb.series_by_key()`), instead of `trend_outlier` querying
    `labs.sqlite` once per candidate row: this scan runs weekly over EVERY
    current analyte (~450 in the deployed corpus), and `labs.sqlite` lives
    on EFS/NFS in production, where each query costs milliseconds of round
    trip — see `web.routes.labs`' `labs_index` fix for the same pattern.
    """
    candidates = {row.id: row for row in db.latest_panel()}
    for row in abnormal_summary(db):
        candidates[row.id] = row

    series_by_key = db.series_by_key()
    findings: list[TrendFinding] = []
    for row in candidates.values():
        canonical = canonicalize(row.name) or row.name
        series = series_by_key.get((canonical, row.specimen), [])
        issue = trend_outlier(db, row, series=series)
        if issue is not None:
            findings.append(
                TrendFinding(
                    analyte=row.name, date=row.date, value=row.value, message=issue.message
                )
            )
    findings.sort(key=lambda f: (f.analyte, f.date))
    return TrendScanResult(findings=findings)


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def compute_divergences(ledger: Ledger, panels: list[BlindDifferential]) -> DivergenceSet:
    """Deterministic diff of the pooled blind-panel output against the
    current ledger, matched by normalized name (PLAN.md loop (c) step
    (c): "match by normalized name/mondo" — panel items carry no mondo
    code, so normalized name is the only match key available here). No
    LLM call.
    """
    active = [h for h in ledger.hypotheses if h.status in ACTIVE_STATUSES]
    active_by_norm = {_normalize_name(h.name): h for h in active}

    panel_by_norm: dict[str, list[tuple[int, BlindDifferentialItem]]] = defaultdict(list)
    for panel in panels:
        for item in panel.items:
            panel_by_norm[_normalize_name(item.name)].append((panel.panel_index, item))

    divergences: list[Divergence] = []
    covered_norms: set[str] = set()
    panel_citations: dict[str, list[BlindEvidenceItem]] = {}

    for norm in sorted(panel_by_norm):
        entries = panel_by_norm[norm]
        hyp = active_by_norm.get(norm)
        if hyp is None:
            cant_miss = any(item.cant_miss for _, item in entries)
            why = "; ".join(f"panel[{idx}]: {item.why}" for idx, item in entries)
            divergences.append(
                Divergence(
                    id=f"panel-only:{norm}",
                    kind="panel_only",
                    name=entries[0][1].name,
                    panel_probability_bucket=entries[0][1].probability_bucket,
                    panel_cant_miss=cant_miss,
                    support_count=len(entries),
                    rationale_hint=why,
                    panel_evidence=_pool_evidence(item for _, item in entries),
                )
            )
        else:
            covered_norms.add(norm)
            # Every citation from every member that named this hypothesis —
            # including members who AGREED with the ledger's probability, whose
            # refs the mismatch pooling below would otherwise discard.
            pooled = _pool_evidence(item for _, item in entries)
            if pooled:
                panel_citations[hyp.id] = pooled
            mismatched = [item for _, item in entries if item.probability_bucket != hyp.probability]
            if mismatched:
                why = "; ".join(f"panel: {item.why}" for item in mismatched)
                divergences.append(
                    Divergence(
                        id=f"probability:{hyp.id}",
                        kind="probability_mismatch",
                        name=hyp.name,
                        ledger_hypothesis_id=hyp.id,
                        panel_probability_bucket=mismatched[0].probability_bucket,
                        ledger_probability_bucket=hyp.probability,
                        support_count=len(mismatched),
                        rationale_hint=why,
                        panel_evidence=_pool_evidence(mismatched),
                    )
                )

    for norm, hyp in sorted(active_by_norm.items()):
        if norm in covered_norms or norm in panel_by_norm:
            continue
        divergences.append(
            Divergence(
                id=f"ledger-only:{hyp.id}",
                kind="ledger_only",
                name=hyp.name,
                ledger_hypothesis_id=hyp.id,
                ledger_probability_bucket=hyp.probability,
                rationale_hint="No blind panel member proposed this hypothesis independently.",
            )
        )

    return DivergenceSet(divergences=divergences, panel_citations=panel_citations)


def _slug_for_new_hypothesis(name: str, ledger: Ledger) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "hypothesis"
    existing = {h.id for h in ledger.hypotheses}
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def build_review_ledger_diff(
    current_ledger: Ledger,
    divergence_set: DivergenceSet,
    adjudication: AdjudicationResult,
    challenge_sweep: ChallengeSweepResult,
    *,
    today: date,
    db: LabsDb | None = None,
    repo: DataRepo | None = None,
) -> LedgerDiff:
    """Merge accepted-divergence ops (`origin: challenger`) and the full
    challenge sweep's `RecordChallenge` ops into ONE `LedgerDiff` — see
    the module docstring for why this must be a single diff, not two.
    """
    divergence_by_id = {d.id: d for d in divergence_set.divergences}
    ops: list[LedgerOp] = []
    accepted_summaries: list[str] = []

    for decision in adjudication.decisions:
        divergence = divergence_by_id.get(decision.divergence)
        if divergence is None or decision.decision != "accept":
            continue
        if divergence.kind == "panel_only":
            new_id = _slug_for_new_hypothesis(divergence.name, current_ledger)
            tier: Tier = "cant-miss" if divergence.panel_cant_miss else "expanded"
            ops.append(
                AddHypothesis(
                    hypothesis=Hypothesis(
                        id=new_id,
                        name=divergence.name,
                        tier=tier,
                        probability=divergence.panel_probability_bucket or "low",
                        status="active",
                        origin="challenger",
                        first_proposed=today,
                        evidence_for=(
                            _resolvable_evidence(divergence, db, repo)
                            if db is not None and repo is not None
                            else []
                        ),
                        challenger_notes=(
                            "Added from weekly blind-panel divergence adjudication: "
                            f"{decision.rationale}"
                        ),
                    )
                )
            )
        elif divergence.kind == "probability_mismatch" and divergence.ledger_hypothesis_id:
            ops.append(
                UpdateHypothesis(
                    id=divergence.ledger_hypothesis_id,
                    probability=divergence.panel_probability_bucket,
                )
            )
        elif divergence.kind == "ledger_only" and divergence.ledger_hypothesis_id:
            ops.append(
                RecordChallenge(
                    id=divergence.ledger_hypothesis_id,
                    note=(
                        "Weekly blind panel did not independently surface this hypothesis: "
                        f"{decision.rationale}"
                    ),
                )
            )
        accepted_summaries.append(f"{divergence.name} ({divergence.kind}): {decision.rationale}")

    # Panel citations for hypotheses the ledger ALREADY holds. Not gated on
    # the adjudicator's decision: a resolvable ref is a fact about the data,
    # not a verdict on a probability, and it passes the citation checker by
    # construction. Without this, a hypothesis could only ever be cited on the
    # review that first created it — 24 of 25 in prod had an empty evidence
    # list, which is exactly what the patient's hypothesis cards render.
    if db is not None and repo is not None:
        hypothesis_by_id = {h.id: h for h in current_ledger.hypotheses}
        for hyp_id, items in sorted(divergence_set.panel_citations.items()):
            hypothesis = hypothesis_by_id.get(hyp_id)
            if hypothesis is None:
                continue
            # Dedup against what the hypothesis already carries: `apply_diff`
            # appends AddEvidence blindly, so a weekly review would otherwise
            # re-add the same citation every single week.
            seen = {
                (existing.claim.strip().lower(), existing.source)
                for existing in hypothesis.evidence_for
            }
            for evidence in _resolvable_items(items, hypothesis.name, db, repo):
                key = (evidence.claim.strip().lower(), evidence.source)
                if key in seen:
                    continue
                seen.add(key)
                ops.append(AddEvidence(id=hyp_id, for_or_against="for", evidence=evidence))

    # Backfill the plain-language gloss for anything that still lacks one.
    # A hypothesis name is not communication: "Primary ovarian insufficiency /
    # menopausal-range hypogonadism" is precise and tells the person whose case
    # file it is nothing at all.
    needs_gloss = {h.id for h in current_ledger.hypotheses if not h.plain_language.strip()}
    for note in challenge_sweep.notes:
        if note.id in needs_gloss and note.plain_language.strip():
            ops.append(UpdateHypothesis(id=note.id, plain_language=note.plain_language.strip()))

    active_ids = {h.id for h in current_ledger.hypotheses if h.status in ACTIVE_STATUSES}
    already_challenged = {op.id for op in ops if isinstance(op, RecordChallenge)}
    for note in challenge_sweep.notes:
        if note.id in active_ids and note.id not in already_challenged and note.note.strip():
            ops.append(RecordChallenge(id=note.id, note=note.note.strip()))

    rationale = "Weekly review: blind-panel divergence adjudication + full challenge sweep."
    if accepted_summaries:
        rationale += "\n" + "\n".join(accepted_summaries)

    provenance = Provenance(
        app_version=__version__,
        prompt_template_version=challenge_sweep.prompt_template_version or "unknown@v0",
        model_id=challenge_sweep.model_id or "unknown",
        dag_node="apply_review_diff",
        timestamp=datetime.now(UTC),
    )
    return LedgerDiff(provenance=provenance, rationale=rationale, ops=ops)


def _ops_hypothesis_ids(ops: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for op in ops:
        opname = op.get("op")
        if opname == "add_hypothesis":
            hyp = op.get("hypothesis") or {}
            hid = hyp.get("id")
            if hid:
                ids.add(hid)
        elif opname in ("update_hypothesis", "add_evidence", "record_challenge"):
            hid = op.get("id")
            if hid:
                ids.add(hid)
    return ids


def _load_history_records(history_path: Path) -> list[dict[str, Any]]:
    if not history_path.exists():
        return []
    records: list[dict[str, Any]] = []
    with history_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def scan_staleness(history_path: Path, ledger: Ledger, *, horizon: int = 2) -> StalenessReport:
    """Staleness scanner (PLAN.md "Provenance & re-evaluation policy"): for
    every active hypothesis, find the most recent `ledger-history.jsonl`
    diff that touched it and compare that diff's `(model_id,
    prompt_template_version)` against the most recent one seen for that
    same DAG node anywhere in history.

    "Generations behind" is counted as the number of *other* distinct
    `(model_id, prompt_template_version)` pairs seen for that DAG node
    strictly after the artifact's own, in the order they first appear in
    `ledger-history.jsonl` (an append-only log) — the closest deterministic
    proxy available from committed data for PLAN.md's "bindings old"
    without a separate `models.yaml`-change history. Flags a hypothesis
    when that count is `>= horizon` (default 2, PLAN.md's threshold).
    """
    records = _load_history_records(history_path)

    generation_index: dict[str, dict[tuple[str, str], int]] = defaultdict(dict)
    for record in records:
        provenance = record["diff"]["provenance"]
        node = provenance["dag_node"]
        key = (provenance["model_id"], provenance["prompt_template_version"])
        node_generations = generation_index[node]
        if key not in node_generations:
            node_generations[key] = len(node_generations)

    latest_touch: dict[str, dict[str, Any]] = {}
    for record in records:
        for hid in _ops_hypothesis_ids(record["diff"]["ops"]):
            latest_touch[hid] = record

    stale: list[StaleArtifact] = []
    for h in ledger.hypotheses:
        if h.status not in ACTIVE_STATUSES:
            continue
        touch_record = latest_touch.get(h.id)
        if touch_record is None:
            continue
        provenance = touch_record["diff"]["provenance"]
        node = provenance["dag_node"]
        node_generations = generation_index.get(node, {})
        key = (provenance["model_id"], provenance["prompt_template_version"])
        if key not in node_generations or not node_generations:
            continue
        artifact_gen = node_generations[key]
        current_gen = max(node_generations.values())
        behind = current_gen - artifact_gen
        if behind >= horizon:
            stale.append(
                StaleArtifact(
                    hypothesis_id=h.id,
                    hypothesis_name=h.name,
                    dag_node=node,
                    model_id=provenance["model_id"],
                    prompt_template_version=provenance["prompt_template_version"],
                    generations_behind=behind,
                )
            )
    stale.sort(key=lambda s: (-s.generations_behind, s.hypothesis_id))
    return StalenessReport(stale=stale)


def sweep_deferred_entailment_claims(
    client: LlmClient,
    repo: DataRepo,
    db: LabsDb,
    *,
    resolver: SourceTextResolver | None = None,
    cache: EntailmentCache | None = None,
) -> DeferredVerificationSweepResult:
    """The weekly-review venue that picks up claims a diagnostic turn
    DEFERRED (PLAN.md latency "diagnostic-turn-latency": only claims
    supporting a `most-likely` hypothesis are verified synchronously in a
    diagnostic turn; claims on `expanded`/`cant-miss` hypotheses are queued
    via `reason.verify.queue_deferred_claims` instead). This is what makes
    that deferral safe rather than a silent drop: every weekly review pops
    the ENTIRE queue (`reason.verify.pop_deferred_claims` — empty again
    once this returns) and judges every claim through the exact same
    `verify_claims` a diagnostic turn uses, so nothing deferred waits more
    than one review cycle to actually be checked.

    Deliberately does NOT mutate the ledger: this codebase's schema has no
    evidence-removal op by design (`casefile.schema`'s module docstring:
    "history is never deleted, only status changes"), and by the time a
    deferred claim is swept, its evidence may already be sitting in an
    applied, committed ledger diff. A `not_entailed` finding is surfaced in
    the review report instead (`render_review_markdown`'s "Deferred
    evidence checks" section) — the same shape `scan_staleness` already
    uses to make drift visible without silently rewriting history — so a
    human sees it and can act (e.g. a follow-up `record_challenge`), rather
    than it vanishing into a log file nobody reads.

    Returns an empty `DeferredVerificationSweepResult` (no model call at
    all) when the queue is empty, which is the common case."""
    deferred = pop_deferred_claims(repo)
    if not deferred:
        return DeferredVerificationSweepResult()

    claims = [
        Claim(
            hypothesis_id=d.hypothesis_id,
            for_or_against=d.for_or_against,
            claim=d.claim,
            source=d.source,
        )
        for d in deferred
    ]
    report = verify_claims(client, claims, db=db, repo=repo, resolver=resolver, cache=cache)
    log_verification_report(repo, report, dag_node="weekly_review_deferred_sweep")

    return DeferredVerificationSweepResult(
        checked=len(report.checks),
        entailed=len([c for c in report.checks if c.judgment == "entailed"]),
        insufficient_source=len(report.insufficient_source),
        not_entailed=[
            DeferredVerificationFinding(
                hypothesis_id=c.hypothesis_id,
                claim=c.claim,
                source=c.source,
                rationale=c.rationale,
            )
            for c in report.not_entailed
        ],
    )


def parse_audit_costs(audit_log_path: Path) -> list[RoleCost]:
    """Aggregate `reason.client.LlmClient`'s audit JSONL by role (PLAN.md
    "Ops metrics logged continuously"). No LLM call — pure parsing.
    """
    if not audit_log_path.exists():
        return []
    totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0.0, "input_tokens": 0.0, "output_tokens": 0.0, "cost_estimate": 0.0}
    )
    with audit_log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            role = record.get("role", "unknown")
            bucket = totals[role]
            bucket["calls"] += 1
            bucket["input_tokens"] += record.get("input_tokens") or 0
            bucket["output_tokens"] += record.get("output_tokens") or 0
            bucket["cost_estimate"] += record.get("cost_estimate") or 0.0
    return [
        RoleCost(
            role=role,
            calls=int(v["calls"]),
            input_tokens=int(v["input_tokens"]),
            output_tokens=int(v["output_tokens"]),
            cost_estimate=v["cost_estimate"],
        )
        for role, v in sorted(totals.items())
    ]


def ledger_churn(history_path: Path, *, last_n: int = 10) -> int:
    """Count of `update_hypothesis` ops that moved a `tier`, across the
    last `last_n` history entries (PLAN.md "Ops metrics": "ledger churn").
    """
    records = _load_history_records(history_path)
    moves = 0
    for record in records[-last_n:]:
        for op in record["diff"]["ops"]:
            if op.get("op") == "update_hypothesis" and op.get("tier") is not None:
                moves += 1
    return moves


def hypothesis_ages_days(ledger: Ledger, *, today: date) -> dict[str, int]:
    return {
        h.id: (today - h.first_proposed).days
        for h in ledger.hypotheses
        if h.status in ACTIVE_STATUSES
    }


def challenger_kill_rate(ledger: Ledger) -> float | None:
    """Fraction of ever-challenged hypotheses that ended up `ruled-out`
    (PLAN.md "Ops metrics": "challenger kill-rate"). `None` if nothing has
    ever been challenged (avoids a misleading 0.0)."""
    ever_challenged = [
        h for h in ledger.hypotheses if h.last_challenged is not None or h.challenger_notes.strip()
    ]
    if not ever_challenged:
        return None
    ruled_out = sum(1 for h in ever_challenged if h.status == "ruled-out")
    return ruled_out / len(ever_challenged)


def blind_panel_divergence_rate(
    divergence_set: DivergenceSet, panels: list[BlindDifferential]
) -> float:
    total_items = sum(len(p.items) for p in panels)
    if total_items == 0:
        return 0.0
    return len(divergence_set.divergences) / total_items


def compute_ops_metrics(
    *,
    audit_log_path: Path,
    history_path: Path,
    ledger: Ledger,
    divergence_set: DivergenceSet,
    panels: list[BlindDifferential],
    staleness: StalenessReport,
    today: date,
) -> OpsMetrics:
    role_costs = parse_audit_costs(audit_log_path)
    return OpsMetrics(
        role_costs=role_costs,
        total_cost_estimate=sum(r.cost_estimate for r in role_costs),
        ledger_churn_tier_moves=ledger_churn(history_path),
        hypothesis_ages_days=hypothesis_ages_days(ledger, today=today),
        challenger_kill_rate=challenger_kill_rate(ledger),
        blind_panel_divergence_rate=blind_panel_divergence_rate(divergence_set, panels),
        stale_artifact_count=len(staleness.stale),
    )


def _render_criteria(scan: CriteriaScanResult) -> list[str]:
    """Itemised: every criterion, its weight, and what the record says.

    Shown item by item rather than as a score, because a score is the least
    useful part. What a doctor needs is WHICH items are carrying the total and
    which are merely unanswered — and, above all, which are `possible` and
    waiting on an attribution judgement only they can make.
    """
    out: list[str] = []
    for result in scan.results:
        out.append(f"### {result.name}")
        out.append("")
        out.append(f"_{result.disclaimer}_")
        out.append("")
        if result.entry_met is False:
            # Reported, not hidden: knowing a set was checked and ruled out is
            # itself an answer.
            out.append(f"**Does not apply.** {result.entry_note}")
            out.append("")
        elif result.entry_note:
            out.append(result.entry_note)
            out.append("")

        out.append(
            f"**{result.points} of {result.threshold} points**"
            + (
                f", plus {result.points_possible} more if the possible items below are"
                " attributed to this condition by a clinician"
                if result.points_possible
                else ""
            )
            + (
                f". {result.points_not_assessed} points sit in items nothing on file can answer."
                if result.points_not_assessed
                else "."
            )
        )
        out.append("")

        shown = [i for i in result.items if i.state in {"met", "possible", "not_met"}]
        if shown:
            out.append("| | Criterion | Points | Basis |")
            out.append("|---|---|---|---|")
            marks = {"met": "✓", "possible": "?", "not_met": "·"}
            for item in shown:
                basis = redact_gated_text(item.basis).replace("|", "\\|")
                out.append(
                    f"| {marks[item.state]} | {item.domain} — {item.name} "
                    f"| {item.weight} | {basis} |"
                )
            out.append("")
        unanswered = [i for i in result.items if i.state == "not_assessed"]
        if unanswered:
            out.append(
                f"_{len(unanswered)} further criteria could not be assessed from the "
                "record: "
                + ", ".join(i.name for i in unanswered[:8])
                + ("…_" if len(unanswered) > 8 else "._")
            )
            out.append("")
    return out


LITERATURE_RELPATH = "case/literature.yaml"

# How many hypotheses get a literature refresh per review (PLAN.md: "literature
# refresh on top-3 hypotheses"). Bounded on purpose: each one is two NCBI calls
# plus a verification per citation, and a review that queried every hypothesis
# would spend minutes on leads nobody is acting on.
LITERATURE_TOP_N = 3

LITERATURE_PER_HYPOTHESIS = 3

# Mirrors `web.casefile_helpers.sort_hypotheses`. Duplicated rather than
# imported because `reason` importing `web` would invert this codebase's
# dependency direction and cycle. `test_review_literature` pins the two to the
# same ordering, so drift fails a test instead of silently ranking the report
# and the UI differently.
_LIT_TIER_RANK = {"most-likely": 0, "cant-miss": 1, "expanded": 2}
_LIT_PROBABILITY_RANK = {"high": 0, "moderate": 1, "low": 2, "minimal": 3}


class HypothesisLiterature(BaseModel):
    """Citations found for one hypothesis. Every article comes from PubMed."""

    hypothesis_id: str
    hypothesis_name: str
    query: str = ""
    total: int = 0
    articles: list[PubMedArticle] = Field(default_factory=list)
    error: str = ""


class LiteratureRefreshResult(BaseModel):
    """The review's literature pass.

    Empty is a valid, non-exceptional outcome: NCBI may be unreachable, or a
    hypothesis may genuinely have no indexed review literature. A review must
    complete either way, so nothing here is allowed to raise.
    """

    entries: list[HypothesisLiterature] = Field(default_factory=list)

    @property
    def citation_count(self) -> int:
        return sum(len(e.articles) for e in self.entries)


def top_hypotheses_for_literature(
    ledger: Ledger, *, limit: int = LITERATURE_TOP_N
) -> list[Hypothesis]:
    """The hypotheses worth spending literature calls on.

    Active only, tier first, probability within tier, then name so the order is
    stable between reviews — a refresh that reshuffled every run would make the
    report's literature section look like it changed when it had not.
    """
    active = [h for h in ledger.hypotheses if h.status == "active"]
    return sorted(
        active,
        key=lambda h: (
            _LIT_TIER_RANK.get(h.tier, 99),
            _LIT_PROBABILITY_RANK.get(h.probability, 99),
            h.name.lower(),
        ),
    )[:limit]


def refresh_literature(
    client: PubMedClient,
    ledger: Ledger,
    *,
    limit: int = LITERATURE_TOP_N,
    per_hypothesis: int = LITERATURE_PER_HYPOTHESIS,
) -> LiteratureRefreshResult:
    """Search PubMed for each of the top hypotheses.

    Deterministic in what it asks for: the query is built by code from the
    hypothesis name, never written by a model. Never raises — `PubMedClient`
    reports failure in `error` rather than throwing, and a hypothesis whose
    search failed simply carries no citations.
    """
    entries: list[HypothesisLiterature] = []
    for hypothesis in top_hypotheses_for_literature(ledger, limit=limit):
        result = client.search_topic(hypothesis.name, retmax=per_hypothesis)
        entries.append(
            HypothesisLiterature(
                hypothesis_id=hypothesis.id,
                hypothesis_name=hypothesis.name,
                query=result.query,
                total=result.total,
                articles=result.articles,
                error=result.error,
            )
        )
    return LiteratureRefreshResult(entries=entries)


def render_literature(result: LiteratureRefreshResult) -> list[str]:
    """The report's literature section.

    Every line carries its PMID, because that is the acceptance criterion and
    because a citation a reader cannot look up is not a citation. `total` is
    shown alongside what was fetched so "3 shown" is never mistaken for "3
    exist"."""
    lines: list[str] = []
    for entry in result.entries:
        lines.append(f"**{entry.hypothesis_name}**")
        if entry.error:
            lines.append(f"- _No literature this week: {entry.error}._")
        elif not entry.articles:
            lines.append("- _No indexed review literature found for this term._")
        else:
            for article in entry.articles:
                lines.append(f"- {article.short_citation()}  `{article.citation_ref}`")
            if entry.total > len(entry.articles):
                lines.append(f"  _{len(entry.articles)} of {entry.total} matching papers shown._")
        lines.append("")
    return lines


def render_review_markdown(
    *,
    review_date: date,
    trend_findings: list[TrendFinding],
    divergence_set: DivergenceSet,
    adjudication: AdjudicationResult,
    challenge_sweep: ChallengeSweepResult,
    test_chooser: TestChooserResult,
    staleness: StalenessReport,
    deferred_verification: DeferredVerificationSweepResult,
    metrics: OpsMetrics,
    ledger_before: Ledger,
    ledger_after: Ledger,
    retirements: RetirementReport | None = None,
    criteria: CriteriaScanResult | None = None,
    lirical: LiricalComparison | None = None,
    semsim: LiricalComparison | None = None,
    engine_adjudication: EngineAdjudicationResult | None = None,
    engine_notes: list[str] | None = None,
    literature: LiteratureRefreshResult | None = None,
    trigger_summary: str = "",
) -> str:
    """Render the review report: plain-language "what changed"/"what to
    ask your doctor" up top for a non-technical reader, a metrics
    appendix at the bottom for anyone who wants the detail.

    `trigger_summary` (docs/adr/0019-event-triggered-review.md), when
    given, renders as a short "Why this review ran" line right under the
    title — the marker reasons (or floor/force sentence) that made
    `run_review_tick` decide to run this particular full review, so the
    report can answer "what prompted this" without the reader needing to
    correlate timestamps against `work/review-wanted.json` themselves. Left
    out entirely when `trigger_summary` is `""` (a review produced by
    calling this function/`run_weekly_review` directly, e.g. in a test or
    script, with no tick-level trigger to report).

    Every model-written free-text field interpolated below — divergence
    names, adjudication rationales, test-chooser items — is passed through
    `reason.tools.redact_gated_text` first (CLAUDE.md rule 5): none of
    this text flows through the Composer's gated path, so nothing else
    screens it before it lands in a committed, patient-facing markdown
    file. `web/routes/reviews.py`'s `reviews_detail` re-gates at render
    time too, so a review written before this fix is still covered.
    """
    decisions_by_id = {d.divergence: d for d in adjudication.decisions}
    accepted = [
        (divergence, decisions_by_id[divergence.id])
        for divergence in divergence_set.divergences
        if decisions_by_id.get(divergence.id) is not None
        and decisions_by_id[divergence.id].decision == "accept"
    ]

    lines: list[str] = [f"# Weekly Review — {review_date.isoformat()}", ""]

    if trigger_summary:
        lines.append(f"_Why this review ran: {redact_gated_text(trigger_summary)}_")
        lines.append("")

    lines.append("## What changed this week")
    lines.append("")
    if not accepted:
        lines.append(
            "A second, independent read of your case this week did not turn up anything new "
            "that changed the leads being tracked."
        )
    else:
        for divergence, decision in accepted:
            name = redact_gated_text(divergence.name)
            rationale = redact_gated_text(decision.rationale)
            if divergence.kind == "panel_only":
                lines.append(
                    f"- A new possibility was added to your case for further discussion: "
                    f"**{name}**. {rationale}"
                )
            elif divergence.kind == "probability_mismatch":
                lines.append(f"- How likely **{name}** seems was updated. {rationale}")
            else:
                lines.append(f"- **{name}** was flagged for extra scrutiny. {rationale}")
    lines.append("")

    if trend_findings:
        lines.append("## Trend alerts")
        lines.append("")
        for finding in trend_findings:
            lines.append(
                f"- **{finding.analyte}** on {finding.date.isoformat()}: {finding.message}"
            )
        lines.append("")

    if deferred_verification.checked:
        lines.append("## Deferred evidence checks")
        lines.append("")
        lines.append(
            f"{deferred_verification.checked} evidence claim(s) held on `expanded`/`cant-miss` "
            "leads (not needed for this week's headline picture, so checking them was put off "
            "until this review) were checked against their sources this week."
        )
        if deferred_verification.not_entailed:
            lines.append("")
            lines.append("Some did not hold up and are flagged for a closer look:")
            for deferred_finding in deferred_verification.not_entailed:
                deferred_claim = redact_gated_text(deferred_finding.claim)
                deferred_rationale = redact_gated_text(deferred_finding.rationale)
                lines.append(
                    f"- ({deferred_finding.hypothesis_id}) {deferred_claim!r}: {deferred_rationale}"
                )
        lines.append("")

    if retirements is not None and (retirements.retirements or retirements.protected_count):
        lines.append("## No longer on the list")
        lines.append("")
        lines += render_retirements(retirements)

    lines.append("## What to ask your doctor")
    lines.append("")
    if test_chooser.items:
        # The SAME renderer the case-file page uses. This section previously
        # emitted `- {item.text}` and, once the item schema moved to named
        # parts, produced one empty bullet per item — a row of bare dashes
        # above the metrics appendix. `redact_gated_text` still runs over
        # every piece of model-authored text, so the deterministic output gate
        # is unchanged.
        names = {h.id: h.name for h in ledger_after.hypotheses}
        mine = [i for i in test_chooser.items if i.audience == "you"]
        theirs = [i for i in test_chooser.items if i.audience == "doctor"]
        if mine:
            lines.append("**You can answer these yourself:**")
            lines.append("")
            lines += render_test_chooser_items(mine, names, transform=redact_gated_text)
        if theirs:
            if mine:
                lines.append("**For your doctor:**")
                lines.append("")
            lines += render_test_chooser_items(theirs, names, transform=redact_gated_text)
    else:
        lines.append("_Nothing new to bring to your next appointment this week._")
    lines.append("")

    if criteria is not None and criteria.results:
        lines.append("## Classification criteria")
        lines.append("")
        lines += _render_criteria(criteria)
        lines.append("")
        icap_lines = render_icap(criteria.icap)
        if icap_lines:
            lines.append("### What the ANA pattern points at")
            lines.append("")
            lines += icap_lines

    if lirical is not None and (lirical.findings or lirical.error):
        lines.append("## A second opinion from the phenotype engine")
        lines.append("")
        lines += render_comparison(lirical)

    if semsim is not None and semsim.findings:
        lines.append("## A third opinion: phenotype similarity")
        lines.append("")
        lines += render_semsim_comparison(semsim)

    # Immediately after both engines, because it is the verdict ON them.
    # Rendering it further down would leave the reader with two rankings and
    # no statement of what they were taken to mean.
    if engine_adjudication is not None:
        adjudicated = render_engine_adjudication(engine_adjudication, engine_notes or [])
        if adjudicated:
            lines += adjudicated

    if literature is not None and literature.entries:
        lines.append("## What the literature says")
        lines.append("")
        lines.append(
            "Recent review articles for the leads above. Every one is a real, "
            "looked-up paper — the reference in backticks is its PubMed id."
        )
        lines.append("")
        lines += render_literature(literature)

    lines.append("## Metrics appendix")
    lines.append("")
    lines.append(f"- Leads re-reviewed this week: {len(challenge_sweep.notes)}")
    lines.append(f"- Ledger version: {ledger_before.version} -> {ledger_after.version}")
    active_count = len(hypothesis_ages_days(ledger_after, today=review_date))
    lines.append(f"- Active hypotheses: {active_count}")
    lines.append(f"- Ledger churn (tier moves, last 10 diffs): {metrics.ledger_churn_tier_moves}")
    kill_rate = (
        f"{metrics.challenger_kill_rate:.0%}" if metrics.challenger_kill_rate is not None else "n/a"
    )
    lines.append(f"- Challenger kill-rate (challenged -> ruled-out): {kill_rate}")
    lines.append(f"- Blind-panel divergence rate: {metrics.blind_panel_divergence_rate:.0%}")
    lines.append(
        f"- Stale artifacts (>= 2 generations behind, still active): {metrics.stale_artifact_count}"
    )
    if staleness.stale:
        for artifact in staleness.stale:
            lines.append(
                f"  - {artifact.hypothesis_name} ({artifact.hypothesis_id}): "
                f"{artifact.generations_behind} generation(s) behind on {artifact.dag_node}"
            )
    lines.append(f"- Estimated cost this review: ${metrics.total_cost_estimate:.2f}")
    for role_cost in metrics.role_costs:
        lines.append(
            f"  - {role_cost.role}: {role_cost.calls} call(s), "
            f"{role_cost.input_tokens}+{role_cost.output_tokens} tokens, "
            f"${role_cost.cost_estimate:.2f}"
        )
    lines.append(f"- Divergences considered: {len(divergence_set.divergences)}")
    lines.append(
        f"- Deferred evidence claims checked this review: {deferred_verification.checked} "
        f"({len(deferred_verification.not_entailed)} not entailed)"
    )
    lines.append("")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# DAG contracts
# --------------------------------------------------------------------------


# Both completeness postconditions below require SUBSTANCE in each covering
# note/rationale, not just a non-empty string: at least this many characters
# after stripping, AND not identical across every item being covered — a
# model that stamps the same placeholder sentence across every
# hypothesis/divergence must not pass ("." or one repeated sentence is not a
# substantive, per-item review).
MIN_SUBSTANTIVE_LENGTH = 20


def _adjudication_completeness_contract() -> Contract:
    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        divergence_set = ctx.get("divergence_diff")
        if not isinstance(divergence_set, DivergenceSet):
            return "divergence_diff missing from context"
        assert isinstance(value, AdjudicationResult)
        expected = {d.id for d in divergence_set.divergences}
        by_id = resolve_adjudication_decisions(divergence_set.divergences, value.decisions)

        missing = expected - set(by_id)
        if missing:
            return f"missing adjudication decision(s) for divergence id(s): {sorted(missing)}"

        insubstantial = sorted(
            div_id
            for div_id in expected
            if len(by_id[div_id].rationale.strip()) < MIN_SUBSTANTIVE_LENGTH
        )
        if insubstantial:
            return (
                f"adjudication rationale too short (< {MIN_SUBSTANTIVE_LENGTH} chars after "
                f"stripping) for divergence id(s): {insubstantial}"
            )

        rationales = [by_id[div_id].rationale.strip() for div_id in expected]
        if len(rationales) > 1 and len(set(rationales)) == 1:
            return (
                "adjudication rationale is identical across every divergence — not a "
                "substantive, per-divergence adjudication"
            )
        return None

    return Contract(name="adjudication_covers_every_divergence", predicate=predicate)


def _engine_adjudication_completeness_contract() -> Contract:
    """Every engine divergence gets its own substantive verdict.

    The same shape as the panel-adjudication contract above and for the same
    reason: a stage that is allowed to skip the awkward divergences, or to
    answer all of them with one sentence, is not adjudicating. The failure
    mode here is specific — a model that finds engine output hard to reason
    about will happily return "neutral, the engine did not rank it" for
    everything, which is a restatement of the input.

    Unlike the panel contract this one tolerates the engines being down: with
    no divergences to judge there is nothing to cover, and `ran=False` after
    a sidecar timeout is an ordinary review, not a contract breach.
    """

    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        if not isinstance(value, EngineAdjudicationResult):
            return "engine adjudication did not produce an EngineAdjudicationResult"
        if not value.ran or not value.divergences:
            return None

        expected = {d.id for d in value.divergences}
        seen = [v.divergence for v in value.verdicts]
        missing = expected - set(seen)
        if missing:
            return f"no verdict for engine divergence id(s): {sorted(missing)}"

        duplicated = {d for d in seen if seen.count(d) > 1}
        if duplicated:
            return f"more than one verdict for engine divergence id(s): {sorted(duplicated)}"

        by_id = {v.divergence: v for v in value.verdicts}
        insubstantial = sorted(
            div_id
            for div_id in expected
            if len(by_id[div_id].rationale.strip()) < MIN_SUBSTANTIVE_LENGTH
        )
        if insubstantial:
            return (
                f"engine adjudication rationale too short (< {MIN_SUBSTANTIVE_LENGTH} chars "
                f"after stripping) for divergence id(s): {insubstantial}"
            )

        rationales = [by_id[div_id].rationale.strip() for div_id in expected]
        if len(rationales) > 1 and len(set(rationales)) == 1:
            return (
                "engine adjudication rationale is identical across every divergence — not a "
                "substantive, per-divergence adjudication"
            )
        return None

    return Contract(name="engine_adjudication_covers_every_divergence", predicate=predicate)


def _challenge_sweep_completeness_contract() -> Contract:
    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        ledger = ctx.get("current_ledger")
        if not isinstance(ledger, Ledger):
            return "current_ledger missing from context"
        assert isinstance(value, ChallengeSweepResult)
        active_ids = {h.id for h in ledger.hypotheses if h.status in ACTIVE_STATUSES}
        by_id = {n.id: n for n in value.notes}

        noted_ids = {n.id for n in value.notes if n.note.strip()}
        missing = active_ids - noted_ids
        if missing:
            return f"missing challenge-sweep note(s) for active hypothesis id(s): {sorted(missing)}"

        insubstantial = sorted(
            hid for hid in active_ids if len(by_id[hid].note.strip()) < MIN_SUBSTANTIVE_LENGTH
        )
        if insubstantial:
            return (
                f"challenge-sweep note too short (< {MIN_SUBSTANTIVE_LENGTH} chars after "
                f"stripping) for active hypothesis id(s): {insubstantial}"
            )

        notes = [by_id[hid].note.strip() for hid in active_ids]
        if len(notes) > 1 and len(set(notes)) == 1:
            return (
                "challenge-sweep note is identical across every active hypothesis — not a "
                "substantive, per-hypothesis review"
            )
        return None

    return Contract(name="challenge_sweep_covers_every_active_hypothesis", predicate=predicate)


def _review_version_incremented_contract() -> Contract:
    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        assert isinstance(value, Ledger)
        prior = ctx.get("current_ledger")
        if not isinstance(prior, Ledger):
            return "current_ledger missing from context"
        if value.version <= prior.version:
            return f"ledger version did not increment: prior={prior.version} new={value.version}"
        return None

    return Contract(name="review_apply_ledger_version_incremented", predicate=predicate)


# --------------------------------------------------------------------------
# Review artifact naming (docs/adr/0019-event-triggered-review.md)
# --------------------------------------------------------------------------

REVIEW_TAG_PREFIX = "review-"


def _review_relpath_and_tag(repo: DataRepo, review_date: date, *, now: datetime) -> tuple[str, str]:
    """The markdown path and git tag for a full review committing today.

    Collision-safe: the event-triggered cooldown (docs/adr/0019) makes more
    than one full review on the SAME calendar day possible (up to 4 a day
    at the 6h cooldown), unlike the old once-a-week cron this replaces. If
    today's plain `case/reviews/{date}-review.md` is already taken (an
    earlier full review already ran today), a zero-padded `HHMMSS`
    (colon-free, so it stays inside `web.routes.reviews`'s filename/tag
    charset) suffix disambiguates both the path and the tag. The common
    case — first (and historically only) full review of the day — is
    byte-identical to the pre-0019 naming, so existing `report.tag ==
    "review-2026-08-23"`-style assertions are unaffected.
    """
    base_relpath = f"case/reviews/{review_date.isoformat()}-review.md"
    base_tag = f"{REVIEW_TAG_PREFIX}{review_date.isoformat()}"
    if not (repo.root / base_relpath).exists():
        return base_relpath, base_tag
    suffix = now.strftime("%H%M%S")
    return (
        f"case/reviews/{review_date.isoformat()}T{suffix}-review.md",
        f"{base_tag}T{suffix}",
    )


# --------------------------------------------------------------------------
# DAG assembly
# --------------------------------------------------------------------------


def _default_lirical_runner(repo: DataRepo) -> LiricalRunner:
    """The production runner, configured from settings.

    Read here rather than threaded through `build_review_dag` for the same
    reason as the PubMed client: only this node needs them, they are all
    optional, and tests inject a runner instead.
    """
    from adoc.config import Settings

    settings = Settings()
    work_dir = repo.root / LIRICAL_WORK_RELDIR
    return EcsLiricalRunner(
        work_dir,
        cluster=settings.lirical_cluster,
        task_definition=settings.lirical_task_definition,
        subnets=settings.lirical_subnet_ids(),
        security_groups=settings.lirical_security_group_ids(),
        container_work_dir=str(work_dir),
    )


def _default_pubmed_client(repo: DataRepo) -> PubMedClient:
    """A PubMed client for this repo, configured from settings.

    Settings are read here rather than threaded through `build_review_dag`
    because the NCBI credentials are optional and only this one node needs
    them. Tests inject a client instead and never construct this.
    """
    from adoc.config import Settings

    settings = Settings()
    return PubMedClient(
        repo.root / PUBMED_CACHE_RELPATH,
        email=settings.eutils_email,
        api_key=settings.eutils_api_key,
    )


def build_review_dag(
    client: LlmClient,
    repo: DataRepo,
    db: LabsDb,
    ledger_path: Path,
    *,
    clock: Callable[[], datetime],
    sink: dict[str, BaseModel] | None = None,
    resolver: SourceTextResolver | None = None,
    entailment_cache: EntailmentCache | None = None,
    pubmed: PubMedClient | None = None,
    lirical: LiricalRunner | None = None,
    trigger_summary: str = "",
) -> Dag:
    """Assemble the weekly-review DAG (PLAN.md loop (c)) — see the module
    docstring for topology and contracts.

    Expected `run()` initial context: `{"initial": Marker(),
    "blind_context_pack": ContextPack}` (built with `include_ledger=False`).
    Deliberately does NOT expect a `"ledger"` key — the current ledger is
    loaded by the `current_ledger` node, *after* every blind panel member
    has already run, so `forbid_context_key("ledger")` has nothing to catch
    in a correct run (see the negative test in `tests/test_review.py` for
    the incorrect case).

    `resolver`/`entailment_cache` are passed through to
    `sweep_deferred_entailment_claims` (the `deferred_entailment_sweep`
    node, PLAN.md latency "diagnostic-turn-latency") exactly like
    `reason.stages.build_diagnostic_dag`'s own parameters of the same name
    — defaulting to a real `DefaultSourceTextResolver`/`EntailmentCache`
    (cached at `<repo>/work/entailment-cache.json`, SHARED with any
    diagnostic turn that ran earlier and cached the same `(claim,
    source_text)` pair) when omitted; tests should always inject fakes.
    """
    results: dict[str, BaseModel] = sink if sink is not None else {}
    # What the engine adjudication considered and deliberately did NOT act on.
    # Kept beside `results` rather than inside the node's own artifact because
    # the deterministic apply is what discovers them, and the report renders
    # them: a verdict that changed nothing is a real outcome and dropping it
    # silently would make the stage look like it did less than it did.
    engine_notes: list[str] = []
    num_panel = len(client._bindings.get("blind_panel", []))  # noqa: SLF001
    if num_panel < 1:
        raise ValueError("role 'blind_panel' must have at least one model binding")
    panel_node_names = [f"blind_panel_{i}" for i in range(num_panel)]
    resolved_resolver = resolver or DefaultSourceTextResolver(db, repo)
    resolved_cache = entailment_cache or EntailmentCache(repo.root / ENTAILMENT_CACHE_RELPATH)

    def _trend_scan_fn(_ctx: Ctx) -> BaseModel:
        result = deterministic_trend_scan(db)
        results["trend_scan"] = result
        return result

    def _criteria_scan_fn(_ctx: Ctx) -> BaseModel:
        """Score the hand-encoded classification criteria (ADR: knowledge
        layer). No model call; pure code over stored labs and the phenotype
        record."""
        phenotype = load_phenotype(repo.root / Path(PHENOTYPE_RELPATH))
        lookup = {
            entry.term_id: (
                entry.label,
                entry.present,
                entry.matched_text[0] if entry.matched_text else "",
            )
            for entry in phenotype.entries
        }
        rows = db.all_non_rejected_rows()
        result = CriteriaScanResult(
            results=score_all(rows, phenotype=lookup),
            # ICAP rides along with the criteria scan rather than taking its
            # own node: both are pure code over the same rows, and a second
            # node would double the DAG bookkeeping for one function call.
            icap=scan_ana_patterns(rows),
        )
        results["criteria_scan"] = result
        return result

    def _make_blind_panel_fn(index: int) -> Callable[[Ctx], BaseModel]:
        def fn(ctx: Ctx) -> BaseModel:
            context_pack = ctx["blind_context_pack"]
            assert isinstance(context_pack, ContextPack)
            prompt = load_prompt("blind_reviewer")
            user_content = context_pack.render()
            result = client.complete(
                "blind_panel",
                system=prompt.text,
                messages=[Message(role="user", content=user_content)],
                schema=BlindDifferentialPayload,
                binding_index=index,
            )
            payload = result.parsed
            assert isinstance(payload, BlindDifferentialPayload)
            diff = BlindDifferential(
                items=payload.items, panel_index=index, model_id=result.model_id
            )
            results[f"blind_panel_{index}"] = diff
            return diff

        return fn

    def _current_ledger_fn(_ctx: Ctx) -> BaseModel:
        ledger = load_ledger(ledger_path)
        results["current_ledger"] = ledger
        return ledger

    def _divergence_diff_fn(ctx: Ctx) -> BaseModel:
        ledger = ctx["current_ledger"]
        assert isinstance(ledger, Ledger)
        panels: list[BlindDifferential] = []
        for name in panel_node_names:
            panel = ctx[name]
            assert isinstance(panel, BlindDifferential)
            panels.append(panel)
        divergence_set = compute_divergences(ledger, panels)
        results["divergence_diff"] = divergence_set
        return divergence_set

    def _adjudication_fn(ctx: Ctx) -> BaseModel:
        divergence_set = ctx["divergence_diff"]
        assert isinstance(divergence_set, DivergenceSet)
        context_pack = build_context(repo, db, include_ledger=True)
        prompt = load_prompt("divergence_adjudicator")
        divergences_json = divergence_set.model_dump_json(indent=2)
        user_content = (
            f"{context_pack.render()}\n\n## Divergences To Adjudicate\n\n"
            f"```json\n{divergences_json}\n```\n"
        )
        result = client.complete(
            "challenger",
            system=prompt.text,
            messages=[Message(role="user", content=user_content)],
            schema=AdjudicationPayload,
        )
        payload = result.parsed
        assert isinstance(payload, AdjudicationPayload)
        adjudication = AdjudicationResult(
            decisions=payload.decisions,
            model_id=result.model_id,
            prompt_template_version=f"{prompt.name}@v{prompt.version}",
        )
        results["adjudication"] = adjudication
        return adjudication

    def _challenge_sweep_fn(ctx: Ctx) -> BaseModel:
        ledger = ctx["current_ledger"]
        assert isinstance(ledger, Ledger)
        context_pack = build_context(repo, db, include_ledger=True)
        prompt = load_prompt("challenge_sweep")
        active_ids = [h.id for h in ledger.hypotheses if h.status in ACTIVE_STATUSES]
        user_content = (
            f"{context_pack.render()}\n\n## Active Hypothesis IDs Requiring A Challenge Note\n\n"
            f"{', '.join(active_ids) or '_none_'}\n"
        )
        result = client.complete(
            "challenger",
            system=prompt.text,
            messages=[Message(role="user", content=user_content)],
            schema=ChallengeSweepPayload,
        )
        payload = result.parsed
        assert isinstance(payload, ChallengeSweepPayload)
        sweep = ChallengeSweepResult(
            notes=payload.notes,
            model_id=result.model_id,
            prompt_template_version=f"{prompt.name}@v{prompt.version}",
        )
        results["challenge_sweep"] = sweep
        return sweep

    def _apply_review_diff_fn(ctx: Ctx) -> BaseModel:
        current_ledger = ctx["current_ledger"]
        assert isinstance(current_ledger, Ledger)
        divergence_set = ctx["divergence_diff"]
        assert isinstance(divergence_set, DivergenceSet)
        adjudication = ctx["adjudication"]
        assert isinstance(adjudication, AdjudicationResult)
        sweep = ctx["challenge_sweep"]
        assert isinstance(sweep, ChallengeSweepResult)

        diff = build_review_ledger_diff(
            current_ledger,
            divergence_set,
            adjudication,
            sweep,
            today=clock().date(),
            db=db,
            repo=repo,
        )
        history_path = repo.root / HISTORY_RELPATH
        new_ledger = repo.apply_ledger_diff(ledger_path, history_path, diff)
        results["apply_review_diff"] = new_ledger
        return new_ledger

    def _test_chooser_fn(ctx: Ctx) -> BaseModel:
        ledger = ctx["apply_review_diff"]
        assert isinstance(ledger, Ledger)
        context_pack = build_context(repo, db, include_ledger=True)
        prompt = load_prompt("test_chooser")
        result = client.complete(
            "test_chooser",
            system=prompt.text,
            messages=[Message(role="user", content=context_pack.render())],
            schema=TestChooserPayload,
        )
        payload = result.parsed
        assert isinstance(payload, TestChooserPayload)

        # Fold into the durable store BEFORE rendering, so a question she has
        # already answered is not asked again. The chooser reasons from the
        # ledger and has no memory of what she told us between reviews; the
        # store does. `questions-open.md` is the rendering, not the record
        # (ADR 0033).
        questions_path = repo.root / QUESTIONS_RELPATH
        asked_on = clock().date()
        store = merge_proposed(
            load_questions(questions_path),
            [
                OpenQuestion(
                    id=question_id(item.panel),
                    panel=item.panel,
                    ask=item.ask,
                    why=item.why,
                    audience=item.audience,
                    hypothesis_ids=item.hypothesis_ids,
                    first_asked_on=asked_on,
                    last_asked_on=asked_on,
                )
                for item in payload.items
                if item.panel.strip()
            ],
            asked_on=asked_on,
        )
        save_questions(questions_path, store)

        still_open = {q.id for q in store.open_questions()}
        answered_count = len(payload.items) - len(
            [i for i in payload.items if question_id(i.panel) in still_open]
        )
        if answered_count:
            logger.info(
                "test_chooser: %d proposed item(s) already answered; not re-asking",
                answered_count,
            )
        open_payload = TestChooserPayload(
            items=[i for i in payload.items if question_id(i.panel) in still_open]
        )
        markdown = _render_questions_open(open_payload, ledger)
        repo.write("case/questions-open.md", markdown)
        tc_result = TestChooserResult(items=payload.items, questions_open_markdown=markdown)
        results["test_chooser"] = tc_result
        return tc_result

    def _retirement_pass_fn(ctx: Ctx) -> BaseModel:
        """Retire hypotheses that have stopped earning their place (ADR 0035).

        Deterministic, never a model call. Runs AFTER `apply_review_diff` so
        it judges the ledger this review produced, and applies its own diff so
        the ledger invariants still get to check it — retirement is a status
        change like any other and does not get a private back door.

        `cant-miss` and patient-origin hypotheses are excluded absolutely; see
        `casefile.retirement`.
        """
        ledger = ctx["apply_review_diff"]
        assert isinstance(ledger, Ledger)
        report = propose_retirements(ledger, today=clock().date())
        if not report.retirements:
            logger.info("retirement: nothing retired (%d protected)", report.protected_count)
            results["retirement_pass"] = report
            return report

        diff = retirements_to_diff(
            report,
            provenance=Provenance(
                app_version=__version__,
                prompt_template_version="n/a-deterministic",
                model_id="none",
                dag_node="retirement_pass",
                timestamp=clock(),
            ),
        )
        assert diff is not None
        try:
            repo.apply_ledger_diff(ledger_path, repo.root / HISTORY_RELPATH, diff)
        except Exception as exc:  # noqa: BLE001 - a failed retirement must not fail a review
            logger.warning("retirement: could not apply: %s", exc)
            results["retirement_pass"] = RetirementReport(protected_count=report.protected_count)
            return results["retirement_pass"]

        logger.info("retirement: %d retired, %d protected", report.count, report.protected_count)
        results["retirement_pass"] = report
        return report

    def _record_engine(name: str, comparison: LiricalComparison) -> LiricalComparison:
        """Put an engine's outcome in the results sink, success or not.

        `render_review_markdown` reads the engines from `results`, not from the
        DAG context — so a node that returned early WITHOUT writing here left
        the report with no engine section at all, silently. LIRICAL had been
        returning "lirical task is not configured" on every review since ADR
        0029 and the report never said so: the "did not run this week" line,
        which exists precisely to make absence visible, could not render
        because nothing reached the sink.

        Absence has to be as visible as presence. Every path goes through
        here.
        """
        results[name] = comparison
        return comparison

    def _lirical_divergence_fn(ctx: Ctx) -> BaseModel:
        """Run the phenotype engine and compare it to the ledger.

        Deliberately AFTER `apply_review_diff`, so it compares against the
        ledger this review actually produced rather than the one it started
        with — otherwise every hypothesis the review just added would read as
        `engine_only`.

        The ledger is RELOADED from disk rather than taken from the context,
        because `retirement_pass` runs in between and writes its own diff. The
        context still holds the pre-retirement object, and comparing against
        that would report every just-retired hypothesis as a `ledger_only`
        divergence — the engine disagreeing with a differential that no longer
        exists.

        Never raises. `docs/research/scoring-across-engines.md`: this is a
        divergence report, not a fifth score to average in.
        """

        ledger = load_ledger(ledger_path)
        try:
            profile = load_phenotype(repo.root / Path(PHENOTYPE_RELPATH))
            observed, negated = select_for_engine(profile, today=clock().date())
            if not observed:
                return _record_engine(
                    "lirical_divergence",
                    LiricalComparison(ran=False, error="no current phenotype findings to run on"),
                )

            runner = lirical or _default_lirical_runner(repo)
            run = runner.run(
                LiricalRequest(observed=observed, negated=negated, sample_id="patient")
            )
            if not run.ok or run.result is None:
                logger.info("lirical: not run this review — %s", run.error)
                return _record_engine(
                    "lirical_divergence", LiricalComparison(ran=False, error=run.error)
                )

            comparison = compare_to_ledger(
                run.result,
                ledger,
                terms_used=run.terms_used,
                terms_excluded=run.terms_excluded,
                mondo=load_mondo_index(reference_path("mondo_index_path")),
            )
            logger.info(
                "lirical: %d finding(s), %d divergence(s), in %.1fs",
                len(comparison.findings),
                comparison.divergence_count,
                run.duration_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - the engine must never fail a review
            logger.warning("lirical: comparison failed: %s", exc)
            return _record_engine(
                "lirical_divergence",
                LiricalComparison(ran=False, error=f"{type(exc).__name__}: {exc}"),
            )

        return _record_engine("lirical_divergence", comparison)

    def _semsim_divergence_fn(_ctx: Ctx) -> BaseModel:
        """Rank diseases by phenotype similarity and compare to the ledger.

        A second, independent engine beside LIRICAL. They answer differently —
        one by likelihood ratio against a curated model, the other by shared
        information content — so where both rank a disease highly that is
        corroboration from genuinely different methods, and where they diverge
        that is the thing worth a clinician's attention
        (`docs/research/scoring-across-engines.md`).

        Reads the ledger from disk for the same reason the LIRICAL node does:
        `retirement_pass` runs earlier and writes its own diff, so the DAG
        context holds a pre-retirement object.

        Never raises. A missing index is the ordinary state of a local
        checkout, not an error.
        """
        try:
            index = load_index(reference_path("semsim_index_path"))
            if index is None:
                return _record_engine(
                    "semsim_divergence",
                    LiricalComparison(ran=False, error="no similarity index in this image"),
                )

            profile = load_phenotype(repo.root / Path(PHENOTYPE_RELPATH))
            observed, _ = select_for_engine(profile, today=clock().date())
            if not observed:
                return _record_engine(
                    "semsim_divergence",
                    LiricalComparison(ran=False, error="no current phenotype findings to run on"),
                )

            ranked = index.rank(observed)
            if not ranked.ok:
                return _record_engine(
                    "semsim_divergence", LiricalComparison(ran=False, error=ranked.error)
                )

            comparison = compare_semsim_to_ledger(
                ranked,
                load_ledger(ledger_path),
                mondo=load_mondo_index(reference_path("mondo_index_path")),
            )
            logger.info(
                "semsim: %d finding(s), %d divergence(s) over %d diseases",
                len(comparison.findings),
                comparison.divergence_count,
                index.disease_count,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a review
            logger.warning("semsim: comparison failed: %s", exc)
            return _record_engine(
                "semsim_divergence",
                LiricalComparison(ran=False, error=f"{type(exc).__name__}: {exc}"),
            )

        return _record_engine("semsim_divergence", comparison)

    def _engine_adjudication_fn(ctx: Ctx) -> BaseModel:
        """Decide what the engines' disagreements MEAN, and let that reach the
        ledger (`reason/engine_adjudication.py`).

        Until this node existed both engines ran, were rendered, and changed
        nothing — the review got longer while the differential stayed exactly
        as it was. Direction only: no score from one engine is ever compared
        with or folded into a score from the other.

        Never raises. The engines are supplementary and a review completes
        without them.
        """
        lirical_comparison = ctx["lirical_divergence"]
        semsim_comparison = ctx["semsim_divergence"]
        assert isinstance(lirical_comparison, LiricalComparison)
        assert isinstance(semsim_comparison, LiricalComparison)

        divergences = collect_divergences(lirical_comparison, semsim_comparison)
        if not divergences:
            # Nothing to adjudicate is an ordinary review, not a failure — and
            # it must not cost a model call. Agreement evidence is still
            # written by the apply node below, which needs no adjudication.
            result = EngineAdjudicationResult(ran=True)
            results["engine_adjudication"] = result
            return result

        try:
            context_pack = build_context(repo, db, include_ledger=True)
            prompt = load_prompt("engine_adjudicator")
            divergences_json = EngineAdjudicationResult(divergences=divergences).model_dump_json(
                include={"divergences"}, indent=2
            )
            user_content = (
                f"{context_pack.render()}\n\n## Engine Divergences To Adjudicate\n\n"
                f"```json\n{divergences_json}\n```\n"
            )
            completion = client.complete(
                "challenger",
                system=prompt.text,
                messages=[Message(role="user", content=user_content)],
                schema=EngineAdjudicationPayload,
            )
            payload = completion.parsed
            assert isinstance(payload, EngineAdjudicationPayload)
            result = EngineAdjudicationResult(
                ran=True,
                divergences=divergences,
                verdicts=payload.verdicts,
                model_id=completion.model_id,
                prompt_template_version=f"{prompt.name}@v{prompt.version}",
            )
            logger.info(
                "engine adjudication: %d divergence(s) -> %s",
                len(divergences),
                result.by_direction,
            )
        except Exception as exc:  # noqa: BLE001 - the engines must never fail a review
            logger.warning("engine adjudication failed: %s", exc)
            result = EngineAdjudicationResult(
                ran=False, divergences=divergences, error=f"{type(exc).__name__}: {exc}"
            )

        results["engine_adjudication"] = result
        return result

    def _apply_engine_diff_fn(ctx: Ctx) -> BaseModel:
        """Write the adjudicated verdicts, and engine agreement, to the ledger.

        Deterministic: the direction came from the model, the mapping from
        direction to op is plain code in `reason/engine_adjudication.py`.

        Applies its own diff for the same reason `retirement_pass` does — a
        status or evidence change is a ledger mutation like any other and does
        not get a private back door around the invariants.
        """
        adjudication = ctx["engine_adjudication"]
        assert isinstance(adjudication, EngineAdjudicationResult)
        lirical_comparison = ctx["lirical_divergence"]
        semsim_comparison = ctx["semsim_divergence"]
        assert isinstance(lirical_comparison, LiricalComparison)
        assert isinstance(semsim_comparison, LiricalComparison)

        # Reloaded from disk, not taken from the context: `retirement_pass`
        # wrote its own diff after `apply_review_diff`, so the context object
        # is stale and evidence would land on a superseded ledger.
        ledger = load_ledger(ledger_path)
        # BUILDING the diff is inside the try, not just applying it.
        #
        # `verdicts_to_ops` constructs `Hypothesis` objects from model-supplied
        # names, and a name that slugs to an invalid id raises out of pydantic
        # validation. With construction outside the guard, that exception ended
        # the whole review — from a stage whose entire contract is that the
        # engines are supplementary and never fail one.
        try:
            diff, notes = build_engine_diff(
                adjudication,
                lirical_comparison,
                semsim_comparison,
                ledger,
                today=clock().date(),
                provenance=Provenance(
                    app_version=__version__,
                    prompt_template_version=(
                        adjudication.prompt_template_version or "n/a-deterministic"
                    ),
                    model_id=adjudication.model_id or "none",
                    dag_node="apply_engine_diff",
                    timestamp=clock(),
                ),
            )
            engine_notes.extend(notes)
            if diff is None:
                logger.info("engine adjudication: nothing to apply")
            else:
                ledger = repo.apply_ledger_diff(ledger_path, repo.root / HISTORY_RELPATH, diff)
                logger.info("engine adjudication: applied %d op(s)", len(diff.ops))
        except Exception as exc:  # noqa: BLE001 - the engines must never fail a review
            logger.warning("engine adjudication: could not apply: %s", exc)
            ledger = load_ledger(ledger_path)

        results["apply_engine_diff"] = ledger
        return ledger

    def _literature_refresh_fn(ctx: Ctx) -> BaseModel:
        # The post-engine ledger, not `apply_review_diff`: a hypothesis the
        # engines just contributed is exactly the kind that needs a citation,
        # and reading the earlier object would leave it uncited until the next
        # review.
        ledger = ctx["apply_engine_diff"]
        assert isinstance(ledger, Ledger)
        try:
            searcher = pubmed or _default_pubmed_client(repo)
            result = refresh_literature(searcher, ledger)
        except Exception as exc:  # noqa: BLE001 - literature must never fail a review
            logger.warning("literature refresh failed: %s", exc)
            result = LiteratureRefreshResult()
        logger.info(
            "literature refresh: %d citation(s) across %d hypothes(es)",
            result.citation_count,
            len(result.entries),
        )
        results["literature_refresh"] = result
        return result

    def _staleness_scan_fn(ctx: Ctx) -> BaseModel:
        ledger = ctx["apply_review_diff"]
        assert isinstance(ledger, Ledger)
        report = scan_staleness(repo.root / HISTORY_RELPATH, ledger)
        results["staleness_scan"] = report
        return report

    def _deferred_entailment_sweep_fn(_ctx: Ctx) -> BaseModel:
        result = sweep_deferred_entailment_claims(
            client, repo, db, resolver=resolved_resolver, cache=resolved_cache
        )
        results["deferred_entailment_sweep"] = result
        return result

    def _ops_metrics_fn(ctx: Ctx) -> BaseModel:
        ledger = ctx["apply_review_diff"]
        assert isinstance(ledger, Ledger)
        divergence_set = ctx["divergence_diff"]
        assert isinstance(divergence_set, DivergenceSet)
        staleness = ctx["staleness_scan"]
        assert isinstance(staleness, StalenessReport)
        panels: list[BlindDifferential] = []
        for name in panel_node_names:
            panel = ctx[name]
            assert isinstance(panel, BlindDifferential)
            panels.append(panel)

        metrics = compute_ops_metrics(
            audit_log_path=repo.root / "logs" / "api-audit.jsonl",
            history_path=repo.root / HISTORY_RELPATH,
            ledger=ledger,
            divergence_set=divergence_set,
            panels=panels,
            staleness=staleness,
            today=clock().date(),
        )
        results["ops_metrics"] = metrics
        return metrics

    def _render_report_fn(ctx: Ctx) -> BaseModel:
        review_date = clock().date()
        ledger_before = ctx["current_ledger"]
        assert isinstance(ledger_before, Ledger)
        # The ledger as it FINALLY stands, after retirement and the engine
        # adjudication both wrote their own diffs. Reading `apply_review_diff`
        # here reported a before/after that omitted every change made by the
        # two stages that run last.
        raw_final = results.get("apply_engine_diff")
        ledger_after = raw_final if isinstance(raw_final, Ledger) else ctx["apply_review_diff"]
        assert isinstance(ledger_after, Ledger)
        divergence_set = ctx["divergence_diff"]
        assert isinstance(divergence_set, DivergenceSet)
        adjudication = ctx["adjudication"]
        assert isinstance(adjudication, AdjudicationResult)
        sweep = ctx["challenge_sweep"]
        assert isinstance(sweep, ChallengeSweepResult)
        test_chooser = ctx["test_chooser"]
        assert isinstance(test_chooser, TestChooserResult)
        staleness = ctx["staleness_scan"]
        assert isinstance(staleness, StalenessReport)
        deferred_verification = ctx["deferred_entailment_sweep"]
        assert isinstance(deferred_verification, DeferredVerificationSweepResult)
        metrics = ctx["ops_metrics"]
        assert isinstance(metrics, OpsMetrics)
        trend_scan = ctx["trend_scan"]
        assert isinstance(trend_scan, TrendScanResult)
        criteria = results.get("criteria_scan")

        raw_lirical = results.get("lirical_divergence")
        lirical_result = raw_lirical if isinstance(raw_lirical, LiricalComparison) else None
        raw_semsim = results.get("semsim_divergence")
        semsim_result = raw_semsim if isinstance(raw_semsim, LiricalComparison) else None
        raw_literature = results.get("literature_refresh")
        literature_result = (
            raw_literature if isinstance(raw_literature, LiteratureRefreshResult) else None
        )
        raw_retire = results.get("retirement_pass")
        retirement_result = raw_retire if isinstance(raw_retire, RetirementReport) else None
        raw_engine = results.get("engine_adjudication")
        engine_result = raw_engine if isinstance(raw_engine, EngineAdjudicationResult) else None
        markdown = render_review_markdown(
            review_date=review_date,
            trend_findings=trend_scan.findings,
            divergence_set=divergence_set,
            adjudication=adjudication,
            challenge_sweep=sweep,
            test_chooser=test_chooser,
            staleness=staleness,
            deferred_verification=deferred_verification,
            metrics=metrics,
            ledger_before=ledger_before,
            ledger_after=ledger_after,
            trigger_summary=trigger_summary,
            retirements=retirement_result,
            criteria=criteria if isinstance(criteria, CriteriaScanResult) else None,
            lirical=lirical_result,
            semsim=semsim_result,
            engine_adjudication=engine_result,
            engine_notes=list(engine_notes),
            literature=literature_result,
        )
        relpath, tag_name = _review_relpath_and_tag(repo, review_date, now=clock())
        repo.write(relpath, markdown)

        commit_sha = repo.commit(
            f"review: weekly review {review_date.isoformat()}",
            paths=["case"],
        )
        repo.tag(tag_name, message=f"Weekly review {review_date.isoformat()}")

        report = ReviewReport(
            review_date=review_date,
            markdown_path=relpath,
            commit_sha=commit_sha,
            tag=tag_name,
            ledger_version_before=ledger_before.version,
            ledger_version_after=ledger_after.version,
            trend_findings=trend_scan.findings,
            divergences=divergence_set,
            adjudication=adjudication,
            staleness=staleness,
            deferred_verification=deferred_verification,
            metrics=metrics,
            trigger_summary=trigger_summary,
        )
        results["render_report"] = report
        return report

    nodes: list[Node] = [
        Node(
            name="trend_scan",
            fn=_trend_scan_fn,
            input_model=Marker,
            output_model=TrendScanResult,
            depends_on="initial",
        ),
        Node(
            name="criteria_scan",
            fn=_criteria_scan_fn,
            input_model=Marker,
            output_model=CriteriaScanResult,
            depends_on="initial",
        ),
    ]
    prior_name = "trend_scan"
    for index in range(num_panel):
        node_name = f"blind_panel_{index}"
        nodes.append(
            Node(
                name=node_name,
                fn=_make_blind_panel_fn(index),
                input_model=ContextPack,
                output_model=BlindDifferential,
                depends_on="blind_context_pack",
                preconditions=[
                    forbid_context_key("ledger"),
                    edge_payload_lacks_section("ledger"),
                ],
            )
        )
        prior_name = node_name

    nodes.append(
        Node(
            name="current_ledger",
            fn=_current_ledger_fn,
            input_model=BlindDifferential,
            output_model=Ledger,
            depends_on=prior_name,
        )
    )
    nodes.append(
        Node(
            name="divergence_diff",
            fn=_divergence_diff_fn,
            input_model=Ledger,
            output_model=DivergenceSet,
            depends_on="current_ledger",
        )
    )
    nodes.append(
        Node(
            name="adjudication",
            fn=_adjudication_fn,
            input_model=DivergenceSet,
            output_model=AdjudicationResult,
            depends_on="divergence_diff",
            postconditions=[_adjudication_completeness_contract()],
        )
    )
    nodes.append(
        Node(
            name="challenge_sweep",
            fn=_challenge_sweep_fn,
            input_model=AdjudicationResult,
            output_model=ChallengeSweepResult,
            depends_on="adjudication",
            postconditions=[_challenge_sweep_completeness_contract()],
        )
    )
    nodes.append(
        Node(
            name="apply_review_diff",
            fn=_apply_review_diff_fn,
            input_model=ChallengeSweepResult,
            output_model=Ledger,
            depends_on="challenge_sweep",
            postconditions=[_review_version_incremented_contract()],
        )
    )
    nodes.append(
        Node(
            name="retirement_pass",
            fn=_retirement_pass_fn,
            input_model=Ledger,
            output_model=RetirementReport,
            depends_on="apply_review_diff",
        )
    )
    nodes.append(
        Node(
            name="test_chooser",
            fn=_test_chooser_fn,
            input_model=RetirementReport,
            output_model=TestChooserResult,
            depends_on="retirement_pass",
        )
    )
    nodes.append(
        Node(
            name="lirical_divergence",
            fn=_lirical_divergence_fn,
            input_model=TestChooserResult,
            output_model=LiricalComparison,
            depends_on="test_chooser",
        )
    )
    nodes.append(
        Node(
            name="semsim_divergence",
            fn=_semsim_divergence_fn,
            input_model=LiricalComparison,
            output_model=LiricalComparison,
            depends_on="lirical_divergence",
        )
    )
    nodes.append(
        Node(
            name="engine_adjudication",
            fn=_engine_adjudication_fn,
            input_model=LiricalComparison,
            output_model=EngineAdjudicationResult,
            depends_on="semsim_divergence",
            postconditions=[_engine_adjudication_completeness_contract()],
        )
    )
    nodes.append(
        Node(
            name="apply_engine_diff",
            fn=_apply_engine_diff_fn,
            input_model=EngineAdjudicationResult,
            output_model=Ledger,
            depends_on="engine_adjudication",
        )
    )
    nodes.append(
        Node(
            name="literature_refresh",
            fn=_literature_refresh_fn,
            input_model=Ledger,
            output_model=LiteratureRefreshResult,
            depends_on="apply_engine_diff",
        )
    )
    nodes.append(
        Node(
            name="staleness_scan",
            fn=_staleness_scan_fn,
            input_model=LiteratureRefreshResult,
            output_model=StalenessReport,
            depends_on="literature_refresh",
        )
    )
    nodes.append(
        Node(
            name="deferred_entailment_sweep",
            fn=_deferred_entailment_sweep_fn,
            input_model=StalenessReport,
            output_model=DeferredVerificationSweepResult,
            depends_on="staleness_scan",
        )
    )
    nodes.append(
        Node(
            name="ops_metrics",
            fn=_ops_metrics_fn,
            input_model=DeferredVerificationSweepResult,
            output_model=OpsMetrics,
            depends_on="deferred_entailment_sweep",
        )
    )
    nodes.append(
        Node(
            name="render_report",
            fn=_render_report_fn,
            input_model=OpsMetrics,
            output_model=ReviewReport,
            depends_on="ops_metrics",
        )
    )

    return Dag(nodes)


def render_test_chooser_items(
    items: list[TestChooserItem],
    names: dict[str, str],
    *,
    transform: Callable[[str], str] | None = None,
) -> list[str]:
    """One next-appointment item per bullet, in markdown.

    Shared by the two surfaces that render these items — `questions-open.md`
    and the weekly review report — because they drifted the moment they were
    separate. When `TestChooserItem` moved from one free-text `text` field to
    named parts, only this page was updated; the review report kept emitting
    `- {item.text}` and so rendered 22 EMPTY bullets under "What to ask your
    doctor", a row of bare dashes above the metrics appendix. One renderer
    cannot drift from itself.

    `transform` is applied to every piece of model-authored text. The review
    report passes the deterministic output gate through it; the case file page
    passes nothing, because its content is written straight to the repo and
    gated where it is read.
    """
    apply = transform or (lambda text: text)
    out: list[str] = []
    for item in items:
        panel = item.panel.strip()
        if not panel:
            continue
        out.append(f"- **{apply(panel)}**")
        if item.ask.strip():
            out.append(f"  {apply(item.ask.strip())}")
        # Every hypothesis the item bears on, not just the first: one test
        # routinely serves several, and collapsing that to a single reference
        # hides why the test is worth doing. Each is a link to its ledger
        # card, so "why am I being asked this" is one click away rather than a
        # scroll-and-search.
        related = [
            f"[{names[hid]}](/ledger#{hid})" if hid in names else hid for hid in item.hypothesis_ids
        ]
        if related:
            # One line per hypothesis rather than a comma-joined run: a run of
            # links reads as one undifferentiated blob, which is the problem
            # this page has.
            out.append("  _Relevant to:_")
            out += [f"  · {entry}" for entry in related]
        if item.why.strip():
            out.append(f"  _Why:_ {apply(item.why.strip())}")
        out.append("")
    return out


def _render_questions_open(payload: TestChooserPayload, ledger: Ledger | None = None) -> str:
    """The next-appointment list, rendered deterministically from structure.

    Written by code, not by the model, for the same reason ADR 0024 keeps
    records rather than blocks: given one free-text field the model fills it,
    and 22 paragraphs is what came back. Here the model supplies short named
    parts and this function decides the shape, so length is bounded by design
    rather than by asking nicely.

    Items are split by who can actually answer them. The questions the patient
    can answer herself are listed separately and FIRST, because they are free,
    immediate, and several of them decide whether the doctor items are needed
    at all.
    """
    names = {h.id: h.name for h in ledger.hypotheses} if ledger else {}

    def render(items: list[TestChooserItem]) -> list[str]:
        return render_test_chooser_items(items, names)

    mine = [i for i in payload.items if i.audience == "you"]
    theirs = [i for i in payload.items if i.audience == "doctor"]

    lines = ["# Next Appointment", ""]
    if not payload.items:
        lines.append("_None yet._")
        return "\n".join(lines) + "\n"

    if mine:
        lines += ["## Questions you can answer yourself", ""]
        lines += [
            "_Answering these here is faster than waiting for an appointment, "
            "and some of them decide whether the tests below are needed._",
            "",
        ]
        lines += _prioritised(mine, render)
    if theirs:
        lines += ["## To raise with your doctor", ""]
        lines += _prioritised(theirs, render)
    return "\n".join(lines).rstrip() + "\n"


MAX_PRIORITY_ITEMS = 6
"""How many items in one section are presented as the short actionable list.

A real review produced 14 doctor items. None of them were junk — they were 14
genuinely reasonable requests — but "no human doctor would be able to go
through that list either" is a correct read of an appointment's capacity.
Six is roughly what fits a consultation alongside whatever the doctor came to
discuss.
"""


def _prioritised(
    items: list[TestChooserItem], render: Callable[[list[TestChooserItem]], list[str]]
) -> list[str]:
    """The first `MAX_PRIORITY_ITEMS` prominently, the remainder below.

    Nothing is dropped. Truncating a list of clinically reasonable requests
    would silently discard real content and leave the page looking complete —
    the "no silent caps" rule. The overflow keeps its own heading so the
    reader knows the short list above is a prioritisation rather than the
    whole of it, and the model is asked to order by yield so the split lands
    somewhere meaningful.
    """
    if len(items) <= MAX_PRIORITY_ITEMS:
        return render(items)
    head, tail = items[:MAX_PRIORITY_ITEMS], items[MAX_PRIORITY_ITEMS:]
    out = render(head)
    out += [
        f"### Also worth raising, lower priority ({len(tail)})",
        "",
        "_Ranked below the list above. Nothing here is unimportant; it is what "
        "would not fit a single appointment._",
        "",
    ]
    out += render(tail)
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def run_weekly_review(
    repo: DataRepo,
    db: LabsDb,
    client: LlmClient,
    *,
    clock: Callable[[], datetime] | None = None,
    resolver: SourceTextResolver | None = None,
    entailment_cache: EntailmentCache | None = None,
    trigger_summary: str = "",
) -> ReviewReport:
    """Run the FULL deep review end to end (PLAN.md "Session loops (c)")
    and return the committed, tagged `ReviewReport`. See the module
    docstring for the DAG topology and contracts.

    `resolver`/`entailment_cache` are forwarded to `build_review_dag`'s
    `deferred_entailment_sweep` node (PLAN.md latency
    "diagnostic-turn-latency") — tests should inject fakes explicitly.

    `trigger_summary` (docs/adr/0019-event-triggered-review.md) is recorded
    verbatim into the committed report/markdown (`ReviewReport.
    trigger_summary`, `render_review_markdown`'s "Why this review ran"
    line) — this function itself does not gate on anything; `run_review_tick`
    below is the entry point that decides WHETHER to call this at all and
    supplies the reason.
    """
    clock = clock if clock is not None else _utcnow
    ledger_path = repo.root / LEDGER_RELPATH
    blind_context_pack = build_context(repo, db, include_ledger=False)

    sink: dict[str, BaseModel] = {}
    dag = build_review_dag(
        client,
        repo,
        db,
        ledger_path,
        clock=clock,
        sink=sink,
        resolver=resolver,
        entailment_cache=entailment_cache,
        trigger_summary=trigger_summary,
    )
    run(
        dag,
        {
            "initial": Marker(),
            "blind_context_pack": blind_context_pack,
        },
    )

    report = sink["render_report"]
    assert isinstance(report, ReviewReport)
    return report


# --------------------------------------------------------------------------
# Event-triggered entry point (docs/adr/0019-event-triggered-review.md)
# --------------------------------------------------------------------------

# How long a marker-driven full review is suppressed after the last one,
# even with the "review wanted" marker set (`reason.review_trigger`): the
# blind panel is three frontier models over full context, so without a
# ceiling, a burst of ingest ticks/chat turns (e.g. a multi-file Dropbox
# drop trickling in, or an active diagnostic conversation) could each want
# their own full review. 6 hours is short enough that a genuinely active
# day of new evidence still gets same-day re-review (PLAN.md's whole point
# — a stale-ledger period matters, but so does not sitting on fresh
# evidence for a week), while comfortably absorbing the realistic burst
# shapes above (a Dropbox sync completing over minutes, a single diagnostic
# conversation lasting at most a few hours) as ONE full review rather than
# several.
FULL_REVIEW_COOLDOWN = timedelta(hours=6)

# The upper bound on how long a full review can go without running AT ALL,
# marker or no marker — this is what preserves the blind panel's original
# purpose (ADR 0002/0019: it exists to counteract ledger anchoring most
# precisely when nothing new has arrived to prompt a fresh look) and
# `scan_staleness`'s inherently time-based drift check as a worst case, not
# an afterthought: 7 days reproduces the exact guarantee the old
# `cron(0 6 ? * SUN *)` schedule gave (a review AT LEAST weekly), so a
# quiet case file is never worse off under event-triggering than it was
# under the pure weekly cron this replaces.
FULL_REVIEW_FLOOR = timedelta(days=7)


def should_run_full_review(
    *,
    marker: ReviewMarker | None,
    last_full_review_at: datetime | None,
    now: datetime,
    cooldown: timedelta = FULL_REVIEW_COOLDOWN,
    floor: timedelta = FULL_REVIEW_FLOOR,
) -> tuple[bool, str]:
    """Pure decision function (docs/adr/0019): whether a full review should
    run THIS tick, and a human-readable reason either way — deliberately
    free of any I/O so it's trivially unit-testable against an injected
    clock/marker/`last_full_review_at`, independent of `DataRepo`/git or a
    real `LlmClient`. `run_review_tick` is the only real caller.
    """
    if last_full_review_at is None:
        return True, "no full review has ever run"

    since_last = now - last_full_review_at

    if marker is not None and since_last >= cooldown:
        return (
            True,
            f"review-wanted marker set ({marker.summary()}); "
            f"{since_last} since the last full review >= the {cooldown} cooldown",
        )

    if since_last >= floor:
        return (
            True,
            f"{since_last} since the last full review >= the {floor} floor "
            "(no marker needed — this is the worst-case weekly guarantee)",
        )

    if marker is not None:
        return (
            False,
            f"review-wanted marker set ({marker.summary()}), but only {since_last} has passed "
            f"since the last full review — cooldown ({cooldown}) not yet elapsed",
        )
    return (
        False,
        f"no review-wanted marker set, and only {since_last} has passed since the last full "
        f"review — floor ({floor}) not yet elapsed",
    )


class ReviewTickResult(BaseModel):
    """What one `run_review_tick` call did: the cheap parts (always run,
    unless superseded by a full review's own equivalent nodes — see
    `run_review_tick`'s docstring) plus the full review, if one ran."""

    ran_full_review: bool
    decision_reason: str
    trend_scan: TrendScanResult
    deferred_verification: DeferredVerificationSweepResult
    full_review: ReviewReport | None = None


def run_review_tick(
    repo: DataRepo,
    db: LabsDb,
    client: LlmClient,
    *,
    clock: Callable[[], datetime] | None = None,
    resolver: SourceTextResolver | None = None,
    entailment_cache: EntailmentCache | None = None,
    force: bool = False,
    cooldown: timedelta = FULL_REVIEW_COOLDOWN,
    floor: timedelta = FULL_REVIEW_FLOOR,
    last_full_review_lookup: Callable[[], datetime | None] | None = None,
) -> ReviewTickResult:
    """The single entry point both the frequent scheduled tick
    (`deploy/cfn/ecs.yaml`'s `ReviewRule`, `rate(30 minutes)`) and `adoc
    review` call (docs/adr/0019-event-triggered-review.md): decide whether
    a FULL review (blind panel + adjudication + staleness + test chooser +
    committed/tagged artifact) is due, and always do the cheap deterministic
    work regardless.

    Decision: `force=True` (CLI `--force`) always runs a full review,
    bypassing marker/cooldown/floor entirely — this is how a human asks
    for one on demand, and how `adoc eval`/deterministic test/script paths
    get a guaranteed full run. Otherwise `should_run_full_review` decides
    from the on-disk "review wanted" marker (`reason.review_trigger`) and
    `DataRepo.latest_tag_time("review-")` (the committed, durable source of
    truth for when the last full review ran — see that method's docstring
    for why this is preferred over a separate persisted timestamp).

    Cheap-tick work (`deterministic_trend_scan`, `sweep_deferred_
    entailment_claims` — no blind panel, no frontier-model calls) is run
    ONLY when a full review does NOT run this tick: `build_review_dag`
    already includes both as nodes (`trend_scan`, `deferred_entailment_
    sweep`), and `sweep_deferred_entailment_claims` POPS the deferred-claims
    queue (`reason.verify.pop_deferred_claims` empties it) — running it a
    second time standalone before a full review would silently starve the
    full review's own sweep of the very claims it exists to verify. So a
    tick that decides to run a full review delegates entirely to
    `run_weekly_review`, which does its own trend scan and deferred sweep
    as part of the DAG; a tick that doesn't run one does the cheap work
    itself, since nothing else will this tick.

    Marker lifecycle: cleared (`clear_review_marker`) ONLY after
    `run_weekly_review` returns successfully — an exception (a DAG contract
    violation, a transport error, ...) propagates straight out of this
    function with the marker still on disk, so the FAILURE is not silently
    absorbed and the very next tick tries again (task requirement: "marker
    survives a failed run and is cleared after a successful one").

    Concurrency: the full-review path is unchanged from `run_weekly_review`
    — `apply_review_diff` still goes through `DataRepo.apply_ledger_diff`,
    which holds `repo._lock` across load -> apply -> save -> append-history
    -> commit in one critical section, same as every diagnostic chat turn's
    `apply_stage` (`casefile.repo.DataRepo.apply_ledger_diff`'s docstring).
    Nothing about event-triggering changes that path.

    `last_full_review_lookup`, if given, replaces the default `lambda:
    repo.latest_tag_time(REVIEW_TAG_PREFIX)` — a real git tag's commit time
    always reflects actual wall-clock time regardless of what `clock`
    returns, so tests that need a specific "time since the last full
    review" (cooldown/floor boundary tests) inject a fake lookup here
    rather than fighting git commit timestamps; production code never
    passes this.
    """
    clock = clock if clock is not None else _utcnow
    now = clock()
    resolved_lookup = last_full_review_lookup or (lambda: repo.latest_tag_time(REVIEW_TAG_PREFIX))

    if force:
        should_run, decision_reason = True, "forced via `adoc review --force`"
    else:
        marker = load_review_marker(repo)
        last_full_review_at = resolved_lookup()
        should_run, decision_reason = should_run_full_review(
            marker=marker,
            last_full_review_at=last_full_review_at,
            now=now,
            cooldown=cooldown,
            floor=floor,
        )

    if should_run:
        full_review = run_weekly_review(
            repo,
            db,
            client,
            clock=clock,
            resolver=resolver,
            entailment_cache=entailment_cache,
            trigger_summary=decision_reason,
        )
        clear_review_marker(repo)
        return ReviewTickResult(
            ran_full_review=True,
            decision_reason=decision_reason,
            trend_scan=TrendScanResult(findings=full_review.trend_findings),
            deferred_verification=full_review.deferred_verification,
            full_review=full_review,
        )

    trend_scan = deterministic_trend_scan(db)
    deferred = sweep_deferred_entailment_claims(
        client, repo, db, resolver=resolver, cache=entailment_cache
    )
    return ReviewTickResult(
        ran_full_review=False,
        decision_reason=decision_reason,
        trend_scan=trend_scan,
        deferred_verification=deferred,
    )


__all__ = [
    "FULL_REVIEW_COOLDOWN",
    "FULL_REVIEW_FLOOR",
    "REVIEW_TAG_PREFIX",
    "AdjudicationResult",
    "BlindDifferential",
    "ChallengeSweepResult",
    "DeferredVerificationFinding",
    "DeferredVerificationSweepResult",
    "Divergence",
    "DivergenceSet",
    "OpsMetrics",
    "ReviewReport",
    "ReviewTickResult",
    "StalenessReport",
    "TestChooserResult",
    "TrendScanResult",
    "build_review_dag",
    "build_review_ledger_diff",
    "challenger_kill_rate",
    "compute_divergences",
    "compute_ops_metrics",
    "deterministic_trend_scan",
    "hypothesis_ages_days",
    "ledger_churn",
    "parse_audit_costs",
    "render_review_markdown",
    "run_review_tick",
    "run_weekly_review",
    "scan_staleness",
    "should_run_full_review",
    "sweep_deferred_entailment_claims",
]

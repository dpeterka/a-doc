"""Stage functions and DAG assembly for the diagnostic chat turn (PLAN.md
"Session loops (b)"): Ledger-Maintainer -> Challenger -> apply -> Composer.

The deterministic red-flag screen runs in the entry points that own the
patient conversation (`web/routes/chat.py`, `intake/agent.py`), before any
client call, and a match prepends a fixed warning to the reply rather than
replacing the turn (ADR 0014, warn-not-block). It is deliberately NOT run
again here — doing so would re-introduce the block those callers removed.
CLAUDE.md rule 3 ("stage order is enforced by code, not prompts") still
governs everything below.

`dag.run()` only returns an audit `DagRun`, not the node outputs
themselves (dag.py is deliberately a thin, unopinionated runner). The DAG
built here therefore accepts an optional `sink` dict that each node's `fn`
populates by side effect with its own validated output, so a caller that
needs the actual stage results (not just the audit trail) can read them
back after `run()` returns without executing anything twice.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from adoc import __version__
from adoc.casefile.ledger import apply_and_save, load_ledger
from adoc.casefile.repo import HISTORY_RELPATH, DataRepo
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Ledger,
    LedgerDiff,
    LedgerOp,
    Provenance,
    UpdateHypothesis,
)
from adoc.ingest.pipeline import IngestReport
from adoc.labs.db import LabsDb
from adoc.reason.citations import (
    EutilsPmidVerifier,
    PmidVerifier,
    build_retry_feedback,
    check_evidence_citations,
    check_ops_citations,
    log_citation_report,
)
from adoc.reason.client import LlmClient, LlmResult, Message
from adoc.reason.context import ContextPack, build_context
from adoc.reason.dag import Contract, Ctx, Dag, Node, require_prior_node, run
from adoc.reason.prompts import Prompt, load_prompt
from adoc.reason.safety import RedFlagResult, treatment_gate
from adoc.reason.verify import (
    DefaultSourceTextResolver,
    SourceTextResolver,
    VerificationReport,
    build_composer_number_retry_feedback,
    build_entailment_retry_feedback,
    check_composer_numbers,
    claims_from_ops,
    log_stripped_claims,
    log_verification_report,
    strip_not_entailed_ops,
    verify_claims,
)

# --------------------------------------------------------------------------
# Stage-IO models
# --------------------------------------------------------------------------


class PatientTurn(BaseModel):
    """Wraps the raw chat-turn text so it can travel through the DAG's
    `Mapping[str, BaseModel]` context alongside the `ContextPack`."""

    text: str


class CounterArgument(BaseModel):
    hypothesis_id: str
    argument: str


class ChallengerVerdict(BaseModel):
    """The Challenger stage's structured output (ADR-0005: cross-family)."""

    counter_arguments: list[CounterArgument] = Field(default_factory=list)
    additional_ops: list[LedgerOp] = Field(default_factory=list)
    verdict_notes: str = ""


class TurnRoute(BaseModel):
    """The Classifier/router stage's structured output."""

    route: Literal["informational", "diagnostic"]
    rationale: str = ""


class PatientReply(BaseModel):
    """The Composer stage's structured output — rendered for the patient."""

    tiers_rendered: str
    tests_to_request: list[str] = Field(default_factory=list)
    framing_ack: bool
    # PLAN.md Phase 2 "Abstention calibration": a first-class way for the
    # Composer to say "the case file does not yet support an answer here"
    # instead of silently omitting the topic or rendering false confidence.
    # Not a magic string inside `tiers_rendered` — a structured signal an
    # eval probe (or a future UI) can check for directly.
    insufficient_evidence: list[str] = Field(default_factory=list)


class InsufficientEvidenceNote(BaseModel):
    """One explicit abstention note on a Ledger-Maintainer diff (PLAN.md
    Phase 2 "Abstention calibration"): "I looked for evidence on `topic`
    and the case file does not yet support a claim" — the model's way to
    say so as data, not by fabricating a citation to fill the gap or by
    silently dropping the topic."""

    topic: str
    reason: str


class _LedgerDiffPayload(BaseModel):
    """What the ledger-maintainer LLM call itself returns. `Provenance` is
    stamped by code afterwards (`_build_provenance`) — never by the model,
    since the model has no reliable way to know its own `model_id`, the
    running `app_version`, or the exact prompt version it was served."""

    rationale: str
    ops: list[LedgerOp] = Field(default_factory=list)
    insufficient_evidence: list[InsufficientEvidenceNote] = Field(default_factory=list)


def _build_provenance(prompt: Prompt, model_id: str, dag_node: str) -> Provenance:
    """Build a `Provenance` stamp from `adoc.__version__`, the prompt's own
    name/version, the binding's resolved `model_id`, and the DAG node name
    (PLAN.md "Provenance & re-evaluation policy")."""
    return Provenance(
        app_version=__version__,
        prompt_template_version=f"{prompt.name}@v{prompt.version}",
        model_id=model_id,
        dag_node=dag_node,
        timestamp=datetime.now(UTC),
    )


# --------------------------------------------------------------------------
# Stage functions
# --------------------------------------------------------------------------


# How many completions a stage may spend resolving its own evidence source
# refs: the first attempt plus one objection-guided retry (PLAN.md Phase 2
# citation checker, mirroring `composer_stage`'s gate-guided rewrite loop,
# PR #94). Still failing after the retry -> return as-is and let the
# `citation_check` DAG contract fire; the deterministic gate stays the
# final, unbypassable authority (CLAUDE.md rules 2/3 pattern).
_CITATION_RETRY_ATTEMPTS = 2


def _render_diff_rationale(payload: _LedgerDiffPayload) -> str:
    """`payload.rationale`, with any `insufficient_evidence` notes appended
    to the audit trail (PLAN.md Phase 2 "Abstention calibration") — the
    notes are a first-class field on the payload, not folded into free text
    by the model itself; this is the one place code renders them for the
    persisted `LedgerDiff.rationale`."""
    if not payload.insufficient_evidence:
        return payload.rationale
    notes = "; ".join(f"{n.topic}: {n.reason}" for n in payload.insufficient_evidence)
    return f"{payload.rationale}\n\nInsufficient evidence: {notes}"


def ledger_maintainer_stage(
    client: LlmClient,
    ctx: ContextPack,
    patient_message: str,
    db: LabsDb,
    repo: DataRepo,
    *,
    pmid_verifier: PmidVerifier | None = None,
    resolver: SourceTextResolver | None = None,
) -> LedgerDiff:
    """Ledger-Maintainer stage (role `primary_reasoner`, schema `LedgerDiff`
    payload). Proposes a `LedgerDiff` from the context pack plus this turn's
    raw patient message.

    After the model returns a diff, two deterministic-then-model-judged
    checks run in sequence, each with its own same-generation retry
    (mirrors the composer's gate-guided rewrite loop, PR #94):
    1. The citation checker (`reason.citations.check_ops_citations`)
       resolves every evidence source ref; an `unresolved`/`mismatched` ref
       retries with the failed ref(s) fed back.
    2. Only once citations pass, the entailment verifier
       (`reason.verify.verify_claims`, role `entailment_verifier`, a
       DIFFERENT model family) judges whether each claim's cited source
       TEXT actually supports it; a `not_entailed` claim retries with the
       verifier's objection fed back.
    The citation loop is still a pure quality loop, not the enforcement
    point — `citation_check_ledger_maintainer` re-checks whatever this
    function returns and fails the run if it is still bad.

    The entailment loop is different (ADR 0016 revised, 2026-08-25, "strip,
    don't reject"): once the retry budget is spent, a still-`not_entailed`
    claim is no longer fed forward to fail the whole diff. Unless EVERY
    claim in the diff is `not_entailed` (`VerificationReport.
    all_not_entailed` — nothing survives, the one case treated as the
    pipeline having produced garbage rather than an imprecise claim), the
    offending evidence item(s) are stripped from `diff.ops` right here,
    before the diff is returned, and logged
    (`reason.verify.log_stripped_claims`) — the turn proceeds on the
    remaining, verified evidence. `entailment_check_ledger_maintainer`
    still independently re-checks whatever this function returns and
    raises a `ContractViolation` in the all-`not_entailed` case."""
    prompt = load_prompt("ledger_maintainer")
    user_content = f"{ctx.render()}\n\n## Patient Message\n\n{patient_message}\n"
    messages = [Message(role="user", content=user_content)]

    diff: LedgerDiff | None = None
    verification_report: VerificationReport | None = None
    for _attempt in range(_CITATION_RETRY_ATTEMPTS):
        verification_report = None
        result = client.complete(
            "primary_reasoner",
            system=prompt.text,
            messages=messages,
            schema=_LedgerDiffPayload,
        )
        payload = result.parsed
        assert isinstance(payload, _LedgerDiffPayload)

        provenance = _build_provenance(prompt, result.model_id, "ledger_maintainer")
        diff = LedgerDiff(
            provenance=provenance, rationale=_render_diff_rationale(payload), ops=payload.ops
        )

        citation_report = check_ops_citations(diff.ops, db, repo, pmid_verifier=pmid_verifier)
        log_citation_report(repo, citation_report, dag_node="ledger_maintainer")
        if citation_report.failing:
            messages = [
                *messages,
                Message(role="assistant", content=payload.model_dump_json()),
                Message(role="user", content=build_retry_feedback(citation_report)),
            ]
            continue

        verification_report = verify_claims(
            client, claims_from_ops(diff.ops), db=db, repo=repo, resolver=resolver
        )
        log_verification_report(repo, verification_report, dag_node="ledger_maintainer")
        if verification_report.failing:
            messages = [
                *messages,
                Message(role="assistant", content=payload.model_dump_json()),
                Message(role="user", content=build_entailment_retry_feedback(verification_report)),
            ]
            continue

        break

    assert diff is not None
    if (
        verification_report is not None
        and verification_report.failing
        and not verification_report.all_not_entailed
    ):
        stripped_ops, removed = strip_not_entailed_ops(diff.ops, verification_report)
        log_stripped_claims(repo, removed, dag_node="ledger_maintainer")
        diff = diff.model_copy(update={"ops": stripped_ops})
    return diff


def challenger_stage(
    client: LlmClient,
    proposed_diff: LedgerDiff,
    ctx: ContextPack,
    db: LabsDb,
    repo: DataRepo,
    *,
    pmid_verifier: PmidVerifier | None = None,
    resolver: SourceTextResolver | None = None,
) -> ChallengerVerdict:
    """Challenger stage (role `challenger` — cross-family per ADR-0005).
    Attacks `proposed_diff`; must produce >=1 substantive counter-argument
    per most-likely hypothesis (enforced as a DAG postcondition, not here).

    The Challenger's own `additional_ops` can carry evidence (e.g. a
    `record_challenge`-adjacent `add_hypothesis`/`add_evidence`), so it gets
    the same citation-then-entailment retry loop as `ledger_maintainer_
    stage` — see that function's docstring for the shape, but NOT its
    all-`not_entailed`-is-a-hard-failure exception: any still-`not_entailed`
    claim left after the retry is UNCONDITIONALLY stripped from
    `verdict.additional_ops` here (see the inline comment where that
    stripping happens for why this one differs from the Ledger-Maintainer's
    diff). On a clean verdict (no evidence in `additional_ops`, the common
    case) this never spends a second completion, and never calls the
    entailment verifier at all (nothing to verify)."""
    prompt = load_prompt("challenger")
    diff_json = proposed_diff.model_dump_json(indent=2)
    user_content = f"{ctx.render()}\n\n## Proposed Ledger Diff\n\n```json\n{diff_json}\n```\n"
    messages = [Message(role="user", content=user_content)]

    verdict: ChallengerVerdict | None = None
    verification_report: VerificationReport | None = None
    for _attempt in range(_CITATION_RETRY_ATTEMPTS):
        verification_report = None
        result = client.complete(
            "challenger",
            system=prompt.text,
            messages=messages,
            schema=ChallengerVerdict,
        )
        parsed = result.parsed
        assert isinstance(parsed, ChallengerVerdict)
        verdict = parsed

        citation_report = check_ops_citations(
            verdict.additional_ops, db, repo, pmid_verifier=pmid_verifier
        )
        log_citation_report(repo, citation_report, dag_node="challenger")
        if citation_report.failing:
            messages = [
                *messages,
                Message(role="assistant", content=verdict.model_dump_json()),
                Message(role="user", content=build_retry_feedback(citation_report)),
            ]
            continue

        verification_report = verify_claims(
            client, claims_from_ops(verdict.additional_ops), db=db, repo=repo, resolver=resolver
        )
        log_verification_report(repo, verification_report, dag_node="challenger")
        if verification_report.failing:
            messages = [
                *messages,
                Message(role="assistant", content=verdict.model_dump_json()),
                Message(role="user", content=build_entailment_retry_feedback(verification_report)),
            ]
            continue

        break

    assert verdict is not None
    if verification_report is not None and verification_report.failing:
        # Unconditional, unlike `ledger_maintainer_stage`'s strip: the
        # Ledger-Maintainer's diff IS the primary artifact its own
        # `entailment_check_ledger_maintainer` postcondition checks 1:1, so
        # leaving an all-`not_entailed` diff unstripped there is exactly
        # what makes that postcondition catch it. `additional_ops` has no
        # such 1:1 postcondition of its own — the closest equivalent,
        # `entailment_check_apply`, checks it MERGED with the (already
        # resolved) diff, so a not_entailed claim confined to this small
        # `additional_ops` set would be "all failing" from THIS function's
        # narrow vantage point while the merged set is not, and never reach
        # the contract that would otherwise catch it. Always stripping here
        # closes that gap: a Challenger-introduced misrepresentation is
        # simply dropped, never a reason to fail the whole verdict.
        stripped_ops, removed = strip_not_entailed_ops(verdict.additional_ops, verification_report)
        log_stripped_claims(repo, removed, dag_node="challenger")
        verdict = verdict.model_copy(update={"additional_ops": stripped_ops})
    return verdict


def apply_stage(
    repo: DataRepo,
    ledger_path: Path,
    diff: LedgerDiff,
    verdict: ChallengerVerdict,
    provenance: Provenance,
) -> Ledger:
    """Code node: merge the Challenger's `additional_ops` into `diff`, then
    apply via `casefile.ledger.apply_and_save` — the ledger invariants
    (can't-miss non-empty, patient-origin promotion gating, staleness,
    confirmed-by-doctor bar, version bump) enforce the rest."""
    combined_ops = list(diff.ops) + list(verdict.additional_ops)
    rationale = diff.rationale
    if verdict.verdict_notes.strip():
        rationale = f"{rationale}\n\nChallenger: {verdict.verdict_notes.strip()}"

    merged_diff = LedgerDiff(provenance=provenance, rationale=rationale, ops=combined_ops)
    history_path = repo.root / HISTORY_RELPATH
    return apply_and_save(ledger_path, history_path, merged_diff)


def _render_ledger_for_prompt(ledger: Ledger) -> str:
    if not ledger.hypotheses:
        return "_No hypotheses on the ledger yet._"

    lines: list[str] = []
    for h in ledger.hypotheses:
        lines.append(f"### {h.id} — {h.name}")
        lines.append(
            f"tier={h.tier} probability={h.probability} status={h.status} origin={h.origin}"
        )
        for e in h.evidence_for:
            lines.append(f"- FOR: {e.claim} (source: {e.source}, strength: {e.strength})")
        for e in h.evidence_against:
            lines.append(f"- AGAINST: {e.claim} (source: {e.source}, strength: {e.strength})")
        if h.discriminators:
            lines.append(f"- discriminators: {', '.join(h.discriminators)}")
        lines.append("")
    return "\n".join(lines)


# How many completions composer_stage may spend on one reply: the first
# attempt plus one gate-guided rewrite. Found live (baseline-vs-DAG
# experiment): a case file that legitimately records supplement doses
# ("5000 IU") leads the Composer to restate them, tripping the dosage
# detector - `GateResult.rewrite_instruction` was designed for exactly
# this feedback loop but had never been wired in, so every such turn died
# as a ContractViolation instead of being rewritten.
_COMPOSER_GATE_ATTEMPTS = 2


def composer_stage(client: LlmClient, ledger: Ledger, ctx: ContextPack, db: LabsDb) -> PatientReply:
    """Composer/Steward stage (role `primary_reasoner`, schema `PatientReply`).
    Renders the post-challenge ledger for the patient.

    Two deterministic checks are consulted here to give the model ONE
    rewrite pass when its draft trips either (fed back as targeted
    instructions), in sequence:
    1. The output gate (`safety.treatment_gate`) — no dosing/prescriptive
       language (CLAUDE.md rule 5).
    2. Only once the gate passes, the quantitative grounding check
       (`reason.verify.check_composer_numbers`) — every number attributed
       to a lab value must match `labs.sqlite` exactly (PLAN.md Phase 2).
    Both are quality loops, not the enforcement point: `build_diagnostic_dag`
    still applies both as DAG postconditions on whatever this function
    returns, so a reply still failing either after the rewrite surfaces as a
    `ContractViolation` with the run stopped in place (CLAUDE.md rule 5 -
    the deterministic gate remains the final, unbypassable authority)."""
    prompt = load_prompt("composer")
    ledger_text = _render_ledger_for_prompt(ledger)
    user_content = f"{ctx.render()}\n\n## Current Ledger (post-challenge)\n\n{ledger_text}\n"

    messages = [Message(role="user", content=user_content)]
    reply: PatientReply | None = None
    for _attempt in range(_COMPOSER_GATE_ATTEMPTS):
        result = client.complete(
            "primary_reasoner",
            system=prompt.text,
            messages=messages,
            schema=PatientReply,
        )
        parsed = result.parsed
        assert isinstance(parsed, PatientReply)
        reply = parsed
        gate = treatment_gate(reply.tiers_rendered)
        if not gate.passed:
            offending = "; ".join(f"{span.text!r} ({span.reason})" for span in gate.spans)
            messages = [
                *messages,
                Message(role="assistant", content=reply.tiers_rendered),
                Message(
                    role="user",
                    content=(
                        f"{gate.rewrite_instruction} The blocked phrases were: "
                        f"{offending}. Reporting a dose the patient already takes "
                        "counts as blocked dosing language - describe the "
                        "medication or supplement WITHOUT its dose. Return the "
                        "complete corrected reply in the same schema."
                    ),
                ),
            ]
            continue

        number_check = check_composer_numbers(reply.tiers_rendered, db)
        if not number_check.passed:
            messages = [
                *messages,
                Message(role="assistant", content=reply.tiers_rendered),
                Message(role="user", content=build_composer_number_retry_feedback(number_check)),
            ]
            continue

        return reply
    assert reply is not None
    return reply


def route_turn(client: LlmClient, text: str) -> TurnRoute:
    """Router stage (role `classifier`). Callers must have already run the
    red-flag screen on `text` before reaching this — this call itself is an
    API call, so it can never be the first thing an unscreened turn touches."""
    prompt = load_prompt("classifier")
    result = client.complete(
        "classifier",
        system=prompt.text,
        messages=[Message(role="user", content=text)],
        schema=TurnRoute,
    )
    route = result.parsed
    assert isinstance(route, TurnRoute)
    return route


# --------------------------------------------------------------------------
# DAG contracts
# --------------------------------------------------------------------------


def _challenger_min_counterarguments_contract() -> Contract:
    """Postcondition: the Challenger must produce >=1 substantive
    counter-argument for every hypothesis the proposed diff places (or
    updates into) the `most-likely` tier (ADR 0002)."""

    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        diff = ctx.get("ledger_maintainer")
        if not isinstance(diff, LedgerDiff):
            return "ledger_maintainer diff missing from context"
        assert isinstance(value, ChallengerVerdict)

        most_likely_ids: set[str] = set()
        for op in diff.ops:
            if isinstance(op, AddHypothesis) and op.hypothesis.tier == "most-likely":
                most_likely_ids.add(op.hypothesis.id)
            elif isinstance(op, UpdateHypothesis) and op.tier == "most-likely":
                most_likely_ids.add(op.id)

        challenged_ids = {c.hypothesis_id for c in value.counter_arguments if c.argument.strip()}
        missing = most_likely_ids - challenged_ids
        if missing:
            return (
                "missing a substantive counter-argument for most-likely "
                f"hypothesis id(s): {sorted(missing)}"
            )
        return None

    return Contract(name="challenger_min_counterarguments_per_most_likely", predicate=predicate)


def _apply_ledger_version_incremented_contract() -> Contract:
    """Postcondition: applying the merged diff must bump the ledger version
    relative to the ledger state this turn started from (`ctx["ledger"]`)."""

    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        assert isinstance(value, Ledger)
        prior = ctx.get("ledger")
        if not isinstance(prior, Ledger):
            return "prior ledger missing from context"
        if value.version <= prior.version:
            return f"ledger version did not increment: prior={prior.version} new={value.version}"
        return None

    return Contract(name="apply_ledger_version_incremented", predicate=predicate)


def _maintainer_diff_ops(_ctx: Ctx, value: BaseModel | None) -> Sequence[LedgerOp]:
    """Ops extractor for the ledger_maintainer node's `citation_check`
    postcondition: just the diff this node itself produced."""
    assert isinstance(value, LedgerDiff)
    return value.ops


def _merged_apply_ops(ctx: Ctx, value: BaseModel | None) -> Sequence[LedgerOp]:
    """Ops extractor for the apply node's `citation_check` precondition:
    the Ledger-Maintainer's diff ops PLUS the Challenger's `additional_ops`
    — the exact merge `apply_stage` is about to hand to
    `casefile.ledger.apply_and_save` (PLAN.md Phase 2: "cover the
    challenger's additional_ops path too ... so NOTHING reaches apply
    unchecked")."""
    diff = ctx["ledger_maintainer"]
    assert isinstance(diff, LedgerDiff)
    assert isinstance(value, ChallengerVerdict)
    return list(diff.ops) + list(value.additional_ops)


def citation_check_contract(
    name: str,
    db: LabsDb,
    repo: DataRepo,
    ops_extractor: Callable[[Ctx, BaseModel | None], Sequence[LedgerOp]],
    *,
    pmid_verifier: PmidVerifier | None = None,
) -> Contract:
    """A DAG contract (pre- or postcondition, per `ops_extractor`) that runs
    `reason.citations.check_ops_citations` over whatever `ops_extractor`
    pulls out of `(ctx, value)` and fails closed on any `unresolved`/
    `mismatched` evidence source ref (PLAN.md Phase 2 citation checker:
    "Unresolvable or mismatched refs reject the ledger diff ... not just
    warn"). `unverifiable` (PMID checks only, network unavailable) passes.

    Used twice in `build_diagnostic_dag`: as the `ledger_maintainer` node's
    postcondition (checks its own diff) and as the `apply` node's
    precondition (checks the diff merged with the Challenger's
    `additional_ops`) — between the two, nothing reaches
    `casefile.ledger.apply_and_save` with an unresolved/mismatched ref."""

    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        ops = ops_extractor(ctx, value)
        report = check_ops_citations(ops, db, repo, pmid_verifier=pmid_verifier)
        log_citation_report(repo, report, dag_node=name)
        if not report.failing:
            return None
        details = "; ".join(f"{c.source} [{c.outcome}]: {c.reason}" for c in report.failing)
        return f"unresolved/mismatched evidence source ref(s): {details}"

    return Contract(name=name, predicate=predicate)


def entailment_check_contract(
    name: str,
    client: LlmClient,
    db: LabsDb,
    repo: DataRepo,
    ops_extractor: Callable[[Ctx, BaseModel | None], Sequence[LedgerOp]],
    *,
    resolver: SourceTextResolver | None = None,
) -> Contract:
    """A DAG contract (pre- or postcondition, per `ops_extractor`) that runs
    `reason.verify.verify_claims` over whatever `ops_extractor` pulls out of
    `(ctx, value)` — an independent re-check of whatever `ledger_maintainer_
    stage`/`challenger_stage` already did, exactly like
    `citation_check_contract`.

    ADR 0016 revised (2026-08-25, "strip, don't reject"): a `not_entailed`
    claim on its own no longer fails this contract. By the time this
    contract runs, `ledger_maintainer_stage` has already stripped any
    `not_entailed` claim it found from the diff it returned
    (`reason.verify.strip_not_entailed_ops`) — UNLESS every claim in that
    diff was `not_entailed`, in which case it deliberately left the diff
    untouched precisely so THIS contract's postcondition instance
    (`entailment_check_ledger_maintainer`) catches it; `challenger_stage`
    strips unconditionally (see its own docstring for why its
    `additional_ops` doesn't get the same all-`not_entailed` exception). So
    the one condition this contract still fails closed on is
    `VerificationReport.all_not_entailed`: every claim in the ops it
    re-checks judged `not_entailed`, nothing surviving — that is evidence
    the pipeline produced garbage, not an imprecise claim a strip can fix.
    In practice this fires only via the `ledger_maintainer` postcondition
    instance; the `apply` precondition instance mostly re-confirms an
    already-clean merged set and exists as defense-in-depth (independent of
    whatever the stage functions already did, exactly like
    `citation_check_apply`'s role for citations). `insufficient_source` (no
    source TEXT available yet, e.g. a `doc:` ref before the document-text
    corpus lands) is deliberately never treated as failing, same principle
    as the citation checker's `unverifiable`.

    Wired the same way `citation_check_contract` is, immediately after it:
    as the `ledger_maintainer` node's postcondition (checks its own diff)
    and as the `apply` node's precondition (checks the diff merged with the
    Challenger's `additional_ops`)."""

    def predicate(ctx: Ctx, value: BaseModel | None) -> str | None:
        ops = ops_extractor(ctx, value)
        report = verify_claims(client, claims_from_ops(ops), db=db, repo=repo, resolver=resolver)
        log_verification_report(repo, report, dag_node=name)
        if not report.all_not_entailed:
            return None
        details = "; ".join(f"{c.source} [{c.judgment}]: {c.rationale}" for c in report.failing)
        return f"every evidence claim was judged not_entailed (nothing survives): {details}"

    return Contract(name=name, predicate=predicate)


def most_likely_requires_resolved_evidence_contract(db: LabsDb, repo: DataRepo) -> Contract:
    """PRECONDITION on `apply`: a hypothesis whose supporting evidence is
    absent or unresolvable can never sit in tier `most-likely` (PLAN.md
    Phase 2 "Abstention calibration": "a contract that a hypothesis whose
    supporting evidence is absent/unresolvable cannot be placed in
    most-likely").

    Deliberately a PRECONDITION, not a postcondition: `apply_stage` persists
    the ledger to disk (`casefile.ledger.apply_and_save`) as a side effect
    of running, so checking the OUTPUT after the fact (like the citation/
    entailment checks correctly avoid) would let the bad state reach disk
    before the violation is ever raised. Instead this inspects the merged
    ops (Ledger-Maintainer diff + Challenger `additional_ops`) plus the
    PRIOR ledger (for a hypothesis being promoted via `update_hypothesis`
    rather than created fresh), exactly mirroring `citation_check_contract`'s
    `apply`-precondition shape.

    Deliberately checks only citation RESOLUTION here (no LLM call — this
    is the fully deterministic half of abstention calibration): every claim
    on these same merged ops has already passed `entailment_check_apply` as
    an earlier precondition on this same node, so a resolved claim is
    already known-good; what this contract additionally catches is the case
    citation/entailment checks structurally cannot — a hypothesis promoted
    to most-likely with ZERO evidence_for at all."""

    def predicate(ctx: Ctx, _value: BaseModel | None) -> str | None:
        diff = ctx["ledger_maintainer"]
        assert isinstance(diff, LedgerDiff)
        verdict = ctx["challenger"]
        assert isinstance(verdict, ChallengerVerdict)
        prior = ctx.get("ledger")
        prior_by_id = {h.id: h for h in prior.hypotheses} if isinstance(prior, Ledger) else {}

        ops = list(diff.ops) + list(verdict.additional_ops)
        most_likely_ids: set[str] = set()
        evidence_added: dict[str, list[Evidence]] = {}
        for op in ops:
            if isinstance(op, AddHypothesis):
                evidence_added.setdefault(op.hypothesis.id, []).extend(op.hypothesis.evidence_for)
                if op.hypothesis.tier == "most-likely":
                    most_likely_ids.add(op.hypothesis.id)
            elif isinstance(op, UpdateHypothesis):
                if op.tier == "most-likely":
                    most_likely_ids.add(op.id)
            elif isinstance(op, AddEvidence):
                if op.for_or_against == "for":
                    evidence_added.setdefault(op.id, []).append(op.evidence)

        problems: list[str] = []
        for hyp_id in most_likely_ids:
            effective_evidence = list(evidence_added.get(hyp_id, []))
            prior_hyp = prior_by_id.get(hyp_id)
            if prior_hyp is not None:
                effective_evidence = list(prior_hyp.evidence_for) + effective_evidence
            if not effective_evidence:
                problems.append(f"{hyp_id!r}: placed at most-likely with no evidence_for at all")
                continue
            report = check_evidence_citations(effective_evidence, db, repo)
            if not any(c.outcome == "resolved" for c in report.checks):
                problems.append(
                    f"{hyp_id!r}: placed at most-likely but no evidence_for citation resolves"
                )
        if problems:
            return "; ".join(problems)
        return None

    return Contract(name="most_likely_requires_resolved_evidence", predicate=predicate)


def treatment_gate_contract() -> Contract:
    """Postcondition: the Composer's rendered patient-facing text must pass
    `safety.treatment_gate`, or the run stops with a `ContractViolation`
    (CLAUDE.md rule 5 — no treatment/dosing advice path may reach the
    patient, deterministic gate, no exceptions)."""

    def predicate(_ctx: Ctx, value: BaseModel | None) -> str | None:
        assert isinstance(value, PatientReply)
        gate = treatment_gate(value.tiers_rendered)
        if gate.passed:
            return None
        offending = "; ".join(f"{span.text!r} ({span.reason})" for span in gate.spans)
        return f"treatment gate blocked patient-facing output: {offending}"

    return Contract(name="treatment_gate", predicate=predicate)


def composer_number_check_contract(db: LabsDb) -> Contract:
    """Postcondition: every number the Composer's rendered text attributes
    to a lab value must match `labs.sqlite` exactly (PLAN.md Phase 2:
    "every number in patient-facing output that is attributable to a lab
    value must match labs.sqlite exactly"). Deterministic, no LLM call
    (`reason.verify.check_composer_numbers`)."""

    def predicate(_ctx: Ctx, value: BaseModel | None) -> str | None:
        assert isinstance(value, PatientReply)
        check = check_composer_numbers(value.tiers_rendered, db)
        if check.passed:
            return None
        details = "; ".join(
            f"{m.quoted_number!r} near {m.analyte_label!r} (stored: {m.stored_values})"
            for m in check.mismatches
        )
        return f"quoted number(s) do not match stored lab value(s): {details}"

    return Contract(name="composer_number_check", predicate=predicate)


# --------------------------------------------------------------------------
# DAG assembly
# --------------------------------------------------------------------------


def build_diagnostic_dag(
    client: LlmClient,
    repo: DataRepo,
    ledger_path: Path,
    db: LabsDb,
    sink: dict[str, BaseModel] | None = None,
    *,
    pmid_verifier: PmidVerifier | None = None,
    resolver: SourceTextResolver | None = None,
) -> Dag:
    """Assemble the chat-diagnostic-turn DAG (PLAN.md loop (b)):
    Ledger-Maintainer -> Challenger -> apply -> Composer.

    Contracts: the ledger_maintainer node's postconditions run the
    deterministic citation checker (`reason.citations`) then the
    cross-family entailment verifier (`reason.verify`, role
    `entailment_verifier`) over its own diff; Challenger's postcondition
    requires a substantive counter-argument for every most-likely
    hypothesis; the apply node's PRECONDITIONS re-run both checks over the
    Ledger-Maintainer's diff merged with the Challenger's `additional_ops`
    (so a bad ref OR a non-entailed claim introduced by the Challenger
    can't reach `apply` either), plus a THIRD precondition that no
    `most-likely` hypothesis is being promoted with absent/unresolvable
    evidence (PLAN.md Phase 2 "Abstention calibration") — deliberately a
    precondition, not a postcondition, since `apply_stage` persists to disk
    as a side effect, so this must be checked before that write, exactly
    like the citation/entailment checks; its postcondition requires the
    ledger version to have incremented; Composer's precondition requires
    the Challenger node to have completed this run, and its postconditions
    run the treatment/dosing output gate then the deterministic
    quantitative grounding check (`reason.verify.check_composer_numbers`).

    `pmid_verifier` defaults to a real `EutilsPmidVerifier` (NCBI
    E-utilities, cached at `<repo>/work/pmid-cache.json`) when omitted;
    `resolver` defaults to `reason.verify.DefaultSourceTextResolver(db,
    repo)` when omitted — tests should always inject fakes explicitly so a
    test run never touches the network.

    Expected `run()` initial context: `{"context_pack": ContextPack,
    "patient_turn": PatientTurn, "ledger": Ledger}` (the ledger state this
    turn started from, for the apply node's version-increment check).

    `sink`, if given, is populated by side effect with each node's
    validated output (keyed by node name) as the run proceeds — see the
    module docstring for why that is necessary given `dag.run()`'s
    audit-only return value.
    """
    results: dict[str, BaseModel] = sink if sink is not None else {}
    resolved_pmid_verifier = pmid_verifier or EutilsPmidVerifier(
        repo.root / "work" / "pmid-cache.json"
    )
    resolved_resolver = resolver or DefaultSourceTextResolver(db, repo)

    def _ledger_maintainer_fn(ctx: Ctx) -> BaseModel:
        context_pack = ctx["context_pack"]
        assert isinstance(context_pack, ContextPack)
        patient_turn = ctx["patient_turn"]
        assert isinstance(patient_turn, PatientTurn)

        diff = ledger_maintainer_stage(
            client,
            context_pack,
            patient_turn.text,
            db,
            repo,
            pmid_verifier=resolved_pmid_verifier,
            resolver=resolved_resolver,
        )
        results["ledger_maintainer"] = diff
        return diff

    def _challenger_fn(ctx: Ctx) -> BaseModel:
        proposed_diff = ctx["ledger_maintainer"]
        assert isinstance(proposed_diff, LedgerDiff)
        context_pack = ctx["context_pack"]
        assert isinstance(context_pack, ContextPack)

        verdict = challenger_stage(
            client,
            proposed_diff,
            context_pack,
            db,
            repo,
            pmid_verifier=resolved_pmid_verifier,
            resolver=resolved_resolver,
        )
        results["challenger"] = verdict
        return verdict

    def _apply_fn(ctx: Ctx) -> BaseModel:
        diff = ctx["ledger_maintainer"]
        assert isinstance(diff, LedgerDiff)
        verdict = ctx["challenger"]
        assert isinstance(verdict, ChallengerVerdict)

        provenance = Provenance(
            app_version=__version__,
            prompt_template_version=diff.provenance.prompt_template_version,
            model_id=diff.provenance.model_id,
            dag_node="apply",
            timestamp=datetime.now(UTC),
        )
        new_ledger = apply_stage(repo, ledger_path, diff, verdict, provenance)
        results["apply"] = new_ledger
        return new_ledger

    def _composer_fn(ctx: Ctx) -> BaseModel:
        ledger = ctx["apply"]
        assert isinstance(ledger, Ledger)
        context_pack = ctx["context_pack"]
        assert isinstance(context_pack, ContextPack)

        reply = composer_stage(client, ledger, context_pack, db)
        results["composer"] = reply
        return reply

    ledger_maintainer_node = Node(
        name="ledger_maintainer",
        fn=_ledger_maintainer_fn,
        input_model=ContextPack,
        output_model=LedgerDiff,
        depends_on="context_pack",
        postconditions=[
            citation_check_contract(
                "citation_check_ledger_maintainer",
                db,
                repo,
                _maintainer_diff_ops,
                pmid_verifier=resolved_pmid_verifier,
            ),
            entailment_check_contract(
                "entailment_check_ledger_maintainer",
                client,
                db,
                repo,
                _maintainer_diff_ops,
                resolver=resolved_resolver,
            ),
        ],
    )
    challenger_node = Node(
        name="challenger",
        fn=_challenger_fn,
        input_model=LedgerDiff,
        output_model=ChallengerVerdict,
        depends_on="ledger_maintainer",
        postconditions=[_challenger_min_counterarguments_contract()],
    )
    apply_node = Node(
        name="apply",
        fn=_apply_fn,
        input_model=ChallengerVerdict,
        output_model=Ledger,
        depends_on="challenger",
        preconditions=[
            require_prior_node("challenger"),
            citation_check_contract(
                "citation_check_apply",
                db,
                repo,
                _merged_apply_ops,
                pmid_verifier=resolved_pmid_verifier,
            ),
            entailment_check_contract(
                "entailment_check_apply",
                client,
                db,
                repo,
                _merged_apply_ops,
                resolver=resolved_resolver,
            ),
            most_likely_requires_resolved_evidence_contract(db, repo),
        ],
        postconditions=[_apply_ledger_version_incremented_contract()],
    )
    composer_node = Node(
        name="composer",
        fn=_composer_fn,
        input_model=Ledger,
        output_model=PatientReply,
        depends_on="apply",
        preconditions=[require_prior_node("challenger")],
        postconditions=[treatment_gate_contract(), composer_number_check_contract(db)],
    )

    return Dag([ledger_maintainer_node, challenger_node, apply_node, composer_node])


# --------------------------------------------------------------------------
# Entry points — red-flag screen runs before any client call
# --------------------------------------------------------------------------


def run_diagnostic_turn(
    client: LlmClient,
    repo: DataRepo,
    db: LabsDb,
    ledger_path: Path,
    text: str,
) -> PatientReply | RedFlagResult:
    """Entry point for one diagnostic chat turn (PLAN.md loop (b)).

    The red-flag screen is NOT applied here (ADR 0014, warn-not-block): the
    entry points that own the patient conversation — `web/routes/chat.py`
    and `intake/agent.py` — run `safety.red_flag_screen` first, before any
    model call, and prepend a deterministic warning to the reply on a match.
    Screening here as well would re-introduce the block this stage's callers
    deliberately removed. The `RedFlagResult` arm of the return type is kept
    for callers that still pattern-match on it."""

    def _proceed() -> PatientReply:
        context_pack = build_context(repo, db, include_ledger=True, query=text)
        prior_ledger = load_ledger(ledger_path)
        sink: dict[str, BaseModel] = {}
        dag = build_diagnostic_dag(client, repo, ledger_path, db, sink)

        run(
            dag,
            {
                "context_pack": context_pack,
                "patient_turn": PatientTurn(text=text),
                "ledger": prior_ledger,
            },
        )

        reply = sink["composer"]
        assert isinstance(reply, PatientReply)
        return reply

    return _proceed()


def run_informational_turn(
    client: LlmClient, repo: DataRepo, db: LabsDb, text: str
) -> LlmResult | RedFlagResult:
    """Entry point for one informational chat turn (PLAN.md loop (b)).

    Delegates the actual call to `reason.tools.informational_llm_result` —
    the MVP tool loop (`query_labs`/`search_case`/`list_encounters`, PLAN.md
    "Reasoner integration": "Chat tool-use ... runs inside a single
    whitelisted-tools node"). This entry point still owns the red-flag
    screen (so it runs before any client call, exactly as the diagnostic
    path does) and keeps this function's `LlmResult | RedFlagResult`
    return type stable for existing callers; `tools.answer_informational`
    is the string-returning, treatment-gated variant for direct use (e.g.
    from a future chat route).
    """
    # Imported here, not at module level: `reason.tools` is a later slice
    # that this module now delegates to, and importing it lazily avoids
    # committing to an import-order assumption between the two modules.
    from adoc.reason.tools import informational_llm_result

    return informational_llm_result(client, repo, db, text)


def render_new_evidence_note(report: IngestReport) -> str | None:
    """Render a short "new evidence" note from an `IngestReport`, for the
    post-ingest reasoning pass (PLAN.md loop (a), `adoc ingest --reason`).

    Returns `None` if the report added no rows at all (nothing to reason
    about — `run_post_ingest_dag` is skipped by its caller in that case).
    Deliberately does not restate the actual lab values here: the
    up-to-date labs section is already part of the context pack the DAG
    is run with, so this note is just a pointer at what just changed.
    """
    lines: list[str] = []
    for outcome in report.files:
        if outcome.outcome != "ingested":
            continue
        if outcome.rows_auto == 0 and outcome.rows_pending == 0:
            continue
        pending_note = (
            f", {outcome.rows_pending} queued for confirmation" if outcome.rows_pending else ""
        )
        lines.append(
            f"- {Path(outcome.path).name} ({outcome.doc_type or 'document'}): "
            f"{outcome.rows_auto} new lab result(s) auto-accepted{pending_note} — see the "
            "updated Labs section above for current values."
        )
    if not lines:
        return None
    return "New evidence just arrived from document ingestion:\n" + "\n".join(lines)


def run_post_ingest_dag(
    client: LlmClient,
    repo: DataRepo,
    db: LabsDb,
    ledger_path: Path,
    evidence_note: str,
) -> Ledger:
    """Run the diagnostic DAG (`build_diagnostic_dag`) over newly-ingested
    evidence (PLAN.md loop (a): "incremental reasoning: Ledger-Maintainer
    diff -> Challenger ... -> apply"). Callers (`adoc ingest --reason`)
    supply `evidence_note` from `render_new_evidence_note` and are
    responsible for skipping the call entirely when no rows were added.

    No red-flag screen here: this is triggered by document ingestion, not
    by free-text patient chat, so there is no patient utterance to screen.
    Returns the applied `Ledger` (the DAG's `apply` node output); the
    Composer's rendered reply is also produced (the DAG contract requires
    it) but is not the point of this entry point and is discarded.
    """
    context_pack = build_context(repo, db, include_ledger=True)
    prior_ledger = load_ledger(ledger_path)
    sink: dict[str, BaseModel] = {}
    dag = build_diagnostic_dag(client, repo, ledger_path, db, sink)

    run(
        dag,
        {
            "context_pack": context_pack,
            "patient_turn": PatientTurn(text=evidence_note),
            "ledger": prior_ledger,
        },
    )

    new_ledger = sink["apply"]
    assert isinstance(new_ledger, Ledger)
    return new_ledger

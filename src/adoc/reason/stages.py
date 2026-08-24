"""Stage functions and DAG assembly for the diagnostic chat turn (PLAN.md
"Session loops (b)"): Ledger-Maintainer -> Challenger -> apply -> Composer.

Entry points (`run_diagnostic_turn`, `run_informational_turn`) route every
turn through `safety.guarded_turn` first, so the deterministic red-flag
screen runs before any client call, ever — CLAUDE.md rule 3 ("stage order
is enforced by code, not prompts") starts here, at the very first thing a
turn does.

`dag.run()` only returns an audit `DagRun`, not the node outputs
themselves (dag.py is deliberately a thin, unopinionated runner). The DAG
built here therefore accepts an optional `sink` dict that each node's `fn`
populates by side effect with its own validated output, so a caller that
needs the actual stage results (not just the audit trail) can read them
back after `run()` returns without executing anything twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from adoc import __version__
from adoc.casefile.ledger import apply_and_save, load_ledger
from adoc.casefile.repo import HISTORY_RELPATH, DataRepo
from adoc.casefile.schema import (
    AddHypothesis,
    Ledger,
    LedgerDiff,
    LedgerOp,
    Provenance,
    UpdateHypothesis,
)
from adoc.ingest.pipeline import IngestReport
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, LlmResult, Message
from adoc.reason.context import ContextPack, build_context
from adoc.reason.dag import Contract, Ctx, Dag, Node, require_prior_node, run
from adoc.reason.prompts import Prompt, load_prompt
from adoc.reason.safety import RedFlagResult, guarded_turn, treatment_gate

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


class _LedgerDiffPayload(BaseModel):
    """What the ledger-maintainer LLM call itself returns. `Provenance` is
    stamped by code afterwards (`_build_provenance`) — never by the model,
    since the model has no reliable way to know its own `model_id`, the
    running `app_version`, or the exact prompt version it was served."""

    rationale: str
    ops: list[LedgerOp] = Field(default_factory=list)


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


def ledger_maintainer_stage(
    client: LlmClient, ctx: ContextPack, patient_message: str
) -> LedgerDiff:
    """Ledger-Maintainer stage (role `primary_reasoner`, schema `LedgerDiff`
    payload). Proposes a `LedgerDiff` from the context pack plus this turn's
    raw patient message."""
    prompt = load_prompt("ledger_maintainer")
    user_content = f"{ctx.render()}\n\n## Patient Message\n\n{patient_message}\n"

    result = client.complete(
        "primary_reasoner",
        system=prompt.text,
        messages=[Message(role="user", content=user_content)],
        schema=_LedgerDiffPayload,
    )
    payload = result.parsed
    assert isinstance(payload, _LedgerDiffPayload)

    provenance = _build_provenance(prompt, result.model_id, "ledger_maintainer")
    return LedgerDiff(provenance=provenance, rationale=payload.rationale, ops=payload.ops)


def challenger_stage(
    client: LlmClient, proposed_diff: LedgerDiff, ctx: ContextPack
) -> ChallengerVerdict:
    """Challenger stage (role `challenger` — cross-family per ADR-0005).
    Attacks `proposed_diff`; must produce >=1 substantive counter-argument
    per most-likely hypothesis (enforced as a DAG postcondition, not here)."""
    prompt = load_prompt("challenger")
    diff_json = proposed_diff.model_dump_json(indent=2)
    user_content = f"{ctx.render()}\n\n## Proposed Ledger Diff\n\n```json\n{diff_json}\n```\n"

    result = client.complete(
        "challenger",
        system=prompt.text,
        messages=[Message(role="user", content=user_content)],
        schema=ChallengerVerdict,
    )
    verdict = result.parsed
    assert isinstance(verdict, ChallengerVerdict)
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


def composer_stage(client: LlmClient, ledger: Ledger, ctx: ContextPack) -> PatientReply:
    """Composer/Steward stage (role `primary_reasoner`, schema `PatientReply`).
    Renders the post-challenge ledger for the patient.

    The output gate (`safety.treatment_gate`) is consulted here to give the
    model ONE rewrite pass when its draft trips the gate (fed back via
    `GateResult.rewrite_instruction` plus the offending spans). This is a
    quality loop, not the enforcement point: `build_diagnostic_dag` still
    applies the same gate as a DAG postcondition on whatever this function
    returns, so a reply that is still gated after the rewrite surfaces as a
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
        if gate.passed:
            return reply
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


# --------------------------------------------------------------------------
# DAG assembly
# --------------------------------------------------------------------------


def build_diagnostic_dag(
    client: LlmClient,
    repo: DataRepo,
    ledger_path: Path,
    sink: dict[str, BaseModel] | None = None,
) -> Dag:
    """Assemble the chat-diagnostic-turn DAG (PLAN.md loop (b)):
    Ledger-Maintainer -> Challenger -> apply -> Composer.

    Contracts: Challenger's postcondition requires a substantive
    counter-argument for every most-likely hypothesis; the apply node's
    postcondition requires the ledger version to have incremented;
    Composer's precondition requires the Challenger node to have completed
    this run, and its postcondition runs the treatment/dosing output gate.

    Expected `run()` initial context: `{"context_pack": ContextPack,
    "patient_turn": PatientTurn, "ledger": Ledger}` (the ledger state this
    turn started from, for the apply node's version-increment check).

    `sink`, if given, is populated by side effect with each node's
    validated output (keyed by node name) as the run proceeds — see the
    module docstring for why that is necessary given `dag.run()`'s
    audit-only return value.
    """
    results: dict[str, BaseModel] = sink if sink is not None else {}

    def _ledger_maintainer_fn(ctx: Ctx) -> BaseModel:
        context_pack = ctx["context_pack"]
        assert isinstance(context_pack, ContextPack)
        patient_turn = ctx["patient_turn"]
        assert isinstance(patient_turn, PatientTurn)

        diff = ledger_maintainer_stage(client, context_pack, patient_turn.text)
        results["ledger_maintainer"] = diff
        return diff

    def _challenger_fn(ctx: Ctx) -> BaseModel:
        proposed_diff = ctx["ledger_maintainer"]
        assert isinstance(proposed_diff, LedgerDiff)
        context_pack = ctx["context_pack"]
        assert isinstance(context_pack, ContextPack)

        verdict = challenger_stage(client, proposed_diff, context_pack)
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

        reply = composer_stage(client, ledger, context_pack)
        results["composer"] = reply
        return reply

    ledger_maintainer_node = Node(
        name="ledger_maintainer",
        fn=_ledger_maintainer_fn,
        input_model=ContextPack,
        output_model=LedgerDiff,
        depends_on="context_pack",
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
        preconditions=[require_prior_node("challenger")],
        postconditions=[_apply_ledger_version_incremented_contract()],
    )
    composer_node = Node(
        name="composer",
        fn=_composer_fn,
        input_model=Ledger,
        output_model=PatientReply,
        depends_on="apply",
        preconditions=[require_prior_node("challenger")],
        postconditions=[treatment_gate_contract()],
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

    `guarded_turn` runs the deterministic red-flag screen before anything
    else: on a flagged turn, this returns the `RedFlagResult` immediately
    and `client.complete` is never called (zero API calls)."""

    def _proceed() -> PatientReply:
        context_pack = build_context(repo, db, include_ledger=True)
        prior_ledger = load_ledger(ledger_path)
        sink: dict[str, BaseModel] = {}
        dag = build_diagnostic_dag(client, repo, ledger_path, sink)

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

    return guarded_turn(text, _proceed)


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

    return guarded_turn(text, lambda: informational_llm_result(client, repo, db, text))


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
    dag = build_diagnostic_dag(client, repo, ledger_path, sink)

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

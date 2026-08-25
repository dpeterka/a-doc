"""Tests for adoc.reason.stages: stage functions, DAG assembly, entry points.

Uses fake `LlmClient` transports throughout — no network, ever. The four
red-team scenarios (patient-theory anchoring, dosing leak blocked by the
gate, zero API calls on a red flag, a Challenger-less DAG failing closed)
are pinned as data in `tests/fixtures/redteam.yaml` and exercised
end-to-end here.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from ruamel.yaml import YAML

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import LedgerDiff
from adoc.config import ModelBinding
from adoc.intake.agent import red_flag_warning_prefix
from adoc.labs.db import DocumentTextPage, LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.client import (
    AnthropicProvider,
    LlmClient,
    OpenAIProvider,
    TransportRequest,
    TransportResponse,
)
from adoc.reason.context import ContextPack, build_context
from adoc.reason.dag import ContractViolation, Ctx, Dag, Node, require_prior_node, run
from adoc.reason.safety import red_flag_screen
from adoc.reason.stages import (
    PatientReply,
    PatientTurn,
    build_diagnostic_dag,
    ledger_maintainer_stage,
    run_diagnostic_turn,
)
from adoc.web.routes.chat import _with_red_flag_warning

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "redteam.yaml"
SHA = "b" * 64


def _load_fixture() -> dict[str, Any]:
    yaml = YAML(typ="safe")
    with FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert isinstance(data, dict)
    return data


FIXTURE = _load_fixture()


def _redteam_case(case_id: str) -> dict[str, Any]:
    for case in FIXTURE["diagnostic_turn_redteam"]:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


# --- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab-result", page_count=1)
    )
    store.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="ana-titer",
                name_raw="ANA",
                value_text="1:640",
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": "ANA"}),
            )
        ]
    )
    return store


# --- fake transport helpers -----------------------------------------------------------------


def _make_primary_transport(
    ledger_ops: list[dict[str, Any]],
    patient_reply: dict[str, Any],
    calls: list[TransportRequest],
):
    """Services role `primary_reasoner` (ledger-maintainer + composer) and,
    if ever exercised, `classifier` — dispatching on the requested schema."""

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is not None
        name = request.schema.__name__
        if name == "_LedgerDiffPayload":
            tool_input: dict[str, Any] = {"rationale": "proposed diff", "ops": ledger_ops}
        elif name == "PatientReply":
            tool_input = patient_reply
        elif name == "TurnRoute":
            tool_input = {"route": "diagnostic", "rationale": "new clinical detail"}
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema for primary transport: {name}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=10, output_tokens=10)

    return transport


def _make_challenger_transport(
    counter_arguments: list[dict[str, Any]],
    additional_ops: list[dict[str, Any]],
    calls: list[TransportRequest],
):
    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        tool_input = {
            "counter_arguments": counter_arguments,
            "additional_ops": additional_ops,
            "verdict_notes": "reviewed",
        }
        return TransportResponse(text="", tool_input=tool_input, input_tokens=10, output_tokens=10)

    return transport


def _make_entailed_entailment_transport():
    """Default fake transport for role `entailment_verifier`: always judges
    every claim_index it is sent as `entailed`. `verify_claims` renders the
    outgoing pairs as a JSON array (each with a `claim_index`) after the
    first blank line of the user message — parsing that back out lets this
    fake respond correctly regardless of how many claims a given call
    carries, with no hardcoded count."""

    def transport(request: TransportRequest) -> TransportResponse:
        _preamble, _blank, payload_text = request.messages[-1].content.partition("\n\n")
        pairs = json.loads(payload_text)
        judgments = [
            {"claim_index": pair["claim_index"], "judgment": "entailed", "rationale": "matches"}
            for pair in pairs
        ]
        tool_input = {"judgments": judgments}
        return TransportResponse(text="", tool_input=tool_input, input_tokens=5, output_tokens=5)

    return transport


def _build_client(
    primary_transport: Any,
    challenger_transport: Any,
    entailment_transport: Any = None,
) -> LlmClient:
    bindings: dict[str, list[ModelBinding]] = {
        "primary_reasoner": [ModelBinding(provider="anthropic", model="fake-primary")],
        "challenger": [ModelBinding(provider="openai", model="fake-challenger")],
        "classifier": [ModelBinding(provider="anthropic", model="fake-primary")],
        "entailment_verifier": [ModelBinding(provider="featherless", model="fake-verifier")],
    }
    providers = {
        "anthropic": AnthropicProvider(api_key=None, transport=primary_transport),
        "featherless": OpenAIProvider(
            api_key=None, transport=entailment_transport or _make_entailed_entailment_transport()
        ),
        "openai": OpenAIProvider(api_key=None, transport=challenger_transport),
    }
    return LlmClient(bindings, providers)


_SLE_MOST_LIKELY_OP = {
    "op": "add_hypothesis",
    "hypothesis": {
        "id": "sle-01",
        "name": "Systemic lupus erythematosus",
        "tier": "most-likely",
        "probability": "moderate",
        "status": "active",
        "origin": "model",
        "first_proposed": "2026-08-01",
        "evidence_for": [
            {"claim": "ANA elevated", "source": "labs:ana-titer:2026-05-02", "strength": "strong"}
        ],
    },
}

_PE_CANT_MISS_OP = {
    "op": "add_hypothesis",
    "hypothesis": {
        "id": "pe-01",
        "name": "Pulmonary embolism",
        "tier": "cant-miss",
        "probability": "low",
        "status": "active",
        "origin": "model",
        "first_proposed": "2026-08-01",
    },
}


# --- happy path -----------------------------------------------------------------------------


def test_full_diagnostic_dag_happy_path(repo: DataRepo, db: LabsDb) -> None:
    calls: list[TransportRequest] = []
    patient_reply = {
        "tiers_rendered": (
            "Most Likely: a lupus-like presentation — this is a lead to discuss with your "
            "doctor.\nCan't-Miss: pulmonary embolism remains on the board.\nAsk your doctor "
            "about ordering a complement (C3/C4) panel."
        ),
        "tests_to_request": ["Complement C3/C4 panel"],
        "framing_ack": True,
    }
    primary_transport = _make_primary_transport(
        [_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], patient_reply, calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(
        client,
        repo,
        db,
        repo.root / LEDGER_RELPATH,
        "My joints have been aching for weeks and I'm constantly exhausted.",
    )

    assert isinstance(result, PatientReply)
    assert "discuss with your doctor" in result.tiers_rendered
    assert result.tests_to_request == ["Complement C3/C4 panel"]
    assert len(calls) == 3  # ledger_maintainer, challenger, composer

    new_ledger = load_ledger(repo.root / LEDGER_RELPATH)
    assert new_ledger.version == 1
    assert {h.id for h in new_ledger.hypotheses} == {"sle-01", "pe-01"}


def test_diagnostic_turn_context_includes_matching_document_excerpts(
    repo: DataRepo, db: LabsDb
) -> None:
    """docs/adr/0015-document-text-corpus.md: `run_diagnostic_turn` passes
    the patient's turn text as `build_context`'s `query`, so a relevant
    document excerpt reaches the ledger-maintainer's prompt."""
    db.replace_document_text(
        SHA,
        [DocumentTextPage(page=2, text="Impression: findings consistent with early arthritis.")],
        extracted_at=datetime(2026, 5, 3),
    )
    calls: list[TransportRequest] = []
    patient_reply = {"tiers_rendered": "leads to discuss with your doctor", "framing_ack": True}
    primary_transport = _make_primary_transport([], patient_reply, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[], additional_ops=[], calls=calls
    )
    client = _build_client(primary_transport, challenger_transport)

    run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "How is my arthritis doing lately?"
    )

    ledger_maintainer_request = calls[0]
    sent = "\n".join(m.content for m in ledger_maintainer_request.messages)
    assert "Relevant Document Excerpts" in sent
    assert "arthritis" in sent.lower()
    assert "doc:doc.pdf#p2" in sent


# --- red-team (a): patient-theory anchoring --------------------------------------------------


def test_redteam_patient_theory_is_quarantined_and_context_wired_through(
    repo: DataRepo, db: LabsDb
) -> None:
    case = _redteam_case("patient_theory_anchoring")
    repo.write(
        "case/patient-theories.md",
        f"# Patient Theories\n\n- I think I have {case['patient_theories_file_contains']}.\n",
    )

    calls: list[TransportRequest] = []
    ledger_ops = [
        {
            "op": "add_hypothesis",
            "hypothesis": {
                "id": "mcas-01",
                "name": "Mast cell activation syndrome",
                "tier": "expanded",
                "probability": "low",
                "status": "patient-proposed",
                "origin": "patient",
                "first_proposed": "2026-08-01",
            },
        },
        _PE_CANT_MISS_OP,
    ]
    patient_reply = {
        "tiers_rendered": (
            "Expanded: your own theory is included here and is a lead to discuss with your "
            "doctor, alongside the other possibilities.\nCan't-Miss: pulmonary embolism."
        ),
        "tests_to_request": [],
        "framing_ack": True,
    }
    primary_transport = _make_primary_transport(ledger_ops, patient_reply, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[],
        additional_ops=[
            {
                "op": "record_challenge",
                "id": "mcas-01",
                "note": "Tryptase level not yet checked; alternative explanations not excluded.",
            }
        ],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(client, repo, db, repo.root / LEDGER_RELPATH, case["turn_text"])

    assert isinstance(result, PatientReply)

    ledger_maintainer_calls = [
        c for c in calls if c.schema is not None and c.schema.__name__ == "_LedgerDiffPayload"
    ]
    assert len(ledger_maintainer_calls) == 1
    sent_content = "\n".join(m.content for m in ledger_maintainer_calls[0].messages)
    assert "Patient Theories" in sent_content
    assert case["patient_theories_file_contains"] in sent_content

    new_ledger = load_ledger(repo.root / LEDGER_RELPATH)
    patient_hyp = next(h for h in new_ledger.hypotheses if h.id == "mcas-01")
    assert patient_hyp.origin == "patient"
    assert patient_hyp.tier != "most-likely"
    assert patient_hyp.challenger_notes  # a challenge was recorded alongside the proposal


# --- red-team (b): dosing leak blocked by the treatment-gate contract ------------------------


def test_redteam_dosing_output_blocked_by_treatment_gate_contract(
    repo: DataRepo, db: LabsDb
) -> None:
    case = _redteam_case("dosing_leak_blocked_by_gate")
    calls: list[TransportRequest] = []
    bad_patient_reply = {
        "tiers_rendered": f"Most Likely: lupus. You should {case['composer_output_contains']}.",
        "tests_to_request": [],
        "framing_ack": True,
    }
    primary_transport = _make_primary_transport(
        [_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], bad_patient_reply, calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA negative on two occasions."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client,
            repo,
            db,
            repo.root / LEDGER_RELPATH,
            "My joints hurt and I'm exhausted.",
        )

    assert excinfo.value.contract_name == case["expected_contract_name"]


# --- red-team (c): red-flag turn makes zero API calls ----------------------------------------


def test_redteam_red_flag_always_warns_and_model_cannot_suppress_it() -> None:
    """Red-flag input must ALWAYS reach the patient carrying the warning.

    This replaces `test_redteam_red_flag_turn_makes_zero_api_calls` (ADR
    0014, warn-not-block, an explicit product-owner decision for this
    single-patient tool). As a hard block the screen made intake unusable:
    recounting history is the whole point of an initial visit, the screen
    deliberately does no tense or negation detection ("chest pain years
    ago" matches), so it fired constantly and cost the patient her entire
    turn each time — and a warning that fires on nearly every message stops
    being read.

    The property that protects the patient is therefore no longer "no API
    call happened" but "the match was surfaced, in fixed text chosen by
    code, that nothing the model returns can drop or soften". A reply which
    mentions no emergency at all still comes back with the warning attached.
    """
    case = _redteam_case("red_flag_always_warns")

    screen = red_flag_screen(case["turn_text"])
    assert screen.flagged is True
    assert screen.category is not None

    model_reply = "Your ferritin trend looks unremarkable. Nothing alarming here."
    shown = _with_red_flag_warning(screen, model_reply)

    assert shown.startswith(red_flag_warning_prefix(screen.category))
    assert model_reply in shown  # the turn still happens; it is annotated, not replaced
    assert screen.category.replace("_", " ") in shown  # the matched category is named


def test_red_flag_warning_is_absent_when_the_screen_does_not_fire() -> None:
    screen = red_flag_screen("my joints have ached for a few months")
    assert screen.flagged is False
    assert _with_red_flag_warning(screen, "a normal reply") == "a normal reply"


# --- red-team (d): a DAG without a completed Challenger node fails closed -------------------


def test_redteam_dag_without_challenger_node_fails_require_prior_node(
    repo: DataRepo, db: LabsDb
) -> None:
    case = _redteam_case("missing_challenger_node_fails_closed")
    calls: list[TransportRequest] = []
    primary_transport = _make_primary_transport([_PE_CANT_MISS_OP], {}, calls)
    client = _build_client(primary_transport, primary_transport)  # challenger never invoked

    def _ledger_maintainer_fn(ctx: Ctx) -> BaseModel:
        context_pack = ctx["context_pack"]
        patient_turn = ctx["patient_turn"]
        assert isinstance(context_pack, ContextPack)
        assert isinstance(patient_turn, PatientTurn)
        return ledger_maintainer_stage(client, context_pack, patient_turn.text, db, repo)

    def _composer_like_fn(_ctx: Ctx) -> BaseModel:  # never reached
        raise AssertionError("composer must not run when the challenger precondition fails")

    ledger_maintainer_node = Node(
        name="ledger_maintainer",
        fn=_ledger_maintainer_fn,
        input_model=ContextPack,
        output_model=LedgerDiff,
        depends_on="context_pack",
    )
    composer_like_node = Node(
        name="composer",
        fn=_composer_like_fn,
        input_model=LedgerDiff,
        output_model=PatientReply,
        depends_on="ledger_maintainer",
        preconditions=[require_prior_node("challenger")],
    )
    dag = Dag([ledger_maintainer_node, composer_like_node])
    context_pack = build_context(repo, db, include_ledger=True)

    with pytest.raises(ContractViolation) as excinfo:
        run(
            dag,
            {
                "context_pack": context_pack,
                "patient_turn": PatientTurn(text="new symptom: joint swelling"),
            },
        )

    assert excinfo.value.contract_name == case["expected_contract_name"]


def test_build_diagnostic_dag_has_the_four_expected_nodes(repo: DataRepo, db: LabsDb) -> None:
    client = _build_client(lambda r: None, lambda r: None)  # not called in this test
    dag = build_diagnostic_dag(client, repo, repo.root / LEDGER_RELPATH, db)
    assert [node.name for node in dag.nodes] == [
        "ledger_maintainer",
        "challenger",
        "apply",
        "composer",
    ]


# --- composer treatment-gate rewrite loop -----------------------------------------------------


def _make_primary_transport_with_reply_sequence(
    ledger_ops: list[dict[str, Any]],
    patient_replies: list[dict[str, Any]],
    calls: list[TransportRequest],
):
    """Like `_make_primary_transport`, but each PatientReply request pops the
    next reply from `patient_replies` - lets a test script "first draft trips
    the gate, rewrite passes" (composer_stage's gate-guided retry)."""
    remaining = list(patient_replies)

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is not None
        name = request.schema.__name__
        if name == "_LedgerDiffPayload":
            tool_input: dict[str, Any] = {"rationale": "proposed diff", "ops": ledger_ops}
        elif name == "PatientReply":
            assert remaining, "composer requested more replies than scripted"
            tool_input = remaining.pop(0)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema for primary transport: {name}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=10, output_tokens=10)

    return transport


_GATED_REPLY = {
    # "5000 IU" trips the dosage detector: restating a dose the patient
    # already takes - the exact live failure the rewrite loop was added for.
    "tiers_rendered": (
        "Most Likely: a lupus-like presentation. Your vitamin D remains low "
        "despite the 5000 IU you take daily - discuss with your doctor."
    ),
    "tests_to_request": [],
    "framing_ack": True,
}

_CLEAN_REPLY = {
    "tiers_rendered": (
        "Most Likely: a lupus-like presentation. Your vitamin D remains low "
        "despite your current supplement - a lead to discuss with your doctor."
    ),
    "tests_to_request": [],
    "framing_ack": True,
}


def test_composer_gate_failure_is_rewritten_once_and_succeeds(repo: DataRepo, db: LabsDb) -> None:
    calls: list[TransportRequest] = []
    primary_transport = _make_primary_transport_with_reply_sequence(
        [_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], [_GATED_REPLY, _CLEAN_REPLY], calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)
    assert "5000" not in result.tiers_rendered
    # ledger_maintainer, challenger, composer draft, composer rewrite.
    assert len(calls) == 4
    rewrite_request = calls[-1]
    feedback = rewrite_request.messages[-1].content
    assert "5000 IU" in feedback  # the offending span is named to the model
    assert "Rewrite this response" in feedback  # GateResult.rewrite_instruction


def test_composer_still_gated_after_rewrite_raises_contract_violation(
    repo: DataRepo, db: LabsDb
) -> None:
    calls: list[TransportRequest] = []
    primary_transport = _make_primary_transport_with_reply_sequence(
        [_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], [_GATED_REPLY, _GATED_REPLY], calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
        )

    assert excinfo.value.contract_name == "treatment_gate"
    assert len(calls) == 4  # exactly one rewrite attempt - never an unbounded loop


# --- Phase 2 citation checker: retry loop + DAG contract gate ---------------------------------


def _sle_op(source: str, claim: str = "ANA elevated") -> dict[str, Any]:
    """`_SLE_MOST_LIKELY_OP` with its evidence source ref swapped out, so a
    test can script "bad ref first, good ref on retry" without repeating
    the whole hypothesis payload."""
    op = json.loads(json.dumps(_SLE_MOST_LIKELY_OP))
    op["hypothesis"]["evidence_for"] = [{"claim": claim, "source": source, "strength": "strong"}]
    return op


def _make_primary_transport_with_diff_sequence(
    ledger_ops_sequence: list[list[dict[str, Any]]],
    patient_reply: dict[str, Any],
    calls: list[TransportRequest],
):
    """Like `_make_primary_transport`, but each `_LedgerDiffPayload` request
    pops the next ops list from `ledger_ops_sequence` — lets a test script
    "first diff cites a fabricated ref, the citation-checker retry gets a
    corrected diff" (`ledger_maintainer_stage`'s citation retry loop)."""
    remaining = list(ledger_ops_sequence)

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is not None
        name = request.schema.__name__
        if name == "_LedgerDiffPayload":
            assert remaining, "ledger_maintainer requested more diffs than scripted"
            tool_input: dict[str, Any] = {"rationale": "proposed diff", "ops": remaining.pop(0)}
        elif name == "PatientReply":
            tool_input = patient_reply
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema for primary transport: {name}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=10, output_tokens=10)

    return transport


def test_fabricated_labs_ref_triggers_retry_and_second_good_diff_applies(
    repo: DataRepo, db: LabsDb
) -> None:
    """Acceptance: a planted fabricated `labs:` ref (no such analyte/date)
    in the maintainer's first diff triggers exactly one retry naming the
    bad ref; a corrected second diff then applies normally."""
    calls: list[TransportRequest] = []
    bad_diff_ops = [_sle_op("labs:made-up-analyte:2026-05-02"), _PE_CANT_MISS_OP]
    good_diff_ops = [_sle_op("labs:ana-titer:2026-05-02"), _PE_CANT_MISS_OP]
    primary_transport = _make_primary_transport_with_diff_sequence(
        [bad_diff_ops, good_diff_ops], _CLEAN_REPLY, calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)
    # ledger_maintainer (bad), ledger_maintainer retry (good), challenger, composer.
    assert len(calls) == 4
    retry_request = calls[1]
    retry_feedback = retry_request.messages[-1].content
    assert "labs:made-up-analyte:2026-05-02" in retry_feedback
    assert "unresolved" in retry_feedback

    new_ledger = load_ledger(repo.root / LEDGER_RELPATH)
    assert {h.id for h in new_ledger.hypotheses} == {"sle-01", "pe-01"}


def test_still_fabricated_labs_ref_after_retry_raises_contract_violation_ledger_unchanged(
    repo: DataRepo, db: LabsDb
) -> None:
    """Acceptance: a diff that is STILL bad after the one retry raises a
    `ContractViolation` naming the failed ref, and the on-disk ledger is
    left completely unchanged (`apply` never runs)."""
    calls: list[TransportRequest] = []
    bad_diff_ops = [_sle_op("labs:made-up-analyte:2026-05-02"), _PE_CANT_MISS_OP]
    primary_transport = _make_primary_transport_with_diff_sequence(
        [bad_diff_ops, bad_diff_ops], _CLEAN_REPLY, calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    ledger_before = load_ledger(repo.root / LEDGER_RELPATH)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
        )

    assert excinfo.value.contract_name == "citation_check_ledger_maintainer"
    assert "labs:made-up-analyte:2026-05-02" in excinfo.value.message
    # Exactly the one retry - the challenger/composer are never reached.
    assert len(calls) == 2

    ledger_after = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger_after == ledger_before


def test_mismatched_quoted_value_triggers_retry_and_veto_path(repo: DataRepo, db: LabsDb) -> None:
    """Acceptance: a claim that quotes a number disagreeing with the stored
    lab value (claim says 12.3, stored 1.23) is `mismatched`, not just
    `unresolved`, and follows the exact same retry/veto path."""
    SHA2 = "c" * 64
    db.upsert_document(
        LabDocument(sha256=SHA2, filename="crp.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="CRP",
                name_raw="CRP",
                value=1.23,
                source_doc=SHA2,
                raw_json=json.dumps({"name_raw": "CRP"}),
            )
        ]
    )

    calls: list[TransportRequest] = []
    bad_diff_ops = [
        _sle_op("labs:crp:2026-05-02", claim="CRP was 12.3 mg/L, markedly elevated"),
        _PE_CANT_MISS_OP,
    ]
    good_diff_ops = [
        _sle_op("labs:crp:2026-05-02", claim="CRP was 1.23 mg/L, mildly elevated"),
        _PE_CANT_MISS_OP,
    ]
    primary_transport = _make_primary_transport_with_diff_sequence(
        [bad_diff_ops, good_diff_ops], _CLEAN_REPLY, calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)
    retry_feedback = calls[1].messages[-1].content
    assert "mismatched" in retry_feedback
    assert "12.3" in retry_feedback


def test_challenger_additional_ops_bad_ref_cannot_reach_apply(repo: DataRepo, db: LabsDb) -> None:
    """Acceptance: a fabricated ref introduced by the Challenger's own
    `additional_ops` (not the Ledger-Maintainer's diff) is caught at the
    same choke point — the `apply` node's precondition — before
    `casefile.ledger.apply_and_save` ever runs."""
    calls: list[TransportRequest] = []
    clean_diff_ops = [_PE_CANT_MISS_OP]
    primary_transport = _make_primary_transport_with_diff_sequence(
        [clean_diff_ops], _CLEAN_REPLY, calls
    )
    bad_additional_ops = [
        {
            "op": "add_evidence",
            "id": "pe-01",
            "for_or_against": "against",
            "evidence": {
                "claim": "D-dimer was normal",
                "source": "labs:made-up-analyte:2026-05-02",
                "strength": "moderate",
            },
        }
    ]
    challenger_transport = _make_challenger_transport(
        counter_arguments=[], additional_ops=bad_additional_ops, calls=calls
    )
    client = _build_client(primary_transport, challenger_transport)

    ledger_before = load_ledger(repo.root / LEDGER_RELPATH)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
        )

    assert excinfo.value.contract_name == "citation_check_apply"
    assert "labs:made-up-analyte:2026-05-02" in excinfo.value.message

    ledger_after = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger_after == ledger_before


# --- Phase 2 entailment verifier: retry loop + DAG contract gate -------------------------------


def _make_scripted_entailment_transport(judgments_sequence: list[str]):
    """Fake transport for role `entailment_verifier`: the Nth call judges
    every claim it is sent with `judgments_sequence[N]` (clamped to the last
    entry once the sequence is exhausted, so calls after the scripted
    portion — e.g. the apply-node precondition re-check — keep whatever the
    sequence settled on)."""
    state = {"n": 0}

    def transport(request: TransportRequest) -> TransportResponse:
        _preamble, _blank, payload_text = request.messages[-1].content.partition("\n\n")
        pairs = json.loads(payload_text)
        judgment = judgments_sequence[min(state["n"], len(judgments_sequence) - 1)]
        state["n"] += 1
        judgments = [
            {"claim_index": p["claim_index"], "judgment": judgment, "rationale": "scripted"}
            for p in pairs
        ]
        return TransportResponse(
            text="", tool_input={"judgments": judgments}, input_tokens=5, output_tokens=5
        )

    return transport


def test_not_entailed_claim_triggers_retry_and_second_pass_applies(
    repo: DataRepo, db: LabsDb
) -> None:
    """Acceptance: a claim the verifier judges `not_entailed` on the first
    pass triggers exactly one retry naming the objection; a second pass
    that the verifier judges `entailed` then applies normally."""
    calls: list[TransportRequest] = []
    diff_ops = [_sle_op("labs:ana-titer:2026-05-02"), _PE_CANT_MISS_OP]
    primary_transport = _make_primary_transport(diff_ops, _CLEAN_REPLY, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    entailment_transport = _make_scripted_entailment_transport(["not_entailed", "entailed"])
    client = _build_client(primary_transport, challenger_transport, entailment_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)
    # ledger_maintainer (not_entailed), ledger_maintainer retry (entailed), challenger, composer.
    assert len(calls) == 4
    retry_request = calls[1]
    retry_feedback = retry_request.messages[-1].content
    assert "labs:ana-titer:2026-05-02" in retry_feedback
    assert "NOT entailed" in retry_feedback


def test_still_not_entailed_after_retry_raises_contract_violation_ledger_unchanged(
    repo: DataRepo, db: LabsDb
) -> None:
    """Acceptance: when EVERY claim in the diff is still judged
    `not_entailed` after the one retry (`VerificationReport.
    all_not_entailed` - here, the diff's one and only evidence claim),
    that is the "nothing survives" case (ADR 0016 revised) and still
    raises a `ContractViolation` naming the objection, with the on-disk
    ledger left completely unchanged (`apply` never runs)."""
    calls: list[TransportRequest] = []
    diff_ops = [_sle_op("labs:ana-titer:2026-05-02"), _PE_CANT_MISS_OP]
    primary_transport = _make_primary_transport(diff_ops, _CLEAN_REPLY, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    entailment_transport = _make_scripted_entailment_transport(["not_entailed"])
    client = _build_client(primary_transport, challenger_transport, entailment_transport)

    ledger_before = load_ledger(repo.root / LEDGER_RELPATH)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
        )

    assert excinfo.value.contract_name == "entailment_check_ledger_maintainer"
    assert "labs:ana-titer:2026-05-02" in excinfo.value.message
    assert "nothing survives" in excinfo.value.message
    # Exactly the one retry - the challenger/composer are never reached.
    assert len(calls) == 2

    ledger_after = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger_after == ledger_before


def _make_claim_scripted_entailment_transport(judgment_by_claim: dict[str, str]):
    """Fake transport for role `entailment_verifier` that judges each claim
    by its OWN text (`judgment_by_claim`), defaulting to `entailed` for any
    claim not named — lets a test script a MIX of judgments within one
    call, unlike `_make_scripted_entailment_transport`'s per-call uniform
    judgment (needed to exercise "strip, don't reject": ADR 0016 revised
    only strips a `not_entailed` claim, not the whole diff, when at least
    one OTHER claim in the same check is not `not_entailed`)."""

    def transport(request: TransportRequest) -> TransportResponse:
        _preamble, _blank, payload_text = request.messages[-1].content.partition("\n\n")
        pairs = json.loads(payload_text)
        judgments = [
            {
                "claim_index": p["claim_index"],
                "judgment": judgment_by_claim.get(p["claim"], "entailed"),
                "rationale": "scripted",
            }
            for p in pairs
        ]
        return TransportResponse(
            text="", tool_input={"judgments": judgments}, input_tokens=5, output_tokens=5
        )

    return transport


def test_some_not_entailed_claims_are_stripped_and_turn_proceeds(
    repo: DataRepo, db: LabsDb
) -> None:
    """Acceptance (ADR 0016 revised, "strip, don't reject"): when SOME but
    not ALL evidence claims in a diff are judged `not_entailed`, the bad
    claim is dropped from the diff and the turn proceeds with the
    remaining, verified evidence - no `ContractViolation`."""
    _seed_crp_row(db)
    calls: list[TransportRequest] = []
    op = json.loads(json.dumps(_SLE_MOST_LIKELY_OP))
    op["hypothesis"]["evidence_for"] = [
        {"claim": "ANA elevated", "source": "labs:ana-titer:2026-05-02", "strength": "strong"},
        {
            "claim": "CRP was sky-high, indicating severe inflammation",
            "source": "labs:crp:2026-05-02",
            "strength": "moderate",
        },
    ]
    primary_transport = _make_primary_transport([op, _PE_CANT_MISS_OP], _CLEAN_REPLY, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    entailment_transport = _make_claim_scripted_entailment_transport(
        {"CRP was sky-high, indicating severe inflammation": "not_entailed"}
    )
    client = _build_client(primary_transport, challenger_transport, entailment_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)
    # ledger_maintainer (mixed judgment, retries), ledger_maintainer retry
    # (same mixed judgment again - the fake never self-corrects), challenger,
    # composer. The strip happens AFTER the retry budget is spent, not
    # instead of it.
    assert len(calls) == 4

    new_ledger = load_ledger(repo.root / LEDGER_RELPATH)
    sle = next(h for h in new_ledger.hypotheses if h.id == "sle-01")
    assert [e.claim for e in sle.evidence_for] == ["ANA elevated"]


def test_insufficient_source_claim_survives_and_reaches_ledger(repo: DataRepo, db: LabsDb) -> None:
    """Acceptance: an `insufficient_source` claim (source text not yet
    resolvable, e.g. a `doc:` ref before its text is extracted) is KEPT,
    never stripped and never blocks - unresolvable is not the same as
    wrong, same principle as the citation checker's `unverifiable`."""
    doc_sha = "d" * 64
    db.upsert_document(
        LabDocument(sha256=doc_sha, filename="scan.pdf", doc_type="imaging", page_count=1)
    )
    calls: list[TransportRequest] = []
    op = json.loads(json.dumps(_SLE_MOST_LIKELY_OP))
    op["hypothesis"]["evidence_for"] = [
        {"claim": "ANA elevated", "source": "labs:ana-titer:2026-05-02", "strength": "strong"},
        {
            "claim": "Imaging findings support inflammatory changes",
            "source": "doc:scan.pdf#p1",
            "strength": "moderate",
        },
    ]
    primary_transport = _make_primary_transport([op, _PE_CANT_MISS_OP], _CLEAN_REPLY, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)
    assert len(calls) == 3  # no retry - nothing was not_entailed

    new_ledger = load_ledger(repo.root / LEDGER_RELPATH)
    sle = next(h for h in new_ledger.hypotheses if h.id == "sle-01")
    assert {e.claim for e in sle.evidence_for} == {
        "ANA elevated",
        "Imaging findings support inflammatory changes",
    }


def test_stripping_composes_with_abstention_contract(repo: DataRepo, db: LabsDb) -> None:
    """Acceptance: the abstention contract keeps its teeth after stripping -
    if dropping a not_entailed claim leaves a most-likely hypothesis with NO
    resolved supporting evidence, `most_likely_requires_resolved_evidence`
    still fires, even though the entailment check itself does not (this
    diff's claims are not ALL not_entailed - pe-01's claim is entailed - so
    it strips rather than hard-fails)."""
    calls: list[TransportRequest] = []
    pe_op = json.loads(json.dumps(_PE_CANT_MISS_OP))
    pe_op["hypothesis"]["evidence_for"] = [
        {"claim": "ANA elevated", "source": "labs:ana-titer:2026-05-02", "strength": "moderate"}
    ]
    sle_op = json.loads(json.dumps(_SLE_MOST_LIKELY_OP))
    sle_op["hypothesis"]["evidence_for"] = [
        {
            "claim": "A fabricated finding not actually in any source",
            "source": "labs:ana-titer:2026-05-02",
            "strength": "strong",
        }
    ]
    primary_transport = _make_primary_transport([sle_op, pe_op], _CLEAN_REPLY, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    entailment_transport = _make_claim_scripted_entailment_transport(
        {"A fabricated finding not actually in any source": "not_entailed"}
    )
    client = _build_client(primary_transport, challenger_transport, entailment_transport)

    ledger_before = load_ledger(repo.root / LEDGER_RELPATH)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
        )

    assert excinfo.value.contract_name == "most_likely_requires_resolved_evidence"
    assert "sle-01" in excinfo.value.message

    ledger_after = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger_after == ledger_before


def test_challenger_not_entailed_additional_evidence_is_stripped(
    repo: DataRepo, db: LabsDb
) -> None:
    """Acceptance: a not_entailed claim in the Challenger's OWN
    `additional_ops` is stripped by `challenger_stage` itself before the
    verdict is returned - it never reaches `apply`, and (since the
    Ledger-Maintainer's own diff still has good evidence) the turn proceeds
    without a `ContractViolation`."""
    calls: list[TransportRequest] = []
    clean_diff_ops = [_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP]
    primary_transport = _make_primary_transport(clean_diff_ops, _CLEAN_REPLY, calls)
    bad_additional_ops = [
        {
            "op": "add_evidence",
            "id": "pe-01",
            "for_or_against": "against",
            "evidence": {
                "claim": "D-dimer disproves this outright",
                "source": "labs:ana-titer:2026-05-02",
                "strength": "moderate",
            },
        }
    ]
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=bad_additional_ops,
        calls=calls,
    )
    entailment_transport = _make_claim_scripted_entailment_transport(
        {"D-dimer disproves this outright": "not_entailed"}
    )
    client = _build_client(primary_transport, challenger_transport, entailment_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)

    new_ledger = load_ledger(repo.root / LEDGER_RELPATH)
    pe = next(h for h in new_ledger.hypotheses if h.id == "pe-01")
    assert pe.evidence_against == []


# --- Phase 2 Composer quantitative check: retry loop + DAG contract gate -----------------------


def _seed_crp_row(db: LabsDb, *, value: float = 8.5) -> None:
    sha = "f" * 64
    db.upsert_document(
        LabDocument(sha256=sha, filename="crp.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="CRP",
                name_raw="CRP",
                value=value,
                source_doc=sha,
                raw_json=json.dumps({"name_raw": "CRP"}),
            )
        ]
    )


_BAD_NUMBER_REPLY = {
    "tiers_rendered": (
        "Most Likely: a lupus-like presentation. Your CRP was 12.0 mg/L, notably elevated - "
        "a lead to discuss with your doctor."
    ),
    "tests_to_request": [],
    "framing_ack": True,
}

_GOOD_NUMBER_REPLY = {
    "tiers_rendered": (
        "Most Likely: a lupus-like presentation. Your CRP was 8.5 mg/L, notably elevated - "
        "a lead to discuss with your doctor."
    ),
    "tests_to_request": [],
    "framing_ack": True,
}


def test_composer_number_mismatch_is_rewritten_once_and_succeeds(
    repo: DataRepo, db: LabsDb
) -> None:
    _seed_crp_row(db)
    calls: list[TransportRequest] = []
    primary_transport = _make_primary_transport_with_reply_sequence(
        [_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], [_BAD_NUMBER_REPLY, _GOOD_NUMBER_REPLY], calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
    )

    assert isinstance(result, PatientReply)
    assert "12.0" not in result.tiers_rendered
    # ledger_maintainer, challenger, composer draft, composer rewrite.
    assert len(calls) == 4
    rewrite_request = calls[-1]
    feedback = rewrite_request.messages[-1].content
    assert "12.0" in feedback
    assert "8.5" in feedback


def test_composer_number_mismatch_persists_raises_contract_violation(
    repo: DataRepo, db: LabsDb
) -> None:
    _seed_crp_row(db)
    calls: list[TransportRequest] = []
    primary_transport = _make_primary_transport_with_reply_sequence(
        [_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP],
        [_BAD_NUMBER_REPLY, _BAD_NUMBER_REPLY],
        calls,
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
        )

    assert excinfo.value.contract_name == "composer_number_check"
    assert len(calls) == 4  # exactly one rewrite attempt - never an unbounded loop


# --- Phase 2 abstention contract: most-likely requires resolved evidence -----------------------


_NO_EVIDENCE_MOST_LIKELY_OP = {
    "op": "add_hypothesis",
    "hypothesis": {
        "id": "no-evidence-01",
        "name": "Undifferentiated connective tissue disease",
        "tier": "most-likely",
        "probability": "moderate",
        "status": "active",
        "origin": "model",
        "first_proposed": "2026-08-01",
    },
}


def test_most_likely_with_no_evidence_at_all_raises_contract_violation(
    repo: DataRepo, db: LabsDb
) -> None:
    calls: list[TransportRequest] = []
    primary_transport = _make_primary_transport(
        [_NO_EVIDENCE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], _CLEAN_REPLY, calls
    )
    challenger_transport = _make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "no-evidence-01", "argument": "Nothing rules this out either."}
        ],
        additional_ops=[],
        calls=calls,
    )
    client = _build_client(primary_transport, challenger_transport)

    ledger_before = load_ledger(repo.root / LEDGER_RELPATH)

    with pytest.raises(ContractViolation) as excinfo:
        run_diagnostic_turn(
            client, repo, db, repo.root / LEDGER_RELPATH, "My joints ache and I'm exhausted."
        )

    assert excinfo.value.contract_name == "most_likely_requires_resolved_evidence"
    assert "no-evidence-01" in excinfo.value.message

    ledger_after = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger_after == ledger_before


# --- Phase 2 abstention calibration: insufficient_evidence signal round-trips -------------------


def test_ledger_maintainer_insufficient_evidence_notes_land_in_rationale(
    repo: DataRepo, db: LabsDb
) -> None:
    context_pack = build_context(repo, db, include_ledger=True)

    def transport(request: TransportRequest) -> TransportResponse:
        assert request.schema is not None
        tool_input = {
            "rationale": "no new hypotheses this turn",
            "ops": [_PE_CANT_MISS_OP],
            "insufficient_evidence": [
                {
                    "topic": "thyroid function",
                    "reason": "no thyroid panel has ever been recorded in this case file",
                }
            ],
        }
        return TransportResponse(text="", tool_input=tool_input, input_tokens=5, output_tokens=5)

    client = _build_client(transport, transport)

    diff = ledger_maintainer_stage(client, context_pack, "How's my thyroid doing?", db, repo)

    assert "Insufficient evidence" in diff.rationale
    assert "thyroid function" in diff.rationale
    assert "no thyroid panel has ever been recorded" in diff.rationale


def test_patient_reply_insufficient_evidence_field_round_trips(repo: DataRepo, db: LabsDb) -> None:
    calls: list[TransportRequest] = []
    reply_with_abstention = {
        "tiers_rendered": "Can't-Miss: pulmonary embolism remains on the board.",
        "tests_to_request": [],
        "framing_ack": True,
        "insufficient_evidence": ["No thyroid function data is on file yet."],
    }
    primary_transport = _make_primary_transport([_PE_CANT_MISS_OP], reply_with_abstention, calls)
    challenger_transport = _make_challenger_transport(
        counter_arguments=[], additional_ops=[], calls=calls
    )
    client = _build_client(primary_transport, challenger_transport)

    result = run_diagnostic_turn(
        client, repo, db, repo.root / LEDGER_RELPATH, "How's my thyroid doing?"
    )

    assert isinstance(result, PatientReply)
    assert result.insufficient_evidence == ["No thyroid function data is on file yet."]

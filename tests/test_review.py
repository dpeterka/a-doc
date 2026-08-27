"""Tests for adoc.reason.review: the weekly-review DAG and its contracts.

Fake `LlmClient` transports throughout — no network, ever.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from adoc.casefile.ledger import apply_and_save
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    Ledger,
    LedgerDiff,
    Provenance,
)
from adoc.config import ModelBinding
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, LabDocument, LabResult
from adoc.reason.client import (
    AnthropicProvider,
    LlmClient,
    OpenAIProvider,
    TransportRequest,
    TransportResponse,
)
from adoc.reason.context import build_context
from adoc.reason.dag import ContractViolation
from adoc.reason.dag import run as dag_run
from adoc.reason.review import (
    FULL_REVIEW_COOLDOWN,
    FULL_REVIEW_FLOOR,
    AdjudicationResult,
    BlindDifferential,
    BlindDifferentialItem,
    BlindDifferentialPayload,
    BlindEvidenceItem,
    ChallengeSweepResult,
    DeferredVerificationSweepResult,
    Divergence,
    DivergenceDecisionPayload,
    DivergenceSet,
    HypothesisChallengeNote,
    Marker,
    OpsMetrics,
    StalenessReport,
    TestChooserItem,
    TestChooserPayload,
    TestChooserResult,
    UpdateHypothesis,
    _pool_evidence,
    _render_questions_open,
    _resolvable_evidence,
    build_review_dag,
    build_review_ledger_diff,
    challenger_kill_rate,
    compute_divergences,
    hypothesis_ages_days,
    ledger_churn,
    parse_audit_costs,
    render_review_markdown,
    resolve_adjudication_decisions,
    run_review_tick,
    run_weekly_review,
    scan_staleness,
    should_run_full_review,
    sweep_deferred_entailment_claims,
)
from adoc.reason.review_trigger import ReviewMarker, ReviewMarkerReason, mark_review_wanted
from adoc.reason.verify import DEFERRED_CLAIMS_RELPATH, Claim, queue_deferred_claims

SLE_ID = "sle-01"
PE_ID = "pe-01"


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    return LabsDb(tmp_path / "labs.sqlite")


def _seed_ledger(repo: DataRepo) -> Ledger:
    """Seed the ledger with two active hypotheses: one `expanded` (SLE, to
    be probability-mismatched by the blind panel) and one `cant-miss` (PE,
    to be unmentioned by the panel — a `ledger_only` divergence)."""
    provenance = Provenance(
        app_version="test",
        prompt_template_version="ledger_maintainer@v1",
        model_id="seed-model",
        dag_node="ledger_maintainer",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    diff = LedgerDiff(
        provenance=provenance,
        rationale="seed",
        ops=[
            AddHypothesis(
                hypothesis=Hypothesis(
                    id=SLE_ID,
                    name="Systemic lupus erythematosus",
                    tier="expanded",
                    probability="moderate",
                    status="active",
                    origin="model",
                    first_proposed=date(2026, 1, 1),
                )
            ),
            AddHypothesis(
                hypothesis=Hypothesis(
                    id=PE_ID,
                    name="Pulmonary embolism",
                    tier="cant-miss",
                    probability="low",
                    status="active",
                    origin="model",
                    first_proposed=date(2026, 1, 1),
                )
            ),
        ],
    )
    return apply_and_save(repo.root / LEDGER_RELPATH, repo.root / HISTORY_RELPATH, diff)


def _blind_panel_bindings() -> list[ModelBinding]:
    return [
        ModelBinding(provider="anthropic", model="fake-blind-0"),
        ModelBinding(provider="anthropic", model="fake-blind-1"),
    ]


def _happy_path_transport(calls: list[TransportRequest]) -> Any:
    panel_items_by_model = {
        "fake-blind-0": [
            {
                "name": "Systemic lupus erythematosus",
                "probability_bucket": "high",
                "why": "ANA 1:640 homogeneous pattern strongly suggestive",
                "cant_miss": False,
            },
            {
                "name": "Sjogren syndrome",
                "probability_bucket": "low",
                "why": "dry eyes reported in patient theories",
                "cant_miss": False,
            },
        ],
        "fake-blind-1": [
            {
                "name": "Systemic lupus erythematosus",
                "probability_bucket": "moderate",
                "why": "consistent with joint pain and fatigue pattern",
                "cant_miss": False,
            },
        ],
    }
    decisions = [
        {
            "divergence": "probability:sle-01",
            "decision": "accept",
            "rationale": "panel's higher probability better fits the recent ANA titer",
        },
        {
            "divergence": "panel-only:sjogrensyndrome",
            "decision": "accept",
            "rationale": "dry eyes/mouth history supports considering this independently",
        },
        {
            "divergence": "ledger-only:pe-01",
            "decision": "reject",
            "rationale": "no new evidence changes the cant-miss assessment",
        },
    ]
    notes = [
        {
            "id": SLE_ID,
            "note": "Reviewed again: anti-dsDNA still pending, no contradicting evidence.",
        },
        {"id": PE_ID, "note": "Reviewed again: no red-flag signs of PE, remains a placeholder."},
    ]
    test_chooser_items = [
        {"text": "Ask your doctor about a complement C3/C4 panel.", "hypothesis_ids": [SLE_ID]}
    ]

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is not None
        name = request.schema.__name__
        if name == "BlindDifferentialPayload":
            tool_input: dict[str, Any] = {"items": panel_items_by_model[request.model]}
        elif name == "AdjudicationPayload":
            tool_input = {"decisions": decisions}
        elif name == "ChallengeSweepPayload":
            tool_input = {"notes": notes}
        elif name == "TestChooserPayload":
            tool_input = {"items": test_chooser_items}
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema: {name}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=10, output_tokens=10)

    return transport


def _build_client(transport: Any) -> LlmClient:
    bindings: dict[str, list[ModelBinding]] = {
        "blind_panel": _blind_panel_bindings(),
        "challenger": [ModelBinding(provider="anthropic", model="fake-challenger")],
        "test_chooser": [ModelBinding(provider="anthropic", model="fake-test-chooser")],
    }
    providers = {"anthropic": AnthropicProvider(api_key=None, transport=transport)}
    return LlmClient(bindings, providers)


def _fixed_clock() -> datetime:
    return datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


# --- happy path -------------------------------------------------------------------------------


def test_full_review_happy_path(repo: DataRepo, db: LabsDb) -> None:
    seeded = _seed_ledger(repo)
    calls: list[TransportRequest] = []
    client = _build_client(_happy_path_transport(calls))

    report = run_weekly_review(repo, db, client, clock=_fixed_clock)

    assert report.review_date == date(2026, 8, 23)
    assert report.ledger_version_before == seeded.version
    assert report.ledger_version_after == seeded.version + 1
    assert report.tag == "review-2026-08-23"
    assert report.commit_sha

    # Blind panel + adjudication + challenge sweep + test chooser = 5 calls.
    assert len(calls) == 5

    markdown_path = repo.root / report.markdown_path
    assert markdown_path.exists()
    text = markdown_path.read_text(encoding="utf-8")
    assert "What changed this week" in text
    assert "What to ask your doctor" in text
    assert "Metrics appendix" in text

    questions = repo.read("case/questions-open.md")
    assert "complement C3/C4" in questions

    git_repo = Repo(repo.root)
    tag_names = [t.name for t in git_repo.tags]
    assert "review-2026-08-23" in tag_names

    # Accepted panel-only divergence added a new challenger-origin hypothesis.
    names = {h.name for h in report_ledger_hypotheses(repo)}
    assert "Sjogren syndrome" in names
    sjogren = next(h for h in report_ledger_hypotheses(repo) if h.name == "Sjogren syndrome")
    assert sjogren.origin == "challenger"

    # Accepted probability-mismatch moved SLE's probability toward the panel's read.
    sle = next(h for h in report_ledger_hypotheses(repo) if h.id == SLE_ID)
    assert sle.probability == "high"
    assert sle.prior_probability == "moderate"


def report_ledger_hypotheses(repo: DataRepo) -> list[Hypothesis]:
    from adoc.casefile.ledger import load_ledger

    return load_ledger(repo.root / LEDGER_RELPATH).hypotheses


# --- deferred entailment sweep (latency: "diagnostic-turn-latency") ----------------------------


def test_deferred_claim_is_picked_up_and_verified_by_the_weekly_review(
    repo: DataRepo, db: LabsDb
) -> None:
    """Acceptance: a claim a diagnostic turn DEFERRED (queued via
    `reason.verify.queue_deferred_claims` because it supported an
    `expanded`/`cant-miss` hypothesis rather than `most-likely` - PLAN.md
    latency "diagnostic-turn-latency") is never silently dropped. The next
    weekly review pops the ENTIRE queue, verifies every claim through the
    same `verify_claims` a diagnostic turn uses, empties the queue, and
    surfaces the result in the committed review report."""
    _seed_ledger(repo)
    sha = "9" * 64
    db.upsert_document(
        LabDocument(sha256=sha, filename="crp.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="CRP",
                name_raw="CRP",
                value=8.5,
                source_doc=sha,
                raw_json=json.dumps({"name_raw": "CRP"}),
            )
        ]
    )
    deferred_claim = Claim(
        hypothesis_id=PE_ID,
        for_or_against="for",
        claim="CRP was sky-high, indicating severe inflammation",
        source="labs:crp:2026-05-02",
    )
    queue_deferred_claims(repo, [deferred_claim], dag_node="ledger_maintainer")
    deferred_path = repo.root / DEFERRED_CLAIMS_RELPATH
    assert json.loads(deferred_path.read_text(encoding="utf-8"))  # queued, not empty yet

    calls: list[TransportRequest] = []

    def entailment_transport(request: TransportRequest) -> TransportResponse:
        _preamble, _blank, payload_text = request.messages[-1].content.partition("\n\n")
        pairs = json.loads(payload_text)
        judgments = [
            {
                "claim_index": p["claim_index"],
                "judgment": "not_entailed",
                "rationale": "the stored row is 8.5, not sky-high",
            }
            for p in pairs
        ]
        return TransportResponse(
            text="", tool_input={"judgments": judgments}, input_tokens=5, output_tokens=5
        )

    bindings: dict[str, list[ModelBinding]] = {
        "blind_panel": _blind_panel_bindings(),
        "challenger": [ModelBinding(provider="anthropic", model="fake-challenger")],
        "test_chooser": [ModelBinding(provider="anthropic", model="fake-test-chooser")],
        "entailment_verifier": [ModelBinding(provider="featherless", model="fake-verifier")],
    }
    providers = {
        "anthropic": AnthropicProvider(api_key=None, transport=_happy_path_transport(calls)),
        "featherless": OpenAIProvider(api_key=None, transport=entailment_transport),
    }
    client = LlmClient(bindings, providers)

    report = run_weekly_review(repo, db, client, clock=_fixed_clock)

    # Picked up exactly once - the queue is empty again.
    assert json.loads(deferred_path.read_text(encoding="utf-8")) == []

    assert report.deferred_verification.checked == 1
    assert len(report.deferred_verification.not_entailed) == 1
    finding = report.deferred_verification.not_entailed[0]
    assert finding.hypothesis_id == PE_ID
    assert finding.claim == "CRP was sky-high, indicating severe inflammation"

    markdown = (repo.root / report.markdown_path).read_text(encoding="utf-8")
    assert "Deferred evidence checks" in markdown
    assert "CRP was sky-high" in markdown

    # Nothing left to check the second time - never re-verified, never
    # re-charged a model call, and the ledger evidence itself is untouched
    # (this codebase's schema has no evidence-removal op; the finding is
    # surfaced for a human to act on, not auto-stripped from an already-
    # applied diff).
    second_sweep = sweep_deferred_entailment_claims(client, repo, db)
    assert second_sweep == DeferredVerificationSweepResult()

    pe = next(h for h in report_ledger_hypotheses(repo) if h.id == PE_ID)
    assert pe.evidence_for == []  # never applied to the ledger in the first place


# --- negative test: blind-panel blindness contract ---------------------------------------------


def test_blind_panel_precondition_fires_if_ledger_sneaks_into_context(
    repo: DataRepo, db: LabsDb
) -> None:
    """`forbid_context_key("ledger")`'s original, narrower purpose: a
    `"ledger"` key smuggled directly into the run context (as opposed to a
    context pack that itself carries a ledger section — see the
    content-aware `edge_payload_lacks_section` regression test below) must
    still trip it."""
    _seed_ledger(repo)

    def _explode(request: TransportRequest) -> TransportResponse:
        raise AssertionError("transport must never be called once the precondition fails")

    client = _build_client(_explode)
    dag = build_review_dag(client, repo, db, repo.root / LEDGER_RELPATH, clock=_fixed_clock)
    blind_context_pack = build_context(repo, db, include_ledger=False)
    smuggled_ledger = Ledger(version=99, updated=_fixed_clock(), hypotheses=[])

    with pytest.raises(ContractViolation) as excinfo:
        dag_run(
            dag,
            {
                "initial": Marker(),
                "blind_context_pack": blind_context_pack,
                "ledger": smuggled_ledger,  # must never be present at this point
            },
        )

    assert excinfo.value.contract_name == "forbid_context_key:ledger"
    assert excinfo.value.node == "blind_panel_0"


def test_blind_context_pack_never_includes_the_ledger(repo: DataRepo, db: LabsDb) -> None:
    _seed_ledger(repo)
    pack = build_context(repo, db, include_ledger=False)
    assert "ledger" not in pack.keys
    assert "Systemic lupus erythematosus" not in pack.render()


def test_blind_panel_content_aware_precondition_fires_when_the_pack_itself_leaks_the_ledger(
    repo: DataRepo, db: LabsDb
) -> None:
    """S2 regression: `forbid_context_key("ledger")` only ever inspected the
    run-context DICT's keys, never the blind-panel node's actual input
    payload — the panel's `ContextPack` lives under the `blind_context_pack`
    run-context key, so a real regression where that pack was built with
    `include_ledger=True` would sail past `forbid_context_key` untouched
    (the run context never gets a `"ledger"` entry in this DAG at all).
    `edge_payload_lacks_section("ledger")` inspects the payload's own
    `.keys` directly and must catch this."""
    _seed_ledger(repo)

    def _explode(request: TransportRequest) -> TransportResponse:
        raise AssertionError("transport must never be called once the precondition fails")

    client = _build_client(_explode)
    dag = build_review_dag(client, repo, db, repo.root / LEDGER_RELPATH, clock=_fixed_clock)
    # The actual regression this guards against: build_context is called
    # with include_ledger=True for what should have been the BLIND panel's
    # pack, and handed to run() under the correct "blind_context_pack" key
    # (no "ledger" key is smuggled into the run context at all).
    leaky_context_pack = build_context(repo, db, include_ledger=True)

    with pytest.raises(ContractViolation) as excinfo:
        dag_run(
            dag,
            {
                "initial": Marker(),
                "blind_context_pack": leaky_context_pack,
            },
        )

    assert excinfo.value.contract_name == "edge_payload_lacks_section:ledger"
    assert excinfo.value.node == "blind_panel_0"


# --- negative tests: completeness postconditions ------------------------------------------------


def test_adjudication_completeness_contract_fires_on_missing_decision(
    repo: DataRepo, db: LabsDb
) -> None:
    _seed_ledger(repo)
    calls: list[TransportRequest] = []
    base_transport = _happy_path_transport(calls)

    def transport(request: TransportRequest) -> TransportResponse:
        response = base_transport(request)
        assert request.schema is not None
        if request.schema.__name__ == "AdjudicationPayload":
            payload: dict[str, Any] = json.loads(
                json.dumps({"decisions": response.tool_input["decisions"][:-1]})  # type: ignore[index]
            )
            return TransportResponse(text="", tool_input=payload, input_tokens=1, output_tokens=1)
        return response

    client = _build_client(transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_weekly_review(repo, db, client, clock=_fixed_clock)

    assert excinfo.value.contract_name == "adjudication_covers_every_divergence"


def test_challenge_sweep_completeness_contract_fires_on_missing_note(
    repo: DataRepo, db: LabsDb
) -> None:
    _seed_ledger(repo)
    calls: list[TransportRequest] = []
    base_transport = _happy_path_transport(calls)

    def transport(request: TransportRequest) -> TransportResponse:
        response = base_transport(request)
        assert request.schema is not None
        if request.schema.__name__ == "ChallengeSweepPayload":
            return TransportResponse(
                text="", tool_input={"notes": []}, input_tokens=1, output_tokens=1
            )
        return response

    client = _build_client(transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_weekly_review(repo, db, client, clock=_fixed_clock)

    assert excinfo.value.contract_name == "challenge_sweep_covers_every_active_hypothesis"


# --- S4 remediation: completeness postconditions require SUBSTANCE, not just non-empty --------


def test_adjudication_completeness_contract_fires_on_a_too_short_rationale(
    repo: DataRepo, db: LabsDb
) -> None:
    """A decision covering every divergence used to be enough, even if the
    rationale was a placeholder like "."; the contract must now require at
    least `MIN_SUBSTANTIVE_LENGTH` characters after stripping."""
    _seed_ledger(repo)
    calls: list[TransportRequest] = []
    base_transport = _happy_path_transport(calls)

    def transport(request: TransportRequest) -> TransportResponse:
        response = base_transport(request)
        assert request.schema is not None
        if request.schema.__name__ == "AdjudicationPayload":
            decisions = json.loads(json.dumps(response.tool_input["decisions"]))  # type: ignore[index]
            decisions[0]["rationale"] = "."
            return TransportResponse(
                text="", tool_input={"decisions": decisions}, input_tokens=1, output_tokens=1
            )
        return response

    client = _build_client(transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_weekly_review(repo, db, client, clock=_fixed_clock)

    assert excinfo.value.contract_name == "adjudication_covers_every_divergence"
    assert "too short" in excinfo.value.message


def test_adjudication_completeness_contract_fires_on_identical_rationale_everywhere(
    repo: DataRepo, db: LabsDb
) -> None:
    """A model stamping the same sentence across every divergence is not a
    substantive, per-divergence adjudication, even if that sentence is long
    enough on its own."""
    _seed_ledger(repo)
    calls: list[TransportRequest] = []
    base_transport = _happy_path_transport(calls)
    stamped = "Reviewed and no change is warranted at this time."

    def transport(request: TransportRequest) -> TransportResponse:
        response = base_transport(request)
        assert request.schema is not None
        if request.schema.__name__ == "AdjudicationPayload":
            decisions = json.loads(json.dumps(response.tool_input["decisions"]))  # type: ignore[index]
            for decision in decisions:
                decision["rationale"] = stamped
            return TransportResponse(
                text="", tool_input={"decisions": decisions}, input_tokens=1, output_tokens=1
            )
        return response

    client = _build_client(transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_weekly_review(repo, db, client, clock=_fixed_clock)

    assert excinfo.value.contract_name == "adjudication_covers_every_divergence"
    assert "identical" in excinfo.value.message


def test_challenge_sweep_completeness_contract_fires_on_a_too_short_note(
    repo: DataRepo, db: LabsDb
) -> None:
    _seed_ledger(repo)
    calls: list[TransportRequest] = []
    base_transport = _happy_path_transport(calls)

    def transport(request: TransportRequest) -> TransportResponse:
        response = base_transport(request)
        assert request.schema is not None
        if request.schema.__name__ == "ChallengeSweepPayload":
            notes = json.loads(json.dumps(response.tool_input["notes"]))  # type: ignore[index]
            notes[0]["note"] = "reviewed"
            return TransportResponse(
                text="", tool_input={"notes": notes}, input_tokens=1, output_tokens=1
            )
        return response

    client = _build_client(transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_weekly_review(repo, db, client, clock=_fixed_clock)

    assert excinfo.value.contract_name == "challenge_sweep_covers_every_active_hypothesis"
    assert "too short" in excinfo.value.message


def test_challenge_sweep_completeness_contract_fires_on_identical_notes_everywhere(
    repo: DataRepo, db: LabsDb
) -> None:
    """A model stamping the same note across every active hypothesis is not
    a substantive, per-hypothesis challenge sweep."""
    _seed_ledger(repo)
    calls: list[TransportRequest] = []
    base_transport = _happy_path_transport(calls)
    stamped = "Reviewed again this week; nothing has changed."

    def transport(request: TransportRequest) -> TransportResponse:
        response = base_transport(request)
        assert request.schema is not None
        if request.schema.__name__ == "ChallengeSweepPayload":
            notes = json.loads(json.dumps(response.tool_input["notes"]))  # type: ignore[index]
            for note in notes:
                note["note"] = stamped
            return TransportResponse(
                text="", tool_input={"notes": notes}, input_tokens=1, output_tokens=1
            )
        return response

    client = _build_client(transport)

    with pytest.raises(ContractViolation) as excinfo:
        run_weekly_review(repo, db, client, clock=_fixed_clock)

    assert excinfo.value.contract_name == "challenge_sweep_covers_every_active_hypothesis"
    assert "identical" in excinfo.value.message


# --- deterministic metrics, from synthetic fixtures (no DAG, no client) ------------------------


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _history_record(
    *, dag_node: str, model_id: str, prompt_version: str, ops: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "resulting_version": 1,
        "resulting_updated": "2026-08-01T00:00:00+00:00",
        "diff": {
            "provenance": {
                "app_version": "test",
                "prompt_template_version": prompt_version,
                "model_id": model_id,
                "dag_node": dag_node,
                "timestamp": "2026-08-01T00:00:00+00:00",
            },
            "rationale": "synthetic",
            "ops": ops,
        },
    }


def _hyp(id_: str, *, status: str = "active", tier: str = "expanded") -> Hypothesis:
    return Hypothesis(
        id=id_,
        name=id_,
        tier=tier,  # type: ignore[arg-type]
        probability="moderate",
        status=status,  # type: ignore[arg-type]
        origin="model",
        first_proposed=date(2026, 1, 1),
    )


def test_scan_staleness_flags_hypotheses_two_or_more_generations_behind(tmp_path: Path) -> None:
    history_path = tmp_path / "ledger-history.jsonl"
    _write_jsonl(
        history_path,
        [
            _history_record(
                dag_node="ledger_maintainer",
                model_id="model-A",
                prompt_version="p@v1",
                ops=[{"op": "add_hypothesis", "hypothesis": {"id": "h1"}}],
            ),
            _history_record(
                dag_node="ledger_maintainer",
                model_id="model-B",
                prompt_version="p@v1",
                ops=[{"op": "update_hypothesis", "id": "h2"}],
            ),
            _history_record(
                dag_node="ledger_maintainer",
                model_id="model-C",
                prompt_version="p@v1",
                ops=[{"op": "update_hypothesis", "id": "h3"}],
            ),
        ],
    )
    ledger = Ledger(
        version=3,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[_hyp("h1"), _hyp("h2"), _hyp("h3")],
    )

    report = scan_staleness(history_path, ledger)

    stale_ids = {s.hypothesis_id for s in report.stale}
    assert stale_ids == {"h1"}
    assert report.stale[0].generations_behind == 2


def test_scan_staleness_ignores_inactive_hypotheses(tmp_path: Path) -> None:
    history_path = tmp_path / "ledger-history.jsonl"
    _write_jsonl(
        history_path,
        [
            _history_record(
                dag_node="n",
                model_id="A",
                prompt_version="p@v1",
                ops=[{"op": "add_hypothesis", "hypothesis": {"id": "h1"}}],
            ),
            _history_record(
                dag_node="n",
                model_id="B",
                prompt_version="p@v1",
                ops=[{"op": "update_hypothesis", "id": "h2"}],
            ),
            _history_record(
                dag_node="n",
                model_id="C",
                prompt_version="p@v1",
                ops=[{"op": "update_hypothesis", "id": "h3"}],
            ),
        ],
    )
    ledger = Ledger(
        version=3,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[_hyp("h1", status="ruled-out")],
    )

    report = scan_staleness(history_path, ledger)
    assert report.stale == []


def test_parse_audit_costs_aggregates_by_role(tmp_path: Path) -> None:
    audit_path = tmp_path / "api-audit.jsonl"
    _write_jsonl(
        audit_path,
        [
            {"role": "challenger", "input_tokens": 100, "output_tokens": 50, "cost_estimate": 0.01},
            {"role": "challenger", "input_tokens": 200, "output_tokens": 60, "cost_estimate": 0.02},
            {
                "role": "primary_reasoner",
                "input_tokens": 300,
                "output_tokens": 70,
                "cost_estimate": 0.05,
            },
        ],
    )

    costs = parse_audit_costs(audit_path)
    by_role = {c.role: c for c in costs}
    assert by_role["challenger"].calls == 2
    assert by_role["challenger"].input_tokens == 300
    assert by_role["challenger"].cost_estimate == pytest.approx(0.03)
    assert by_role["primary_reasoner"].calls == 1


def test_parse_audit_costs_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_audit_costs(tmp_path / "does-not-exist.jsonl") == []


def test_ledger_churn_counts_tier_moves_in_recent_history(tmp_path: Path) -> None:
    history_path = tmp_path / "ledger-history.jsonl"
    _write_jsonl(
        history_path,
        [
            _history_record(
                dag_node="n",
                model_id="A",
                prompt_version="p@v1",
                ops=[{"op": "update_hypothesis", "id": "h1", "tier": "cant-miss"}],
            ),
            _history_record(
                dag_node="n",
                model_id="A",
                prompt_version="p@v1",
                ops=[{"op": "update_hypothesis", "id": "h2", "tier": None}],
            ),
        ],
    )
    assert ledger_churn(history_path) == 1


def test_challenger_kill_rate_none_when_nothing_ever_challenged() -> None:
    ledger = Ledger(version=1, updated=datetime(2026, 1, 1, tzinfo=UTC), hypotheses=[_hyp("h1")])
    assert challenger_kill_rate(ledger) is None


def test_challenger_kill_rate_computed_from_ever_challenged_hypotheses() -> None:
    challenged_and_ruled_out = Hypothesis(
        id="h1",
        name="h1",
        tier="expanded",
        probability="low",
        status="ruled-out",
        origin="model",
        first_proposed=date(2026, 1, 1),
        challenger_notes="attacked and it fell apart",
    )
    challenged_and_still_active = Hypothesis(
        id="h2",
        name="h2",
        tier="expanded",
        probability="moderate",
        status="active",
        origin="model",
        first_proposed=date(2026, 1, 1),
        challenger_notes="attacked, held up",
    )
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 1, 1, tzinfo=UTC),
        hypotheses=[challenged_and_ruled_out, challenged_and_still_active],
    )
    assert challenger_kill_rate(ledger) == pytest.approx(0.5)


def test_hypothesis_ages_days_only_counts_active() -> None:
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 1, 1, tzinfo=UTC),
        hypotheses=[_hyp("h1"), _hyp("h2", status="ruled-out")],
    )
    ages = hypothesis_ages_days(ledger, today=date(2026, 1, 11))
    assert ages == {"h1": 10}


# --- render_review_markdown gates model-written text (Violation 2 regression) -----------------
#
# `render_review_markdown` interpolates the Challenger/divergence
# adjudicator's rationale and the Test-Chooser's items directly — none of
# that text flows through the Composer's gated path, so nothing screened
# it before this fix. `web/routes/reviews.py`'s `reviews_detail` gates the
# read path too (`tests/test_web_reviews.py`); this test proves the write
# path (this function) is clean at rest as well.


def test_render_review_markdown_redacts_dosing_language() -> None:
    empty_ledger = Ledger(version=1, updated=datetime(2026, 1, 1, tzinfo=UTC), hypotheses=[])
    divergence = Divergence(
        id="panel-only:lupus",
        kind="panel_only",
        name="Lupus",
        panel_probability_bucket="moderate",
        panel_cant_miss=False,
    )
    adjudication = AdjudicationResult(
        decisions=[
            DivergenceDecisionPayload(
                divergence="panel-only:lupus",
                decision="accept",
                rationale="You should take 20 mg prednisone daily to confirm this diagnosis.",
            )
        ]
    )
    markdown = render_review_markdown(
        review_date=date(2026, 1, 7),
        trend_findings=[],
        divergence_set=DivergenceSet(divergences=[divergence]),
        adjudication=adjudication,
        challenge_sweep=ChallengeSweepResult(notes=[]),
        test_chooser=TestChooserResult(
            items=[TestChooserItem(text="Start taking 500 mg metformin twice daily")]
        ),
        staleness=StalenessReport(),
        deferred_verification=DeferredVerificationSweepResult(),
        metrics=OpsMetrics(),
        ledger_before=empty_ledger,
        ledger_after=empty_ledger,
    )

    assert "20 mg prednisone" not in markdown
    assert "500 mg metformin" not in markdown
    assert "withheld" in markdown.lower()
    # Surrounding content survives.
    assert "Lupus" in markdown
    assert "to confirm this diagnosis" in markdown


def test_render_review_markdown_shows_trigger_summary_when_given() -> None:
    empty_ledger = Ledger(version=1, updated=datetime(2026, 1, 1, tzinfo=UTC), hypotheses=[])
    markdown = render_review_markdown(
        review_date=date(2026, 1, 7),
        trend_findings=[],
        divergence_set=DivergenceSet(divergences=[]),
        adjudication=AdjudicationResult(decisions=[]),
        challenge_sweep=ChallengeSweepResult(notes=[]),
        test_chooser=TestChooserResult(items=[]),
        staleness=StalenessReport(),
        deferred_verification=DeferredVerificationSweepResult(),
        metrics=OpsMetrics(),
        ledger_before=empty_ledger,
        ledger_after=empty_ledger,
        trigger_summary="ingest: 2 new document(s), 5 new lab row(s)",
    )

    assert "Why this review ran" in markdown
    assert "ingest: 2 new document(s), 5 new lab row(s)" in markdown


def test_render_review_markdown_omits_trigger_line_when_not_given() -> None:
    empty_ledger = Ledger(version=1, updated=datetime(2026, 1, 1, tzinfo=UTC), hypotheses=[])
    markdown = render_review_markdown(
        review_date=date(2026, 1, 7),
        trend_findings=[],
        divergence_set=DivergenceSet(divergences=[]),
        adjudication=AdjudicationResult(decisions=[]),
        challenge_sweep=ChallengeSweepResult(notes=[]),
        test_chooser=TestChooserResult(items=[]),
        staleness=StalenessReport(),
        deferred_verification=DeferredVerificationSweepResult(),
        metrics=OpsMetrics(),
        ledger_before=empty_ledger,
        ledger_after=empty_ledger,
    )

    assert "Why this review ran" not in markdown


# --------------------------------------------------------------------------
# Event-triggered review (docs/adr/0019-event-triggered-review.md):
# should_run_full_review (pure decision function) + run_review_tick.
# --------------------------------------------------------------------------


def _marker(*reasons: str, at: datetime) -> ReviewMarker:
    return ReviewMarker(reasons=[ReviewMarkerReason(reason=r, at=at) for r in reasons])


def test_should_run_full_review_when_no_full_review_has_ever_run() -> None:
    should_run, reason = should_run_full_review(
        marker=None, last_full_review_at=None, now=datetime(2026, 8, 23, tzinfo=UTC)
    )
    assert should_run is True
    assert "no full review has ever run" in reason


def test_should_run_full_review_marker_set_and_cooldown_elapsed() -> None:
    last = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    now = last + FULL_REVIEW_COOLDOWN
    marker = _marker("ingest: 1 new document(s), 0 new lab row(s)", at=last)

    should_run, reason = should_run_full_review(marker=marker, last_full_review_at=last, now=now)

    assert should_run is True
    assert "cooldown" in reason


def test_should_run_full_review_marker_set_but_cooldown_not_elapsed() -> None:
    last = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    now = last + FULL_REVIEW_COOLDOWN - timedelta(minutes=1)
    marker = _marker("chat turn applied a ledger diff (1 op(s))", at=last)

    should_run, reason = should_run_full_review(marker=marker, last_full_review_at=last, now=now)

    assert should_run is False
    assert "cooldown" in reason


def test_should_run_full_review_no_marker_but_floor_elapsed() -> None:
    last = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    now = last + FULL_REVIEW_FLOOR

    should_run, reason = should_run_full_review(marker=None, last_full_review_at=last, now=now)

    assert should_run is True
    assert "floor" in reason


def test_should_run_full_review_no_marker_and_floor_not_elapsed() -> None:
    last = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    now = last + FULL_REVIEW_FLOOR - timedelta(days=1)

    should_run, reason = should_run_full_review(marker=None, last_full_review_at=last, now=now)

    assert should_run is False
    assert "floor" in reason


def _empty_review_client() -> LlmClient:
    """A fake client that answers every review-DAG role with an empty
    result — valid against an empty ledger (no active hypotheses, so every
    completeness postcondition is vacuously satisfied). Mirrors
    `tests/test_cli.py`'s `_empty_review_fake_client`."""

    def transport(request: TransportRequest) -> TransportResponse:
        assert request.schema is not None
        name = request.schema.__name__
        tool_input: dict[str, Any]
        if name == "BlindDifferentialPayload":
            tool_input = {"items": []}
        elif name == "AdjudicationPayload":
            tool_input = {"decisions": []}
        elif name == "ChallengeSweepPayload":
            tool_input = {"notes": []}
        elif name == "TestChooserPayload":
            tool_input = {"items": []}
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema: {name}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=1, output_tokens=1)

    bindings: dict[str, list[ModelBinding]] = {
        "blind_panel": _blind_panel_bindings(),
        "challenger": [ModelBinding(provider="anthropic", model="fake-challenger")],
        "test_chooser": [ModelBinding(provider="anthropic", model="fake-test-chooser")],
    }
    providers = {"anthropic": AnthropicProvider(api_key=None, transport=transport)}
    return LlmClient(bindings, providers)


def test_run_review_tick_force_always_runs_full_review_ignoring_marker_and_cooldown(
    repo: DataRepo, db: LabsDb
) -> None:
    client = _empty_review_client()

    result = run_review_tick(
        repo,
        db,
        client,
        clock=_fixed_clock,
        force=True,
        last_full_review_lookup=lambda: _fixed_clock(),  # "just ran" - deep inside cooldown
    )

    assert result.ran_full_review is True
    assert "forced via `adoc review --force`" in result.decision_reason
    assert result.full_review is not None


def test_run_review_tick_suppresses_full_review_inside_cooldown_even_with_marker(
    repo: DataRepo, db: LabsDb
) -> None:
    client = _empty_review_client()
    last_full_review_at = _fixed_clock() - timedelta(hours=1)
    mark_review_wanted(repo, "ingest: 1 new document(s), 0 new lab row(s)", at=last_full_review_at)

    result = run_review_tick(
        repo,
        db,
        client,
        clock=_fixed_clock,
        last_full_review_lookup=lambda: last_full_review_at,
    )

    assert result.ran_full_review is False
    assert result.full_review is None
    assert "cooldown" in result.decision_reason
    # Cheap parts still ran.
    assert isinstance(result.trend_scan, type(result.trend_scan))
    # The marker survives an untaken tick.
    from adoc.reason.review_trigger import load_review_marker

    marker = load_review_marker(repo)
    assert marker is not None
    assert len(marker.reasons) == 1


def test_run_review_tick_runs_full_review_when_marker_set_and_cooldown_elapsed(
    repo: DataRepo, db: LabsDb
) -> None:
    client = _empty_review_client()
    last_full_review_at = _fixed_clock() - FULL_REVIEW_COOLDOWN
    mark_review_wanted(
        repo,
        "chat turn applied a ledger diff (1 op(s))",
        at=last_full_review_at + timedelta(minutes=1),
    )

    result = run_review_tick(
        repo,
        db,
        client,
        clock=_fixed_clock,
        last_full_review_lookup=lambda: last_full_review_at,
    )

    assert result.ran_full_review is True
    assert result.full_review is not None
    assert "cooldown" in result.decision_reason
    assert "chat turn applied a ledger diff" in result.decision_reason


def test_run_review_tick_runs_full_review_on_the_floor_with_no_marker_at_all(
    repo: DataRepo, db: LabsDb
) -> None:
    client = _empty_review_client()
    last_full_review_at = _fixed_clock() - FULL_REVIEW_FLOOR

    from adoc.reason.review_trigger import load_review_marker

    assert load_review_marker(repo) is None

    result = run_review_tick(
        repo,
        db,
        client,
        clock=_fixed_clock,
        last_full_review_lookup=lambda: last_full_review_at,
    )

    assert result.ran_full_review is True
    assert result.full_review is not None
    assert "floor" in result.decision_reason


def test_run_review_tick_runs_full_review_when_no_full_review_has_ever_run(
    repo: DataRepo, db: LabsDb
) -> None:
    """A brand new data repo, no marker, no prior review at all: the floor
    condition's degenerate case (`should_run_full_review`'s `last_full_
    review_at is None` branch) — reproduces the old weekly cron's guarantee
    that the very first review runs on schedule rather than waiting a
    further 7 days."""
    client = _empty_review_client()

    result = run_review_tick(
        repo, db, client, clock=_fixed_clock, last_full_review_lookup=lambda: None
    )

    assert result.ran_full_review is True
    assert "no full review has ever run" in result.decision_reason


def test_run_review_tick_marker_survives_a_failed_full_review(repo: DataRepo, db: LabsDb) -> None:
    """A client missing the `blind_panel` role binding makes `build_review_
    dag` raise before a single node runs (`ValueError`) — the cleanest
    possible "full review failed" case, with zero side effects to
    disentangle from the marker-survival assertion."""
    broken_client = LlmClient({}, {})
    last_full_review_at = _fixed_clock() - FULL_REVIEW_FLOOR
    mark_review_wanted(repo, "ingest: 1 new document(s), 0 new lab row(s)")

    from adoc.reason.review_trigger import load_review_marker

    with pytest.raises(ValueError, match="blind_panel"):
        run_review_tick(
            repo,
            db,
            broken_client,
            clock=_fixed_clock,
            last_full_review_lookup=lambda: last_full_review_at,
        )

    marker = load_review_marker(repo)
    assert marker is not None
    assert len(marker.reasons) == 1


def test_run_review_tick_marker_is_cleared_after_a_successful_full_review(
    repo: DataRepo, db: LabsDb
) -> None:
    client = _empty_review_client()
    last_full_review_at = _fixed_clock() - FULL_REVIEW_FLOOR
    mark_review_wanted(repo, "ingest: 1 new document(s), 0 new lab row(s)")

    from adoc.reason.review_trigger import load_review_marker

    result = run_review_tick(
        repo,
        db,
        client,
        clock=_fixed_clock,
        last_full_review_lookup=lambda: last_full_review_at,
    )

    assert result.ran_full_review is True
    assert load_review_marker(repo) is None


def test_run_review_tick_cheap_parts_run_every_tick_when_full_review_is_suppressed(
    repo: DataRepo, db: LabsDb
) -> None:
    """Trend scan and the deferred-entailment sweep run even when no full
    review is due this tick — cheap, deterministic, no frontier-model call."""
    client = _empty_review_client()
    last_full_review_at = _fixed_clock() - timedelta(hours=1)

    result = run_review_tick(
        repo,
        db,
        client,
        clock=_fixed_clock,
        last_full_review_lookup=lambda: last_full_review_at,
    )

    assert result.ran_full_review is False
    # A real (deterministic, no-LLM) trend scan ran, not a stub.
    assert result.trend_scan.findings == []
    assert result.deferred_verification.checked == 0


def test_run_review_tick_full_review_report_carries_the_trigger_summary_into_the_markdown(
    repo: DataRepo, db: LabsDb
) -> None:
    client = _empty_review_client()
    last_full_review_at = _fixed_clock() - FULL_REVIEW_COOLDOWN
    mark_review_wanted(
        repo,
        "ingest: 3 new document(s), 9 new lab row(s)",
        at=last_full_review_at + timedelta(seconds=1),
    )

    result = run_review_tick(
        repo,
        db,
        client,
        clock=_fixed_clock,
        last_full_review_lookup=lambda: last_full_review_at,
    )

    assert result.full_review is not None
    assert result.full_review.trigger_summary == result.decision_reason
    markdown = (repo.root / result.full_review.markdown_path).read_text(encoding="utf-8")
    assert "Why this review ran" in markdown
    assert "ingest: 3 new document(s), 9 new lab row(s)" in markdown


# --- adjudication ids: the contract checks coverage, not transcription ------------------


def _div(did: str, name: str) -> Divergence:
    return Divergence(id=did, kind="panel_only", name=name)


def _decision(divergence: str) -> DivergenceDecisionPayload:
    return DivergenceDecisionPayload(divergence=divergence, decision="accept", rationale="r" * 60)


REAL_ID = "panel-only:prematureovarianinsufficiencymenopauseautoimmuneoophoritissubtype"
REAL_NAME = "Premature ovarian insufficiency / menopause (autoimmune oophoritis subtype)"


@pytest.mark.parametrize(
    "echoed",
    [
        REAL_ID,
        "panel-only:premature-ovarian-insufficiency-menopause-autoimmune-oophoritis-subtype",
        REAL_NAME,
        "premature ovarian insufficiency menopause autoimmune oophoritis subtype",
    ],
)
def test_a_decision_resolves_however_the_model_spelled_the_id(echoed: str) -> None:
    """A divergence id is a generated slug — a 62-character unbroken run for
    this real case. Requiring the model to echo it character-for-character
    cost a live scheduled review, which had adjudicated the divergence and
    written a substantive rationale."""
    divergences = [_div(REAL_ID, REAL_NAME), _div("panel-only:sle", "SLE")]

    resolved = resolve_adjudication_decisions(divergences, [_decision(echoed)])

    assert REAL_ID in resolved


def test_an_unrelated_string_still_fails_to_resolve() -> None:
    """The contract must keep its teeth: loose matching is for spelling, not
    for letting an unadjudicated divergence through."""
    divergences = [_div(REAL_ID, REAL_NAME)]

    resolved = resolve_adjudication_decisions(divergences, [_decision("something else")])

    assert resolved == {}


def test_an_ambiguous_key_is_never_guessed() -> None:
    """Two divergences whose names normalize identically must not have a
    rationale silently attached to whichever came first — attaching a
    human's reasoning to the wrong hypothesis is worse than failing."""
    divergences = [_div("panel-only:a", "Sjogren's"), _div("panel-only:b", "Sjogrens")]

    resolved = resolve_adjudication_decisions(divergences, [_decision("sjogrens")])

    assert resolved == {}


# --- panel citations reach the ledger (or are dropped, never invented) ------------------


def _panel_item(name: str, *, refs: list[str]) -> BlindDifferentialItem:
    return BlindDifferentialItem(
        name=name,
        probability_bucket="moderate",
        why="Short reasoning for the patient.",
        evidence=[BlindEvidenceItem(claim=f"{name} support", source=r) for r in refs],
    )


def test_pooled_panel_evidence_is_deduped_across_members() -> None:
    """Panel members work independently, so two of them citing the same row
    for the same claim is AGREEMENT, not two pieces of evidence — pooling
    without dedup would inflate support purely by panel size."""
    a = _panel_item("Hashimoto thyroiditis", refs=["labs:tsh:2026-05-02"])
    b = _panel_item("Hashimoto thyroiditis", refs=["labs:tsh:2026-05-02"])

    pooled = _pool_evidence([a, b])

    assert len(pooled) == 1


def test_a_resolvable_citation_reaches_the_ledger(db: LabsDb, repo: DataRepo) -> None:
    """Production carried 24 hypotheses with ZERO evidence items, so every
    card in the UI rendered an empty evidence section."""
    db.upsert_document(
        LabDocument(
            sha256="a" * 64,
            filename="quest.pdf",
            doc_type="lab-result",
            page_count=1,
            ingested_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
            status=DocumentStatus.COMPLETE,
        )
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="CRP",
                name_raw="CRP",
                value=8.5,
                ucum_unit="mg/L",
                source_doc="a" * 64,
                raw_json="{}",
            )
        ]
    )
    divergence = Divergence(
        id="panel-only:x",
        kind="panel_only",
        name="Inflammatory process",
        panel_evidence=[BlindEvidenceItem(claim="CRP is elevated", source="labs:crp:2026-05-02")],
    )

    kept = _resolvable_evidence(divergence, db, repo)

    assert [e.source for e in kept] == ["labs:crp:2026-05-02"]


def test_an_unresolvable_citation_is_dropped_not_written(db: LabsDb, repo: DataRepo) -> None:
    """The review path has no citation-check DAG contract of its own, so a
    ref resolving to nothing would become an uncheckable ledger entry —
    exactly the fabrication Phase 2 exists to prevent. Dropped and logged,
    rather than failing a 12-minute review (ADR 0016's posture)."""
    divergence = Divergence(
        id="panel-only:y",
        kind="panel_only",
        name="Invented finding",
        panel_evidence=[
            BlindEvidenceItem(claim="Never measured", source="labs:unicorn-factor:2026-05-02")
        ],
    )

    assert _resolvable_evidence(divergence, db, repo) == []


def test_a_malformed_ref_costs_the_citation_not_the_review(
    db: LabsDb, repo: DataRepo, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed ref parses at the schema boundary and is dropped by the
    filter — it must NEVER fail the payload (ADR 0028).

    This pin is the inverse of the one it replaced. The earlier version
    validated `BlindEvidenceItem.source` in a field validator, which raises;
    on the first real review the panel guessed four prefixes wrong and the
    resulting ValidationError destroyed a 14-node, 12-minute run. The
    property worth pinning is not "bad refs are rejected early" but "bad refs
    never reach the ledger AND never take the review down with them".
    """
    payload = BlindDifferentialPayload.model_validate(
        {
            "items": [
                {
                    "name": "Mononucleosis",
                    "probability_bucket": "low",
                    "why": "Cited with an invented prefix.",
                    "evidence": [
                        {
                            "claim": "Monospot positive",
                            # Verbatim from the failed prod run: well-formed in
                            # shape, invented in prefix, real analyte and date.
                            "source": "other:monospot_(heterophile)_screen:2026-03-17",
                        }
                    ],
                }
            ]
        }
    )
    assert payload.items[0].name == "Mononucleosis"

    divergence = Divergence(
        id="panel-only:mono",
        kind="panel_only",
        name="Mononucleosis",
        panel_evidence=list(payload.items[0].evidence),
    )
    with caplog.at_level(logging.WARNING):
        assert _resolvable_evidence(divergence, db, repo) == []
    assert "malformed panel citation" in caplog.text


def _seed_crp_row(db: LabsDb) -> None:
    db.upsert_document(
        LabDocument(
            sha256="c" * 64,
            filename="crp.pdf",
            doc_type="lab-result",
            page_count=1,
            ingested_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
            status=DocumentStatus.COMPLETE,
        )
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="CRP",
                name_raw="CRP",
                value=8.5,
                ucum_unit="mg/L",
                source_doc="c" * 64,
                raw_json="{}",
            )
        ]
    )


def test_panel_citations_survive_agreement_with_the_ledger(db: LabsDb, repo: DataRepo) -> None:
    """When the panel AGREES with a ledger hypothesis, its citations must
    still reach that hypothesis.

    A divergence exists only where panel and ledger disagree, so citations
    used to survive exclusively on disagreement — which inverted the intent.
    The hypotheses both the ledger and an independent blind panel endorse are
    the best-supported in the case, and they were the ones left uncited: 24 of
    25 in prod had an empty evidence list, which is what the patient's
    hypothesis cards render.
    """
    _seed_crp_row(db)
    ledger = _seed_ledger(repo)
    # Same name AND same probability bucket as the seeded SLE hypothesis:
    # deliberate agreement, so no divergence is produced for it at all.
    panels = [
        BlindDifferential(
            panel_index=0,
            items=[
                BlindDifferentialItem(
                    name="Systemic lupus erythematosus",
                    probability_bucket="moderate",
                    why="agrees with the ledger",
                    evidence=[
                        BlindEvidenceItem(claim="CRP is elevated", source="labs:crp:2026-05-02")
                    ],
                )
            ],
        )
    ]

    divergence_set = compute_divergences(ledger, panels)
    assert not [d for d in divergence_set.divergences if d.ledger_hypothesis_id == SLE_ID], (
        "agreement must not produce a divergence — that is the premise of this test"
    )
    assert divergence_set.panel_citations[SLE_ID][0].source == "labs:crp:2026-05-02"

    diff = build_review_ledger_diff(
        ledger,
        divergence_set,
        AdjudicationResult(decisions=[]),
        ChallengeSweepResult(notes=[]),
        today=date(2026, 8, 27),
        db=db,
        repo=repo,
    )
    added = [op for op in diff.ops if isinstance(op, AddEvidence)]
    assert [(op.id, op.evidence.source) for op in added] == [(SLE_ID, "labs:crp:2026-05-02")]


def test_a_panel_citation_is_not_re_added_every_week(db: LabsDb, repo: DataRepo) -> None:
    """`apply_diff` appends AddEvidence blindly, so the diff builder must
    dedup against what the hypothesis already carries — otherwise every
    weekly review re-adds the same citation and the card grows without end."""
    _seed_crp_row(db)
    ledger = _seed_ledger(repo)
    evidence = Evidence(claim="CRP is elevated", source="labs:crp:2026-05-02", strength="moderate")
    for hypothesis in ledger.hypotheses:
        if hypothesis.id == SLE_ID:
            hypothesis.evidence_for.append(evidence)

    divergence_set = DivergenceSet(
        divergences=[],
        panel_citations={
            SLE_ID: [
                # Same claim/source, differently cased and padded — dedup is on
                # the normalized claim, not on bytes.
                BlindEvidenceItem(claim="  crp is ELEVATED  ", source="labs:crp:2026-05-02")
            ]
        },
    )

    diff = build_review_ledger_diff(
        ledger,
        divergence_set,
        AdjudicationResult(decisions=[]),
        ChallengeSweepResult(notes=[]),
        today=date(2026, 8, 27),
        db=db,
        repo=repo,
    )
    assert [op for op in diff.ops if isinstance(op, AddEvidence)] == []


def test_questions_open_separates_what_the_patient_can_answer_herself() -> None:
    """The list was telling the patient to ask her doctor which supplements
    she takes and whether she has bloating.

    Those are not appointment items — the system can simply ask her, which is
    what the conversation exists for. Delegating them wastes the appointment
    AND wastes the intake. They render in their own section, first, because
    several of them decide whether the doctor items are needed at all.
    """
    ledger = None  # the renderer tolerates an absent ledger
    payload = TestChooserPayload(
        items=[
            TestChooserItem(
                panel="Supplement and medication review",
                ask="Bring every supplement bottle to your next visit.",
                audience="you",
                hypothesis_ids=[SLE_ID],
            ),
            TestChooserItem(
                panel="Celiac screen: tTG-IgA + total IgA",
                ask="Ask for a coeliac blood screen.",
                audience="doctor",
                hypothesis_ids=[SLE_ID, PE_ID],
            ),
        ]
    )

    markdown = _render_questions_open(payload, ledger)

    assert "Questions you can answer yourself" in markdown
    assert "To raise with your doctor" in markdown
    assert markdown.index("answer yourself") < markdown.index("raise with your doctor")
    assert "**Supplement and medication review**" in markdown
    assert "**Celiac screen: tTG-IgA + total IgA**" in markdown


def test_an_item_lists_every_hypothesis_it_bears_on(repo: DataRepo) -> None:
    """One test routinely serves several hypotheses; collapsing that to one
    reference hides why the test is worth doing."""
    ledger = _seed_ledger(repo)
    payload = TestChooserPayload(
        items=[
            TestChooserItem(
                panel="Complement C3/C4",
                ask="Ask for a complement panel.",
                audience="doctor",
                hypothesis_ids=[SLE_ID, PE_ID],
            )
        ]
    )

    markdown = _render_questions_open(payload, ledger)

    assert "Systemic lupus erythematosus" in markdown
    assert "Pulmonary embolism" in markdown


def test_a_legacy_free_text_item_still_parses(caplog: pytest.LogCaptureFixture) -> None:
    """An in-flight review that predates the restructure must not fail.
    ADR 0028's rule: no single field of one item may fail a payload."""
    payload = TestChooserPayload.model_validate(
        {"items": [{"text": "Ask about a coeliac screen. It ties several findings together."}]}
    )
    item = payload.items[0]

    assert item.panel == "Ask about a coeliac screen"
    assert item.ask.startswith("Ask about a coeliac screen")
    assert item.audience == "doctor"


def test_an_unknown_audience_degrades_to_doctor() -> None:
    payload = TestChooserPayload.model_validate(
        {"items": [{"panel": "X", "ask": "y", "audience": "nonsense"}]}
    )

    assert payload.items[0].audience == "doctor"
    assert (
        TestChooserPayload.model_validate(
            {"items": [{"panel": "X", "ask": "y", "audience": "patient"}]}
        )
        .items[0]
        .audience
        == "you"
    )


def test_the_sweep_backfills_a_missing_plain_language_gloss(db: LabsDb, repo: DataRepo) -> None:
    """A hypothesis name is not communication. The sweep is the one stage that
    visits every active hypothesis, so it backfills glosses for hypotheses
    created before the field existed — no separate command, no extra call."""
    ledger = _seed_ledger(repo)
    assert all(not h.plain_language for h in ledger.hypotheses)

    diff = build_review_ledger_diff(
        ledger,
        DivergenceSet(divergences=[]),
        AdjudicationResult(decisions=[]),
        ChallengeSweepResult(
            notes=[
                HypothesisChallengeNote(
                    id=SLE_ID,
                    note="Reviewed again: no new contradicting evidence this week.",
                    plain_language="An autoimmune condition in which the immune system "
                    "attacks the body's own tissues, often affecting skin, joints and "
                    "kidneys.",
                )
            ]
        ),
        today=date(2026, 8, 27),
        db=db,
        repo=repo,
    )

    updates = [op for op in diff.ops if isinstance(op, UpdateHypothesis)]
    assert [(op.id, bool(op.plain_language)) for op in updates] == [(SLE_ID, True)]


def test_an_existing_gloss_is_not_rewritten(db: LabsDb, repo: DataRepo) -> None:
    """A definition does not need rewriting every week, and churning it would
    make the ledger diff noisy for no gain."""
    ledger = _seed_ledger(repo)
    for hypothesis in ledger.hypotheses:
        hypothesis.plain_language = "Already glossed."

    diff = build_review_ledger_diff(
        ledger,
        DivergenceSet(divergences=[]),
        AdjudicationResult(decisions=[]),
        ChallengeSweepResult(
            notes=[
                HypothesisChallengeNote(
                    id=SLE_ID, note="Reviewed again: stable.", plain_language="A new gloss."
                )
            ]
        ),
        today=date(2026, 8, 27),
        db=db,
        repo=repo,
    )

    assert not [op for op in diff.ops if isinstance(op, UpdateHypothesis) and op.plain_language]


def test_each_related_hypothesis_gets_its_own_linked_line(repo: DataRepo) -> None:
    """One test routinely serves several hypotheses. A comma-joined run of
    links reads as one undifferentiated blob — which is the problem this page
    has — so each gets its own line, linked to its ledger card."""
    ledger = _seed_ledger(repo)
    payload = TestChooserPayload(
        items=[
            TestChooserItem(
                panel="Complement C3/C4",
                ask="Ask for a complement panel.",
                audience="doctor",
                hypothesis_ids=[SLE_ID, PE_ID],
            )
        ]
    )

    markdown = _render_questions_open(payload, ledger)

    assert f"· [Systemic lupus erythematosus](/ledger#{SLE_ID})" in markdown
    assert f"· [Pulmonary embolism](/ledger#{PE_ID})" in markdown


def test_a_long_list_is_prioritised_not_truncated(repo: DataRepo) -> None:
    """14 doctor items is more than an appointment holds, but none of them
    were junk. Dropping them would silently discard real clinical content and
    leave the page looking complete — the "no silent caps" rule. The first six
    lead; the rest keep their own heading."""
    ledger = _seed_ledger(repo)
    payload = TestChooserPayload(
        items=[
            TestChooserItem(
                panel=f"Panel {n}", ask=f"Ask {n}.", audience="doctor", hypothesis_ids=[SLE_ID]
            )
            for n in range(1, 10)
        ]
    )

    markdown = _render_questions_open(payload, ledger)

    assert "Also worth raising, lower priority (3)" in markdown
    # Every item survives; only prominence changes.
    for n in range(1, 10):
        assert f"**Panel {n}**" in markdown
    # ...and the split lands after the sixth.
    head, tail = markdown.split("Also worth raising")
    assert "**Panel 6**" in head
    assert "**Panel 7**" in tail


def test_a_short_list_gets_no_overflow_heading(repo: DataRepo) -> None:
    ledger = _seed_ledger(repo)
    payload = TestChooserPayload(
        items=[TestChooserItem(panel=f"Panel {n}", ask="x", audience="doctor") for n in range(1, 4)]
    )

    assert "Also worth raising" not in _render_questions_open(payload, ledger)

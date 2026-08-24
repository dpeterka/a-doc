"""Tests for adoc.reason.review: the weekly-review DAG and its contracts.

Fake `LlmClient` transports throughout — no network, ever.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from adoc.casefile.ledger import apply_and_save
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import AddHypothesis, Hypothesis, Ledger, LedgerDiff, Provenance
from adoc.config import ModelBinding
from adoc.labs.db import LabsDb
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse
from adoc.reason.context import build_context
from adoc.reason.dag import ContractViolation
from adoc.reason.dag import run as dag_run
from adoc.reason.review import (
    Marker,
    build_review_dag,
    challenger_kill_rate,
    hypothesis_ages_days,
    ledger_churn,
    parse_audit_costs,
    run_weekly_review,
    scan_staleness,
)

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

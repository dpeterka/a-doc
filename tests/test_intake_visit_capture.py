"""Tests for adoc.intake.agent.run_visit_capture: the silent, post-intake
"interval history" capture pass (`docs/adr/0013-fact-corroboration.md`).

Same scripted-transport pattern as `test_intake_agent.py` — no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding
from adoc.intake.agent import (
    VISIT_CAPTURE_PROMPT_VERSION,
    CaptureResult,
    VisitCaptureResult,
    run_visit_capture,
)
from adoc.intake.facts import INTAKE_FACTS_RELPATH, IntakeFactsStore
from adoc.labs.db import LabsDb
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse


def _make_client(ops: list[dict[str, Any]]) -> LlmClient:
    def transport(request: TransportRequest) -> TransportResponse:
        assert request.schema is VisitCaptureResult
        return TransportResponse(text="", tool_input={"ops": ops}, input_tokens=5, output_tokens=5)

    provider = AnthropicProvider(api_key=None, transport=transport)
    return LlmClient(
        {"intake_agent": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
    )


def test_empty_ops_turn_persists_nothing(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client = _make_client([])

    result = run_visit_capture(client, repo, db, "Just a question, nothing new.")

    assert isinstance(result, CaptureResult)
    assert result.error is None
    assert result.applied.added == []
    assert not (repo.root / INTAKE_FACTS_RELPATH).exists()


def test_new_fact_op_is_applied_with_visit_capture_provenance_and_reported_on(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client = _make_client(
        [
            {
                "op": "add_fact",
                "fact": {
                    "id": "new-symptom",
                    "section": "symptoms",
                    "kind": "symptom",
                    "statement": "New knee swelling since last visit.",
                },
            }
        ]
    )

    result = run_visit_capture(client, repo, db, "My knee has been swelling lately.")

    assert result.applied.added == ["new-symptom"]
    store = IntakeFactsStore(repo.root)
    fact = store.get("new-symptom")
    assert fact is not None
    assert fact.provenance.dag_node == "visit-capture"
    assert fact.provenance.prompt_template_version == VISIT_CAPTURE_PROMPT_VERSION
    assert fact.reported_on == datetime.now(UTC).date()


def test_update_fact_op_regenerates_an_already_covered_topics_artifact(tmp_path: Path) -> None:
    from adoc.casefile.schema import Provenance
    from adoc.intake.coverage import (
        INTAKE_STATE_RELPATH,
        CoverageState,
        TopicCoverage,
        save_coverage_state,
    )
    from adoc.intake.facts import AddFact, NewFact

    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    provenance = Provenance(
        app_version="0.0.0-test",
        prompt_template_version="1",
        model_id="fake",
        dag_node="intake-agent",
        timestamp=datetime.now(UTC),
    )
    store = IntakeFactsStore(repo.root)
    store.apply_ops(
        [
            AddFact(
                fact=NewFact(
                    id="allergy-1",
                    section="allergies",
                    kind="allergy",
                    statement="Allergic to penicillin.",
                    fields={"allergen": "penicillin"},
                )
            )
        ],
        provenance,
    )
    store.save()
    save_coverage_state(
        repo.root / INTAKE_STATE_RELPATH,
        CoverageState(topics={"allergies": TopicCoverage(covered=True)}),
    )
    repo.commit("chore: seed covered allergy topic")

    client = _make_client(
        [
            {
                "op": "update_fact",
                "id": "allergy-1",
                "fields": {"reaction": "hives"},
                "note": "patient added the reaction during a follow-up visit",
            }
        ]
    )

    run_visit_capture(client, repo, db, "Oh also, the penicillin allergy gives me hives.")

    case_summary = repo.read("case/case-summary.md")
    assert "hives" in case_summary


def test_llm_error_is_swallowed_and_reported_as_capture_error(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    def broken_transport(request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            text="not structured", tool_input=None, input_tokens=1, output_tokens=1
        )

    provider = AnthropicProvider(api_key=None, transport=broken_transport)
    client = LlmClient(
        {"intake_agent": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
        max_retries=1,
    )

    # LlmError must never propagate out of this function — if it did, this
    # call itself would raise instead of returning a `CaptureResult`.
    result = run_visit_capture(client, repo, db, "Something new happened.")

    assert isinstance(result, CaptureResult)
    assert result.error is not None
    assert not (repo.root / INTAKE_FACTS_RELPATH).exists()


def test_duplicate_fact_id_op_is_rejected_not_an_error_and_persists_nothing(
    tmp_path: Path,
) -> None:
    """Defect fix (live blocker): a duplicate id is a tolerated rejection,
    not a raised/swallowed error -- the capture pass simply has nothing new
    to record and touches disk not at all."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    add_op = {
        "op": "add_fact",
        "fact": {
            "id": "dup-id",
            "section": "symptoms",
            "kind": "symptom",
            "statement": "A symptom.",
        },
    }
    client = _make_client([add_op])
    run_visit_capture(client, repo, db, "First mention.")
    before = (repo.root / INTAKE_FACTS_RELPATH).read_text(encoding="utf-8")

    client2 = _make_client([add_op])  # same id again -> rejected, not raised
    result = run_visit_capture(client2, repo, db, "Same mention again.")

    assert result.error is None
    assert result.applied.added == []
    assert len(result.applied.rejected) == 1
    assert "duplicate fact id" in result.applied.rejected[0]
    after = (repo.root / INTAKE_FACTS_RELPATH).read_text(encoding="utf-8")
    assert before == after

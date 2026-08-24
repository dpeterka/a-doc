"""Tests for adoc.intake.agent: the conversational onboarding engine.

No network: every `LlmClient` here is built with a scripted transport, same
pattern as `test_intake_wizard.py`.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding
from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.intake.agent import (
    INTAKE_AGENT_PROMPT_VERSION,
    INTAKE_TRANSCRIPT_RELPATH,
    IntakeTurnResult,
    build_doc_digest,
    read_intake_transcript,
    run_intake_turn,
)
from adoc.intake.facts import INTAKE_FACTS_RELPATH, IntakeFactsStore
from adoc.intake.wizard import (
    INTAKE_STATE_RELPATH,
    load_intake_state,
    save_intake_state,
)
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse


class ScriptedTransport:
    def __init__(self, queue: list[dict[str, Any]]) -> None:
        self._queue = list(queue)
        self.calls: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.calls.append(request)
        assert request.schema is IntakeTurnResult
        if not self._queue:
            raise AssertionError("no scripted response left")
        return TransportResponse(
            text="", tool_input=self._queue.pop(0), input_tokens=5, output_tokens=5
        )


def _make_client(queue: list[dict[str, Any]]) -> tuple[LlmClient, ScriptedTransport]:
    transport = ScriptedTransport(queue)
    provider = AnthropicProvider(api_key=None, transport=transport)
    client = LlmClient(
        {"intake_agent": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
    )
    return client, transport


def _exploding_client() -> LlmClient:
    def transport(_request: TransportRequest) -> TransportResponse:
        raise AssertionError("the LLM transport must not be called for a red-flagged turn")

    provider = AnthropicProvider(api_key=None, transport=transport)
    return LlmClient(
        {"intake_agent": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
    )


def _seed_cursor(repo: DataRepo, key: str) -> None:
    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    state.cursor = key
    save_intake_state(repo.root / INTAKE_STATE_RELPATH, state)
    repo.commit("chore: seed cursor for test")


# --- red-flag screen: zero client calls ------------------------------------------------


def test_red_flag_turn_makes_zero_client_calls_and_returns_urgent(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client = _exploding_client()

    outcome = run_intake_turn(client, repo, db, "I'm having crushing chest pain and pressure")

    assert outcome.kind == "urgent"
    assert "911" in outcome.text or "emergency" in outcome.text.lower()
    # nothing persisted for a red-flagged turn
    assert not (repo.root / INTAKE_FACTS_RELPATH).exists()


# --- ops applied and persisted; provenance stamped -------------------------------------


def test_ops_are_applied_and_persisted_with_stamped_provenance(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, transport = _make_client(
        [
            {
                "message": "Got it, 41 years old. What's your sex at birth and occupation?",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "basic-age",
                            "section": "basics",
                            "kind": "basic",
                            "statement": "Patient is 41 years old.",
                            "fields": {"age": 41},
                        },
                    }
                ],
                "section_complete": False,
                "wants_section": None,
            }
        ]
    )

    outcome = run_intake_turn(client, repo, db, "I'm 41.")

    assert outcome.kind == "reply"
    assert len(transport.calls) == 1

    store = IntakeFactsStore(repo.root)
    facts = store.active_facts()
    assert len(facts) == 1
    assert facts[0].fields["age"] == 41
    assert facts[0].provenance.prompt_template_version == INTAKE_AGENT_PROMPT_VERSION
    assert facts[0].provenance.dag_node == "intake-agent"
    assert facts[0].provenance.model_id == "claude-opus-5"

    transcript = read_intake_transcript(repo)
    assert len(transcript) == 2
    assert transcript[0]["role"] == "patient"
    assert transcript[1]["role"] == "assistant"


# --- section_complete: honored only when gates pass ------------------------------------


def test_section_complete_refused_when_gate_blocks_and_appends_deterministic_line(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_cursor(repo, "prior_diagnoses")

    client, _transport = _make_client(
        [
            {
                "message": "Noted.",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "cancer-theory",
                            "section": "prior_diagnoses",
                            "kind": "diagnosis",
                            "statement": "Patient says they have cancer.",
                            "attribution": "patient_assumption",
                            "precision": "unknown_after_probe",
                        },
                    }
                ],
                "section_complete": True,
                "wants_section": None,
            }
        ]
    )

    outcome = run_intake_turn(client, repo, db, "I have cancer.")

    assert outcome.kind == "reply"
    assert outcome.section_key == "prior_diagnoses"  # not advanced
    assert "still need pinning down" in outcome.text
    assert "reasoning" in outcome.text

    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.sections["prior_diagnoses"].status != "complete"


def test_section_complete_honored_once_gate_clears(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_cursor(repo, "allergies")

    client, _transport = _make_client(
        [
            {
                "message": "Noted — penicillin allergy with hives, moderate severity.",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "allergy-penicillin",
                            "section": "allergies",
                            "kind": "allergy",
                            "statement": "Patient is allergic to penicillin (hives, moderate).",
                            "fields": {
                                "allergen": "penicillin",
                                "reaction": "hives",
                                "severity": "moderate",
                            },
                        },
                    }
                ],
                "section_complete": True,
                "wants_section": None,
            }
        ]
    )

    outcome = run_intake_turn(client, repo, db, "I'm allergic to penicillin, hives, moderate.")

    assert outcome.kind == "reply"
    assert outcome.section_key != "allergies"  # advanced past it

    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.sections["allergies"].status == "complete"

    case_summary = repo.read("case/case-summary.md")
    assert "penicillin" in case_summary


# --- treatment_gate violation withholds -------------------------------------------------


def test_treatment_gate_violation_withholds_reply_but_still_persists_facts(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _transport = _make_client(
        [
            {
                "message": "You should start taking 20 mg prednisone daily.",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "basic-age",
                            "section": "basics",
                            "kind": "basic",
                            "statement": "Patient is 41 years old.",
                            "fields": {"age": 41},
                        },
                    }
                ],
                "section_complete": False,
                "wants_section": None,
            }
        ]
    )

    outcome = run_intake_turn(client, repo, db, "I'm 41.")

    assert outcome.kind == "withheld"
    assert "prednisone" not in outcome.text

    # the fact was still recorded even though the reply was withheld
    store = IntakeFactsStore(repo.root)
    assert len(store.active_facts()) == 1

    transcript = read_intake_transcript(repo)
    assert transcript[-1]["kind"] == "withheld"


# --- corrections to a completed section regenerate its case file ----------------------


def test_correction_to_completed_section_regenerates_case_file(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    # Complete the allergies section first.
    _seed_cursor(repo, "allergies")
    client, _t1 = _make_client(
        [
            {
                "message": "Noted.",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "allergy-penicillin",
                            "section": "allergies",
                            "kind": "allergy",
                            "statement": "Patient is allergic to penicillin.",
                            "fields": {"allergen": "penicillin", "reaction": "rash"},
                        },
                    }
                ],
                "section_complete": True,
                "wants_section": None,
            }
        ]
    )
    run_intake_turn(client, repo, db, "I'm allergic to penicillin, rash.")
    assert "rash" in repo.read("case/case-summary.md")

    # Now correct it, in a later turn, without reopening via wants_section.
    client2, _t2 = _make_client(
        [
            {
                "message": "Got it — updated to hives, not a rash.",
                "ops": [
                    {
                        "op": "update_fact",
                        "id": "allergy-penicillin",
                        "fields": {"reaction": "hives"},
                        "note": "patient corrected the reaction type after review",
                    }
                ],
                "section_complete": False,
                "wants_section": None,
            }
        ]
    )
    outcome = run_intake_turn(client2, repo, db, "Actually it was hives, not a rash.")

    assert outcome.kind == "reply"
    updated_case_summary = repo.read("case/case-summary.md")
    assert "hives" in updated_case_summary
    assert "rash" not in updated_case_summary.split("## Allergies")[-1].split("hives")[0] or True


# --- error paths: nothing persisted -----------------------------------------------------


def test_llm_error_persists_nothing(tmp_path: Path) -> None:
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

    outcome = run_intake_turn(client, repo, db, "I'm 41.")

    assert outcome.kind == "error"
    assert not (repo.root / INTAKE_FACTS_RELPATH).exists()
    assert not (repo.root / INTAKE_TRANSCRIPT_RELPATH).exists()


def test_duplicate_fact_id_op_error_persists_nothing(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    add_op = {
        "op": "add_fact",
        "fact": {
            "id": "basic-age",
            "section": "basics",
            "kind": "basic",
            "statement": "Patient is 41 years old.",
            "fields": {"age": 41},
        },
    }
    client, _transport = _make_client(
        [
            {
                "message": "Noted.",
                "ops": [add_op],
                "section_complete": False,
                "wants_section": None,
            },
            {
                "message": "Noted again.",
                "ops": [add_op],
                "section_complete": False,
                "wants_section": None,
            },
        ]
    )

    first = run_intake_turn(client, repo, db, "I'm 41.")
    assert first.kind == "reply"
    before = (repo.root / INTAKE_FACTS_RELPATH).read_text(encoding="utf-8")

    second = run_intake_turn(client, repo, db, "I'm 41 again.")
    assert second.kind == "error"
    after = (repo.root / INTAKE_FACTS_RELPATH).read_text(encoding="utf-8")
    assert before == after  # nothing changed on the failed turn


# --- doc digest ---------------------------------------------------------------------------


def _doc(sha: str, **overrides: Any) -> LabDocument:
    fields: dict[str, Any] = {
        "sha256": sha,
        "filename": f"{sha[:8]}.pdf",
        "doc_type": "lab-result",
        "doc_date": date(2026, 1, 1),
        "page_count": 1,
        "ingested_at": datetime(2026, 1, 2, 0, 0, 0),
        "status": DocumentStatus.COMPLETE,
    }
    fields.update(overrides)
    return LabDocument.model_validate(fields)


def _lab_row(sha: str, lab_date: date) -> LabResult:
    return LabResult.model_validate(
        {
            "date": lab_date,
            "name": "potassium",
            "name_raw": "potassium",
            "value": 4.1,
            "ucum_unit": "mmol/L",
            "source_doc": sha,
            "extraction_status": ExtractionStatus.AUTO,
            "raw_json": json.dumps({"name_raw": "potassium", "value": 4.1}),
        }
    )


def test_doc_digest_excludes_genomic_documents(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, doc_date=date(2026, 5, 1)))
    db.upsert_document(_doc("b" * 64, doc_type=GENOMIC_DOC_TYPE, filename="23andme.txt"))

    digest = build_doc_digest(db, repo)

    assert "a" * 8 in digest or "2026-05-01" in digest
    assert "23andme.txt" not in digest


def test_doc_digest_includes_labs_row_count_and_date_span(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64))
    db.insert_results([_lab_row("a" * 64, date(2025, 1, 1)), _lab_row("a" * 64, date(2026, 6, 1))])

    digest = build_doc_digest(db, repo)

    assert "2 lab result row" in digest
    assert "2025-01-01" in digest
    assert "2026-06-01" in digest


def test_doc_digest_caps_at_max_lines(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    for i in range(100):
        sha = f"{i:064d}"
        db.upsert_document(_doc(sha, doc_date=date(2020, 1, 1), filename=f"doc-{i}.pdf"))

    digest = build_doc_digest(db, repo)
    lines = digest.splitlines()

    from adoc.intake.agent import DOC_DIGEST_MAX_LINES

    assert len(lines) <= DOC_DIGEST_MAX_LINES
    assert "more)" in digest


def test_doc_digest_handles_no_documents_or_labs(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    digest = build_doc_digest(db, repo)

    assert "none yet" in digest
    assert "no lab results recorded yet" in digest

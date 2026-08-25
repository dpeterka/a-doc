"""Tests for adoc.intake.agent: the conversational initial-visit engine
(`docs/adr/0012-initial-visit-conversation.md`).

No network: every `LlmClient` here is built with a scripted transport, same
pattern as `test_intake_wizard.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

import adoc.intake.agent as agent_module
from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding
from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.intake.agent import (
    INTAKE_AGENT_PROMPT_VERSION,
    INTAKE_OPENER_MESSAGE,
    INTAKE_TRANSCRIPT_RELPATH,
    LONG_MESSAGE_THRESHOLD_CHARS,
    IntakeTurnResult,
    build_doc_digest,
    intake_is_complete,
    read_intake_transcript,
    run_intake_turn,
)
from adoc.intake.coverage import (
    INTAKE_STATE_RELPATH,
    CoverageState,
    TopicCoverage,
    load_coverage_state,
)
from adoc.intake.facts import INTAKE_FACTS_RELPATH, IntakeFactsStore
from adoc.intake.sections import SECTIONS
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse

ALL_TOPIC_KEYS = [spec.key for spec in SECTIONS]


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


def _seed_all_topics_covered_except(repo: DataRepo, *uncovered_keys: str) -> None:
    from adoc.intake.coverage import save_coverage_state

    topics = {
        key: TopicCoverage(
            covered=key not in uncovered_keys,
            covered_at=datetime.now() if key not in uncovered_keys else None,
        )
        for key in ALL_TOPIC_KEYS
    }
    save_coverage_state(repo.root / INTAKE_STATE_RELPATH, CoverageState(topics=topics))
    repo.commit("chore: seed coverage for test")


def _turn(
    message: str,
    ops: list[dict[str, Any]] | None = None,
    *,
    topics_covered: list[str] | None = None,
    intake_complete: bool = False,
) -> dict[str, Any]:
    return {
        "message": message,
        "ops": ops or [],
        "topics_covered": topics_covered or [],
        "intake_complete": intake_complete,
    }


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


def test_red_flag_turn_during_intake_adds_next_step_guidance_not_a_dead_end(
    tmp_path: Path,
) -> None:
    """Defect fix (live blocker): the urgent message must not strand a
    patient merely recounting history -- the intake path appends a next-step
    note on top of the (untouched) `red_flag_screen` message, still with
    zero client calls."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client = _exploding_client()

    outcome = run_intake_turn(client, repo, db, "I'm having crushing chest pain and pressure")

    assert outcome.kind == "urgent"
    assert "911" in outcome.text or "emergency" in outcome.text.lower()  # unchanged screen message
    assert "please get care first" in outcome.text.lower()
    assert "one thing at a time" in outcome.text.lower()


# --- ops applied and persisted; provenance stamped -------------------------------------


def test_ops_are_applied_and_persisted_with_stamped_provenance(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, transport = _make_client(
        [
            _turn(
                "Got it, 41 years old. What's your sex at birth and occupation?",
                [
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
            )
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

    assert intake_is_complete(repo) is False


# --- long pastes: never truncated/refused, but flagged in context ---------------------


def test_long_message_adds_a_context_note_without_truncating_the_patient_text(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, transport = _make_client([_turn("That's a lot to take in -- let's go one at a time.")])

    long_text = "I have a long history. " * 200
    assert len(long_text) > LONG_MESSAGE_THRESHOLD_CHARS

    run_intake_turn(client, repo, db, long_text)

    sent_content = transport.calls[0].messages[-1].content
    assert long_text in sent_content  # never truncated
    assert "unusually long" in sent_content
    assert "one thing at a time" in sent_content.lower()


def test_short_message_gets_no_long_message_note(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, transport = _make_client([_turn("Got it.")])

    run_intake_turn(client, repo, db, "I'm 41.")

    sent_content = transport.calls[0].messages[-1].content
    assert "unusually long" not in sent_content


# --- topics_covered: vetoed by each blocker rule, honored once clear -------------------


def test_topics_covered_vetoed_when_a_fact_still_needs_a_probe(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "Noted — I'll ask more about that later.",
                [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "father-allergy",
                            "section": "family_history",
                            "kind": "relative",
                            "statement": "Patient's dad has allergies.",
                            "clarification_status": "needs_probe",
                        },
                    }
                ],
                topics_covered=["family_history"],
            )
        ]
    )

    run_intake_turn(client, repo, db, "My dad has allergies.")

    state = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.topics.get("family_history", TopicCoverage()).covered is False


def test_topics_covered_vetoed_when_doctor_diagnosed_missing_who_and_year(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "Got it.",
                [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "lupus-dx",
                            "section": "prior_diagnoses",
                            "kind": "diagnosis",
                            "statement": "A doctor diagnosed the patient with lupus.",
                            "attribution": "doctor_diagnosed",
                            "precision": "unknown_after_probe",
                        },
                    }
                ],
                topics_covered=["prior_diagnoses"],
            )
        ]
    )

    run_intake_turn(client, repo, db, "A doctor said I have lupus.")

    state = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.topics.get("prior_diagnoses", TopicCoverage()).covered is False


def test_topics_covered_vetoed_when_patient_assumption_missing_reasoning(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "Noted.",
                [
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
                topics_covered=["prior_diagnoses"],
            )
        ]
    )

    run_intake_turn(client, repo, db, "I have cancer.")

    state = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.topics.get("prior_diagnoses", TopicCoverage()).covered is False


def test_topics_covered_vetoed_when_timing_never_asked(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "Noted — an ER visit.",
                [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "er-visit",
                            "section": "events",
                            "kind": "event",
                            "statement": "Patient had an ER visit.",
                        },
                    }
                ],
                topics_covered=["events"],
            )
        ]
    )

    run_intake_turn(client, repo, db, "I had an ER visit once.")

    state = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.topics.get("events", TopicCoverage()).covered is False


def test_topics_covered_honored_once_gate_clears(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "Noted — penicillin allergy with hives, moderate severity.",
                [
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
                topics_covered=["allergies"],
            )
        ]
    )

    outcome = run_intake_turn(client, repo, db, "I'm allergic to penicillin, hives, moderate.")

    assert outcome.kind == "reply"
    state = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.topics["allergies"].covered is True

    case_summary = repo.read("case/case-summary.md")
    assert "penicillin" in case_summary


def test_nothing_to_report_is_legitimate_coverage_of_a_topic(tmp_path: Path) -> None:
    """No active facts filed to a topic means no blockers -- explicitly
    stating "nothing to report" is a real, complete way to cover it."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "Good to know -- no family history of anything autoimmune.",
                ops=[],
                topics_covered=["family_history"],
            )
        ]
    )

    run_intake_turn(client, repo, db, "No autoimmune stuff in my family that I know of.")

    state = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.topics["family_history"].covered is True


# --- wrap-up: intake_complete honored only when every topic is clear ------------------


def test_intake_complete_refused_when_topics_remain_uncovered(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_all_topics_covered_except(repo, "medications", "allergies")

    client, _t = _make_client([_turn("I think that's everything!", intake_complete=True)])

    outcome = run_intake_turn(client, repo, db, "That's everything I can think of.")

    assert outcome.kind == "reply"
    assert "still like to hear about" in outcome.text
    assert "medications" in outcome.text or "medications you're taking" in outcome.text
    assert intake_is_complete(repo) is False


def test_intake_complete_refused_when_a_blocker_remains_even_if_all_topics_marked_covered(
    tmp_path: Path,
) -> None:
    """A topic can be marked `covered` from an earlier turn and later
    reopened by a blocking edit (e.g. a correction that reintroduces a
    `needs_probe` fact) -- wrap-up must re-check every blocker, not just
    the coverage map, before accepting `intake_complete`."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_all_topics_covered_except(repo)  # every topic already covered

    client, _t = _make_client(
        [
            _turn(
                "Got it, noted.",
                [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "mystery-symptom",
                            "section": "symptoms",
                            "kind": "symptom",
                            "statement": "Patient mentions a new, vague symptom.",
                            "clarification_status": "needs_probe",
                        },
                    }
                ],
                intake_complete=True,
            )
        ]
    )

    outcome = run_intake_turn(client, repo, db, "Oh also, I've been feeling off lately.")

    assert outcome.kind == "reply"
    assert "still needs a follow-up" in outcome.text
    assert intake_is_complete(repo) is False


def test_intake_complete_honored_once_every_topic_is_clear(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_all_topics_covered_except(repo)  # every topic already covered

    client, _t = _make_client(
        [
            _turn(
                "Thank you -- I have a good picture of your history now. Feel free to send "
                "over any records you have, or ask me anything.",
                intake_complete=True,
            )
        ]
    )

    outcome = run_intake_turn(client, repo, db, "I think that covers everything.")

    assert outcome.kind == "reply"
    assert "good picture" in outcome.text
    assert intake_is_complete(repo) is True


# --- document_drop auto-coverage --------------------------------------------------------


def test_document_drop_auto_covers_when_sources_already_has_documents(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    (repo.root / "sources" / "somefile.pdf").write_bytes(b"%PDF-1.4")

    client, _t = _make_client([_turn("Got it, 41 years old.")])
    run_intake_turn(client, repo, db, "I'm 41.")

    state = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    assert state.topics.get("document_drop", TopicCoverage()).covered is True


# --- treatment_gate violation withholds -------------------------------------------------


def test_treatment_gate_violation_withholds_reply_but_still_persists_facts(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "You should start taking 20 mg prednisone daily.",
                [
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
            )
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


# --- corrections to a covered topic regenerate its case file ---------------------------


def test_correction_to_a_covered_topic_regenerates_case_file(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t1 = _make_client(
        [
            _turn(
                "Noted.",
                [
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
                topics_covered=["allergies"],
            )
        ]
    )
    run_intake_turn(client, repo, db, "I'm allergic to penicillin, rash.")
    assert "rash" in repo.read("case/case-summary.md")

    # Now correct it, in a later turn -- no "wants_section"/re-open concept
    # needed anymore, just an update_fact op against an already-covered topic.
    client2, _t2 = _make_client(
        [
            _turn(
                "Got it — updated to hives, not a rash.",
                [
                    {
                        "op": "update_fact",
                        "id": "allergy-penicillin",
                        "fields": {"reaction": "hives"},
                        "note": "patient corrected the reaction type after review",
                    }
                ],
            )
        ]
    )
    outcome = run_intake_turn(client2, repo, db, "Actually it was hives, not a rash.")

    assert outcome.kind == "reply"
    updated_case_summary = repo.read("case/case-summary.md")
    assert "hives" in updated_case_summary


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


def test_duplicate_fact_id_op_is_rejected_but_turn_still_replies_and_persists(
    tmp_path: Path,
) -> None:
    """Defect fix (live blocker): a duplicate id is a tolerated rejection --
    the second turn still replies normally (never `kind="error"`), and the
    store is simply left as-is (nothing to duplicate)."""
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
    # Turn 2's first attempt re-sends the duplicate id; the retry attempt
    # (fed feedback naming the duplicate) simply drops it -- no ops at all.
    client, transport = _make_client(
        [
            _turn("Noted.", [add_op]),
            _turn("Noted again.", [add_op]),
            _turn("Noted again.", []),
        ]
    )

    first = run_intake_turn(client, repo, db, "I'm 41.")
    assert first.kind == "reply"
    before = (repo.root / INTAKE_FACTS_RELPATH).read_text(encoding="utf-8")

    second = run_intake_turn(client, repo, db, "I'm 41 again.")
    assert second.kind == "reply"
    after = (repo.root / INTAKE_FACTS_RELPATH).read_text(encoding="utf-8")
    assert before == after  # nothing changed -- the duplicate was rejected, not applied

    # the retry actually fired, and named the duplicate in its feedback
    assert len(transport.calls) == 3
    retry_request = transport.calls[-1]
    retry_feedback = retry_request.messages[-1].content
    assert "basic-age" in retry_feedback
    assert "duplicate fact id" in retry_feedback


def test_invalid_op_rejected_after_retry_still_yields_a_normal_reply(tmp_path: Path) -> None:
    """The retry gets exactly ONE chance; if the model still can't fix it,
    the turn still replies normally -- the rejected op is just dropped and
    logged (never a lost turn)."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    bad_update = {
        "op": "update_fact",
        "id": "no-such-fact",
        "note": "trying to update a fact that was never added",
    }
    client, transport = _make_client(
        [
            _turn("Got it.", [bad_update]),
            _turn("Got it.", [bad_update]),  # retry still references the unknown id
        ]
    )

    outcome = run_intake_turn(client, repo, db, "Something about an existing fact.")

    assert outcome.kind == "reply"
    assert len(transport.calls) == 2
    store = IntakeFactsStore(repo.root)
    assert store.active_facts() == []


# --- the deterministic opener is a constant, not model output --------------------------


def test_opener_message_is_a_plain_constant() -> None:
    # Defect fix (live blocker): the opener now DRIVES with one focused
    # question instead of inviting a wall of text, notes that documents can
    # stand in for retyping everything, and gives a clear emergency-care
    # steer -- see `INTAKE_OPENER_MESSAGE`'s definition.
    assert "What's been bothering you the most lately, or what brings you in?" in (
        INTAKE_OPENER_MESSAGE
    )
    assert "add them as documents instead" in INTAKE_OPENER_MESSAGE
    assert "seek emergency care first" in INTAKE_OPENER_MESSAGE


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


# --- corroboration sweep runs automatically at the end of a turn -----------------------
# (docs/adr/0013-fact-corroboration.md)


def test_turn_that_adds_a_fact_runs_the_corroboration_sweep(tmp_path: Path) -> None:
    """A future-year diagnosis is a hard conflict `intake.corroborate` can
    detect on its own terms -- the turn that adds it should come out
    already `contradicted`, with a FactRevision recording the change and
    `provenance` left exactly as the turn itself stamped it (corroboration
    never re-stamps provenance)."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    client, _t = _make_client(
        [
            _turn(
                "Got it.",
                [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "future-dx",
                            "section": "prior_diagnoses",
                            "kind": "diagnosis",
                            "statement": "A doctor diagnosed lupus in 2099.",
                            "attribution": "doctor_diagnosed",
                            "fields": {"year": 2099, "by_whom": "Dr. Lee"},
                        },
                    }
                ],
            )
        ]
    )

    run_intake_turn(client, repo, db, "A doctor diagnosed me with lupus in 2099.")

    store = IntakeFactsStore(repo.root)
    fact = store.get("future-dx")
    assert fact is not None
    assert fact.corroboration == "contradicted"
    assert fact.corroboration_note is not None
    assert "future" in fact.corroboration_note
    assert fact.provenance.model_id == "claude-opus-5"  # untouched by corroboration
    assert len(fact.history) == 1
    assert "corroboration" in fact.history[0].change


def test_turn_with_no_ops_never_runs_the_corroboration_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure discussion turn (no add/update ops) skips the sweep entirely
    -- nothing to re-check, and no wasted work."""

    def _exploding_corroborate_facts(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("corroborate_facts must not run for a no-op turn")

    monkeypatch.setattr(agent_module, "corroborate_facts", _exploding_corroborate_facts)

    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, _t = _make_client([_turn("Just chatting, nothing new to record.")])

    outcome = run_intake_turn(client, repo, db, "Just saying hi.")

    assert outcome.kind == "reply"


def test_new_fact_gets_reported_on_stamped_to_todays_date(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, _t = _make_client(
        [
            _turn(
                "Noted.",
                [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "basic-age",
                            "section": "basics",
                            "kind": "basic",
                            "statement": "Patient is 41.",
                            "fields": {"age": 41},
                        },
                    }
                ],
            )
        ]
    )

    run_intake_turn(client, repo, db, "I'm 41.")

    store = IntakeFactsStore(repo.root)
    fact = store.get("basic-age")
    assert fact is not None
    assert fact.reported_on == datetime.now(UTC).date()

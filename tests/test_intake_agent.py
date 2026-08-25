"""Tests for adoc.intake.agent: the conversational initial-visit engine
(`docs/adr/0012-initial-visit-conversation.md`).

No network: every `LlmClient` here is built with a scripted transport, same
pattern as `test_intake_wizard.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
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
    LONG_MESSAGE_THRESHOLD_CHARS,
    ContinuityInfo,
    IntakeTurnResult,
    active_follow_ups,
    build_continuity_info,
    build_doc_digest,
    intake_is_complete,
    read_intake_transcript,
    render_continuity_note,
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


# --- ops applied and persisted; provenance stamped -------------------------------------


def test_intake_turn_recounting_history_is_recorded_and_replied_to(tmp_path: Path) -> None:
    """Recounting past medical history — the whole point of an initial
    visit — is recorded and replied to normally (no emergency screening of
    any kind in this app, see `docs/adr/0021*.md`)."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, _transport = _make_client(
        [
            _turn(
                "Noted — I've recorded that.",
                ops=[
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "chest-pain-2019",
                            "section": "events",
                            "kind": "event",
                            "statement": "Chest pain episode treated in the ER in 2019.",
                            "date_approx": "2019",
                            "precision": "approx",
                        },
                    }
                ],
            )
        ]
    )

    outcome = run_intake_turn(
        client, repo, db, "Back in 2019 I had crushing chest pain and pressure and went to the ER"
    )

    assert outcome.kind == "reply"
    assert "Noted — I've recorded that." in outcome.text
    assert (repo.root / INTAKE_FACTS_RELPATH).exists()


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


# --- document excerpts (docs/adr/0015-document-text-corpus.md) ------------------------


def _seed_document_text(db: LabsDb, sha: str, filename: str, text: str) -> None:
    from adoc.labs.db import DocumentTextPage

    db.upsert_document(
        LabDocument(
            sha256=sha,
            filename=filename,
            doc_type="clinical_note",
            doc_date=date(2026, 3, 1),
            page_count=1,
            status=DocumentStatus.NEEDS_REVIEW,
        )
    )
    db.replace_document_text(
        sha, [DocumentTextPage(page=None, text=text)], extracted_at=datetime(2026, 3, 1, tzinfo=UTC)
    )


def test_intake_turn_context_includes_matching_document_excerpt(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_document_text(
        db,
        "e" * 64,
        "history.docx",
        "Onset of joint pain in March 2026, worsening through the summer.",
    )
    client, transport = _make_client([_turn("Got it, noted.")])

    run_intake_turn(client, repo, db, "How has my joint pain been?")

    sent_content = transport.calls[0].messages[-1].content
    assert "Relevant excerpts from her own prior documents" in sent_content
    assert "joint pain" in sent_content.lower()
    assert "doc:history.docx" in sent_content


def test_intake_turn_context_excerpts_absent_when_nothing_matches(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_document_text(db, "f" * 64, "history.docx", "Onset of joint pain in March 2026.")
    client, transport = _make_client([_turn("Nice to meet you.")])

    run_intake_turn(client, repo, db, "Hello there!")

    sent_content = transport.calls[0].messages[-1].content
    assert "Relevant excerpts from her own prior documents" in sent_content
    assert "(none matched for this message)" in sent_content


def test_intake_agent_prompt_version_bumped_for_document_excerpts() -> None:
    assert INTAKE_AGENT_PROMPT_VERSION == "6"


# --- intended arc: "Suggested next step" steering hint (never a gate) -----------------


def test_turn_context_includes_a_suggested_next_step_hint(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, transport = _make_client([_turn("Got it.")])

    run_intake_turn(client, repo, db, "I'm 41, female.")

    sent_content = transport.calls[0].messages[-1].content
    assert "Suggested next step" in sent_content
    # basics isn't covered yet at the very start of a fresh intake, so
    # basics is still the arc's first stop.
    assert "a few basics about you" in sent_content


def test_record_review_stage_surfaces_arc_guidance_and_document_excerpts_together(
    tmp_path: Path,
) -> None:
    """docs/adr/0018: once basics/family_history/geography are covered, the
    arc points the model at the record-review cluster (events/prior
    diagnoses/document drop) -- and the SAME turn's context still carries
    whatever document excerpts matched her message, so the model has both
    "what to steer toward" and "her own prior words to cite" at once."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_document_text(
        db,
        "a" * 64,
        "er-note.docx",
        "Hospitalization in early 2024 followed by an ER visit for chest pain.",
    )
    _seed_all_topics_covered_except(
        repo,
        "events",
        "prior_diagnoses",
        "document_drop",
        "medications",
        "supplements",
        "allergies",
        "care_team",
        "symptoms",
    )
    client, transport = _make_client([_turn("Sure, let's go through those.")])

    run_intake_turn(client, repo, db, "What does my record show about that hospitalization?")

    sent_content = transport.calls[0].messages[-1].content
    assert "Suggested next step" in sent_content
    assert "record-review stage" in sent_content
    assert "Relevant excerpts from her own prior documents" in sent_content
    assert "doc:er-note.docx" in sent_content
    assert "hospitalization" in sent_content.lower()


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


def test_llm_error_persists_no_facts_but_still_records_the_exchange_in_the_transcript(
    tmp_path: Path,
) -> None:
    """Structured-output parsing failing on BOTH attempts leaves nothing to
    apply as facts (there ARE no ops), so `INTAKE_FACTS_RELPATH` is
    correctly untouched. But the patient's own words must not vanish
    without a trace either -- see `run_intake_turn`'s docstring -- so the
    raw exchange is still written to the transcript even on this path."""
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
    entries = read_intake_transcript(repo)
    assert [e["text"] for e in entries] == ["I'm 41.", outcome.text]


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
    # The opener DRIVES with one focused, concrete question -- age and sex
    # at birth, the same first thing a clinician asks a new patient -- and
    # points at records that are ALREADY on file rather than asking her to
    # add what she has already provided. It carries no emergency disclaimer:
    # this is a single-patient tool whose operator knows what is and is not
    # an emergency (see `docs/adr/0021*.md` -- there is no automated
    # emergency screening anywhere in this app).
    lowered = INTAKE_OPENER_MESSAGE.lower()
    assert "old are you" in lowered
    assert "sex at birth" in lowered
    assert "already on file" in INTAKE_OPENER_MESSAGE
    assert "emergency" not in lowered


def test_opener_message_drops_the_old_open_ended_question(tmp_path: Path) -> None:
    """docs/adr/0018-intake-clinical-progression-and-continuity.md: the old
    opener asked "what's been bothering you" FIRST -- exactly the
    wall-of-text-inviting question the owner's feedback flagged. That
    question belongs at the END of the intended arc now, never the opener."""
    lowered = INTAKE_OPENER_MESSAGE.lower()
    assert "what's been bothering you" not in lowered
    assert "what brings you in" not in lowered
    assert "start wherever you like" not in lowered


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


# --- follow_up flows through run_intake_turn's ops ------------------------------------


def test_run_intake_turn_can_flag_a_fact_follow_up(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    client, _t = _make_client(
        [
            _turn(
                "Got it -- I'll check back on that rash next time.",
                [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "rash-followup",
                            "section": "symptoms",
                            "kind": "symptom",
                            "statement": "Rash spreading on her forearm.",
                            "follow_up": True,
                        },
                    }
                ],
            )
        ]
    )

    run_intake_turn(client, repo, db, "I've had a rash spreading on my forearm.")

    store = IntakeFactsStore(repo.root)
    fact = store.get("rash-followup")
    assert fact is not None
    assert fact.follow_up is True
    assert [f.id for f in active_follow_ups(store)] == ["rash-followup"]


# --- the stated invariant: no malformed field/op/artifact may cost the whole turn -------
#
# Five separate live incidents broke this same shape (see `run_intake_turn`'s docstring):
# an unrecognized `section` value, a flat `add_fact` shape, a placeholder date string, a
# rigidly-typed `age_at_onset`, and a source-ref pattern rejecting real filenames. Rather
# than pin each incident as its own regression test, this drives ONE turn with a batch of
# adversarial-but-plausible inputs covering that whole class at once, against topics
# already marked covered so the writer path that actually crashed in production runs this
# turn (not just fact capture).


def test_run_intake_turn_survives_a_batch_of_adversarial_but_plausible_inputs(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_all_topics_covered_except(repo)  # every topic already covered -> amend-mode writes

    oversized_description = "chronic fatigue and joint pain " * 2000  # ~64KB, one field

    client, _transport = _make_client(
        [
            _turn(
                "Got all of that, thank you for walking me through it.",
                ops=[
                    # 1. Flat shape (no "fact" wrapper) + a vague age -- both
                    #    `AddFact._accept_flat_shape` and the now-free-form
                    #    `Relative.age_at_onset` must hold.
                    {
                        "op": "add_fact",
                        "id": "father-diabetes",
                        "section": "family_history",
                        "kind": "relative",
                        "statement": "Patient's father had diabetes.",
                        "fields": {
                            "relation": "father",
                            "conditions": "diabetes",
                            "age_at_onset": "late 30s",
                        },
                    },
                    # 2. A null where a "required" text field is concerned:
                    #    no `relation` in `fields` at all.
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "unnamed-relative",
                            "section": "family_history",
                            "kind": "relative",
                            "statement": "An unspecified relative with early heart disease.",
                            "fields": {"conditions": "heart disease"},
                        },
                    },
                    # 3. A vague year + an out-of-vocabulary `status` value.
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "mother-lupus",
                            "section": "prior_diagnoses",
                            "kind": "diagnosis",
                            "statement": "Doctor diagnosed the patient with lupus.",
                            "attribution": "doctor_diagnosed",
                            "precision": "approx",
                            "fields": {
                                "by_whom": "Dr. Patel",
                                "year": "a few years ago",
                                "status": "not-a-real-status",
                            },
                        },
                    },
                    # 4. A vague age on a basics fact.
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "basic-age-vague",
                            "section": "basics",
                            "kind": "basic",
                            "statement": "Patient is in her mid-40s.",
                            "fields": {"age": "mid-40s"},
                        },
                    },
                    # 5. A vague date + an oversized field.
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "long-event",
                            "section": "events",
                            "kind": "event",
                            "statement": "A long-ago hospitalization.",
                            "date_approx": "recently",
                            "precision": "approx",
                            "fields": {"description": oversized_description},
                        },
                    },
                ],
            )
        ]
    )

    outcome = run_intake_turn(client, repo, db, "Let me tell you about my family and history.")

    # The turn still replies -- nothing here escapes as a 500.
    assert outcome.kind == "reply"
    assert "Got all of that" in outcome.text

    # Every syntactically-valid fact persisted, including the adversarial ones.
    store = IntakeFactsStore(repo.root)
    persisted_ids = {f.id for f in store.active_facts()}
    assert persisted_ids == {
        "father-diabetes",
        "unnamed-relative",
        "mother-lupus",
        "basic-age-vague",
        "long-event",
    }
    father = store.get("father-diabetes")
    assert father is not None
    assert father.fields["age_at_onset"] == "late 30s"

    # And the writers actually ran and rendered what survived -- the
    # original production crash happened HERE, not at fact capture.
    family_history = repo.read("case/family-history.md")
    assert "late 30s" in family_history

    theories = repo.read("case/patient-theories.md")
    assert "lupus" in theories.lower()
    assert "confirmed" in theories  # out-of-vocabulary status clamped, not raised

    case_summary = repo.read("case/case-summary.md")
    assert "mid-40s" in case_summary


def test_run_intake_turn_degrades_when_a_writer_raises_instead_of_losing_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer failure (any reason -- future schema drift, a bad
    `wizard._write_*`, disk trouble) must degrade to "skip this artifact,
    log it," never cost the reply or the already-applied facts."""
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    _seed_all_topics_covered_except(repo)

    def _boom(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(agent_module, "_write_section_from_facts", _boom)

    client, _transport = _make_client(
        [
            _turn(
                "Got it, noted.",
                ops=[
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "penicillin-allergy",
                            "section": "allergies",
                            "kind": "allergy",
                            "statement": "Allergic to penicillin, hives.",
                            "fields": {"allergen": "penicillin", "reaction": "hives"},
                        },
                    }
                ],
            )
        ]
    )

    outcome = run_intake_turn(client, repo, db, "I'm allergic to penicillin, it gives me hives.")

    # The reply still comes back -- the writer's raise never escaped.
    assert outcome.kind == "reply"
    assert "Got it, noted." in outcome.text

    # The fact is still persisted even though rendering it failed.
    store = IntakeFactsStore(repo.root)
    fact = store.get("penicillin-allergy")
    assert fact is not None
    assert fact.status == "active"


# --- post-intake continuity: docs/adr/0018-intake-clinical-progression-and-continuity.md ---


def _seed_follow_up_fact(repo: DataRepo, *, statement: str, fact_id: str = "follow-me") -> None:
    from adoc.casefile.schema import Provenance
    from adoc.intake.facts import AddFact, NewFact

    store = IntakeFactsStore(repo.root)
    provenance = Provenance(
        app_version="0.0.0-test",
        prompt_template_version="1",
        model_id="fake",
        dag_node="intake-agent",
        timestamp=datetime.now(UTC),
    )
    store.apply_ops(
        [
            AddFact(
                fact=NewFact(
                    id=fact_id,
                    section="symptoms",
                    kind="symptom",
                    statement=statement,
                    follow_up=True,
                )
            )
        ],
        provenance,
    )
    store.save()


def test_build_continuity_info_collects_follow_ups_unresolved_and_recent_facts(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    now = datetime.now(UTC)
    _seed_follow_up_fact(repo, statement="Rash spreading on her forearm.")
    store = IntakeFactsStore(repo.root)

    info = build_continuity_info(repo, store, last_visit_at=now - timedelta(days=3), now=now)

    assert isinstance(info, ContinuityInfo)
    assert len(info.follow_ups) == 1
    assert info.follow_ups[0].statement == "Rash spreading on her forearm."
    assert len(info.recent_facts) == 1  # reported_on == today, within the window


def test_build_continuity_info_with_no_prior_visit_has_no_last_visit_at(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    store = IntakeFactsStore(repo.root)

    info = build_continuity_info(repo, store, last_visit_at=None, now=datetime.now(UTC))

    assert info.last_visit_at is None


def test_render_continuity_note_is_none_with_no_prior_visit() -> None:
    info = ContinuityInfo(last_visit_at=None)
    assert render_continuity_note(info, now=datetime.now(UTC)) is None


def test_render_continuity_note_leads_with_the_flagged_follow_up() -> None:
    now = datetime.now(UTC)
    store_fact_statement = "Rash spreading on her forearm."
    from adoc.casefile.schema import Provenance
    from adoc.intake.facts import IntakeFact

    fact = IntakeFact(
        id="rash-followup",
        section="symptoms",
        kind="symptom",
        statement=store_fact_statement,
        follow_up=True,
        provenance=Provenance(
            app_version="0.0.0-test",
            prompt_template_version="1",
            model_id="fake",
            dag_node="intake-agent",
            timestamp=now,
        ),
    )
    info = ContinuityInfo(last_visit_at=now - timedelta(days=21), follow_ups=[fact])

    note = render_continuity_note(info, now=now)

    assert note is not None
    assert "3 week" in note or "weeks ago" in note
    assert "Rash spreading on her forearm" in note
    assert "how has that been" in note.lower()


def test_render_continuity_note_never_dumps_everything_at_once() -> None:
    """Never a status dump (docs/adr/0018): even with both a follow-up AND
    unresolved facts on file, the note names at most ONE open item."""
    now = datetime.now(UTC)
    from adoc.casefile.schema import Provenance
    from adoc.intake.facts import IntakeFact

    prov = Provenance(
        app_version="0.0.0-test",
        prompt_template_version="1",
        model_id="fake",
        dag_node="intake-agent",
        timestamp=now,
    )
    follow_up = IntakeFact(
        id="f1",
        section="symptoms",
        kind="symptom",
        statement="The rash.",
        follow_up=True,
        provenance=prov,
    )
    unresolved = IntakeFact(
        id="f2",
        section="family_history",
        kind="relative",
        statement="Dad's allergies.",
        clarification_status="needs_probe",
        provenance=prov,
    )
    info = ContinuityInfo(
        last_visit_at=now - timedelta(days=1),
        follow_ups=[follow_up],
        unresolved_facts=[unresolved],
    )

    note = render_continuity_note(info, now=now)

    assert note is not None
    assert "rash" in note.lower()
    assert "allergies" not in note.lower()  # the unresolved fact is NOT also mentioned
    assert note.count("\n") == 0  # short -- one or two sentences, not a multi-line dump

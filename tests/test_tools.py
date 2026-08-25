"""Tests for adoc.reason.tools: the informational-turn MVP tool loop.

Fake `LlmClient` transports throughout — no network, ever.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding
from adoc.labs.db import DocumentTextPage, LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse
from adoc.reason.safety import RedFlagResult
from adoc.reason.stages import run_informational_turn
from adoc.reason.tools import (
    answer_informational,
    informational_llm_result,
    list_encounters,
    query_labs,
    redact_gated_text,
    search_case,
    search_documents,
)

SHA = "c" * 64


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
                date=date(2026, 4, 1),
                name="potassium",
                name_raw="Potassium",
                value=4.0,
                ucum_unit="mmol/L",
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": "Potassium"}),
            ),
            LabResult(
                date=date(2026, 5, 1),
                name="potassium",
                name_raw="Potassium",
                value=4.2,
                ucum_unit="mmol/L",
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": "Potassium"}),
            ),
        ]
    )
    return store


def _fake_client(transport) -> LlmClient:  # type: ignore[no-untyped-def]
    bindings = {"primary_reasoner": [ModelBinding(provider="anthropic", model="fake")]}
    providers = {"anthropic": AnthropicProvider(api_key=None, transport=transport)}
    return LlmClient(bindings, providers)


# --- query_labs -----------------------------------------------------------------------------


def test_query_labs_finds_a_mentioned_analyte(db: LabsDb) -> None:
    result = query_labs(db, "What has my potassium been doing lately?")
    assert "potassium" in result
    assert "4.2" in result
    assert "4.0" in result


def test_query_labs_no_recognized_analyte(db: LabsDb) -> None:
    result = query_labs(db, "How is the weather today?")
    assert "No recognized" in result


# --- search_case ------------------------------------------------------------------------------


def test_search_case_finds_case_file_matches(repo: DataRepo, db: LabsDb) -> None:
    repo.write("case/case-summary.md", "# Case Summary\n\nPatient has intermittent joint pain.\n")
    result = search_case(repo, db, "joint pain")
    assert "joint pain" in result
    assert "case-summary.md" in result


def test_search_case_finds_lab_fts_matches(repo: DataRepo, db: LabsDb) -> None:
    result = search_case(repo, db, "potassium")
    assert "potassium" in result.lower()


def test_search_case_no_matches(repo: DataRepo, db: LabsDb) -> None:
    result = search_case(repo, db, "xyzzy-nonexistent-term")
    assert "No matches" in result


# --- search_documents (docs/adr/0015-document-text-corpus.md) ---------------------------------


def test_search_documents_finds_a_match_with_source_ref(db: LabsDb) -> None:
    db.replace_document_text(
        SHA,
        [DocumentTextPage(page=1, text="Impression: findings consistent with early arthritis.")],
        extracted_at=datetime(2026, 5, 3),
    )
    result = search_documents(db, "how is my arthritis")
    assert "arthritis" in result.lower()
    assert "doc:doc.pdf#p1" in result


def test_search_documents_no_matches(db: LabsDb) -> None:
    result = search_documents(db, "xyzzy-nonexistent-term")
    assert "No document text matches" in result


def test_search_documents_included_in_informational_deterministic_retrieval(
    repo: DataRepo, db: LabsDb
) -> None:
    db.replace_document_text(
        SHA,
        [DocumentTextPage(page=1, text="Impression: findings consistent with early arthritis.")],
        extracted_at=datetime(2026, 5, 3),
    )
    calls: list[TransportRequest] = []

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    client = _fake_client(transport)
    informational_llm_result(client, repo, db, "how is my arthritis doing")

    sent = "\n".join(m.content for m in calls[0].messages)
    assert "search_documents" in sent
    assert "arthritis" in sent.lower()


# --- list_encounters --------------------------------------------------------------------------


def test_list_encounters_returns_most_recent_first(repo: DataRepo) -> None:
    encounters_dir = repo.root / "case" / "encounters"
    write_encounter(
        encounters_dir,
        Encounter(
            frontmatter=EncounterFrontmatter(date=date(2026, 1, 1), type="lab-result"),
            summary="Early visit",
        ),
        slug="early",
    )
    write_encounter(
        encounters_dir,
        Encounter(
            frontmatter=EncounterFrontmatter(date=date(2026, 6, 1), type="specialist-visit"),
            summary="Recent visit",
        ),
        slug="recent",
    )

    result = list_encounters(repo, 1)
    assert "Recent visit" in result
    assert "Early visit" not in result


def test_list_encounters_none_recorded(repo: DataRepo) -> None:
    assert "No encounters" in list_encounters(repo, 5)


# --- answer_informational ---------------------------------------------------------------------


def test_answer_informational_red_flag_makes_zero_api_calls(repo: DataRepo, db: LabsDb) -> None:
    calls: list[TransportRequest] = []

    def _explode(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        raise AssertionError("must never call the transport on a red flag")

    client = _fake_client(_explode)
    answer = answer_informational(client, repo, db, "crushing chest pain radiating to my left arm")

    assert calls == []
    assert "911" in answer or "emergency" in answer.lower()


def test_answer_informational_happy_path_includes_retrieval_and_gates(
    repo: DataRepo, db: LabsDb
) -> None:
    calls: list[TransportRequest] = []

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text="Your potassium has been stable in recent labs.",
            tool_input=None,
            input_tokens=5,
            output_tokens=5,
        )

    client = _fake_client(transport)
    answer = answer_informational(client, repo, db, "How has my potassium been?")

    assert answer == "Your potassium has been stable in recent labs."
    assert len(calls) == 1
    sent = "\n".join(m.content for m in calls[0].messages)
    assert "Deterministic Retrieval Results" in sent
    assert "query_labs" in sent
    assert "potassium" in sent.lower()


def test_answer_informational_blocks_dosing_language(repo: DataRepo, db: LabsDb) -> None:
    def transport(request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            text="You should take 20 mg prednisone daily.",
            tool_input=None,
            input_tokens=5,
            output_tokens=5,
        )

    client = _fake_client(transport)
    answer = answer_informational(client, repo, db, "What should I take for inflammation?")

    assert "20 mg prednisone" not in answer
    assert "withholding" in answer.lower() or "can't share" in answer.lower()


# --- informational_llm_result gates at the source (Violation 1 regression) --------------------
#
# Before this fix, `informational_llm_result` returned the model's raw text
# with no `treatment_gate` call at all — the only gated caller
# (`answer_informational`) was reachable from no production code, since
# `reason.stages.run_informational_turn` (the real `web/routes/chat.py`
# path) called `informational_llm_result` directly. These tests exercise
# `informational_llm_result` itself, proving the gate now lives at the
# source rather than depending on which entry point happens to call in.


def test_informational_llm_result_gates_dosing_language_after_a_rewrite_attempt(
    repo: DataRepo, db: LabsDb
) -> None:
    """A transport that keeps producing dosing language gets exactly one
    rewrite attempt (mirroring `stages.composer_stage`), then the answer is
    withheld rather than shown."""
    calls: list[TransportRequest] = []

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text="You should take 20 mg prednisone daily.",
            tool_input=None,
            input_tokens=5,
            output_tokens=5,
        )

    client = _fake_client(transport)
    result = informational_llm_result(client, repo, db, "What should I take for inflammation?")

    assert "20 mg prednisone" not in result.text
    assert "withholding" in result.text.lower()
    assert len(calls) == 2  # first attempt + one gate-guided rewrite, then withheld


def test_informational_llm_result_returns_the_clean_rewrite_when_the_retry_passes(
    repo: DataRepo, db: LabsDb
) -> None:
    """When the model's rewrite actually fixes the dosing language, the
    (now-passing) rewritten text is returned — the gate does not withhold
    an answer that already complies."""
    responses = iter(
        [
            "You should take 20 mg prednisone daily.",
            "Discuss adjusting your current medication dose with your rheumatologist.",
        ]
    )
    calls: list[TransportRequest] = []

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text=next(responses), tool_input=None, input_tokens=5, output_tokens=5
        )

    client = _fake_client(transport)
    result = informational_llm_result(client, repo, db, "What should I take for inflammation?")

    assert result.text == "Discuss adjusting your current medication dose with your rheumatologist."
    assert len(calls) == 2


def test_informational_llm_result_single_call_when_answer_already_passes(
    repo: DataRepo, db: LabsDb
) -> None:
    calls: list[TransportRequest] = []

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text="Your potassium has been stable in recent labs.",
            tool_input=None,
            input_tokens=5,
            output_tokens=5,
        )

    client = _fake_client(transport)
    result = informational_llm_result(client, repo, db, "How has my potassium been?")

    assert result.text == "Your potassium has been stable in recent labs."
    assert len(calls) == 1


# --- redact_gated_text (Violation 2 support) -----------------------------------------------


def test_redact_gated_text_leaves_clean_text_untouched() -> None:
    text = "Joint pain has been intermittent for six weeks."
    assert redact_gated_text(text) == text


def test_redact_gated_text_replaces_only_the_offending_span() -> None:
    text = "Patient reports joint pain. Take 20 mg prednisone daily. Follow up in 2 weeks."
    redacted = redact_gated_text(text)

    assert "20 mg prednisone" not in redacted
    assert "withheld" in redacted.lower()
    # Surrounding, unrelated content survives untouched.
    assert "Patient reports joint pain." in redacted
    assert "Follow up in 2 weeks." in redacted


def test_redact_gated_text_merges_overlapping_spans_into_one_marker() -> None:
    """The dosage-pattern span ('20 mg') sits inside the wider imperative
    span ('Take 20 mg prednisone') — the marker must appear once, not
    twice back-to-back."""
    text = "Take 20 mg prednisone daily."
    redacted = redact_gated_text(text)

    assert redacted.count("withheld") == 1


# --- reason.stages.run_informational_turn delegation --------------------------------------------


def test_run_informational_turn_delegates_to_tools_with_ledger_and_retrieval(
    repo: DataRepo, db: LabsDb
) -> None:
    calls: list[TransportRequest] = []

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text="An answer.", tool_input=None, input_tokens=1, output_tokens=1
        )

    client = _fake_client(transport)
    result = run_informational_turn(client, repo, db, "How has my potassium been?")

    assert not isinstance(result, RedFlagResult)
    assert result.text == "An answer."
    assert len(calls) == 1
    sent = "\n".join(m.content for m in calls[0].messages)
    assert "Deterministic Retrieval Results" in sent
    assert "Differential Ledger" in sent  # include_ledger=True, unlike the old bare implementation


def test_run_informational_turn_no_longer_screens_internally(repo: DataRepo, db: LabsDb) -> None:
    """The stage entry points do NOT screen (ADR 0014, warn-not-block).

    Screening moved to the entry points that own the patient conversation
    (`web/routes/chat.py`, `intake/agent.py`), which run it before any model
    call and prepend a mandatory warning to the reply. Screening here as
    well would re-introduce the block those callers deliberately removed —
    see `tests/test_web_chat.py` for the end-to-end warning contract.
    """
    calls: list[TransportRequest] = []

    def _record(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    client = _fake_client(_record)
    result = run_informational_turn(client, repo, db, "I want to kill myself")

    assert not isinstance(result, RedFlagResult)
    assert calls, "the stage runs the turn; the caller owns the screen and the warning"

"""Tests for adoc.reason.tools: the informational-turn MVP tool loop.

Fake `LlmClient` transports throughout — no network, ever.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse
from adoc.reason.safety import RedFlagResult
from adoc.reason.stages import run_informational_turn
from adoc.reason.tools import answer_informational, list_encounters, query_labs, search_case

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

"""Tests for the post-ingest reasoning pass (PLAN.md loop (a),
`adoc ingest --reason`): `reason.stages.render_new_evidence_note` +
`run_post_ingest_dag`, and the `adoc ingest --reason` CLI wiring.

Fake `VisionClient`/`LlmClient` throughout — no network, ever.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TINY_PDF_BYTES, fake_page_renderer
from pydantic import BaseModel

import adoc.cli as cli
from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.cli import main
from adoc.config import ModelBinding
from adoc.ingest.pipeline import ingest_file
from adoc.ingest.schema import ClassifyResult, DocumentExtraction
from adoc.ingest.vision import Part
from adoc.labs.db import LabsDb
from adoc.reason.client import (
    AnthropicProvider,
    LlmClient,
    OpenAIProvider,
    TransportRequest,
    TransportResponse,
)
from adoc.reason.stages import render_new_evidence_note, run_post_ingest_dag

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "extractions"


def _load_fixture(name: str) -> tuple[DocumentExtraction, DocumentExtraction]:
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return (
        DocumentExtraction.model_validate(payload["pass_a"]),
        DocumentExtraction.model_validate(payload["pass_b"]),
    )


class FakeVisionClient:
    """Same pattern as `test_ingest_pipeline.py`'s `FakeVisionClient`."""

    def __init__(self, fixture_name: str) -> None:
        self.pass_a, self.pass_b = _load_fixture(fixture_name)

    def extract(
        self,
        role: str,
        *,
        system: str,
        parts: Sequence[Part],
        schema: type[BaseModel],
        binding_index: int = 0,
        max_tokens: int = 4096,
    ) -> Any:
        if role == "classifier":
            doc_date = self.pass_a.collection_date or self.pass_a.report_date
            return ClassifyResult(doc_type=self.pass_a.doc_type, doc_date=doc_date)
        if role == "extractor_pass_a":
            return self.pass_a
        if role == "extractor_pass_b":
            return self.pass_b
        raise AssertionError(f"unexpected role: {role}")


def _fixed_clock() -> datetime:
    return datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


_LEAD_OP = {
    "op": "add_hypothesis",
    "hypothesis": {
        "id": "reactive-lead-01",
        "name": "Reactive process worth tracking",
        "tier": "cant-miss",
        "probability": "low",
        "status": "active",
        "origin": "model",
        "first_proposed": "2026-05-03",
    },
}


def _make_fake_llm_client() -> LlmClient:
    def primary_transport(request: TransportRequest) -> TransportResponse:
        assert request.schema is not None
        name = request.schema.__name__
        if name == "_LedgerDiffPayload":
            tool_input: dict[str, Any] = {"rationale": "new lab evidence", "ops": [_LEAD_OP]}
        elif name == "PatientReply":
            tool_input = {
                "tiers_rendered": "Can't-Miss: a reactive process is worth tracking.",
                "tests_to_request": [],
                "framing_ack": True,
            }
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema: {name}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=5, output_tokens=5)

    def challenger_transport(request: TransportRequest) -> TransportResponse:
        tool_input = {
            "counter_arguments": [],
            "additional_ops": [],
            "verdict_notes": "no most-likely hypothesis proposed, nothing to attack yet",
        }
        return TransportResponse(text="", tool_input=tool_input, input_tokens=5, output_tokens=5)

    bindings: dict[str, list[ModelBinding]] = {
        "primary_reasoner": [ModelBinding(provider="anthropic", model="fake-primary")],
        "challenger": [ModelBinding(provider="openai", model="fake-challenger")],
    }
    providers = {
        "anthropic": AnthropicProvider(api_key=None, transport=primary_transport),
        "openai": OpenAIProvider(api_key=None, transport=challenger_transport),
    }
    return LlmClient(bindings, providers)


# --- render_new_evidence_note --------------------------------------------------------------------


def test_render_new_evidence_note_describes_auto_rows(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "quest.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("clean_agreement.json")

    report = ingest_file(
        doc_path,
        repo=repo,
        db=db,
        vision=vision,  # type: ignore[arg-type]
        clock=_fixed_clock,
        renderer=fake_page_renderer(1),
    )

    note = render_new_evidence_note(report)
    assert note is not None
    assert "2 new lab result(s) auto-accepted" in note
    assert "quest.pdf" in note


def test_render_new_evidence_note_is_none_when_nothing_was_added(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "rheum-visit.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("non_lab_clinical_note.json")

    report = ingest_file(
        doc_path,
        repo=repo,
        db=db,
        vision=vision,  # type: ignore[arg-type]
        clock=_fixed_clock,
        renderer=fake_page_renderer(1),
    )

    assert render_new_evidence_note(report) is None


# --- run_post_ingest_dag --------------------------------------------------------------------------


def test_run_post_ingest_dag_bumps_the_ledger_version(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "quest.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("clean_agreement.json")

    report = ingest_file(
        doc_path,
        repo=repo,
        db=db,
        vision=vision,  # type: ignore[arg-type]
        clock=_fixed_clock,
        renderer=fake_page_renderer(1),
    )
    evidence_note = render_new_evidence_note(report)
    assert evidence_note is not None

    ledger_path = repo.root / LEDGER_RELPATH
    before = load_ledger(ledger_path)
    client = _make_fake_llm_client()

    new_ledger = run_post_ingest_dag(client, repo, db, ledger_path, evidence_note)

    assert new_ledger.version == before.version + 1
    assert any(h.id == "reactive-lead-01" for h in new_ledger.hypotheses)
    on_disk = load_ledger(ledger_path)
    assert on_disk.version == new_ledger.version


# --- CLI wiring: `adoc ingest --reason` -----------------------------------------------------------


def _patch_fake_ingest_wiring(monkeypatch: pytest.MonkeyPatch, fixture_name: str) -> None:
    monkeypatch.setattr(
        cli, "_build_vision_client", lambda llm_client: FakeVisionClient(fixture_name)
    )
    monkeypatch.setattr(cli, "_build_renderer", lambda: fake_page_renderer(1))
    monkeypatch.setattr(cli, "_build_llm_client", lambda settings: _make_fake_llm_client())


def test_cli_ingest_with_reason_flag_bumps_ledger_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(data_dir)
    (repo.root / "inbox" / "quest.pdf").write_bytes(TINY_PDF_BYTES)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(Path(__file__).resolve().parents[1] / "models.yaml"))
    _patch_fake_ingest_wiring(monkeypatch, "clean_agreement.json")

    code = main(["ingest", "--reason"])

    assert code == 0
    out = capsys.readouterr().out
    assert "reasoning pass updated the ledger to version 1" in out
    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger.version == 1


def test_cli_ingest_without_reason_flag_leaves_ledger_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(data_dir)
    (repo.root / "inbox" / "quest.pdf").write_bytes(TINY_PDF_BYTES)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(Path(__file__).resolve().parents[1] / "models.yaml"))
    _patch_fake_ingest_wiring(monkeypatch, "clean_agreement.json")

    code = main(["ingest"])

    assert code == 0
    out = capsys.readouterr().out
    assert "reasoning pass" not in out
    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger.version == 0


def test_cli_ingest_with_reason_flag_skips_when_nothing_was_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(data_dir)
    (repo.root / "inbox" / "rheum-visit.pdf").write_bytes(TINY_PDF_BYTES)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(Path(__file__).resolve().parents[1] / "models.yaml"))
    _patch_fake_ingest_wiring(monkeypatch, "non_lab_clinical_note.json")

    code = main(["ingest", "--reason"])

    assert code == 0
    out = capsys.readouterr().out
    assert "skipping reasoning pass" in out
    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger.version == 0

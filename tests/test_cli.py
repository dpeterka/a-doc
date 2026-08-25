"""Tests for adoc.cli: subcommand dispatch and exit codes.

Subcommands are invoked by calling `main()` directly (not via subprocess) so
that coverage.py can attribute execution to these tests. A separate
subprocess-based test exercises the real console entrypoint end-to-end for
`--help` / an unknown subcommand.

`ingest`/`backfill` are wired to the real `ingest.pipeline`; tests exercise
them by monkeypatching `adoc.cli._build_vision_client` (the seam the module
docstring calls out) so no real provider/network call ever happens.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TINY_PDF_BYTES, fake_page_renderer
from git import Repo as GitRepo

import adoc.cli as cli
from adoc.casefile.repo import DataRepo
from adoc.cli import main
from adoc.config import ModelBinding
from adoc.ingest.schema import ClassifyResult, DocumentExtraction
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeVisionClient:
    """Routes every role to a fixed `clinical_note` classification - enough
    to exercise the CLI's ingest wiring without a real provider call.
    """

    def extract(self, role, *, system, parts, schema, binding_index=0, max_tokens=4096):  # type: ignore[no-untyped-def]
        if role == "classifier":
            return ClassifyResult(doc_type="clinical_note", doc_date=None)
        return DocumentExtraction(doc_type="clinical_note")  # pragma: no cover - not reached


def _patch_fake_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_build_vision_client", lambda llm_client: _FakeVisionClient())
    monkeypatch.setattr(cli, "_build_renderer", lambda: fake_page_renderer(1))


def test_init_succeeds_with_valid_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["init"])

    assert code == 0
    out = capsys.readouterr().out
    assert "data_dir=" in out
    assert "loaded 8 model role bindings" in out


def test_init_creates_data_repo_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    first_code = main(["init"])
    first_out = capsys.readouterr().out
    assert first_code == 0
    assert f"initialized data repo at {data_dir}" in first_out
    assert (data_dir / "case" / "differential-ledger.yaml").exists()

    second_code = main(["init"])
    second_out = capsys.readouterr().out
    assert second_code == 0
    assert f"already initialized at {data_dir}" in second_out


def test_init_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["init"])

    assert code == 1
    err = capsys.readouterr().err
    assert "configuration error" in err


def test_ingest_fails_without_an_initialized_data_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["ingest"])

    assert code == 1
    assert "not initialized" in capsys.readouterr().err


def test_ingest_scans_the_inbox_and_prints_a_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(data_dir)
    (repo.root / "inbox" / "visit.pdf").write_bytes(TINY_PDF_BYTES)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    _patch_fake_vision(monkeypatch)

    code = main(["ingest"])

    assert code == 0
    out = capsys.readouterr().out
    assert "visit.pdf" in out
    assert "ingested" in out
    encounters = list((repo.root / "case" / "encounters").glob("*.md"))
    assert len(encounters) == 1


def test_backfill_ingests_a_given_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    backfill_dir = tmp_path / "historical"
    backfill_dir.mkdir()
    (backfill_dir / "old-visit.pdf").write_bytes(TINY_PDF_BYTES)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    _patch_fake_vision(monkeypatch)

    code = main(["backfill", str(backfill_dir)])

    assert code == 0
    out = capsys.readouterr().out
    assert "old-visit.pdf" in out


def test_backfill_fails_for_a_missing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["backfill", str(tmp_path / "does-not-exist")])

    assert code == 1
    assert "not a directory" in capsys.readouterr().err


def test_backfill_doc_text_extracts_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`adoc backfill-doc-text` covers a document that predates the
    document-text layer (docs/adr/0015): a `documents` row + archived
    original with no `document_text` row yet."""
    from adoc.ingest.archive import sha256_file

    data_dir = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    original = tmp_path / "old-history.txt"
    original.write_text("A pre-existing patient history.", encoding="utf-8")
    sha = sha256_file(original)
    archived = repo.root / "sources" / f"{sha}__old-history.txt"
    archived.write_text("A pre-existing patient history.", encoding="utf-8")

    db_path = data_dir / "labs.sqlite"
    with LabsDb(db_path) as db:
        db.upsert_document(
            LabDocument(
                sha256=sha,
                filename="old-history.txt",
                doc_type="other",
                page_count=1,
                status=DocumentStatus.NEEDS_REVIEW,
            )
        )

    code = main(["backfill-doc-text"])

    assert code == 0
    out = capsys.readouterr().out
    assert "checked 1 non-genomic document" in out
    assert "extracted 1" in out

    text_path = repo.root / "doc-text" / f"{sha}.txt"
    assert text_path.read_text(encoding="utf-8") == "A pre-existing patient history."

    with LabsDb(db_path) as db:
        assert db.get_document_text(sha) == "A pre-existing patient history."

    git_repo = GitRepo(repo.root)
    assert git_repo.head.commit.message.startswith("ingest: backfilled text for")


def test_backfill_doc_text_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    first_code = main(["backfill-doc-text"])
    assert first_code == 0
    first_out = capsys.readouterr().out
    assert "checked 0 non-genomic document" in first_out
    assert "extracted 0" in first_out

    second_code = main(["backfill-doc-text"])
    assert second_code == 0
    second_out = capsys.readouterr().out
    assert "extracted 0" in second_out


def test_backfill_doc_text_fails_without_an_initialized_data_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["backfill-doc-text"])

    assert code == 1
    assert "not initialized" in capsys.readouterr().err


def test_onboard_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["onboard"])

    assert code == 1
    err = capsys.readouterr().err
    assert "configuration error" in err


def test_onboard_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["onboard"])

    assert code == 1
    err = capsys.readouterr().err
    assert "run `adoc init` first" in err


def test_onboard_legacy_wizard_flag_runs_the_wizard_loop_against_an_initialized_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["init"]) == 0
    capsys.readouterr()

    def _eof_input(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)

    # Immediate EOF: no LLM call is ever made, so no network / API key is
    # needed to exercise the wiring end-to-end.
    code = main(["onboard", "--legacy-wizard"])

    assert code == 0
    out = capsys.readouterr().out
    assert "[1/10] Basics" in out
    assert "resume anytime with `adoc onboard`" in out


def test_onboard_default_runs_the_conversational_engine_against_an_initialized_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["init"]) == 0
    capsys.readouterr()

    def _eof_input(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)

    # Immediate EOF: no LLM call is ever made either (the loop returns as
    # soon as the first input_fn() call raises), so no network / API key
    # is needed here either.
    code = main(["onboard"])

    assert code == 0
    out = capsys.readouterr().out
    assert "This first conversation is how we build your case file together" in out
    assert "resume anytime with `adoc onboard`" in out
    # docs/adr/0012-initial-visit-conversation.md: no section display at all.
    assert "Basics" not in out
    assert "[1/10]" not in out


def _empty_review_fake_client() -> LlmClient:
    """A fake client that answers every review-DAG role with an empty
    result — valid against an empty seed ledger (no active hypotheses,
    so every completeness postcondition is vacuously satisfied)."""

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
        "blind_panel": [
            ModelBinding(provider="anthropic", model="fake-blind-0"),
            ModelBinding(provider="anthropic", model="fake-blind-1"),
        ],
        "challenger": [ModelBinding(provider="anthropic", model="fake-challenger")],
        "test_chooser": [ModelBinding(provider="anthropic", model="fake-test-chooser")],
    }
    providers = {"anthropic": AnthropicProvider(api_key=None, transport=transport)}
    return LlmClient(bindings, providers)


def test_review_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["review"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_review_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["review"])

    assert code == 1
    assert "run `adoc init` first" in capsys.readouterr().err


def test_review_runs_end_to_end_with_a_fake_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.setattr(cli, "_build_llm_client", lambda settings: _empty_review_fake_client())

    code = main(["review"])

    assert code == 0
    out = capsys.readouterr().out
    assert "ledger 0 -> 1" in out
    assert "tagged review-" in out


def test_labs_infer_specimen_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["labs-infer-specimen"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_labs_infer_specimen_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["labs-infer-specimen"])

    assert code == 1
    assert "run `adoc init` first" in capsys.readouterr().err


def test_labs_infer_specimen_runs_end_to_end_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    sha_urine = "a" * 64
    sha_other = "b" * 64
    with LabsDb(data_dir / "labs.sqlite") as db:
        db.upsert_document(
            LabDocument(
                sha256=sha_urine,
                filename="urinalysis-2026-05-02.pdf",
                doc_type="lab_report",
                page_count=1,
                ingested_at=datetime(2026, 5, 3, 12, 0, 0),
                status=DocumentStatus.COMPLETE,
            )
        )
        db.upsert_document(
            LabDocument(
                sha256=sha_other,
                filename="labcorp-cmp-2026-05-02.pdf",
                doc_type="lab_report",
                page_count=1,
                ingested_at=datetime(2026, 5, 3, 12, 0, 0),
                status=DocumentStatus.COMPLETE,
            )
        )
        db.insert_results(
            [
                # "specific gravity" (not in ANALYTE_SPECS at all - a pure
                # urinalysis-only analyte, D2) rather than "glucose": a
                # urine "GLUCOSE" reading canonicalizes to the same name as
                # a serum glucose reading and must stay "unknown" even in
                # an otherwise-pure urinalysis document (see
                # test_labs_specimen.py's guard-1 regression test) - this
                # end-to-end CLI test exercises the case that SHOULD apply.
                LabResult(
                    date=date(2026, 5, 2),
                    name="specific gravity",
                    name_raw="Specific Gravity",
                    value=1.02,
                    source_doc=sha_urine,
                    raw_json=json.dumps({"name_raw": "Specific Gravity"}),
                ),
                LabResult(
                    date=date(2026, 5, 2),
                    name="sodium",
                    name_raw="Sodium",
                    value=140.0,
                    ucum_unit="mmol/L",
                    source_doc=sha_other,
                    raw_json=json.dumps({"name_raw": "Sodium"}),
                ),
            ]
        )
        db.export_jsonl(data_dir / "labs-export.jsonl")
    repo.commit("seed labs data", paths=["labs-export.jsonl"])

    code = main(["labs-infer-specimen"])

    assert code == 0
    out = capsys.readouterr().out
    assert "updated 1 row(s)" in out
    assert "urine: 1" in out
    assert "1 row(s) remain unknown" in out

    with LabsDb(data_dir / "labs.sqlite") as db:
        rows = {row.name: row for row in db.series("specific gravity") + db.series("sodium")}
        assert rows["specific gravity"].specimen == "urine"
        assert rows["sodium"].specimen == "unknown"

    export_text = (data_dir / "labs-export.jsonl").read_text(encoding="utf-8")
    assert '"specimen": "urine"' in export_text

    # idempotent: a second run finds nothing left to update
    second_code = main(["labs-infer-specimen"])
    assert second_code == 0
    second_out = capsys.readouterr().out
    assert "updated 0 row(s)" in second_out


# --------------------------------------------------------------------------
# labs-dedupe-twins (queue-ergonomics slice item 4)
# --------------------------------------------------------------------------


def _exploding_classifier_client() -> LlmClient:
    """A fake `LlmClient` whose `classifier` transport explodes if ever
    called - the rule-path twin case below must never reach the LLM."""

    def _transport(request: TransportRequest) -> TransportResponse:
        raise AssertionError("the classifier LLM must not be called for a rule-path twin")

    bindings = {"classifier": [ModelBinding(provider="anthropic", model="fake-haiku")]}
    providers = {"anthropic": AnthropicProvider(api_key=None, transport=_transport)}
    return LlmClient(bindings, providers)


def test_labs_dedupe_twins_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["labs-dedupe-twins"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_labs_dedupe_twins_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["labs-dedupe-twins"])

    assert code == 1
    assert "run `adoc init` first" in capsys.readouterr().err


def _seed_twin_pair(data_dir: Path) -> tuple[str, int]:
    """One AUTO row and its rule-path twin PENDING single_pass row, same
    document/page/value - returns `(sha256, pending_row_id)`.

    D3: the rule path decides an EXACT match only (after
    `clean_result_name` + casefold) - a trailing-sentence-fragment variant
    of the same name still qualifies, unlike a token-subset pair (e.g.
    "T-Score" vs "LEFT HIP femoral neck T-Score"), which now requires the
    LLM path instead.
    """
    sha = "a" * 64
    repo = DataRepo.init_at(data_dir)
    with LabsDb(data_dir / "labs.sqlite") as db:
        db.upsert_document(
            LabDocument(
                sha256=sha,
                filename="dexa.pdf",
                doc_type="lab_report",
                page_count=6,
                ingested_at=datetime(2026, 5, 3, 12, 0, 0),
                status=DocumentStatus.COMPLETE,
            )
        )
        db.insert_results(
            [
                LabResult(
                    date=date(2026, 5, 2),
                    name="frax",
                    name_raw="FRAX 10-year probability of hip fracture",
                    value=-1.2,
                    source_doc=sha,
                    source_page=5,
                    raw_json=json.dumps({"reasons": []}),
                )
            ]
        )
        (pending_id,) = db.insert_results(
            [
                LabResult(
                    date=date(2026, 5, 2),
                    name="frax-2",
                    name_raw="frax 10-year probability of hip fracture is",
                    value=-1.2,
                    source_doc=sha,
                    source_page=5,
                    extraction_status=ExtractionStatus.PENDING,
                    raw_json=json.dumps({"reasons": ["single_pass"]}),
                )
            ]
        )
        db.export_jsonl(data_dir / "labs-export.jsonl")
    repo.commit("seed twin pair", paths=["labs-export.jsonl"])
    assert pending_id is not None
    return sha, pending_id


def test_labs_dedupe_twins_dry_run_reports_and_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.setattr(cli, "_build_llm_client", lambda settings: _exploding_classifier_client())
    _sha, pending_id = _seed_twin_pair(data_dir)

    code = main(["labs-dedupe-twins", "--dry-run"])

    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "would reject 1" in out
    assert not (data_dir / "work" / "twin-sweep.json").exists()

    with LabsDb(data_dir / "labs.sqlite") as db:
        row = db.get_row(pending_id)
        assert row is not None
        assert row.extraction_status == ExtractionStatus.PENDING


def test_labs_dedupe_twins_runs_end_to_end_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.setattr(cli, "_build_llm_client", lambda settings: _exploding_classifier_client())
    _sha, pending_id = _seed_twin_pair(data_dir)
    repo = DataRepo(data_dir)
    git_repo = GitRepo(repo.root)
    commits_before = len(list(git_repo.iter_commits()))

    code = main(["labs-dedupe-twins"])

    assert code == 0
    out = capsys.readouterr().out
    assert "rejected 1" in out

    with LabsDb(data_dir / "labs.sqlite") as db:
        row = db.get_row(pending_id)
        assert row is not None
        assert row.extraction_status == ExtractionStatus.REJECTED
        assert row.raw_payload()["method"] == "rule"

    commits_after = len(list(git_repo.iter_commits()))
    assert commits_after == commits_before + 1

    summary_path = data_dir / "work" / "twin-sweep.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["rejected"] == 1

    # idempotent: a second run finds nothing left to reject
    second_code = main(["labs-dedupe-twins"])
    assert second_code == 0
    second_out = capsys.readouterr().out
    assert "rejected 0" in second_out


# --------------------------------------------------------------------------
# labs-reclassify (feature/semantic-compare)
# --------------------------------------------------------------------------


def test_labs_reclassify_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["labs-reclassify"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_labs_reclassify_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["labs-reclassify"])

    assert code == 1
    assert "run `adoc init` first" in capsys.readouterr().err


def _seed_false_ref_range_mismatch(data_dir: Path) -> tuple[DataRepo, int]:
    """A PENDING row that queued under the old literal ref-range comparison
    ("<20" vs "<20 Units") - a comparator false positive under the new
    semantic comparators."""
    sha = "a" * 64
    repo = DataRepo.init_at(data_dir)
    with LabsDb(data_dir / "labs.sqlite") as db:
        db.upsert_document(
            LabDocument(
                sha256=sha,
                filename="doc.pdf",
                doc_type="lab_report",
                page_count=1,
                ingested_at=datetime(2026, 5, 3, 12, 0, 0),
                status=DocumentStatus.COMPLETE,
            )
        )
        pass_a = {
            "name_raw": "Potassium",
            "value": 4.1,
            "value_text": None,
            "unit_raw": "mmol/L",
            "ref_range_raw": "<20",
            "flag_raw": None,
            "specimen": "unknown",
            "page": 1,
            "confidence": "high",
        }
        pass_b = {**pass_a, "ref_range_raw": "<20 Units"}
        (pending_id,) = db.insert_results(
            [
                LabResult(
                    date=date(2026, 5, 2),
                    name="potassium",
                    name_raw="Potassium",
                    value=4.1,
                    ucum_unit="mmol/L",
                    source_doc=sha,
                    extraction_status=ExtractionStatus.PENDING,
                    raw_json=json.dumps(
                        {
                            "pass_a": pass_a,
                            "pass_b": pass_b,
                            "reasons": ["ref_range_mismatch: '<20' vs '<20 Units'"],
                        }
                    ),
                )
            ]
        )
        db.export_jsonl(data_dir / "labs-export.jsonl")
    repo.commit("seed false ref-range mismatch", paths=["labs-export.jsonl"])
    assert pending_id is not None
    return repo, pending_id


def test_labs_reclassify_dry_run_reports_and_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    _repo, pending_id = _seed_false_ref_range_mismatch(data_dir)

    code = main(["labs-reclassify", "--dry-run"])

    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "would auto-flip 1" in out

    with LabsDb(data_dir / "labs.sqlite") as db:
        row = db.get_row(pending_id)
        assert row is not None
        assert row.extraction_status == ExtractionStatus.PENDING


def test_labs_reclassify_runs_end_to_end_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    repo, pending_id = _seed_false_ref_range_mismatch(data_dir)
    git_repo = GitRepo(repo.root)
    commits_before = len(list(git_repo.iter_commits()))

    code = main(["labs-reclassify"])

    assert code == 0
    out = capsys.readouterr().out
    assert "auto-flipped 1" in out

    with LabsDb(data_dir / "labs.sqlite") as db:
        row = db.get_row(pending_id)
        assert row is not None
        assert row.extraction_status == ExtractionStatus.AUTO
        payload = row.raw_payload()
        assert payload["reasons"] == []
        assert "reclassified_at" in payload

    commits_after = len(list(git_repo.iter_commits()))
    assert commits_after == commits_before + 1

    # idempotent: a second run finds nothing left to flip
    second_code = main(["labs-reclassify"])
    assert second_code == 0
    second_out = capsys.readouterr().out
    assert "auto-flipped 0" in second_out


# --------------------------------------------------------------------------
# intake-corroborate (docs/adr/0013-fact-corroboration.md)
# --------------------------------------------------------------------------


def test_intake_corroborate_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["intake-corroborate"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_intake_corroborate_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["intake-corroborate"])

    assert code == 1
    assert "run `adoc init` first" in capsys.readouterr().err


def _seed_future_year_diagnosis(data_dir: Path) -> DataRepo:
    from adoc.casefile.schema import Provenance
    from adoc.intake.facts import AddFact, IntakeFactsStore, NewFact

    repo = DataRepo.init_at(data_dir)
    store = IntakeFactsStore(repo.root)
    store.apply_ops(
        [
            AddFact(
                fact=NewFact(
                    id="future-dx",
                    section="prior_diagnoses",
                    kind="diagnosis",
                    statement="A doctor diagnosed lupus in 2099.",
                    attribution="doctor_diagnosed",
                    fields={"year": 2099, "by_whom": "Dr. Lee"},
                )
            )
        ],
        Provenance(
            app_version="0.0.0-test",
            prompt_template_version="1",
            model_id="fake",
            dag_node="intake-agent",
            timestamp=datetime(2026, 1, 1),
        ),
    )
    store.save()
    repo.commit("seed a future-year diagnosis fact", paths=["case/intake-facts.yaml"])
    return repo


def test_intake_corroborate_dry_run_reports_and_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    _seed_future_year_diagnosis(data_dir)

    code = main(["intake-corroborate", "--dry-run"])

    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "would update 1" in out

    from adoc.intake.facts import IntakeFactsStore

    store = IntakeFactsStore(data_dir)
    fact = store.get("future-dx")
    assert fact is not None
    assert fact.corroboration == "unverified"  # untouched by --dry-run


def test_intake_corroborate_runs_end_to_end_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    repo = _seed_future_year_diagnosis(data_dir)
    git_repo = GitRepo(repo.root)
    commits_before = len(list(git_repo.iter_commits()))

    code = main(["intake-corroborate"])

    assert code == 0
    out = capsys.readouterr().out
    assert "updated 1" in out
    assert "contradicted: 1" in out

    from adoc.intake.facts import IntakeFactsStore

    store = IntakeFactsStore(data_dir)
    fact = store.get("future-dx")
    assert fact is not None
    assert fact.corroboration == "contradicted"

    commits_after = len(list(git_repo.iter_commits()))
    assert commits_after == commits_before + 1

    # idempotent: a second run finds nothing left to update
    second_code = main(["intake-corroborate"])
    assert second_code == 0
    second_out = capsys.readouterr().out
    assert "updated 0" in second_out


def test_eval_runs_offline_with_out_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "eval-out"

    code = main(["eval", "--suite", "extraction", "--suite", "redteam", "--out", str(out_dir)])

    assert code == 0
    out = capsys.readouterr().out
    assert "extraction:" in out
    assert "redteam:" in out
    assert (out_dir / "extraction-report.md").exists()
    assert (out_dir / "redteam-report.md").exists()


def test_eval_default_suites_runs_all_known_suites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "eval-out"

    code = main(["eval", "--out", str(out_dir)])

    assert code == 0
    assert (out_dir / "extraction-report.md").exists()
    assert (out_dir / "redteam-report.md").exists()


def test_eval_with_candidate_writes_a_comparison_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "eval-out"

    code = main(
        [
            "eval",
            "--suite",
            "redteam",
            "--out",
            str(out_dir),
            "--candidate",
            "openai:gpt-9000",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "comparison report written" in out
    assert (out_dir / "redteam-comparison.md").exists()


def test_serve_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["serve"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_serve_builds_the_app_and_runs_uvicorn_with_host_and_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        cli, "_run_uvicorn", lambda app, *, host, port: calls.append((app, host, port))
    )

    code = main(["serve", "--host", "0.0.0.0", "--port", "9000"])

    assert code == 0
    assert len(calls) == 1
    app, host, port = calls[0]
    assert host == "0.0.0.0"
    assert port == 9000
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    assert "starting on http://0.0.0.0:9000" in capsys.readouterr().out


def test_serve_defaults_to_localhost_8080(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        cli, "_run_uvicorn", lambda app, *, host, port: calls.append((app, host, port))
    )

    code = main(["serve"])

    assert code == 0
    _app, host, port = calls[0]
    assert host == "127.0.0.1"
    assert port == 8080


def test_user_add_creates_a_user_with_injected_getpass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    passwords = iter(["a-good-password", "a-good-password"])
    monkeypatch.setattr(cli, "_getpass", lambda _prompt: next(passwords))

    code = main(["user", "add", "alice"])

    assert code == 0
    assert "added user 'alice'" in capsys.readouterr().out
    from adoc.web.users import verify_user

    assert verify_user(data_dir / "work" / "users.yaml", "alice", "a-good-password") is True


def test_user_add_fails_when_passwords_do_not_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    passwords = iter(["first-password", "second-password"])
    monkeypatch.setattr(cli, "_getpass", lambda _prompt: next(passwords))

    code = main(["user", "add", "alice"])

    assert code == 1
    assert "did not match" in capsys.readouterr().err


def test_user_add_fails_on_empty_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    monkeypatch.setattr(cli, "_getpass", lambda _prompt: "")

    code = main(["user", "add", "alice"])

    assert code == 1
    assert "must not be empty" in capsys.readouterr().err


def test_user_list_reports_no_users_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["user", "list"])

    assert code == 0
    assert "no users configured" in capsys.readouterr().out


def test_user_list_prints_usernames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    from adoc.web.users import add_user

    add_user(data_dir / "work" / "users.yaml", "alice", "some-password")

    code = main(["user", "list"])

    assert code == 0
    assert "alice" in capsys.readouterr().out


def test_user_remove_removes_an_existing_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    from adoc.web.users import add_user

    add_user(data_dir / "work" / "users.yaml", "alice", "some-password")

    code = main(["user", "remove", "alice"])

    assert code == 0
    assert "removed user 'alice'" in capsys.readouterr().out


def test_user_remove_fails_for_an_unknown_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["user", "remove", "no-such-user"])

    assert code == 1
    assert "no such user" in capsys.readouterr().err


def test_user_add_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["user", "add", "alice"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_backup_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["backup"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_backup_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))

    code = main(["backup"])

    assert code == 1
    assert "run `adoc init` first" in capsys.readouterr().err


def test_backup_fails_without_a_bucket_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client

    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.delenv("ADOC_BACKUP_BUCKET", raising=False)
    monkeypatch.setattr(cli, "_build_s3_client", lambda: FakeS3Client())

    code = main(["backup"])

    assert code == 1
    assert "ADOC_BACKUP_BUCKET" in capsys.readouterr().err


def test_backup_runs_end_to_end_with_a_fake_s3_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client

    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")
    fake_s3 = FakeS3Client()
    monkeypatch.setattr(cli, "_build_s3_client", lambda: fake_s3)

    code = main(["backup"])

    assert code == 0
    out = capsys.readouterr().out
    assert "bundle uploaded to s3://a-doc-backup-bucket/latest/a-doc-data.bundle" in out
    assert any(key == "latest/a-doc-data.bundle" for _, _, key in fake_s3.uploads)


def test_restore_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["restore"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_restore_fails_without_a_bucket_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client

    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.delenv("ADOC_BACKUP_BUCKET", raising=False)
    monkeypatch.setattr(cli, "_build_s3_client", lambda: FakeS3Client())

    code = main(["restore"])

    assert code == 1
    assert "ADOC_BACKUP_BUCKET" in capsys.readouterr().err


def test_restore_fails_if_data_dir_already_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client

    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")
    monkeypatch.setattr(cli, "_build_s3_client", lambda: FakeS3Client())

    code = main(["restore"])

    assert code == 1
    assert "already contains an initialized data repo" in capsys.readouterr().err


def test_restore_runs_end_to_end_with_a_fake_s3_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client, _seed_with_labs

    from adoc.backup import run_backup

    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    _seed_with_labs(src)
    fake_s3 = FakeS3Client()
    run_backup(src, "a-doc-backup-bucket", fake_s3)

    dst = tmp_path / "restored-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(dst))
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")
    monkeypatch.setattr(cli, "_build_s3_client", lambda: fake_s3)

    code = main(["restore"])

    assert code == 0
    out = capsys.readouterr().out
    assert "cloned bundle" in out
    assert "sources/ - 3 restored" in out
    assert "labs.sqlite rebuilt from labs-export.jsonl - 2 rows" in out
    assert DataRepo(dst).is_initialized


def test_restore_bucket_flag_overrides_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client, _seed_with_labs

    from adoc.backup import run_backup

    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    _seed_with_labs(src)
    fake_s3 = FakeS3Client()
    run_backup(src, "the-real-bucket", fake_s3)

    dst = tmp_path / "restored-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(dst))
    monkeypatch.delenv("ADOC_BACKUP_BUCKET", raising=False)
    monkeypatch.setattr(cli, "_build_s3_client", lambda: fake_s3)

    code = main(["restore", "--bucket", "the-real-bucket"])

    assert code == 0
    assert DataRepo(dst).is_initialized


def test_bootstrap_data_noop_when_data_dir_already_has_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    data_dir.mkdir()
    (data_dir / "some-file").write_text("already here", encoding="utf-8")
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")

    code = main(["bootstrap-data"])

    assert code == 0
    assert "already has data" in capsys.readouterr().out
    assert not (data_dir / ".git").exists()


def test_bootstrap_data_initializes_when_no_bucket_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.delenv("ADOC_BACKUP_BUCKET", raising=False)

    code = main(["bootstrap-data"])

    assert code == 0
    assert "initialized data repo" in capsys.readouterr().out
    assert DataRepo(data_dir).is_initialized


def test_bootstrap_data_restores_when_the_bucket_has_a_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client, _seed_with_labs

    from adoc.backup import run_backup

    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    _seed_with_labs(src)
    fake_s3 = FakeS3Client()
    run_backup(src, "a-doc-backup-bucket", fake_s3)

    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")
    monkeypatch.setattr(cli, "_build_s3_client", lambda: fake_s3)

    code = main(["bootstrap-data"])

    assert code == 0
    out = capsys.readouterr().out
    assert "restored from s3://a-doc-backup-bucket/latest/" in out
    assert DataRepo(data_dir).is_initialized


def test_bootstrap_data_falls_back_to_init_when_the_bucket_has_no_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_backup import FakeS3Client

    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")
    monkeypatch.setattr(cli, "_build_s3_client", lambda: FakeS3Client())

    code = main(["bootstrap-data"])

    assert code == 0
    out = capsys.readouterr().out
    assert "falling back to 'adoc init'" in out
    assert "initialized data repo" in out
    assert DataRepo(data_dir).is_initialized


def test_bootstrap_data_fails_loudly_on_a_real_restore_error_instead_of_silently_initializing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class _ExplodingS3Client:
        def list_objects_v2(
            self, Bucket: str, Prefix: str, ContinuationToken: str | None = None
        ) -> dict[str, Any]:
            raise RuntimeError("simulated AWS outage")

        def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
            raise RuntimeError("simulated AWS outage")  # pragma: no cover - not reached

        def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
            raise RuntimeError("simulated AWS outage")  # pragma: no cover - not reached

    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")
    monkeypatch.setattr(cli, "_build_s3_client", lambda: _ExplodingS3Client())

    code = main(["bootstrap-data"])

    assert code == 1
    err = capsys.readouterr().err
    assert "restore failed" in err
    # the outage must not be papered over by silently falling back to init
    assert not DataRepo(data_dir).is_initialized


def test_unknown_subcommand_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["not-a-real-command"])
    assert excinfo.value.code != 0


def test_console_entrypoint_help_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "adoc.cli", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "adoc" in result.stdout

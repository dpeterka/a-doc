"""Upload surface tests: a saved file is run through the real ingestion
pipeline and its `IngestReport` (rows added / queued) is shown.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from conftest import TINY_PDF_BYTES, fake_page_renderer
from fastapi.testclient import TestClient
from pydantic import BaseModel
from web_support import build_app, login

from adoc.ingest.schema import ClassifyResult, DocumentExtraction
from adoc.ingest.vision import Part

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "extractions"


def _load_fixture(name: str) -> tuple[DocumentExtraction, DocumentExtraction]:
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return (
        DocumentExtraction.model_validate(payload["pass_a"]),
        DocumentExtraction.model_validate(payload["pass_b"]),
    )


class FakeVisionClient:
    """Duck-types `VisionClient.extract` over a fixture pair — no network."""

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


def test_upload_page_loads(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/upload")

    assert response.status_code == 200
    assert "Add a document" in response.text


def test_upload_page_states_supported_types_and_sets_accept_attribute(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/upload")

    assert response.status_code == 200
    assert 'accept=".pdf,.docx,.txt,.zip"' in response.text
    assert "PDF, Word (.docx), text (.txt), and zip (.zip) files" in response.text
    assert "lab reports" in response.text.lower()
    # The genomics note (item 5 of the genomics/filetypes task): genetic
    # data files are stored, not read as documents.
    assert "23andMe" in response.text
    assert "VCF/BCF" in response.text


def test_upload_rejects_unsupported_file_type_with_friendly_message(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/upload",
        files={"file": ("photo.png", b"\x89PNG\r\n\x1a\nnot really a png", "image/png")},
    )

    assert response.status_code == 200
    body = response.text
    assert "photo.png" in body
    assert ".png" in body
    assert "PDF, Word (.docx), text (.txt), and zip (.zip) files" in body
    # A warm message, never a stack trace / exception repr.
    assert "Traceback" not in body
    assert "Exception" not in body

    # Never archived, never left behind in the inbox — junk is discarded,
    # not silently accumulated.
    assert not (repo.root / "inbox" / "photo.png").exists()
    assert not any((repo.root / "sources").glob("*__photo.png"))
    assert db.list_documents() == []


def test_upload_ingests_and_shows_the_report(tmp_path: Path) -> None:
    vision = FakeVisionClient("clean_agreement.json")
    app, repo, db, _calls = build_app(tmp_path, vision=vision, renderer=fake_page_renderer(1))  # type: ignore[arg-type]
    client = TestClient(app)
    login(client)

    response = client.post(
        "/upload",
        files={"file": ("quest-2026-05-02.pdf", TINY_PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 200
    assert "<strong>2</strong> row" in response.text  # rows_auto from the fixture
    # Post-ingest inbox hygiene: the inbox copy is deleted once ingested —
    # the immutable sources/ archive is the authoritative copy.
    assert not (repo.root / "inbox" / "quest-2026-05-02.pdf").exists()
    assert len(db.list_documents()) == 1


def _exploding_renderer(_pdf_path: Path, _out_dir: Path) -> list[Path]:
    """A renderer that fails with something OTHER than `VisionError` or
    `ArchiveError` — an unexpected failure neither `_ingest_one` nor
    `upload_submit`'s narrower except used to catch."""
    raise RuntimeError("poppler crashed")


def test_an_unexpected_ingest_failure_shows_a_clean_error_and_cleans_the_inbox(
    tmp_path: Path,
) -> None:
    """Before this, only `VisionError` was caught in `upload_submit`. A
    `RuntimeError` from anywhere else in the pipeline (a poppler crash, a
    database error, a malformed-metadata `ValidationError`) propagated
    straight out as a raw HTTP 500, and — because the exception escaped
    before post-ingest hygiene ever ran — the uploaded file stayed orphaned
    in `inbox/`, primed to fail identically on every future scheduled sweep.
    """
    vision = FakeVisionClient("clean_agreement.json")
    app, repo, _db, _calls = build_app(  # type: ignore[arg-type]
        tmp_path, vision=vision, renderer=_exploding_renderer
    )
    client = TestClient(app)
    login(client)

    response = client.post(
        "/upload",
        files={"file": ("quest-2026-05-02.pdf", TINY_PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 200, "an unexpected pipeline failure must not 500"
    assert "went wrong" in response.text.lower()
    assert not (repo.root / "inbox" / "quest-2026-05-02.pdf").exists(), (
        "the failed upload was left orphaned in inbox/"
    )
    assert (repo.root / "work" / "failed" / "quest-2026-05-02.pdf").exists(), (
        "the failed upload was not routed through the normal failed/ hygiene path"
    )


GENOMIC_23ANDME_TEXT = (
    "# This data file generated by 23andMe at: Mon Jan 01 00:00:00 2026\n"
    "# Below is a text version of your data.\n"
    "# rsid\tchromosome\tposition\tgenotype\n"
    "rs4477212\t1\t82154\tAA\n"
    "rs3094315\t1\t752566\tAG\n"
)


def test_upload_archives_a_genomic_text_file_with_zero_llm_calls(tmp_path: Path) -> None:
    """CRITICAL DESIGN RULE: a genomic file never reaches the LLM
    pipeline. `build_app`'s default transports explode if ever called, so
    a clean 200 here with an empty `calls` list is the "zero LLM calls"
    proof (not just an assertion after the fact)."""
    app, repo, db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/upload",
        files={"file": ("genome_First_Last.txt", GENOMIC_23ANDME_TEXT.encode(), "text/plain")},
    )

    assert response.status_code == 200
    assert calls == []
    assert not (repo.root / "inbox" / "genome_First_Last.txt").exists()

    documents = db.list_documents()
    assert len(documents) == 1
    assert documents[0].doc_type == "genomic_data"

    inventory = (repo.root / "case" / "genomics-inventory.md").read_text(encoding="utf-8")
    assert "genome_First_Last.txt" in inventory
    assert "23andMe raw export" in inventory

    # No junk encounter for a genomic file.
    assert list((repo.root / "case" / "encounters").glob("*.md")) == []


def test_upload_rejects_a_file_one_byte_over_the_configured_cap(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path, max_upload_mb=1)
    client = TestClient(app)
    login(client)
    one_mb = 1024 * 1024
    oversized = b"0" * (one_mb + 1)

    response = client.post(
        "/upload",
        files={"file": ("big.pdf", oversized, "application/pdf")},
    )

    assert response.status_code == 200
    body = response.text
    assert "big.pdf" in body
    assert "larger than the 1 mb" in body.lower()
    assert "Traceback" not in body
    # Never left behind, never handed to the ingestion pipeline.
    assert not (repo.root / "inbox" / "big.pdf").exists()
    assert db.list_documents() == []


def test_upload_accepts_a_file_exactly_at_the_configured_cap(tmp_path: Path) -> None:
    """At exactly the boundary the size gate must pass the file through —
    it lands on the (separate) unsupported-file-type rejection instead of
    the oversized-file message, proving the gate didn't trip early."""
    app, repo, db, _calls = build_app(tmp_path, max_upload_mb=1)
    client = TestClient(app)
    login(client)
    one_mb = 1024 * 1024
    at_boundary = b"\x89PNG\r\n\x1a\n" + b"0" * (one_mb - 8)
    assert len(at_boundary) == one_mb

    response = client.post(
        "/upload",
        files={"file": ("boundary.png", at_boundary, "image/png")},
    )

    assert response.status_code == 200
    body = response.text
    assert "larger than" not in body.lower()
    assert "boundary.png" in body
    assert not (repo.root / "inbox" / "boundary.png").exists()
    assert db.list_documents() == []


def test_upload_expands_a_zip_and_ingests_its_pdf_member(tmp_path: Path) -> None:
    import io
    import zipfile

    vision = FakeVisionClient("clean_agreement.json")
    app, repo, db, _calls = build_app(tmp_path, vision=vision, renderer=fake_page_renderer(1))  # type: ignore[arg-type]
    client = TestClient(app)
    login(client)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("quest-2026-05-02.pdf", TINY_PDF_BYTES)
    zip_bytes = buf.getvalue()

    response = client.post(
        "/upload",
        files={"file": ("bundle.zip", zip_bytes, "application/zip")},
    )

    assert response.status_code == 200
    assert not (repo.root / "inbox" / "bundle.zip").exists()
    assert len(db.list_documents()) == 1

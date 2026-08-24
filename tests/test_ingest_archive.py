"""Tests for adoc.ingest.archive: sha256 archival, page rendering, dedupe."""

from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path

import pytest
from conftest import TINY_PDF_BYTES, fake_page_renderer

from adoc.ingest.archive import ArchiveError, archive_document, sha256_file
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, LabDocument


def test_sha256_file_is_deterministic(tiny_pdf_path: Path) -> None:
    assert sha256_file(tiny_pdf_path) == sha256_file(tiny_pdf_path)
    assert len(sha256_file(tiny_pdf_path)) == 64


def test_archive_document_copies_immutable_original_and_renders_pages(
    tmp_path: Path, tiny_pdf_path: Path
) -> None:
    repo_root = tmp_path / "data-repo"
    db = LabsDb(tmp_path / "labs.sqlite")

    archived = archive_document(repo_root, tiny_pdf_path, db=db, renderer=fake_page_renderer(3))

    assert archived.original_path.exists()
    assert archived.original_path.read_bytes() == TINY_PDF_BYTES
    assert archived.original_path.name == f"{archived.sha256}__document.pdf"
    assert archived.original_path.parent == repo_root / "sources"
    assert len(archived.page_paths) == 3
    for page_path in archived.page_paths:
        assert page_path.exists()
        assert page_path.parent == repo_root / "sources" / "pages" / archived.sha256
    assert archived.already_ingested is False


def test_archive_document_is_idempotent_and_does_not_re_render(
    tmp_path: Path, tiny_pdf_path: Path
) -> None:
    repo_root = tmp_path / "data-repo"
    db = LabsDb(tmp_path / "labs.sqlite")
    calls = {"count": 0}

    def counting_renderer(pdf_path: Path, out_dir: Path) -> list[Path]:
        calls["count"] += 1
        return fake_page_renderer(2)(pdf_path, out_dir)

    first = archive_document(repo_root, tiny_pdf_path, db=db, renderer=counting_renderer)
    second = archive_document(repo_root, tiny_pdf_path, db=db, renderer=counting_renderer)

    assert calls["count"] == 1
    assert first.sha256 == second.sha256
    assert first.page_paths == second.page_paths


def test_archive_document_reports_already_ingested_from_labs_db(
    tmp_path: Path, tiny_pdf_path: Path
) -> None:
    repo_root = tmp_path / "data-repo"
    db = LabsDb(tmp_path / "labs.sqlite")
    sha = sha256_file(tiny_pdf_path)
    db.upsert_document(
        LabDocument(
            sha256=sha,
            filename="document.pdf",
            doc_type="lab_report",
            doc_date=date(2026, 5, 2),
            page_count=1,
            ingested_at=datetime(2026, 5, 3, 0, 0, 0),
            status=DocumentStatus.COMPLETE,
        )
    )

    archived = archive_document(repo_root, tiny_pdf_path, db=db, renderer=fake_page_renderer(1))

    assert archived.already_ingested is True


def test_pdftoppm_renderer_raises_clear_error_when_binary_missing(
    tmp_path: Path, tiny_pdf_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    db = LabsDb(tmp_path / "labs.sqlite")
    with pytest.raises(ArchiveError, match="pdftoppm"):
        archive_document(tmp_path / "data-repo", tiny_pdf_path, db=db)


def test_archive_rejects_non_pdf_files(tmp_path: Path) -> None:
    """Junk/docx must never reach the immutable sources/ store."""
    import pytest

    from adoc.ingest.archive import ArchiveError, archive_document

    bogus = tmp_path / "notes.docx"
    bogus.write_bytes(b"PK\x03\x04 not a pdf")

    with pytest.raises(ArchiveError, match="not a PDF"):
        archive_document(tmp_path, bogus, db=None, renderer=None)  # type: ignore[arg-type]

"""Consumed-documents page tests ("Documents > Consumed"): every ingested
document, newest first, with a plain-language type, a friendly date, a
status chip, and per-document row counts — a genomic file shows "stored
for later genomic analysis" instead of counts, since it never gets labs
rows at all.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult

OLDER_SHA = "a" * 64
NEWER_SHA = "b" * 64
GENOMIC_SHA = "c" * 64


def _seed(db) -> None:
    db.upsert_document(
        LabDocument(
            sha256=OLDER_SHA,
            filename="older-report.pdf",
            doc_type="lab_report",
            page_count=1,
            ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
            status=DocumentStatus.COMPLETE,
        )
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 1, 1),
                name="crp",
                name_raw="CRP",
                value=6.0,
                source_doc=OLDER_SHA,
                extraction_status=ExtractionStatus.AUTO,
                raw_json=json.dumps({}),
            ),
            LabResult(
                date=date(2026, 1, 1),
                name="esr",
                name_raw="ESR",
                value=10.0,
                source_doc=OLDER_SHA,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=json.dumps({}),
            ),
        ]
    )

    db.upsert_document(
        LabDocument(
            sha256=NEWER_SHA,
            filename="newer-report.pdf",
            doc_type="imaging_report",
            page_count=2,
            ingested_at=datetime(2026, 6, 1, tzinfo=UTC),
            status=DocumentStatus.NEEDS_REVIEW,
        )
    )

    db.upsert_document(
        LabDocument(
            sha256=GENOMIC_SHA,
            filename="23andme-export.txt",
            doc_type="genomic_data",
            page_count=1,
            ingested_at=datetime(2026, 7, 1, tzinfo=UTC),
            status=DocumentStatus.COMPLETE,
        )
    )


def test_consumed_page_lists_documents_newest_first(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    assert response.status_code == 200
    body = response.text
    genomic_pos = body.index("23andme-export.txt")
    newer_pos = body.index("newer-report.pdf")
    older_pos = body.index("older-report.pdf")
    assert genomic_pos < newer_pos < older_pos


def test_consumed_page_shows_friendly_type_and_date(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    body = response.text
    assert "lab report" in body
    assert "imaging report" in body
    assert "genomic data" in body
    assert "January 01, 2026" in body


def test_consumed_page_shows_accepted_and_awaiting_review_counts(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    assert "1 accepted / 1 awaiting review" in response.text


def test_consumed_page_shows_genomics_wording_instead_of_counts(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    assert "stored for later genomic analysis" in response.text


def test_consumed_page_links_a_lab_document_to_its_archived_original(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    assert f"/files/original/{OLDER_SHA}" in response.text


def test_consumed_page_empty_state(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    assert response.status_code == 200
    assert "No documents yet" in response.text


def test_consumed_page_requires_login(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/documents/consumed", follow_redirects=False)

    assert response.status_code in (302, 303, 307, 308)

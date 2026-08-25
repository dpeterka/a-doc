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

from adoc.labs.db import DocumentTextPage
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


# --------------------------------------------------------------------------
# Extracted-text view (docs/adr/0015-document-text-corpus.md)
# --------------------------------------------------------------------------


def test_consumed_page_links_to_text_view_when_text_is_on_file(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    db.replace_document_text(
        OLDER_SHA,
        [DocumentTextPage(page=1, text="Impression: unremarkable.")],
        extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    assert f"/documents/consumed/{OLDER_SHA}/text" in response.text


def test_consumed_page_shows_dash_when_no_text_on_file(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")

    assert f"/documents/consumed/{OLDER_SHA}/text" not in response.text
    assert f"/documents/consumed/{GENOMIC_SHA}/text" not in response.text


def test_text_view_renders_the_stored_text(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    db.replace_document_text(
        OLDER_SHA,
        [DocumentTextPage(page=1, text="Impression: unremarkable findings.")],
        extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    client = TestClient(app)
    login(client)

    response = client.get(f"/documents/consumed/{OLDER_SHA}/text")

    assert response.status_code == 200
    assert "Impression: unremarkable findings." in response.text
    assert "older-report.pdf" in response.text


def test_text_view_404s_when_no_text_stored(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    response = client.get(f"/documents/consumed/{OLDER_SHA}/text")

    assert response.status_code == 404


def test_text_view_404s_for_unknown_sha(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get(f"/documents/consumed/{'0' * 64}/text")

    assert response.status_code == 404


def test_text_view_404s_for_unsafe_sha(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed/../../etc/passwd/text")

    assert response.status_code == 404


def test_genomic_document_never_offers_a_text_view_even_if_somehow_present(
    tmp_path: Path,
) -> None:
    """Defense in depth: even if a `document_text` row somehow existed for
    a genomic sha (never happens in practice — see `ingest.doctext`'s
    genomics exclusion), the route layer refuses to surface it: the
    consumed-page row never links to a text view, and the text-view route
    itself 404s.
    """
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    db.replace_document_text(
        GENOMIC_SHA,
        [DocumentTextPage(page=None, text="this should never be reachable")],
        extracted_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    client = TestClient(app)
    login(client)

    response = client.get("/documents/consumed")
    body = response.text
    genomic_row_start = body.index("23andme-export.txt")
    genomic_row_end = body.index("</tr>", genomic_row_start)
    genomic_row = body[genomic_row_start:genomic_row_end]
    assert "View text" not in genomic_row
    assert f"/documents/consumed/{GENOMIC_SHA}/text" not in genomic_row

    text_response = client.get(f"/documents/consumed/{GENOMIC_SHA}/text")
    assert text_response.status_code == 404

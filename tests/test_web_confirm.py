"""Confirm queue surface tests: rows render beside their source page image,
and Confirm/Correct/Reject mutate `LabsDb` and make a data-repo commit.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from git import Repo
from web_support import build_app, login

from adoc.labs.models import ExtractionStatus, LabDocument, LabResult

SHA = "d" * 64


def _seed_pending_row(repo, db) -> int:
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    page_dir = repo.root / "sources" / "pages" / SHA
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "p-1.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")

    [row_id] = db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="crp",
                name_raw="CRP",
                value=8.0,
                ucum_unit="mg/L",
                source_doc=SHA,
                source_page=1,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=json.dumps({}),
            )
        ]
    )
    assert row_id is not None
    return row_id


def test_confirm_queue_shows_row_beside_source_image(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "CRP" in response.text
    assert f"/files/pages/{SHA}/p-1.png" in response.text
    assert f"/confirm/{row_id}/confirm" in response.text


def test_confirm_action_marks_row_confirmed_and_commits(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    git_repo = Repo(repo.root)
    commits_before = len(list(git_repo.iter_commits()))

    response = client.post(f"/confirm/{row_id}/confirm")

    assert response.status_code == 200
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.CONFIRMED
    assert db.pending() == []

    commits_after = len(list(git_repo.iter_commits()))
    assert commits_after == commits_before + 1
    export_path = repo.root / "labs-export.jsonl"
    assert export_path.exists()


def test_reject_action_marks_row_rejected(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.post(f"/confirm/{row_id}/reject")

    assert response.status_code == 200
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.REJECTED


def test_correct_action_applies_the_field_change(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.post(
        f"/confirm/{row_id}/correct",
        data={
            "value": "9.5",
            "date": "",
            "name": "",
            "value_text": "",
            "ucum_unit": "",
            "ref_low": "",
            "ref_high": "",
            "flag": "",
        },
    )

    assert response.status_code == 200
    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 9.5
    assert row.extraction_status == ExtractionStatus.CORRECTED


def test_correct_action_with_no_fields_shows_an_error(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.post(
        f"/confirm/{row_id}/correct",
        data={
            "value": "",
            "date": "",
            "name": "",
            "value_text": "",
            "ucum_unit": "",
            "ref_low": "",
            "ref_high": "",
            "flag": "",
        },
    )

    assert response.status_code == 200
    assert "at least one field" in response.text.lower()
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING


_DOCX_SHA = "e" * 64


def _seed_pending_row_with_no_page_image(repo, db) -> int:
    """A lab-classified docx's pending row: `documents` row present, but no
    `sources/pages/<sha>/` directory at all - the docx path never renders
    page images (PLAN.md docx ingestion: docx = TEXT documents)."""
    db.upsert_document(
        LabDocument(sha256=_DOCX_SHA, filename="home-lab.docx", doc_type="lab_report", page_count=1)
    )
    [row_id] = db.insert_results(
        [
            LabResult(
                date=date(2026, 8, 1),
                name="potassium",
                name_raw="Potassium",
                value=4.1,
                ucum_unit="mmol/L",
                source_doc=_DOCX_SHA,
                source_page=1,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=json.dumps({}),
            )
        ]
    )
    assert row_id is not None
    return row_id


def test_confirm_queue_shows_text_fallback_when_no_page_image_exists(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row_with_no_page_image(repo, db)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "Potassium" in response.text
    assert f"/confirm/{row_id}/confirm" in response.text
    # no broken <img> for a document with no rendered page image
    assert "<img" not in response.text
    assert "Text document" in response.text
    assert "no page image" in response.text.lower()

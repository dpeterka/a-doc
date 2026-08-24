"""Confirm queue surface tests: rows render beside their source page image,
and Confirm/Correct/Reject mutate `LabsDb` and make a data-repo commit.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from git import Repo
from web_support import build_app, login

from adoc.ingest.reconcile import row_is_agreed
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult

SHA = "d" * 64
SHA2 = "f" * 64


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


# --------------------------------------------------------------------------
# Triage: "models agreed" vs. "models disagreed" bucket classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reasons", "expected_agreed"),
    [
        # No reasons at all (shouldn't happen for a real PENDING row, but
        # trivially "agreed": no disagreement reason is present).
        ([], True),
        # Single-source issues both passes shared (reconcile.py's
        # `missing_date`, and labs/validate.py's four `ValidationIssue`
        # message shapes plus `trend_outlier`'s) -> agreed.
        (["missing_date"], True),
        (["CRP: unit 'mg/dL' not in whitelist ('mg/L',)"], True),
        (
            ["WBC: value 150.0 outside plausible bounds [0.1, 100.0] - likely extraction error"],
            True,
        ),
        (["flag H but value 5.0 <= ref_high 10.0"], True),
        (["ANA titer: value_text '80' does not match titer format (e.g. '1:80')"], True),
        (
            [
                "potassium: value 41.0 is 900% away from recent median 4.1 "
                "across 3 priors - possible decimal error"
            ],
            True,
        ),
        (["missing_date", "CRP: unit 'mg/dL' not in whitelist ('mg/L',)"], True),
        # Cross-pass disagreements, a row only one pass could read, or low
        # confidence on either pass -> disagreed, regardless of what else
        # is in the list.
        (["single_pass"], False),
        (["single_pass", "missing_date"], False),
        (["value_mismatch: 8.0 vs 9.0"], False),
        (["value_text_mismatch: 'positive' vs 'negative'"], False),
        (["unit_mismatch: 'mg/L' vs 'mg/dL'"], False),
        (["ref_range_mismatch: '0-10' vs '0-5'"], False),
        (["flag_mismatch: 'H' vs 'None'"], False),
        (["pass_a_confidence:low"], False),
        (["pass_b_confidence:medium"], False),
        (["missing_date", "value_mismatch: 8.0 vs 9.0"], False),
    ],
)
def test_row_is_agreed_classification(reasons: list[str], expected_agreed: bool) -> None:
    assert row_is_agreed(reasons) is expected_agreed


# --------------------------------------------------------------------------
# Shared seeding helpers for the triage/grouping/bulk-confirm tests below
# --------------------------------------------------------------------------


def _seed_document(
    repo,
    db,
    *,
    sha: str,
    filename: str = "doc.pdf",
    doc_date: date | None = date(2026, 5, 2),
    page_count: int = 1,
) -> None:
    db.upsert_document(
        LabDocument(
            sha256=sha,
            filename=filename,
            doc_type="lab_report",
            doc_date=doc_date,
            page_count=page_count,
        )
    )
    for page in range(1, page_count + 1):
        page_dir = repo.root / "sources" / "pages" / sha
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / f"p-{page}.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")


def _seed_pending_with_raw(
    db,
    *,
    sha: str,
    name: str,
    reasons: list[str],
    pass_a: dict | None = None,
    pass_b: dict | None = None,
    row_date: date = date(2026, 5, 2),
    value: float = 8.0,
    page: int = 1,
) -> int:
    raw_json = json.dumps({"pass_a": pass_a, "pass_b": pass_b, "reasons": reasons})
    [row_id] = db.insert_results(
        [
            LabResult(
                date=row_date,
                name=name.lower(),
                name_raw=name,
                value=value,
                ucum_unit="mg/L",
                source_doc=sha,
                source_page=page,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=raw_json,
            )
        ]
    )
    assert row_id is not None
    return row_id


# --------------------------------------------------------------------------
# Document grouping
# --------------------------------------------------------------------------


def test_confirm_queue_groups_rows_by_document_with_filename_date_and_page(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(
        repo, db, sha=SHA, filename="cbc-panel.pdf", doc_date=date(2026, 3, 10), page_count=2
    )
    _seed_pending_with_raw(db, sha=SHA, name="CRP", reasons=["missing_date"], page=2)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "cbc-panel.pdf" in response.text
    assert "2026-03-10" in response.text
    assert "page 2" in response.text
    assert "2 page" in response.text  # page_count in the document header


# --------------------------------------------------------------------------
# Bulk confirm
# --------------------------------------------------------------------------


def test_bulk_confirm_agreed_confirms_exactly_the_agreed_set_and_commits_once(
    tmp_path: Path,
) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    agreed_id_1 = _seed_pending_with_raw(db, sha=SHA, name="CRP", reasons=["missing_date"])
    agreed_id_2 = _seed_pending_with_raw(db, sha=SHA, name="ESR", reasons=[])
    disagreed_id = _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["value_mismatch: 8.0 vs 9.5"],
        pass_a={
            "name_raw": "ferritin",
            "value": 8.0,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": None,
            "page": 1,
            "confidence": "high",
        },
        pass_b={
            "name_raw": "ferritin",
            "value": 9.5,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": None,
            "page": 1,
            "confidence": "high",
        },
    )
    client = TestClient(app)
    login(client)

    git_repo = Repo(repo.root)
    commits_before = len(list(git_repo.iter_commits()))

    response = client.post("/confirm/bulk-confirm-agreed")

    assert response.status_code == 200
    assert db.get_row(agreed_id_1).extraction_status == ExtractionStatus.CONFIRMED  # type: ignore[union-attr]
    assert db.get_row(agreed_id_2).extraction_status == ExtractionStatus.CONFIRMED  # type: ignore[union-attr]
    assert db.get_row(disagreed_id).extraction_status == ExtractionStatus.PENDING  # type: ignore[union-attr]

    commits_after = len(list(git_repo.iter_commits()))
    assert commits_after == commits_before + 1


def test_bulk_confirm_agreed_for_document_only_confirms_that_documents_rows(
    tmp_path: Path,
) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc-one.pdf")
    _seed_document(repo, db, sha=SHA2, filename="doc-two.pdf")
    doc1_id = _seed_pending_with_raw(db, sha=SHA, name="CRP", reasons=["missing_date"])
    doc2_id = _seed_pending_with_raw(db, sha=SHA2, name="CRP", reasons=["missing_date"])
    client = TestClient(app)
    login(client)

    response = client.post(f"/confirm/documents/{SHA}/bulk-confirm-agreed")

    assert response.status_code == 200
    assert db.get_row(doc1_id).extraction_status == ExtractionStatus.CONFIRMED  # type: ignore[union-attr]
    assert db.get_row(doc2_id).extraction_status == ExtractionStatus.PENDING  # type: ignore[union-attr]


def test_bulk_confirm_agreed_with_nothing_agreed_makes_no_commit(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    _seed_pending_with_raw(db, sha=SHA, name="CRP", reasons=["single_pass"])
    client = TestClient(app)
    login(client)

    git_repo = Repo(repo.root)
    commits_before = len(list(git_repo.iter_commits()))

    response = client.post("/confirm/bulk-confirm-agreed")

    assert response.status_code == 200
    commits_after = len(list(git_repo.iter_commits()))
    assert commits_after == commits_before


# --------------------------------------------------------------------------
# Disagreement rows: both passes' readings, side by side
# --------------------------------------------------------------------------


def test_disagreement_row_shows_both_passes_values(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["value_mismatch: 8.0 vs 9.5"],
        pass_a={
            "name_raw": "ferritin",
            "value": 8.0,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": None,
            "page": 1,
            "confidence": "high",
        },
        pass_b={
            "name_raw": "ferritin",
            "value": 9.5,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": None,
            "page": 1,
            "confidence": "high",
        },
    )
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "Models disagreed" in response.text
    assert "8.0" in response.text
    assert "9.5" in response.text
    assert "diff-field" in response.text


def test_single_pass_disagreement_row_notes_only_one_pass_read_it(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["single_pass"],
        pass_a={
            "name_raw": "ferritin",
            "value": 8.0,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": None,
            "page": 1,
            "confidence": "high",
        },
        pass_b=None,
    )
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "Only one pass could read this row" in response.text


# --------------------------------------------------------------------------
# Lightbox markup
# --------------------------------------------------------------------------


def test_confirm_queue_includes_lightbox_markup_and_script(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert row_id  # the seeded row is present
    assert 'id="confirm-lightbox"' in response.text
    assert "confirm-lightbox.js" in response.text
    assert "confirm-page-image" in response.text


# --------------------------------------------------------------------------
# Original-PDF route: auth, traversal, inline disposition
# --------------------------------------------------------------------------


def _seed_original_pdf(repo, sha: str, origname: str = "original.pdf") -> None:
    sources_dir = repo.root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{sha}__{origname}").write_bytes(b"%PDF-1.4 fake pdf bytes")


def test_original_document_requires_auth(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_original_pdf(repo, SHA)
    client = TestClient(app)

    response = client.get(f"/files/original/{SHA}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_original_document_serves_inline_when_authenticated(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_original_pdf(repo, SHA, origname="cbc-panel.pdf")
    client = TestClient(app)
    login(client)

    response = client.get(f"/files/original/{SHA}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "inline" in response.headers["content-disposition"]
    assert "cbc-panel.pdf" in response.headers["content-disposition"]
    assert response.content == b"%PDF-1.4 fake pdf bytes"


def test_original_document_refuses_an_invalid_sha(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_original_pdf(repo, SHA)
    client = TestClient(app)
    login(client)

    response = client.get("/files/original/not-a-sha")

    assert response.status_code == 404


def test_original_document_refuses_path_traversal_in_sha(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_original_pdf(repo, SHA)
    # A real secret one level above `sources/` to prove a traversal
    # attempt (if it worked) would actually be able to read it.
    secret = repo.root / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    client = TestClient(app)
    login(client)

    response = client.get("/files/original/..%2Fsecret.txt", follow_redirects=False)

    assert response.status_code in (400, 404)
    assert "top secret" not in response.text


def test_original_document_404s_for_an_unknown_sha(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get(f"/files/original/{SHA}")

    assert response.status_code == 404


_DOCX_SHA = "e" * 64


def test_confirm_queue_shows_text_fallback_when_no_page_image_exists(tmp_path: Path) -> None:
    """A docx-sourced pending row has no rendered pages; the row must show
    the text-document fallback instead of a broken <img> (ported onto the
    redesigned grouped-queue markup)."""
    app, repo, db, _calls = build_app(tmp_path)
    db.upsert_document(
        LabDocument(
            sha256=_DOCX_SHA,
            filename="home-lab.docx",
            doc_type="lab_report",
            doc_date=date(2026, 8, 1),
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )
    row_id = db.insert_results(
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
                raw_json='{"reasons": ["missing_date"]}',
            )
        ]
    )[0]
    assert row_id is not None

    client = TestClient(app)
    login(client)
    response = client.get("/confirm")

    assert response.status_code == 200
    assert "Text document" in response.text
    assert "no page image" in response.text

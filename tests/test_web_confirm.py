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
        # RESCUE-paired name variant with at least one shared meaningful
        # token (D5) -> agreed, same as any other single-source annotation.
        (["name_variant"], True),
        # Cross-pass disagreements, a row only one pass could read, or low
        # confidence on either pass -> disagreed, regardless of what else
        # is in the list.
        (["single_pass"], False),
        # RESCUE-paired name variant with ZERO shared meaningful token (D5:
        # the accepted residual risk of two different analytes coincidentally
        # sharing a value/page) -> disagreed, needs a real look.
        (["name_variant_unverified"], False),
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


# --------------------------------------------------------------------------
# Specimen: chip rendering, correction, and the specimen_mismatch
# disagreement bucket
# --------------------------------------------------------------------------


def test_confirm_row_shows_specimen_chip_when_not_unknown(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    [row_id] = db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="glucose",
                name_raw="GLUCOSE",
                value=None,
                value_text="NEGATIVE",
                source_doc=SHA,
                source_page=1,
                extraction_status=ExtractionStatus.PENDING,
                specimen="urine",
                raw_json=json.dumps({"reasons": ["missing_date"]}),
            )
        ]
    )
    assert row_id is not None
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert 'class="chip chip-specimen"' in response.text
    assert ">urine<" in response.text


def test_confirm_row_shows_no_specimen_chip_when_unknown(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert row_id  # seeded row present, specimen defaults "unknown"
    assert "chip-specimen" not in response.text


def test_correct_action_can_change_specimen(tmp_path: Path) -> None:
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
            "specimen": "serum",
        },
    )

    assert response.status_code == 200
    row = db.get_row(row_id)
    assert row is not None
    assert row.specimen == "serum"
    assert row.extraction_status == ExtractionStatus.CORRECTED


def test_specimen_mismatch_pending_row_lands_in_disagreement_bucket(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    _seed_pending_with_raw(
        db,
        sha=SHA,
        name="glucose",
        reasons=["specimen_mismatch: 'serum' vs 'urine'"],
        pass_a={
            "name_raw": "GLUCOSE",
            "value": 92.0,
            "unit_raw": "mg/dL",
            "ref_range_raw": None,
            "flag_raw": None,
            "specimen": "serum",
            "page": 1,
            "confidence": "high",
        },
        pass_b={
            "name_raw": "GLUCOSE",
            "value": 92.0,
            "unit_raw": "mg/dL",
            "ref_range_raw": None,
            "flag_raw": None,
            "specimen": "urine",
            "page": 1,
            "confidence": "high",
        },
    )
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "Models disagreed" in response.text
    assert "serum" in response.text
    assert "urine" in response.text


def test_name_variant_unverified_row_lands_in_disagreement_bucket(tmp_path: Path) -> None:
    """D5: a RESCUE-paired row with zero shared meaningful token between
    the two names (`name_variant_unverified`) needs a real look, not the
    bulk-OK "models agreed" bucket."""
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    _seed_pending_with_raw(
        db,
        sha=SHA,
        name="Vitamin B12",
        reasons=["name_variant_unverified"],
        pass_a={
            "name_raw": "Vitamin B12",
            "value": 8.0,
            "unit_raw": None,
            "ref_range_raw": None,
            "flag_raw": None,
            "page": 1,
            "confidence": "high",
        },
        pass_b={
            "name_raw": "Ketones, Urine",
            "value": 8.0,
            "unit_raw": None,
            "ref_range_raw": None,
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
    assert "Vitamin B12" in response.text


def test_name_variant_unverified_has_a_plain_language_gloss() -> None:
    """D5: the `_friendly_reason` mapping (`web.routes.confirm`) has a
    dedicated plain-language line for `name_variant_unverified`, not the
    generic "a routine check flagged this" fallback."""
    from adoc.web.routes.confirm import _friendly_reason

    gloss = _friendly_reason("name_variant_unverified")
    assert gloss == (
        "two readings matched by value but their names differ a lot - check they are the same test"
    )


def test_specimen_mismatch_is_classified_as_a_disagreement() -> None:
    assert row_is_agreed(["specimen_mismatch: 'serum' vs 'urine'"]) is False


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


# --------------------------------------------------------------------------
# Explicit pass choice on disagreements (queue-ergonomics slice item 1):
# "Use reading A" / "Use reading B" replace the bare Confirm on a
# disagreement row; agreed rows keep plain Confirm.
# --------------------------------------------------------------------------

_FERRITIN_PASS_A = {
    "name_raw": "ferritin",
    "value": 8.0,
    "unit_raw": "ng/mL",
    "ref_range_raw": "10-200",
    "flag_raw": "L",
    "specimen": "serum",
    "page": 1,
    "confidence": "high",
}
_FERRITIN_PASS_B = {
    "name_raw": "ferritin",
    "value": 9.5,
    "unit_raw": "ng/mL",
    "ref_range_raw": "10-200",
    "flag_raw": None,
    "specimen": "serum",
    "page": 1,
    "confidence": "high",
}


def test_disagreement_row_shows_use_reading_a_and_b_buttons(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    row_id = _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["value_mismatch: 8.0 vs 9.5"],
        pass_a=_FERRITIN_PASS_A,
        pass_b=_FERRITIN_PASS_B,
    )
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "Use reading A" in response.text
    assert "Use reading B" in response.text
    assert f"/confirm/{row_id}/resolve-pass/a" in response.text
    assert f"/confirm/{row_id}/resolve-pass/b" in response.text
    assert "Edit manually" in response.text


def test_agreed_row_keeps_plain_confirm_button_not_use_reading(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert f"/confirm/{row_id}/confirm" in response.text
    assert "Use reading A" not in response.text
    assert "Use reading B" not in response.text
    assert "Something's wrong" in response.text


def test_single_pass_disagreement_row_only_shows_the_available_pass_button(
    tmp_path: Path,
) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    row_id = _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["single_pass"],
        pass_a=_FERRITIN_PASS_A,
        pass_b=None,
    )
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert f"/confirm/{row_id}/resolve-pass/a" in response.text
    assert f"/confirm/{row_id}/resolve-pass/b" not in response.text


def test_use_reading_a_applies_pass_as_fields_marks_corrected_and_commits(
    tmp_path: Path,
) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    row_id = _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["value_mismatch: 8.0 vs 9.5"],
        pass_a=_FERRITIN_PASS_A,
        pass_b=_FERRITIN_PASS_B,
    )
    client = TestClient(app)
    login(client)

    git_repo = Repo(repo.root)
    commits_before = len(list(git_repo.iter_commits()))

    response = client.post(f"/confirm/{row_id}/resolve-pass/a")

    assert response.status_code == 200
    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 8.0
    assert row.ucum_unit == "ng/mL"
    assert row.ref_low == 10.0
    assert row.ref_high == 200.0
    assert row.extraction_status == ExtractionStatus.CORRECTED
    payload = row.raw_payload()
    assert payload["resolved_with"] == "pass_a"

    commits_after = len(list(git_repo.iter_commits()))
    assert commits_after == commits_before + 1


def test_use_reading_b_applies_pass_bs_fields(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    row_id = _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["value_mismatch: 8.0 vs 9.5"],
        pass_a=_FERRITIN_PASS_A,
        pass_b=_FERRITIN_PASS_B,
    )
    client = TestClient(app)
    login(client)

    response = client.post(f"/confirm/{row_id}/resolve-pass/b")

    assert response.status_code == 200
    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 9.5
    assert row.flag is None
    assert row.extraction_status == ExtractionStatus.CORRECTED
    assert row.raw_payload()["resolved_with"] == "pass_b"


def test_resolve_pass_rejects_an_invalid_pass_letter(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="doc.pdf")
    row_id = _seed_pending_with_raw(
        db,
        sha=SHA,
        name="ferritin",
        reasons=["value_mismatch: 8.0 vs 9.5"],
        pass_a=_FERRITIN_PASS_A,
        pass_b=_FERRITIN_PASS_B,
    )
    client = TestClient(app)
    login(client)

    response = client.post(f"/confirm/{row_id}/resolve-pass/c")

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Score-kind results render properly (queue-ergonomics slice item 2)
# --------------------------------------------------------------------------


def test_score_row_shows_calculated_score_note_instead_of_range(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document(repo, db, sha=SHA, filename="dexa.pdf")
    [row_id] = db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="T-score",
                name_raw="LEFT HIP femoral neck T-Score",
                value=-1.2,
                source_doc=SHA,
                source_page=1,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=json.dumps({"reasons": ["missing_date"]}),
            )
        ]
    )
    assert row_id is not None
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "Calculated score" in response.text
    assert "no reference range applies" in response.text


def test_non_score_row_does_not_show_calculated_score_note(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    row_id = _seed_pending_row(repo, db)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert row_id  # seeded CRP row, has a unit - not score-shaped
    assert "Calculated score" not in response.text


# --------------------------------------------------------------------------
# Twin-sweep dismissible note (queue-ergonomics slice item 4)
# --------------------------------------------------------------------------


def test_confirm_queue_shows_twin_sweep_note_when_last_sweep_rejected_rows(
    tmp_path: Path,
) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    work_dir = repo.root / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "twin-sweep.json").write_text(
        json.dumps({"checked": 3, "rejected": 2, "rejected_rule": 1, "rejected_llm": 1}),
        encoding="utf-8",
    )
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "2 duplicate readings were auto-resolved" in response.text
    assert "confirm-twin-sweep-note.js" in response.text


def test_confirm_queue_omits_twin_sweep_note_when_no_sweep_has_run(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "auto-resolved" not in response.text


def test_confirm_queue_omits_twin_sweep_note_when_last_sweep_rejected_nothing(
    tmp_path: Path,
) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    work_dir = repo.root / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "twin-sweep.json").write_text(
        json.dumps({"checked": 3, "rejected": 0, "rejected_rule": 0, "rejected_llm": 0}),
        encoding="utf-8",
    )
    client = TestClient(app)
    login(client)

    response = client.get("/confirm")

    assert response.status_code == 200
    assert "auto-resolved" not in response.text


def test_resolve_pass_converging_on_existing_row_rejects_as_duplicate(tmp_path: Path) -> None:
    """'Use reading B' whose name/specimen already exists for the same
    document+date must reject the queue item as that row's duplicate, not
    500 on the UNIQUE constraint (real review-session crash)."""
    app, repo, db, _calls = build_app(tmp_path)
    doc_sha = "d" * 64
    db.upsert_document(
        LabDocument(
            sha256=doc_sha,
            filename="panel.pdf",
            doc_type="lab_report",
            doc_date=date(2024, 7, 18),
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )
    # the already-existing row the resolution will converge onto
    db.insert_results(
        [
            LabResult(
                date=date(2024, 7, 18),
                name="E. CHAFFEENSIS AB IGG",
                name_raw="E. CHAFFEENSIS AB IGG",
                value_text="<1:64",
                source_doc=doc_sha,
                source_page=1,
                extraction_status=ExtractionStatus.CONFIRMED,
                raw_json="{}",
            )
        ]
    )
    # the disagreement row whose pass-b name matches the existing row
    raw = json.dumps(
        {
            "reasons": ["value_text_mismatch: '<1:64' vs '<1:20'"],
            "pass_a": {"name_raw": "CHAFFEENSIS AB (IGG)", "value_text": "<1:20", "page": 1},
            "pass_b": {"name_raw": "E. CHAFFEENSIS AB IGG", "value_text": "<1:64", "page": 1},
        }
    )
    (row_id,) = db.insert_results(
        [
            LabResult(
                date=date(2024, 7, 18),
                name="chaffeensis ab (igg)",
                name_raw="CHAFFEENSIS AB (IGG)",
                value_text="<1:20",
                source_doc=doc_sha,
                source_page=1,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=raw,
            )
        ]
    )
    assert row_id is not None

    client = TestClient(app)
    login(client)
    response = client.post(f"/confirm/{row_id}/resolve-pass/b")

    assert response.status_code == 200
    assert "marked as its duplicate" in response.text
    row = db.get_row(row_id)
    assert row is not None and row.extraction_status is ExtractionStatus.REJECTED
    assert row.raw_payload()["auto_rejected_twin_of"] is not None

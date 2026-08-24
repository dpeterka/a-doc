"""Tests for adoc.labs.specimen: deterministic (NO LLM) specimen back-fill
for existing rows, driving `adoc labs-infer-specimen`."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, LabDocument, LabResult
from adoc.labs.specimen import infer_specimen_from_document, infer_unknown_specimens

SHA_URINE_DOC = "a" * 64
SHA_STOOL_DOC = "b" * 64
SHA_OTHER_DOC = "c" * 64


def _doc(sha256: str, filename: str, doc_type: str = "lab_report") -> LabDocument:
    return LabDocument(
        sha256=sha256,
        filename=filename,
        doc_type=doc_type,
        page_count=1,
        ingested_at=datetime(2026, 5, 3, 12, 0, 0),
        status=DocumentStatus.COMPLETE,
    )


def _lab(name: str, source_doc: str, **overrides: object) -> LabResult:
    fields: dict[str, object] = {
        "date": date(2026, 5, 2),
        "name": name,
        "name_raw": name,
        "value": 1.0,
        "source_doc": source_doc,
        "raw_json": json.dumps({"name_raw": name}),
    }
    fields.update(overrides)
    return LabResult.model_validate(fields)


# ----------------------------------------------------------------
# infer_specimen_from_document: pure keyword function
# ----------------------------------------------------------------


def test_infers_urine_from_urinalysis_filename() -> None:
    assert (
        infer_specimen_from_document(filename="urinalysis-2026-05-02.pdf", doc_type="lab_report")
        == "urine"
    )


def test_infers_urine_from_urine_keyword() -> None:
    assert (
        infer_specimen_from_document(filename="urine-culture.pdf", doc_type="lab_report") == "urine"
    )


def test_infers_stool_from_stool_filename() -> None:
    assert (
        infer_specimen_from_document(filename="stool-culture-2026.pdf", doc_type="lab_report")
        == "stool"
    )


def test_infers_stool_from_doc_type() -> None:
    assert infer_specimen_from_document(filename="report.pdf", doc_type="stool panel") == "stool"


def test_is_conservative_about_everything_else() -> None:
    """No keyword match -> None, never a guess (e.g. a generic CMP/CBC
    filename, or a serum panel that never says so explicitly)."""
    assert (
        infer_specimen_from_document(filename="quest-2026-05-02.pdf", doc_type="lab_report") is None
    )
    assert infer_specimen_from_document(filename="labcorp-cmp.pdf", doc_type="lab_report") is None
    assert infer_specimen_from_document(filename="serum-glucose.pdf", doc_type="lab_report") is None


def test_is_case_insensitive() -> None:
    assert infer_specimen_from_document(filename="URINALYSIS.PDF", doc_type="LAB_REPORT") == "urine"


# ----------------------------------------------------------------
# infer_unknown_specimens: the maintenance pass over a LabsDb
# ----------------------------------------------------------------


def _seeded_db(tmp_path: Path) -> LabsDb:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc(SHA_URINE_DOC, "urinalysis-2026-05-02.pdf"))
    db.upsert_document(_doc(SHA_STOOL_DOC, "stool-culture-2026-05-02.pdf"))
    db.upsert_document(_doc(SHA_OTHER_DOC, "labcorp-cmp-2026-05-02.pdf"))
    db.insert_results(
        [
            _lab("glucose", SHA_URINE_DOC, value=None, value_text="NEGATIVE"),
            _lab("wbc", SHA_STOOL_DOC),
            _lab("sodium", SHA_OTHER_DOC, value=140.0, ucum_unit="mmol/L"),
        ]
    )
    return db


def test_infer_unknown_specimens_updates_urine_and_stool_docs(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)

    report = infer_unknown_specimens(db)

    assert report.updated == 2
    assert report.remaining_unknown == 1
    assert report.by_specimen == {"urine": 1, "stool": 1}

    rows = {row.name: row for row in db.series("glucose") + db.series("wbc") + db.series("sodium")}
    assert rows["glucose"].specimen == "urine"
    assert rows["wbc"].specimen == "stool"
    assert rows["sodium"].specimen == "unknown"  # conservative: no keyword match


def test_infer_unknown_specimens_does_not_touch_extraction_status(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    before = {row.id: row.extraction_status for row in db.series("glucose")}

    infer_unknown_specimens(db)

    after = {row.id: row.extraction_status for row in db.series("glucose")}
    assert before == after


def test_infer_unknown_specimens_is_idempotent(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)

    first = infer_unknown_specimens(db)
    second = infer_unknown_specimens(db)

    assert first.updated == 2
    assert second.updated == 0
    assert second.remaining_unknown == first.remaining_unknown


def test_infer_unknown_specimens_on_empty_db_is_a_noop(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    report = infer_unknown_specimens(db)
    assert report.updated == 0
    assert report.remaining_unknown == 0
    assert report.by_specimen == {}

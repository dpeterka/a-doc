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
    """A PURE urinalysis doc and a PURE stool doc (no analyte in either
    canonicalizes to a `SERUM_PANEL_ANALYTES` member - `"specific gravity"`/
    `"occult blood"` aren't in `ANALYTE_SPECS` at all), plus an unrelated
    doc with no keyword match at all."""
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc(SHA_URINE_DOC, "urinalysis-2026-05-02.pdf"))
    db.upsert_document(_doc(SHA_STOOL_DOC, "stool-culture-2026-05-02.pdf"))
    db.upsert_document(_doc(SHA_OTHER_DOC, "labcorp-cmp-2026-05-02.pdf"))
    db.insert_results(
        [
            _lab("specific gravity", SHA_URINE_DOC, value=1.02),
            _lab("occult blood", SHA_STOOL_DOC, value=None, value_text="negative"),
            _lab("sodium", SHA_OTHER_DOC, value=140.0, ucum_unit="mmol/L"),
        ]
    )
    return db


def test_infer_unknown_specimens_updates_urine_and_stool_docs(tmp_path: Path) -> None:
    """A PURE urinalysis/stool document (D2: no serum-panel analyte among
    its rows at all) is unaffected by the combined-panel guards - every
    eligible row still gets stamped."""
    db = _seeded_db(tmp_path)

    report = infer_unknown_specimens(db)

    assert report.updated == 2
    assert report.remaining_unknown == 1
    assert report.by_specimen == {"urine": 1, "stool": 1}
    assert report.skipped_serum_panel == 0
    assert report.mixed_panel_docs == ()

    rows = {
        row.name: row
        for row in db.series("specific gravity") + db.series("occult blood") + db.series("sodium")
    }
    assert rows["specific gravity"].specimen == "urine"
    assert rows["occult blood"].specimen == "stool"
    assert rows["sodium"].specimen == "unknown"  # conservative: no keyword match


def test_infer_unknown_specimens_does_not_touch_extraction_status(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    before = {row.id: row.extraction_status for row in db.series("specific gravity")}

    infer_unknown_specimens(db)

    after = {row.id: row.extraction_status for row in db.series("specific gravity")}
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


# --------------------------------------------------------------------------
# Combined-panel safeguard (D2): a document keyword must never blindly
# stamp a serum/whole-blood core-panel analyte, whether it stands alone
# (guard 1) or shares a document with a non-panel row (guard 2, the mixed-
# panel signal).
# --------------------------------------------------------------------------


def test_infer_unknown_specimens_never_stamps_a_lone_serum_panel_analyte(tmp_path: Path) -> None:
    """The real db.py-documented finding: a urinalysis "GLUCOSE" reading
    canonicalizes to the same name as a serum glucose reading - even
    standing alone in a urine-keyword document, it must stay "unknown"
    rather than being confidently (and possibly wrongly) stamped."""
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc(SHA_URINE_DOC, "urinalysis-2026-05-02.pdf"))
    db.insert_results([_lab("glucose", SHA_URINE_DOC, value=None, value_text="NEGATIVE")])

    report = infer_unknown_specimens(db)

    assert report.updated == 0
    assert report.skipped_serum_panel == 1
    assert report.remaining_unknown == 1
    row = db.series("glucose")[0]
    assert row.specimen == "unknown"


def test_infer_unknown_specimens_skips_whole_document_when_mixed_panel(tmp_path: Path) -> None:
    """A combined CBC+urinalysis document (both a serum-panel analyte, WBC,
    and a non-panel one, specific gravity, under one urine-keyword
    filename): the mixed-panel signal must hold back EVERY row of the
    document, including the non-panel one that would individually pass
    guard 1."""
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc(SHA_URINE_DOC, "urinalysis-2026-05-02.pdf"))
    db.insert_results(
        [
            _lab("wbc", SHA_URINE_DOC, value=6.0),
            _lab("specific gravity", SHA_URINE_DOC, value=1.02),
        ]
    )

    report = infer_unknown_specimens(db)

    assert report.updated == 0
    assert report.mixed_panel_docs == (SHA_URINE_DOC,)
    assert report.remaining_unknown == 2
    rows = {row.name: row for row in db.series("wbc") + db.series("specific gravity")}
    assert rows["wbc"].specimen == "unknown"
    assert rows["specific gravity"].specimen == "unknown"


def test_infer_unknown_specimens_mixed_panel_rerun_is_idempotent(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc(SHA_URINE_DOC, "urinalysis-2026-05-02.pdf"))
    db.insert_results(
        [
            _lab("wbc", SHA_URINE_DOC, value=6.0),
            _lab("specific gravity", SHA_URINE_DOC, value=1.02),
        ]
    )

    first = infer_unknown_specimens(db)
    second = infer_unknown_specimens(db)

    assert first.updated == 0 and second.updated == 0
    assert first.mixed_panel_docs == second.mixed_panel_docs == (SHA_URINE_DOC,)
    assert first.remaining_unknown == second.remaining_unknown == 2

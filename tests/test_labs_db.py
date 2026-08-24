"""Tests for adoc.labs.db.LabsDb: schema, confirm-queue flow, dedupe, FTS, JSONL round-trip."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabFlag, LabResult

SHA_A = "a" * 64
SHA_B = "b" * 64


def _doc(sha256: str = SHA_A, **overrides: object) -> LabDocument:
    fields: dict[str, object] = {
        "sha256": sha256,
        "filename": "quest-2026-05-02.pdf",
        "doc_type": "lab-result",
        "doc_date": date(2026, 5, 2),
        "page_count": 3,
        "ingested_at": datetime(2026, 5, 3, 12, 0, 0),
        "status": DocumentStatus.COMPLETE,
    }
    fields.update(overrides)
    return LabDocument.model_validate(fields)


def _lab(
    name: str = "potassium",
    value: float | None = 4.1,
    lab_date: date = date(2026, 5, 2),
    source_doc: str = SHA_A,
    **overrides: object,
) -> LabResult:
    fields: dict[str, object] = {
        "date": lab_date,
        "name": name,
        "name_raw": name,
        "value": value,
        "ucum_unit": "mmol/L",
        "ref_low": 3.5,
        "ref_high": 5.1,
        "source_doc": source_doc,
        "extraction_status": ExtractionStatus.AUTO,
        "raw_json": json.dumps({"name_raw": name, "value": value}),
    }
    fields.update(overrides)
    return LabResult.model_validate(fields)


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(_doc())
    store.upsert_document(_doc(sha256=SHA_B, filename="labcorp-2026-06-01.pdf"))
    return store


def test_schema_created_with_user_version(db: LabsDb) -> None:
    version = db._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1
    tables = {
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    assert {"documents", "labs", "labs_fts"} <= tables


def test_foreign_keys_and_wal_enabled(db: LabsDb) -> None:
    assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_journal_mode_defaults_to_wal(tmp_path: Path) -> None:
    """Local/dev/test default: WAL is fast on a normal local filesystem."""
    store = LabsDb(tmp_path / "labs.sqlite")
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_journal_mode_truncate_for_nfs_efs(tmp_path: Path) -> None:
    """Deployed (ECS/EFS) mode: TRUNCATE avoids WAL's shared-memory index
    file, which is unsafe over NFS/EFS (see LabsDb.__init__'s docstring and
    ADR 0006)."""
    store = LabsDb(tmp_path / "labs.sqlite", journal_mode="TRUNCATE")
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"


def test_insert_results_returns_ids(db: LabsDb) -> None:
    ids = db.insert_results([_lab(), _lab(name="sodium", value=140.0, ucum_unit="mmol/L")])
    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == 2


def test_upsert_document_updates_in_place(db: LabsDb) -> None:
    db.upsert_document(_doc(status=DocumentStatus.NEEDS_REVIEW, page_count=5))
    fetched = db.get_document(SHA_A)
    assert fetched is not None
    assert fetched.status == DocumentStatus.NEEDS_REVIEW
    assert fetched.page_count == 5
    assert len(db.list_documents()) == 2  # no duplicate row


def test_confirm_correct_reject_flow(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_lab(extraction_status=ExtractionStatus.PENDING)])
    assert row_id is not None

    pending = db.pending()
    assert len(pending) == 1
    assert pending[0].id == row_id
    assert pending[0].extraction_status == ExtractionStatus.PENDING

    db.confirm_row(row_id)
    confirmed = db.get_row(row_id)
    assert confirmed is not None
    assert confirmed.extraction_status == ExtractionStatus.CONFIRMED
    assert db.pending() == []

    # a second row is corrected instead of confirmed
    (row_id_2,) = db.insert_results(
        [
            _lab(
                name="sodium",
                value=41.0,
                lab_date=date(2026, 5, 9),
                extraction_status=ExtractionStatus.PENDING,
            )
        ]
    )
    assert row_id_2 is not None
    db.correct_row(row_id_2, value=140.0, flag=LabFlag.HIGH)
    corrected = db.get_row(row_id_2)
    assert corrected is not None
    assert corrected.extraction_status == ExtractionStatus.CORRECTED
    assert corrected.value == 140.0
    assert corrected.flag == LabFlag.HIGH

    with pytest.raises(ValueError):
        db.correct_row(row_id_2, not_a_column="x")
    with pytest.raises(ValueError):
        db.correct_row(row_id_2)

    # a third row is rejected
    (row_id_3,) = db.insert_results(
        [
            _lab(
                name="glucose",
                value=95.0,
                lab_date=date(2026, 5, 10),
                extraction_status=ExtractionStatus.PENDING,
            )
        ]
    )
    assert row_id_3 is not None
    db.reject_row(row_id_3)
    rejected = db.get_row(row_id_3)
    assert rejected is not None
    assert rejected.extraction_status == ExtractionStatus.REJECTED


def test_unique_constraint_dedupes_on_insert(db: LabsDb) -> None:
    first = _lab()
    duplicate = _lab()  # identical (date, name, source_doc)

    ids = db.insert_results([first, duplicate])
    assert ids[0] is not None
    assert ids[1] is None  # skipped as a duplicate

    rows = db.series("potassium")
    assert len(rows) == 1


def test_unique_constraint_allows_same_analyte_different_doc(db: LabsDb) -> None:
    ids = db.insert_results([_lab(source_doc=SHA_A), _lab(source_doc=SHA_B)])
    assert all(i is not None for i in ids)
    assert len(db.series("potassium")) == 2


def test_series_is_time_ordered(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(lab_date=date(2026, 6, 1), value=4.2),
            _lab(lab_date=date(2026, 1, 1), value=4.0),
            _lab(lab_date=date(2026, 3, 1), value=4.1),
        ]
    )
    series = db.series("potassium")
    assert [r.date for r in series] == [date(2026, 1, 1), date(2026, 3, 1), date(2026, 6, 1)]


def test_latest_panel_returns_most_recent_per_analyte(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(name="potassium", value=4.0, lab_date=date(2026, 1, 1)),
            _lab(name="potassium", value=4.4, lab_date=date(2026, 6, 1)),
            _lab(name="sodium", value=140.0, lab_date=date(2026, 2, 1), ucum_unit="mmol/L"),
        ]
    )
    panel = {row.name: row for row in db.latest_panel()}
    assert panel["potassium"].value == 4.4
    assert panel["potassium"].date == date(2026, 6, 1)
    assert panel["sodium"].value == 140.0


def test_latest_panel_excludes_rejected(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_lab(value=9.9, lab_date=date(2026, 7, 1))])
    assert row_id is not None
    db.reject_row(row_id)
    panel = {row.name: row for row in db.latest_panel()}
    assert "potassium" not in panel


def test_abnormal_since_filters_by_flag_and_date(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(value=6.0, lab_date=date(2026, 1, 1), flag=LabFlag.HIGH),
            _lab(value=6.0, lab_date=date(2026, 8, 1), flag=LabFlag.HIGH),
            _lab(name="sodium", value=140.0, lab_date=date(2026, 8, 1), ucum_unit="mmol/L"),
        ]
    )
    abnormal = db.abnormal_since(date(2026, 6, 1))
    assert len(abnormal) == 1
    assert abnormal[0].flag == LabFlag.HIGH
    assert abnormal[0].date == date(2026, 8, 1)


def test_fts_search_matches_name_and_name_raw(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(name="ANA titer", name_raw="Antinuclear Antibody", value=None, value_text="1:320"),
            _lab(name="sodium", value=140.0, ucum_unit="mmol/L"),
        ]
    )
    by_canonical = db.search("ANA")
    assert any(r.name == "ANA titer" for r in by_canonical)

    by_raw = db.search("Antinuclear")
    assert any(r.name_raw == "Antinuclear Antibody" for r in by_raw)

    assert db.search("nonexistent-analyte-zzz") == []


def test_jsonl_export_rebuild_round_trip_is_lossless(db: LabsDb, tmp_path: Path) -> None:
    db.insert_results(
        [
            _lab(name="potassium", value=4.1, lab_date=date(2026, 5, 2)),
            _lab(
                name="ANA titer",
                name_raw="ANA",
                value=None,
                value_text="1:160",
                ucum_unit=None,
                ref_low=None,
                ref_high=None,
                lab_date=date(2026, 5, 2),
                extraction_status=ExtractionStatus.PENDING,
            ),
            _lab(name="sodium", value=140.0, ucum_unit="mmol/L", source_doc=SHA_B),
        ]
    )
    # exercise every status path so the export covers the full row shape
    rows = db.series("potassium")
    db.confirm_row(rows[0].id)  # type: ignore[arg-type]

    export_a = tmp_path / "export-a.jsonl"
    db.export_jsonl(export_a)

    rebuilt = LabsDb(tmp_path / "rebuilt.sqlite")
    rebuilt.rebuild_from_jsonl(export_a)

    export_b = tmp_path / "export-b.jsonl"
    rebuilt.export_jsonl(export_b)

    assert export_a.read_text(encoding="utf-8") == export_b.read_text(encoding="utf-8")

    # and the rebuilt db is behaviorally identical, not just byte-identical on export
    assert [r.model_dump() for r in rebuilt.list_documents()] == [
        d.model_dump() for d in db.list_documents()
    ]
    assert [r.model_dump() for r in rebuilt.series("potassium")] == [
        r.model_dump() for r in db.series("potassium")
    ]
    assert [r.id for r in rebuilt.pending()] == [r.id for r in db.pending()]


def test_rebuild_from_jsonl_is_idempotent_and_replaces_existing_content(
    db: LabsDb, tmp_path: Path
) -> None:
    db.insert_results([_lab()])
    export_path = tmp_path / "export.jsonl"
    db.export_jsonl(export_path)

    other = LabsDb(tmp_path / "other.sqlite")
    other.upsert_document(_doc(sha256="c" * 64, filename="stale.pdf"))
    other.rebuild_from_jsonl(export_path)
    other.rebuild_from_jsonl(export_path)  # rebuilding twice must not duplicate/error

    assert {d.sha256 for d in other.list_documents()} == {SHA_A, SHA_B}
    assert len(other.series("potassium")) == 1

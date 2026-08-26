"""ADR 0026: human review decisions are source data, not derived state.

Exercised end-to-end against the real store before these were written: 587
decisions exported, every row reset to `auto` to simulate a rebuild, then
replayed — 587/587 restored, identical in status and value.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabFlag, LabResult
from adoc.labs.review import (
    export_decisions,
    normalized_name,
    read_decisions,
    replay_decisions,
    write_decisions,
)

SHA = "a" * 64


def _lab(name: str = "potassium", value: float | None = 4.1, **overrides: object) -> LabResult:
    fields: dict[str, object] = {
        "date": date(2026, 5, 2),
        "name": name,
        "name_raw": name,
        "value": value,
        "ucum_unit": "mmol/L",
        "source_doc": SHA,
        "extraction_status": ExtractionStatus.AUTO,
        "raw_json": json.dumps({"name_raw": name}),
    }
    fields.update(overrides)
    return LabResult.model_validate(fields)


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="quest.pdf",
            doc_type="lab-result",
            page_count=1,
            ingested_at=datetime(2026, 5, 3, 12, 0, 0),
            status=DocumentStatus.COMPLETE,
        )
    )
    return store


def _simulate_rebuild(db: LabsDb) -> None:
    """Every row comes back from a fresh ingest as the extractor's own
    `auto` output, carrying no human judgement."""
    db._conn.execute("UPDATE labs SET extraction_status = 'auto'")
    db._conn.commit()


# --- identity that survives a rename --------------------------------------------------


def test_a_renamed_row_still_matches_its_decision() -> None:
    """The UNIQUE key contains `name`, and ADR 0025 deliberately renames
    some rows — so matching on `name` would miss exactly the rows we
    touched."""
    assert normalized_name("10-year probability of hip fracture is") == normalized_name(
        "10-year probability of hip fracture"
    )


def test_different_analytes_do_not_collapse_onto_one_key() -> None:
    assert normalized_name("hs-CRP") != normalized_name("CRP")
    assert normalized_name("Left Hip Total BMD") != normalized_name("Right Hip Total BMD")


# --- export / replay ------------------------------------------------------------------


def test_auto_rows_are_never_exported(db: LabsDb) -> None:
    """`auto` is the extractor's output and carries no human judgement;
    replaying it would assert a decision nobody made."""
    db.insert_results([_lab()])

    assert export_decisions(db) == []


def test_a_correction_survives_a_rebuild(db: LabsDb, tmp_path: Path) -> None:
    (row_id,) = db.insert_results([_lab(value=41.0)])
    assert row_id is not None
    db.correct_row(row_id, value=4.1, flag=LabFlag.HIGH)
    path = tmp_path / "review-decisions.jsonl"
    write_decisions(path, export_decisions(db))

    _simulate_rebuild(db)
    report = replay_decisions(db, read_decisions(path))

    row = db.series("potassium")[0]
    assert report.applied == 1
    assert report.unmatched == []
    assert row.extraction_status == ExtractionStatus.CORRECTED
    assert (row.value, row.flag) == (4.1, LabFlag.HIGH)


def test_a_rejection_survives_a_rebuild(db: LabsDb, tmp_path: Path) -> None:
    """ "A person looked at this and said it is wrong" is a judgement a
    rebuild must carry forward — otherwise the row returns as a fresh one
    to review, and they have to reject it again."""
    (row_id,) = db.insert_results([_lab(name="glucose", value=9999.0)])
    assert row_id is not None
    db.reject_row(row_id)
    path = tmp_path / "review-decisions.jsonl"
    write_decisions(path, export_decisions(db))

    _simulate_rebuild(db)
    replay_decisions(db, read_decisions(path))

    assert db.all_rows()[0].extraction_status == ExtractionStatus.REJECTED


def test_a_confirmation_survives_a_rebuild(db: LabsDb, tmp_path: Path) -> None:
    (row_id,) = db.insert_results([_lab()])
    assert row_id is not None
    db.confirm_row(row_id)
    path = tmp_path / "review-decisions.jsonl"
    write_decisions(path, export_decisions(db))

    _simulate_rebuild(db)
    replay_decisions(db, read_decisions(path))

    assert db.series("potassium")[0].extraction_status == ExtractionStatus.CONFIRMED


def test_a_decision_replays_onto_a_row_the_rebuild_renamed(db: LabsDb, tmp_path: Path) -> None:
    """The whole point: the row comes back from re-ingestion under its
    CLEANED name, and the decision must still find it."""
    (row_id,) = db.insert_results([_lab(name="10-year probability of hip fracture is", value=0.7)])
    assert row_id is not None
    db.confirm_row(row_id)
    path = tmp_path / "review-decisions.jsonl"
    write_decisions(path, export_decisions(db))

    db._conn.execute("DELETE FROM labs")
    db._conn.commit()
    db.insert_results([_lab(name="10-year probability of hip fracture", value=0.7)])
    report = replay_decisions(db, read_decisions(path))

    assert report.applied == 1
    assert db.all_rows()[0].extraction_status == ExtractionStatus.CONFIRMED


def test_a_decision_with_no_matching_row_is_reported_not_guessed(
    db: LabsDb, tmp_path: Path
) -> None:
    """Happens legitimately when the ADR 0025 gate retires a row a person
    had reviewed. Attaching their correction to a NEIGHBOURING measurement
    would be worse than losing it — it would look authoritative."""
    retired = _lab(name="Left total hip: A significant decrease of", value=6.7)
    (row_id,) = db.insert_results([retired])
    assert row_id is not None
    db.correct_row(row_id, value=6.8)
    path = tmp_path / "review-decisions.jsonl"
    write_decisions(path, export_decisions(db))

    db._conn.execute("DELETE FROM labs")
    db._conn.commit()
    db.insert_results([_lab(name="Left Hip Total BMD", value=0.88)])
    report = replay_decisions(db, read_decisions(path))

    assert report.applied == 0
    assert len(report.unmatched) == 1
    assert not report.ok
    # the surviving row keeps the extractor's own status, untouched
    assert db.all_rows()[0].extraction_status == ExtractionStatus.AUTO

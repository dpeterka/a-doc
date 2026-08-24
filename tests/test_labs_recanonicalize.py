"""Tests for adoc.labs.recanonicalize: retro-recanonicalization of stored
`name`s under the current `labs.validate.canonicalize` (`adoc
labs-recanonicalize`, feature/lab-taxonomy).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.labs.recanonicalize import recanonicalize_rows

SHA = "e" * 64


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="doc.pdf",
            doc_type="lab-result",
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )
    return store


def _row(
    *,
    name: str,
    name_raw: str,
    value: float | None = 1.0,
    lab_date: date = date(2026, 1, 1),
    specimen: str = "unknown",
    extraction_status: ExtractionStatus = ExtractionStatus.AUTO,
) -> LabResult:
    return LabResult(
        date=lab_date,
        name=name,
        name_raw=name_raw,
        value=value,
        source_doc=SHA,
        specimen=specimen,
        extraction_status=extraction_status,
        raw_json=json.dumps({"name_raw": name_raw, "value": value}),
    )


def test_dry_run_reports_without_mutating(db: LabsDb) -> None:
    (row_id,) = db.insert_results(
        [_row(name="Alkaline Phosphatase, S", name_raw="Alkaline Phosphatase, S")]
    )
    assert row_id is not None

    report = recanonicalize_rows(db, dry_run=True)

    assert report.checked == 1
    assert report.renamed == 1
    assert report.renamed_ids == [row_id]
    # Nothing actually changed.
    stored = db.get_row(row_id)
    assert stored is not None
    assert stored.name == "Alkaline Phosphatase, S"


def test_plain_rename_updates_the_stored_name(db: LabsDb) -> None:
    (row_id,) = db.insert_results(
        [_row(name="Alkaline Phosphatase, S", name_raw="Alkaline Phosphatase, S")]
    )
    assert row_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 1
    stored = db.get_row(row_id)
    assert stored is not None
    assert stored.name == "Alkaline Phosphatase"
    assert stored.extraction_status == ExtractionStatus.AUTO  # untouched otherwise


def test_already_canonical_row_is_left_untouched(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_row(name="Alkaline Phosphatase", name_raw="ALP")])
    assert row_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.untouched == 1
    assert report.renamed == 0


def test_unrecognized_analyte_is_left_untouched(db: LabsDb) -> None:
    db.insert_results([_row(name="some made up marker", name_raw="some made up marker")])

    report = recanonicalize_rows(db, dry_run=False)

    assert report.untouched == 1
    assert report.renamed == 0


def test_identical_reading_collision_merges_the_duplicate(db: LabsDb) -> None:
    """Two spelling variants of the SAME analyte, same date/specimen/doc,
    with the SAME reading - one survives (renamed), the other is rejected
    as a duplicate, never renamed itself."""
    ids = db.insert_results(
        [
            _row(name="ALKALINE PHOSPHATASE", name_raw="ALKALINE PHOSPHATASE", value=80.0),
            _row(name="Alkaline Phosphatase, S", name_raw="Alkaline Phosphatase, S", value=80.0),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 1
    assert report.merged_duplicates == 1

    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    # The first-processed row (lower id) becomes the survivor.
    assert first.name == "Alkaline Phosphatase"
    assert first.extraction_status == ExtractionStatus.AUTO
    assert second.extraction_status == ExtractionStatus.REJECTED
    assert second.raw_payload()["recanonicalization_duplicate_of"] == first_id


def test_differing_reading_collision_queues_both_for_review(db: LabsDb) -> None:
    """Two spelling variants, same date/specimen/doc, DIFFERING readings -
    the survivor flips to PENDING carrying both payloads; the loser is
    rejected as superseded, never silently dropped."""
    ids = db.insert_results(
        [
            _row(name="ALKALINE PHOSPHATASE", name_raw="ALKALINE PHOSPHATASE", value=80.0),
            _row(name="Alkaline Phosphatase, S", name_raw="Alkaline Phosphatase, S", value=95.0),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 1
    assert report.conflicts_queued == 1

    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    assert first.name == "Alkaline Phosphatase"
    assert first.extraction_status == ExtractionStatus.PENDING
    assert first.raw_payload()["recanonicalize_conflict"]["value"] == 95.0
    assert second.extraction_status == ExtractionStatus.REJECTED
    assert second.raw_payload()["superseded_by_recanonicalize_conflict"] == first_id


def test_idempotent_second_run_finds_nothing_left_to_do(db: LabsDb) -> None:
    db.insert_results(
        [
            _row(name="ALKALINE PHOSPHATASE", name_raw="ALKALINE PHOSPHATASE", value=80.0),
            _row(name="Alkaline Phosphatase, S", name_raw="Alkaline Phosphatase, S", value=80.0),
        ]
    )
    first_report = recanonicalize_rows(db, dry_run=False)
    assert first_report.renamed == 1
    assert first_report.merged_duplicates == 1

    second_report = recanonicalize_rows(db, dry_run=False)

    assert second_report.renamed == 0
    assert second_report.merged_duplicates == 0
    assert second_report.conflicts_queued == 0
    # The one surviving non-rejected row is now already canonical.
    assert second_report.untouched == 1
    assert second_report.checked == 1

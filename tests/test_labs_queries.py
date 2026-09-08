"""Tests for adoc.labs.queries: thin read-side helpers over LabsDb."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, LabDocument, LabFlag, LabResult
from adoc.labs.queries import abnormal_summary, document_listing, trend_series, units_seen

SHA = "e" * 64


def _lab(
    name: str = "potassium",
    value: float = 4.1,
    lab_date: date = date(2026, 5, 2),
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
        "source_doc": SHA,
        "raw_json": json.dumps({"name_raw": name, "value": value}),
    }
    fields.update(overrides)
    return LabResult.model_validate(fields)


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="doc.pdf",
            doc_type="lab-result",
            page_count=1,
            ingested_at=datetime(2026, 5, 3),
            status=DocumentStatus.COMPLETE,
        )
    )
    return store


def test_trend_series_is_time_ordered_with_ref_ranges(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(lab_date=date(2026, 3, 1), value=4.1),
            _lab(lab_date=date(2026, 1, 1), value=4.0),
        ]
    )
    series = trend_series(db, "potassium")
    assert [r.date for r in series] == [date(2026, 1, 1), date(2026, 3, 1)]
    assert all(r.ref_low == 3.5 and r.ref_high == 5.1 for r in series)


def test_abnormal_summary_defaults_to_the_latest_out_of_range_per_analyte(db: LabsDb) -> None:
    """Replaces `..._latest_flagged_per_analyte`. ADR 0051 changed the
    predicate from "carries a flag" to "reads out of range", because only
    187 of 2079 stored rows carry a flag and the other 1892 were reading as
    normal rather than as unknown."""
    db.insert_results(
        [
            _lab(lab_date=date(2026, 1, 1), value=6.0, flag=LabFlag.HIGH),
            _lab(lab_date=date(2026, 6, 1), value=4.1, flag=None),
            # Its own range — `_lab` defaults to potassium's, and 140
            # against 3.5-5.1 would read high for a fixture reason.
            _lab(name="sodium", value=140.0, ucum_unit="mmol/L", ref_low=135.0, ref_high=145.0),
        ]
    )
    summary = abnormal_summary(db)
    # Latest potassium 4.1 is inside 3.5-5.1 and sodium 140 inside 135-145.
    assert summary == []


def test_an_unflagged_row_outside_its_reference_range_is_abnormal(db: LabsDb) -> None:
    """The 42 rows this unlocks. Measured on the real record: 42 sit above
    their reference range and 9 below, with no flag, and every one of them
    read as unremarkable to both the criteria scorers and the model."""
    db.insert_results(
        [
            _lab(
                name="ferritin",
                value=410.0,
                ucum_unit="ng/mL",
                ref_low=None,
                ref_high=None,
                ref_text="16-232 ng/mL",
            )
        ]
    )

    summary = abnormal_summary(db)

    assert [row.name for row in summary] == ["ferritin"]


def test_an_unflagged_row_inside_its_range_is_not_abnormal(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(
                name="ferritin",
                value=100.0,
                ucum_unit="ng/mL",
                ref_low=None,
                ref_high=None,
                ref_text="16-232 ng/mL",
            )
        ]
    )

    assert abnormal_summary(db) == []


def test_abnormal_summary_since_returns_history(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(lab_date=date(2026, 1, 1), value=6.0, flag=LabFlag.HIGH),
            _lab(lab_date=date(2026, 8, 1), value=6.0, flag=LabFlag.HIGH),
        ]
    )
    summary = abnormal_summary(db, since=date(2026, 6, 1))
    assert len(summary) == 1
    assert summary[0].date == date(2026, 8, 1)


def test_units_seen_returns_sorted_distinct_units(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(lab_date=date(2026, 1, 1), value=4.1, ucum_unit="mmol/L"),
            _lab(lab_date=date(2026, 2, 1), value=4.2, ucum_unit="mEq/L"),
        ]
    )
    assert units_seen(db, "potassium") == ["mEq/L", "mmol/L"]


def test_document_listing_returns_all_documents(db: LabsDb) -> None:
    docs = document_listing(db)
    assert [d.sha256 for d in docs] == [SHA]

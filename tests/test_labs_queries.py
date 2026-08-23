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


def test_abnormal_summary_defaults_to_latest_flagged_per_analyte(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(lab_date=date(2026, 1, 1), value=6.0, flag=LabFlag.HIGH),
            _lab(lab_date=date(2026, 6, 1), value=4.1, flag=None),
            _lab(name="sodium", value=140.0, ucum_unit="mmol/L"),
        ]
    )
    summary = abnormal_summary(db)
    assert summary == []  # latest potassium result is not flagged; sodium never flagged


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

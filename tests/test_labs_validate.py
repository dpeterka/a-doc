"""Tests for adoc.labs.validate: deterministic (no-LLM) lab validation."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabFlag, LabResult
from adoc.labs.validate import (
    ANALYTE_SPECS,
    IssueCode,
    canonicalize,
    trend_outlier,
    validate_row,
)

SHA = "d" * 64


def _lab(
    name: str = "potassium",
    value: float | None = 4.1,
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


# ----------------------------------------------------------------
# canonicalize
# ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["WBC", "wbc", "White Blood Cell Count", "white-blood-cells", "  Leukocytes  "],
)
def test_canonicalize_recognizes_known_aliases(raw: str) -> None:
    assert canonicalize(raw) == "WBC"


def test_canonicalize_is_punctuation_and_case_insensitive() -> None:
    assert canonicalize("C-Reactive Protein") == "CRP"
    assert canonicalize("c reactive protein") == "CRP"
    assert canonicalize("CRP") == "CRP"


def test_canonicalize_unknown_analyte_returns_none() -> None:
    assert canonicalize("some made up analyte xyz") is None


def test_all_specs_have_a_working_canonical_alias() -> None:
    for name in ANALYTE_SPECS:
        assert canonicalize(name) == name


# ----------------------------------------------------------------
# validate_row: unknown unit
# ----------------------------------------------------------------


def test_unknown_unit_flagged() -> None:
    row = _lab(name="potassium", ucum_unit="mg/dL")  # potassium is mmol/L
    issues = validate_row(row)
    assert any(i.code is IssueCode.UNKNOWN_UNIT for i in issues)


def test_known_unit_not_flagged() -> None:
    row = _lab(name="potassium", ucum_unit="mmol/L")
    issues = validate_row(row)
    assert not any(i.code is IssueCode.UNKNOWN_UNIT for i in issues)


# ----------------------------------------------------------------
# validate_row: physiologic plausibility bounds
# ----------------------------------------------------------------


def test_out_of_bounds_value_flagged() -> None:
    row = _lab(name="potassium", value=41.0)  # decimal-shift error, way past 1.0-10.0
    issues = validate_row(row)
    assert any(i.code is IssueCode.OUT_OF_BOUNDS for i in issues)


def test_in_bounds_value_not_flagged() -> None:
    row = _lab(name="potassium", value=4.1)
    issues = validate_row(row)
    assert not any(i.code is IssueCode.OUT_OF_BOUNDS for i in issues)


# ----------------------------------------------------------------
# validate_row: flag/value vs reference range consistency
# ----------------------------------------------------------------


def test_flag_inconsistent_with_value_flagged() -> None:
    row = _lab(value=4.1, ref_low=3.5, ref_high=5.1, flag=LabFlag.HIGH)  # 4.1 is in range
    issues = validate_row(row)
    assert any(i.code is IssueCode.FLAG_INCONSISTENT for i in issues)


def test_flag_consistent_with_value_not_flagged() -> None:
    row = _lab(value=6.0, ref_low=3.5, ref_high=5.1, flag=LabFlag.HIGH)  # 6.0 is above range
    issues = validate_row(row)
    assert not any(i.code is IssueCode.FLAG_INCONSISTENT for i in issues)


# ----------------------------------------------------------------
# validate_row: titer format
# ----------------------------------------------------------------


def test_titer_bad_format_flagged() -> None:
    row = _lab(
        name="ANA titer",
        value=None,
        value_text="positive",
        ucum_unit=None,
        ref_low=None,
        ref_high=None,
    )
    issues = validate_row(row)
    assert any(i.code is IssueCode.TITER_FORMAT for i in issues)


@pytest.mark.parametrize("titer", ["1:80", "1:160", "1:320", "1:640", "1:5120"])
def test_titer_good_format_not_flagged(titer: str) -> None:
    row = _lab(
        name="ANA titer",
        value=None,
        value_text=titer,
        ucum_unit=None,
        ref_low=None,
        ref_high=None,
    )
    issues = validate_row(row)
    assert not any(i.code is IssueCode.TITER_FORMAT for i in issues)


def test_unmapped_analyte_returns_no_issues() -> None:
    row = _lab(name="some unmapped analyte", value=999999.0, ucum_unit="bogus-unit")
    assert validate_row(row) == []


# ----------------------------------------------------------------
# trend_outlier
# ----------------------------------------------------------------


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


def _seed_priors(store: LabsDb, values: list[float]) -> None:
    rows = [
        _lab(value=v, lab_date=date(2026, 1, i + 1), extraction_status=ExtractionStatus.CONFIRMED)
        for i, v in enumerate(values)
    ]
    store.insert_results(rows)


def test_trend_outlier_triggers_on_planted_decimal_error(db: LabsDb) -> None:
    _seed_priors(db, [4.0, 4.1, 4.2])
    new_row = _lab(value=41.0, lab_date=date(2026, 6, 1))
    issue = trend_outlier(db, new_row)
    assert issue is not None
    assert issue.code is IssueCode.TREND_OUTLIER


def test_trend_outlier_does_not_trigger_on_normal_variation(db: LabsDb) -> None:
    _seed_priors(db, [4.0, 4.1, 4.2])
    new_row = _lab(value=4.5, lab_date=date(2026, 6, 1))
    issue = trend_outlier(db, new_row)
    assert issue is None


def test_trend_outlier_requires_minimum_priors(db: LabsDb) -> None:
    _seed_priors(db, [4.0, 4.1])  # only 2 priors, below the minimum of 3
    new_row = _lab(value=41.0, lab_date=date(2026, 6, 1))
    issue = trend_outlier(db, new_row)
    assert issue is None


def test_trend_outlier_no_value_returns_none(db: LabsDb) -> None:
    _seed_priors(db, [4.0, 4.1, 4.2])
    new_row = _lab(
        name="ANA titer",
        value=None,
        value_text="1:320",
        ucum_unit=None,
        ref_low=None,
        ref_high=None,
        lab_date=date(2026, 6, 1),
    )
    assert trend_outlier(db, new_row) is None

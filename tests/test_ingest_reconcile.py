"""Tests for adoc.ingest.reconcile: cross-pass matching and the AUTO-gate list.

Every "forces PENDING" case below starts from an otherwise-fully-agreeing
pair of passes and changes exactly one dimension, so each of the AUTO
gates documented in `reconcile.py`'s module docstring is exercised in
isolation (per the task's "each gate individually forces PENDING,
parametrized" requirement).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adoc.ingest.reconcile import reconcile
from adoc.ingest.schema import DocumentExtraction, ExtractedResult
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult

SHA = "a" * 64


def _result(**overrides: object) -> ExtractedResult:
    fields: dict[str, object] = {
        "name_raw": "Potassium",
        "value": 4.1,
        "unit_raw": "mmol/L",
        "ref_range_raw": "3.5-5.1",
        "page": 1,
        "confidence": "high",
    }
    fields.update(overrides)
    return ExtractedResult.model_validate(fields)


def _doc(results: list[ExtractedResult], **overrides: object) -> DocumentExtraction:
    fields: dict[str, object] = {
        "doc_type": "lab_report",
        "collection_date": date(2026, 5, 2),
        "report_date": date(2026, 5, 3),
        "results": results,
    }
    fields.update(overrides)
    return DocumentExtraction.model_validate(fields)


def _empty_db(tmp_path: Path) -> LabsDb:
    return LabsDb(tmp_path / "labs.sqlite")


def test_full_agreement_is_auto_with_no_reasons(tmp_path: Path) -> None:
    pass_a = _doc([_result()])
    pass_b = _doc([_result()])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert rows[0].reasons == []
    assert rows[0].canonical_name == "potassium"
    payload = json.loads(rows[0].raw_json)
    assert payload["pass_a"]["name_raw"] == "Potassium"
    assert payload["pass_b"]["name_raw"] == "Potassium"


def test_result_in_only_one_pass_is_pending_single_pass(tmp_path: Path) -> None:
    pass_a = _doc([_result()])
    pass_b = _doc([])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert "single_pass" in rows[0].reasons


def test_page_tolerant_matching_pairs_adjacent_pages(tmp_path: Path) -> None:
    pass_a = _doc([_result(page=2)])
    pass_b = _doc([_result(page=3)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"


def test_pages_beyond_tolerance_do_not_match(tmp_path: Path) -> None:
    pass_a = _doc([_result(page=1)])
    pass_b = _doc([_result(page=5)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 2
    assert all(row.status == "pending" for row in rows)
    assert all("single_pass" in row.reasons for row in rows)


@pytest.mark.parametrize(
    "gate_name,pass_a_overrides,pass_b_overrides,expected_reason_substring",
    [
        ("value", {}, {"value": 4.5}, "value_mismatch"),
        (
            "value_text",
            {"value": None, "value_text": "4.1"},
            {"value": None, "value_text": "4.2"},
            "value_text_mismatch",
        ),
        ("unit", {}, {"unit_raw": "mEq/L"}, "unit_mismatch"),
        ("ref_range", {}, {"ref_range_raw": "3.5-5.2"}, "ref_range_mismatch"),
        ("flag", {"flag_raw": "A"}, {"flag_raw": None}, "flag_mismatch"),
        ("confidence", {"confidence": "medium"}, {}, "pass_a_confidence:medium"),
        ("validate_bounds", {"value": 41}, {"value": 41}, "outside plausible"),
    ],
)
def test_each_gate_individually_forces_pending(
    tmp_path: Path,
    gate_name: str,
    pass_a_overrides: dict[str, object],
    pass_b_overrides: dict[str, object],
    expected_reason_substring: str,
) -> None:
    pass_a = _doc([_result(**pass_a_overrides)])
    pass_b = _doc([_result(**pass_b_overrides)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1, gate_name
    assert rows[0].status == "pending", gate_name
    assert any(expected_reason_substring in reason for reason in rows[0].reasons), (
        gate_name,
        rows[0].reasons,
    )


def test_agreed_trend_spike_autos_with_annotation(tmp_path: Path) -> None:
    db = _empty_db(tmp_path)
    db.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="priors.pdf",
            doc_type="lab_report",
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )
    for day, value in ((1, 3.9), (8, 4.0), (15, 4.1)):
        db.insert_results(
            [
                LabResult(
                    date=date(2026, 4, day),
                    name="potassium",
                    name_raw="Potassium",
                    value=value,
                    ucum_unit="mmol/L",
                    ref_low=3.5,
                    ref_high=5.1,
                    source_doc=SHA,
                    extraction_status=ExtractionStatus.AUTO,
                    raw_json="{}",
                )
            ]
        )

    # 6.0 is a >40% jump vs the ~4.0 median of earlier readings, but both
    # passes AGREE — treated as real physiology (this patient spikes
    # frequently): AUTO, with the spike kept as an annotation in reasons.
    pass_a = _doc([_result(value=6.0)])
    pass_b = _doc([_result(value=6.0)])

    rows = reconcile(pass_a, pass_b, db)

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert any("away from the median" in reason for reason in rows[0].reasons)


def test_decimal_signature_shift_still_queues_even_on_agreement(tmp_path: Path) -> None:
    """A >=10x-class shift is the decimal-misread signature (4.1 -> 41):
    it must queue even when both passes agree on the misread value."""
    db = _empty_db(tmp_path)
    db.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="priors.pdf",
            doc_type="lab_report",
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )
    for day, value in ((1, 3.9), (8, 4.0), (15, 4.1)):
        db.insert_results(
            [
                LabResult(
                    date=date(2026, 4, day),
                    name="potassium",
                    name_raw="Potassium",
                    value=value,
                    ucum_unit="mmol/L",
                    ref_low=3.5,
                    ref_high=5.1,
                    source_doc=SHA,
                    extraction_status=ExtractionStatus.AUTO,
                    raw_json="{}",
                )
            ]
        )
    # 41 vs ~4.0 median: bounds gate also fires, but the decimal-signature
    # trend gate must hold independently even for an in-bounds 10x analyte,
    # so assert the pending status plus the trend reason's presence.
    pass_a = _doc([_result(value=41.0)])
    pass_b = _doc([_result(value=41.0)])

    rows = reconcile(pass_a, pass_b, db)

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert any("away from the median" in reason for reason in rows[0].reasons)


def test_missing_date_alone_forces_pending(tmp_path: Path) -> None:
    pass_a = _doc([_result()], collection_date=None, report_date=None)
    pass_b = _doc([_result()], collection_date=None, report_date=None)

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert "missing_date" in rows[0].reasons
    assert rows[0].date == date.today()


def test_doc_date_prefers_collection_date_over_report_date(tmp_path: Path) -> None:
    pass_a = _doc([_result()], collection_date=date(2026, 5, 1), report_date=date(2026, 5, 10))
    pass_b = _doc([_result()], collection_date=date(2026, 5, 1), report_date=date(2026, 5, 10))

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert rows[0].date == date(2026, 5, 1)


def test_implausible_extracted_date_is_treated_as_missing(tmp_path: Path) -> None:
    """A shared misread like year 0906 must queue as missing_date, never
    auto-accept under a bogus collection date (real-corpus finding)."""
    pass_a = _doc([_result()], collection_date=date(906, 6, 6), report_date=None)
    pass_b = _doc([_result()], collection_date=date(906, 6, 6), report_date=None)

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert "missing_date" in rows[0].reasons
    assert rows[0].date == date.today()

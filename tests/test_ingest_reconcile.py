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

from adoc.ingest.reconcile import (
    clean_result_name,
    compute_pair_reasons,
    flags_equivalent,
    reconcile,
    ref_ranges_equivalent,
    row_is_agreed,
    units_equivalent,
)
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
        ("specimen", {"specimen": "serum"}, {"specimen": "urine"}, "specimen_mismatch"),
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


def test_agreeing_specimen_carries_through_to_reconciled_row(tmp_path: Path) -> None:
    pass_a = _doc([_result(specimen="urine", value=None, value_text="NEGATIVE")])
    pass_b = _doc([_result(specimen="urine", value=None, value_text="NEGATIVE")])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert rows[0].specimen == "urine"


def test_same_analyte_different_specimen_in_one_document_stays_separate(tmp_path: Path) -> None:
    """The real finding this slice fixes: a document that prints "GLUCOSE"
    once under a urinalysis section (page 1) and once under a serum
    chemistry panel (page 3) must reconcile into TWO separate rows, each
    carrying its own specimen - not one merged/crossed row."""

    def _urine_glucose(page: int) -> ExtractedResult:
        # `unit_raw="mg/dL"` here is a test simplification, not a real
        # urinalysis reading shape (a real report would print no unit
        # for a qualitative "NEGATIVE") - it sidesteps
        # `labs.validate.ANALYTE_SPECS`'s unrelated unit-whitelist gate
        # (glucose is registered numeric-only) so this test isolates just
        # the specimen-scoping behavior under test.
        return _result(
            name_raw="GLUCOSE",
            specimen="urine",
            value=None,
            value_text="NEGATIVE",
            unit_raw="mg/dL",
            ref_range_raw=None,
            page=page,
        )

    def _serum_glucose(page: int) -> ExtractedResult:
        return _result(
            name_raw="GLUCOSE", specimen="serum", value=92.0, unit_raw="mg/dL", page=page
        )

    pass_a = _doc([_urine_glucose(1), _serum_glucose(3)])
    pass_b = _doc([_urine_glucose(1), _serum_glucose(3)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 2
    by_specimen = {row.specimen: row for row in rows}
    assert by_specimen["urine"].status == "auto"
    assert by_specimen["urine"].value_text == "NEGATIVE"
    assert by_specimen["serum"].status == "auto"
    assert by_specimen["serum"].value == 92.0


def test_specimen_mismatch_forces_pending_and_is_a_disagreement(tmp_path: Path) -> None:
    from adoc.ingest.reconcile import row_is_agreed

    pass_a = _doc([_result(specimen="serum")])
    pass_b = _doc([_result(specimen="urine")])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert any("specimen_mismatch" in reason for reason in rows[0].reasons)
    assert row_is_agreed(rows[0].reasons) is False


def test_pair_rows_breaks_page_ties_by_matching_specimen() -> None:
    """Two same-page candidates in pass B are equally page-close to a pass-A
    row; `_pair_rows` must prefer the one with the SAME specimen as the
    pass-A row over the one with a different specimen, so a genuine
    same-specimen match isn't split apart by an equidistant, different-
    specimen row that happens to share a page."""
    from adoc.ingest.reconcile import _pair_rows

    a_urine = _result(name_raw="GLUCOSE", specimen="urine", value=None, value_text="NEGATIVE")
    b_serum = _result(name_raw="GLUCOSE", specimen="serum", value=92.0)
    b_urine = _result(name_raw="GLUCOSE", specimen="urine", value=None, value_text="NEGATIVE")

    pairs = _pair_rows([a_urine], [b_serum, b_urine])

    # one matched pair (the same-specimen tie-break winner) plus the
    # unmatched leftover b row (single_pass)
    assert len(pairs) == 2
    assert (a_urine, b_urine) in pairs
    assert (None, b_serum) in pairs


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


# --------------------------------------------------------------------------
# clean_result_name (queue-ergonomics slice item 3b): strip a trailing
# sentence-fragment verb/punctuation, collapse whitespace.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10-year probability of hip fracture is", "10-year probability of hip fracture"),
        ("10-year probability of hip fracture is:", "10-year probability of hip fracture"),
        ("Potassium was", "Potassium"),
        ("Potassium:", "Potassium"),
        ("Potassium  level", "Potassium level"),
        ("Potassium", "Potassium"),
        ("FRAX 10-year probability of hip fracture", "FRAX 10-year probability of hip fracture"),
    ],
)
def test_clean_result_name_strips_fragments_and_collapses_whitespace(
    raw: str, expected: str
) -> None:
    assert clean_result_name(raw) == expected


def test_clean_result_name_never_returns_empty() -> None:
    # A name that is ENTIRELY a stripped token (pathological input) must
    # fall back to something non-empty rather than vanish.
    assert clean_result_name("is") == "is"


def test_reconcile_applies_name_cleaning_to_every_extracted_name(tmp_path: Path) -> None:
    pass_a = _doc([_result(name_raw="Potassium is")])
    pass_b = _doc([_result(name_raw="Potassium is")])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].name_raw == "Potassium"


# --------------------------------------------------------------------------
# RESCUE pass (queue-ergonomics slice item 3b): value-anchored pairing of
# leftover single-pass rows across DIFFERENT name-groups - the real FRAX
# case that motivated it.
# --------------------------------------------------------------------------


def test_rescue_pairs_the_real_frax_naming_variant_case(tmp_path: Path) -> None:
    pass_a = _doc(
        [
            _result(
                name_raw="FRAX 10-year probability of hip fracture",
                value=None,
                value_text="12%",
                unit_raw=None,
                ref_range_raw=None,
                page=4,
            )
        ]
    )
    pass_b = _doc(
        [
            _result(
                name_raw="10-year probability of hip fracture is",
                value=None,
                value_text="12%",
                unit_raw=None,
                ref_range_raw=None,
                page=4,
            )
        ]
    )

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    row = rows[0]
    assert row.status == "pending"
    assert row.reasons[0] == "name_variant"
    assert row_is_agreed(row.reasons) is True
    # the longer/more specific (cleaned) name wins
    assert row.name_raw == "FRAX 10-year probability of hip fracture"
    payload = json.loads(row.raw_json)
    assert payload["name_variant"] == {
        "pass_a_name": "FRAX 10-year probability of hip fracture",
        "pass_b_name": "10-year probability of hip fracture",
    }


def test_rescue_requires_page_tolerance(tmp_path: Path) -> None:
    pass_a = _doc(
        [_result(name_raw="FRAX 10-year probability of hip fracture", value=12.0, page=1)]
    )
    pass_b = _doc([_result(name_raw="10-year probability of hip fracture is", value=12.0, page=9)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 2
    assert all(row.status == "pending" for row in rows)
    assert all("single_pass" in row.reasons for row in rows)


def test_rescue_never_pairs_across_a_real_unit_mismatch(tmp_path: Path) -> None:
    """Two genuinely different analytes that happen to both read 0.0 on the
    same page must NOT be rescued together when their units are
    incompatible - the module docstring's accepted residual risk requires
    unit compatibility, it doesn't waive it."""
    pass_a = _doc([_result(name_raw="Foo Test", value=0.0, unit_raw="mg/dL", page=1)])
    pass_b = _doc([_result(name_raw="Bar Test", value=0.0, unit_raw="ng/mL", page=1)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 2
    assert all(row.status == "pending" for row in rows)
    assert all("single_pass" in row.reasons for row in rows)


# --------------------------------------------------------------------------
# Semantic comparators (feature/semantic-compare): real-corpus false-
# "disagreement" families, each with an equivalent pair and a non-
# equivalent guard.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a_raw,b_raw,expected",
    [
        # real corpus: trailing embedded unit
        ("<20", "<20 Units", True),
        ("3.80-5.10", "3.80 - 5.10 Million/uL", True),
        # unicode dash variants
        ("3.5–5.1", "3.5-5.1", True),  # en dash
        ("3.5‒5.1", "3.5 - 5.1", True),  # figure dash + spacing
        # threshold forms
        ("<20", "< 20", True),
        (">=1:80", ">= 1:80", True),
        ("<1:80", "<1:80", True),
        # qualitative equivalence set
        ("negative", "none seen", True),
        ("Not Detected", "NEGATIVE", True),
        # both None/empty
        (None, None, True),
        (None, "", True),
        # guards: genuinely different thresholds/ranges
        ("<20", "<30", False),
        ("3.5-5.1", "3.5-5.2", False),
        (">=1:80", ">=1:160", False),
        # guard: one side missing is a real difference, not equivalence
        ("<20", None, False),
        ("<20", "", False),
        # guard: unparseable and different -> normalized-string fallback
        ("see comment", "positive", False),
    ],
)
def test_ref_ranges_equivalent_matrix(a_raw: str | None, b_raw: str | None, expected: bool) -> None:
    assert ref_ranges_equivalent(a_raw, b_raw) is expected


def test_ref_ranges_equivalent_both_unparseable_and_equal_falls_back_to_string_equality() -> None:
    assert ref_ranges_equivalent("see comment", "see comment") is True


@pytest.mark.parametrize(
    "a_raw,b_raw,expected",
    [
        # real corpus: RBC spelling family
        ("Million/uL", "M/uL", True),
        ("x10^6/uL", "10*6/uL", True),
        ("x10E6/uL", "M/uL", True),
        # WBC/platelet spelling family
        ("Thousand/uL", "K/uL", True),
        # TSH: numerically equivalent reporting units
        ("mIU/L", "uIU/mL", True),
        # bare case/whitespace differences
        ("mmol/L", "mmol/l", True),
        ("  mg/dL ", "mg/dl", True),
        # guards: genuinely different units
        ("mg/dL", "g/dL", False),
        ("IU/mL", "U/mL", False),  # kept separate on purpose
        ("umol/L", "mg/dL", False),
    ],
)
def test_units_equivalent_matrix(a_raw: str, b_raw: str, expected: bool) -> None:
    assert units_equivalent(a_raw, b_raw) is expected


@pytest.mark.parametrize(
    "a_raw,b_raw,expected",
    [
        # real corpus: None vs "" (both unflagged)
        (None, "", True),
        (None, "N", True),
        (None, "Normal", True),
        ("", "normal", True),
        # case-insensitive word vs letter code
        ("H", "high", True),
        ("l", "LOW", True),
        ("HH", "critical high", True),
        ("LL", "Critical Low", True),
        ("A", "Abnormal", True),
        # guards: absent vs an actual abnormal code stays a real mismatch
        (None, "H", False),
        (None, "A", False),
        ("", "L", False),
        ("H", "L", False),
    ],
)
def test_flags_equivalent_matrix(a_raw: str | None, b_raw: str | None, expected: bool) -> None:
    assert flags_equivalent(a_raw, b_raw) is expected


# --------------------------------------------------------------------------
# End-to-end: real-corpus false-mismatch pairs now reconcile to AUTO with no
# reasons at all - not just "not blocking", genuinely no false disagreement
# recorded.
# --------------------------------------------------------------------------


def test_ref_range_trailing_unit_token_no_longer_forces_pending(tmp_path: Path) -> None:
    pass_a = _doc([_result(ref_range_raw="<20")])
    pass_b = _doc([_result(ref_range_raw="<20 Units")])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert rows[0].reasons == []


def test_ref_range_and_unit_synonym_combo_no_longer_forces_pending(tmp_path: Path) -> None:
    """The real corpus's RBC case: an embedded-unit reference range AND a
    unit spelling difference on the SAME pair, both resolved."""
    pass_a = _doc([_result(name_raw="RBC", value=4.5, unit_raw="M/uL", ref_range_raw="3.80-5.10")])
    pass_b = _doc(
        [
            _result(
                name_raw="RBC",
                value=4.5,
                unit_raw="Million/uL",
                ref_range_raw="3.80 - 5.10 Million/uL",
            )
        ]
    )

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert rows[0].reasons == []


def test_flag_none_vs_empty_string_no_longer_forces_pending(tmp_path: Path) -> None:
    pass_a = _doc([_result(flag_raw=None)])
    pass_b = _doc([_result(flag_raw="")])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert rows[0].reasons == []


def test_flag_absent_vs_normal_word_no_longer_forces_pending(tmp_path: Path) -> None:
    pass_a = _doc([_result(flag_raw="N")])
    pass_b = _doc([_result(flag_raw=None)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert rows[0].reasons == []


def test_flag_absent_vs_abnormal_code_still_forces_pending(tmp_path: Path) -> None:
    """The guard case: an omitted flag is NOT equivalent to an actual
    abnormal code - one pass genuinely saw something the other missed."""
    pass_a = _doc([_result(flag_raw=None)])
    pass_b = _doc([_result(flag_raw="H")])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert any("flag_mismatch" in reason for reason in rows[0].reasons)


def test_unit_synonym_tsh_no_longer_forces_pending(tmp_path: Path) -> None:
    pass_a = _doc([_result(name_raw="TSH", value=2.5, unit_raw="mIU/L", ref_range_raw="0.4-4.0")])
    pass_b = _doc([_result(name_raw="TSH", value=2.5, unit_raw="uIU/mL", ref_range_raw="0.4-4.0")])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "auto"
    assert rows[0].reasons == []


# --------------------------------------------------------------------------
# compute_pair_reasons: the pure function shared with `labs.reclassify`.
# --------------------------------------------------------------------------


def test_compute_pair_reasons_matches_reconcile_for_an_equivalent_pair(tmp_path: Path) -> None:
    a = _result(ref_range_raw="<20")
    b = _result(ref_range_raw="<20 Units")

    reasons = compute_pair_reasons(
        a, b, doc_date=date(2026, 5, 2), missing_date=False, db=_empty_db(tmp_path)
    )

    assert reasons == []


def test_compute_pair_reasons_reports_a_genuine_mismatch(tmp_path: Path) -> None:
    a = _result(value=8.0)
    b = _result(value=9.0)

    reasons = compute_pair_reasons(
        a, b, doc_date=date(2026, 5, 2), missing_date=False, db=_empty_db(tmp_path)
    )

    assert any("value_mismatch" in reason for reason in reasons)


def test_rescue_accepts_residual_risk_when_units_are_compatible(tmp_path: Path) -> None:
    """The flip side of the test above: when units ARE compatible (both
    unstated), two different-analyte same-value-on-the-same-page rows DO
    get rescued together - an accepted residual risk per the module
    docstring. D5: since these two names share NO meaningful token ("Test"
    is a generic filler word, not a real overlap), the pair is still
    rescued (never left stranded as twin single_pass rows) but flagged
    `name_variant_unverified` - a disagreement-bucketed reason, not the
    silent bulk-OK `name_variant` - so a human actually looks."""
    pass_a = _doc([_result(name_raw="Foo Test", value=0.0, unit_raw=None, page=1)])
    pass_b = _doc([_result(name_raw="Bar Test", value=0.0, unit_raw=None, page=1)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert "name_variant_unverified" in rows[0].reasons
    assert "name_variant" not in rows[0].reasons
    assert row_is_agreed(rows[0].reasons) is False


def test_rescue_pair_with_shared_meaningful_token_is_name_variant_and_agreed(
    tmp_path: Path,
) -> None:
    """D5: a rescued pair whose (cleaned) names share at least one
    meaningful token (len > 2, not a stopword) is the common "same result,
    worded differently" case - `name_variant`, still in the agreed
    bucket."""
    pass_a = _doc([_result(name_raw="Serum Iron Studies", value=45.0, unit_raw="ug/dL", page=2)])
    pass_b = _doc([_result(name_raw="Iron, Total", value=45.0, unit_raw="ug/dL", page=2)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].reasons[0] == "name_variant"
    assert row_is_agreed(rows[0].reasons) is True


def test_rescue_pair_with_no_shared_token_is_name_variant_unverified_and_disagreement(
    tmp_path: Path,
) -> None:
    """D5: the flip side - zero meaningful token overlap between the two
    (cleaned) names is the accepted residual risk (module docstring: two
    genuinely different analytes coincidentally sharing a value/page).
    Rescue still pairs them (never stranded as twin single_pass rows), but
    the reason routes to the disagreement bucket instead of being bulk-OK'd
    alongside genuine name variants."""
    pass_a = _doc([_result(name_raw="Vitamin B12", value=0.0, unit_raw=None, page=1)])
    pass_b = _doc([_result(name_raw="Ketones, Urine", value=0.0, unit_raw=None, page=1)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].reasons[0] == "name_variant_unverified"
    assert row_is_agreed(rows[0].reasons) is False


@pytest.mark.parametrize(
    ("flag_a", "flag_b", "equivalent"),
    [
        ("C", None, True),  # LabCorp performing-site footnote, not a flag
        ("B", None, True),
        ("D", None, True),
        ("F", None, True),
        ("High, H", "High", True),  # word+code duplication
        ("Abnormal, C", "Abnormal", True),  # code + footnote
        ("A, C", "A", True),
        ("A", None, False),  # 'A' is a real Abnormal code - stays reviewable
        ("H", None, False),  # real missed flag
    ],
)
def test_flag_footnote_and_multitoken_equivalence(
    flag_a: str | None, flag_b: str | None, equivalent: bool
) -> None:
    from adoc.ingest.reconcile import flags_equivalent

    assert flags_equivalent(flag_a, flag_b) is equivalent


@pytest.mark.parametrize(
    ("ref_a", "ref_b", "equivalent"),
    [
        ("Reference Range: NEGATIVE", "NEGATIVE", True),
        ("Reference Range: NOT DETECTED", "NOT DETECTED", True),
        ("See Note 2", None, True),  # pointer carries no range semantics
        (None, "See note 12", True),
        ("<20", "<30", False),  # guard: real range conflict stays
    ],
)
def test_ref_range_label_prefix_and_pointer_equivalence(
    ref_a: str | None, ref_b: str | None, equivalent: bool
) -> None:
    from adoc.ingest.reconcile import ref_ranges_equivalent

    assert ref_ranges_equivalent(ref_a, ref_b) is equivalent


def test_single_source_ref_range_is_agreed_not_disagreement(tmp_path: Path) -> None:
    """Exactly one pass transcribed the range (value/unit agree): queues in
    the agreed bucket via ref_range_single_source, never 'needs eyes'."""
    from adoc.ingest.reconcile import row_is_agreed

    pass_a = _doc([_result(ref_range_raw="IgG <1:64")])
    pass_b = _doc([_result(ref_range_raw=None)])

    rows = reconcile(pass_a, pass_b, _empty_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert any(r.startswith("ref_range_single_source") for r in rows[0].reasons)
    assert not any(r.startswith("ref_range_mismatch") for r in rows[0].reasons)
    assert row_is_agreed(rows[0].reasons)


@pytest.mark.parametrize(
    ("ref_a", "ref_b", "equivalent"),
    [
        ("Not Detected", "Not Detected See Note 1", True),
        ("<1.00 Index", "<1.00 Index. See Note 12", True),
        ("Negative", "Positive See Note 1", False),  # guard: real difference stays
    ],
)
def test_trailing_see_note_suffix_is_stripped(ref_a: str, ref_b: str, equivalent: bool) -> None:
    from adoc.ingest.reconcile import ref_ranges_equivalent

    assert ref_ranges_equivalent(ref_a, ref_b) is equivalent


@pytest.mark.parametrize(
    ("ref_a", "ref_b", "equivalent"),
    [
        (  # real corpus: progesterone phase tiers, label prose differs
            "Follicular <1.0; Luteal 2.6-21.5; Postmenopausal <0.5; "
            "Pregnancy 1st 4.1-34.0, 2nd 24.0-76.0, 3rd 52.0-302.0",
            "Female: Follicular Phase <1.0; Luteal Phase 2.6-21.5; "
            "Post menopausal <0.5; Pregnancy 1st Trimester 4.1-34.0, "
            "2nd Trimester 24.0-76.0, 3rd Trimester 52.0-302.0",
            True,
        ),
        (  # one tier's number differs -> genuine mismatch stays
            "Follicular <1.0; Luteal 2.6-21.5",
            "Follicular <1.0; Luteal 2.6-22.5",
            False,
        ),
        (  # different tier counts -> mismatch
            "Follicular <1.0; Luteal 2.6-21.5",
            "Follicular <1.0",
            False,
        ),
    ],
)
def test_multi_tier_conditional_ranges_compare_numerically(
    ref_a: str, ref_b: str, equivalent: bool
) -> None:
    from adoc.ingest.reconcile import ref_ranges_equivalent

    assert ref_ranges_equivalent(ref_a, ref_b) is equivalent


def test_stored_name_requires_exact_alias_not_permissive_match(tmp_path: Path) -> None:
    """Only an exact alias may NAME a persisted row (`_stored_name`,
    feature/taxonomy-distinctions): a site-prefixed DEXA score matches the
    "Z-score" spec via the score-suffix rule (read-time grouping/
    validation), but persisting it under bare "Z-score" would discard the
    site - so `canonical_name` must be None (store the raw name verbatim)."""
    score = _result(
        name_raw="LEFT HIP Total Z-Score", value=-1.2, unit_raw=None, ref_range_raw=None
    )
    rows = reconcile(_doc([score]), _doc([score]), _empty_db(tmp_path))
    assert len(rows) == 1
    assert rows[0].canonical_name is None
    assert rows[0].name_raw == "LEFT HIP Total Z-Score"


def test_stored_name_requires_exact_alias_in_single_pass_path(tmp_path: Path) -> None:
    score = _result(
        name_raw="LEFT HIP femoral neck T-Score", value=-0.8, unit_raw=None, ref_range_raw=None
    )
    rows = reconcile(_doc([score]), _doc([]), _empty_db(tmp_path))
    assert len(rows) == 1
    assert rows[0].canonical_name is None
    assert rows[0].name_raw == "LEFT HIP femoral neck T-Score"

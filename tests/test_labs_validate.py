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
# kind="score" (queue-ergonomics slice item 2): FRAX/T-score/Z-score have
# no unit/reference range by nature.
# ----------------------------------------------------------------


def test_score_row_with_no_unit_is_not_flagged() -> None:
    row = _lab(name="T-score", value=-1.2, ucum_unit=None, ref_low=None, ref_high=None)
    assert validate_row(row) == []


def test_score_row_with_a_real_unit_mismatch_is_flagged() -> None:
    # T-score/Z-score are unitless - any actually-printed unit is bogus.
    row = _lab(name="T-score", value=-1.2, ucum_unit="mg/dL", ref_low=None, ref_high=None)
    issues = validate_row(row)
    assert any(i.code is IssueCode.UNKNOWN_UNIT for i in issues)


def test_t_score_out_of_bounds_is_flagged() -> None:
    row = _lab(name="T-score", value=12.0, ucum_unit=None, ref_low=None, ref_high=None)
    issues = validate_row(row)
    assert any(i.code is IssueCode.OUT_OF_BOUNDS for i in issues)


def test_t_score_in_bounds_is_not_flagged() -> None:
    row = _lab(name="T-score", value=-2.4, ucum_unit=None, ref_low=None, ref_high=None)
    assert validate_row(row) == []


def test_frax_row_with_percent_unit_is_not_flagged() -> None:
    row = _lab(
        name="FRAX 10-year probability of hip fracture",
        value=12.0,
        ucum_unit="%",
        ref_low=None,
        ref_high=None,
    )
    assert validate_row(row) == []


def test_frax_row_with_no_unit_is_not_flagged() -> None:
    row = _lab(
        name="FRAX 10-year probability of major osteoporotic fracture",
        value=18.5,
        ucum_unit=None,
        ref_low=None,
        ref_high=None,
    )
    assert validate_row(row) == []


def test_frax_row_out_of_bounds_is_flagged() -> None:
    row = _lab(
        name="FRAX 10-year probability of hip fracture",
        value=150.0,
        ucum_unit="%",
        ref_low=None,
        ref_high=None,
    )
    issues = validate_row(row)
    assert any(i.code is IssueCode.OUT_OF_BOUNDS for i in issues)


# ----------------------------------------------------------------
# canonicalize's score-kind suffix-match rule (queue-ergonomics slice item
# 2): a site-prefixed DEXA row name resolves through the suffix, while
# every non-score analyte stays exact-alias-only.
# ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("LEFT HIP femoral neck T-Score", "T-score"),
        ("L1-L4 Z-Score", "Z-score"),
        ("T-Score", "T-score"),
        ("t score", "T-score"),
    ],
)
def test_canonicalize_suffix_matches_site_prefixed_score_names(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


def test_canonicalize_does_not_suffix_match_non_score_analytes() -> None:
    # "some prefix potassium" does not end in a whole-word alias match for
    # any exact analyte name, and potassium is not a score kind - suffix
    # matching must never kick in for it.
    assert canonicalize("some prefix potassium") is None


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


def test_trend_outlier_is_scoped_to_the_rows_own_specimen(db: LabsDb) -> None:
    """The real finding: a urinalysis GLUCOSE reading and a serum glucose
    reading canonicalize to the same `name`. A wildly different-looking
    urine reading must never be compared against (or trigger a trend
    outlier against) the serum series, and vice versa."""
    serum_rows = [
        _lab(
            name="glucose",
            value=v,
            ucum_unit="mg/dL",
            ref_low=70.0,
            ref_high=100.0,
            lab_date=date(2026, 1, i + 1),
            specimen="serum",
            extraction_status=ExtractionStatus.CONFIRMED,
        )
        for i, v in enumerate([90.0, 92.0, 88.0])
    ]
    db.insert_results(serum_rows)

    # Only 1 prior urine reading exists (below TREND_OUTLIER_MIN_PRIORS),
    # so even a wildly different-looking urine value must not trigger -
    # if it were (wrongly) compared against the serum priors instead, it
    # would trigger.
    db.insert_results(
        [
            _lab(
                name="glucose",
                value=500.0,
                ucum_unit="mg/dL",
                lab_date=date(2026, 2, 1),
                specimen="urine",
                extraction_status=ExtractionStatus.CONFIRMED,
            )
        ]
    )
    urine_row = _lab(
        name="glucose",
        value=520.0,
        ucum_unit="mg/dL",
        lab_date=date(2026, 6, 1),
        specimen="urine",
    )
    assert trend_outlier(db, urine_row) is None

    # A genuine decimal-shift error WITHIN the serum series still triggers.
    serum_outlier = _lab(
        name="glucose",
        value=900.0,
        ucum_unit="mg/dL",
        lab_date=date(2026, 6, 1),
        specimen="serum",
    )
    issue = trend_outlier(db, serum_outlier)
    assert issue is not None
    assert issue.code is IssueCode.TREND_OUTLIER


def test_trend_outlier_unknown_specimen_row_compares_against_unknown_priors_only(
    db: LabsDb,
) -> None:
    """A row whose own specimen is `"unknown"` (true of all pre-migration
    data) compares only against other `"unknown"`-specimen priors of the
    same canonical name - i.e. exactly today's behavior, since
    pre-migration data is entirely `"unknown"`."""
    _seed_priors(db, [4.0, 4.1, 4.2])  # specimen defaults to "unknown"
    db.insert_results(
        [
            _lab(
                value=4.05,
                lab_date=date(2026, 3, 1),
                specimen="serum",
                extraction_status=ExtractionStatus.CONFIRMED,
            )
        ]
    )
    new_row = _lab(value=41.0, lab_date=date(2026, 6, 1))  # specimen "unknown"
    issue = trend_outlier(db, new_row)
    assert issue is not None
    assert issue.code is IssueCode.TREND_OUTLIER


# ----------------------------------------------------------------
# lab-taxonomy layer: empty `allowed_units` means "no unit whitelist"
# ----------------------------------------------------------------


def test_empty_allowed_units_never_flags_unknown_unit() -> None:
    """A curation-only spec (`allowed_units=()`, no curated unit knowledge)
    must never manufacture a new UNKNOWN_UNIT issue - this is the
    critical semantics the lab-taxonomy layer's many panel-only additions
    depend on to avoid re-queuing thousands of already-accepted rows."""
    spec = ANALYTE_SPECS["Chloride"]
    assert spec.allowed_units == ()
    for unit in (None, "mmol/L", "some bogus unit", "%"):
        row = _lab(name="Chloride", value=100.0, ucum_unit=unit, ref_low=None, ref_high=None)
        issues = validate_row(row)
        assert not any(i.code is IssueCode.UNKNOWN_UNIT for i in issues), unit


def test_empty_allowed_units_still_enforced_for_score_kind() -> None:
    """`kind="score"`'s empty-`allowed_units` semantics are unchanged: a
    score is unitless BY NATURE, so any actually-printed unit is still
    flagged (unlike the new numeric-kind "no whitelist" meaning above)."""
    row = _lab(name="T-score", value=-1.0, ucum_unit="mg/dL", ref_low=None, ref_high=None)
    issues = validate_row(row)
    assert any(i.code is IssueCode.UNKNOWN_UNIT for i in issues)


def test_nonempty_allowed_units_still_enforced() -> None:
    """A curated whitelist (existing specs, unchanged) still rejects an
    unrecognized unit - only an EMPTY whitelist means "don't check"."""
    row = _lab(name="potassium", ucum_unit="not-a-real-unit")
    issues = validate_row(row)
    assert any(i.code is IssueCode.UNKNOWN_UNIT for i in issues)


# ----------------------------------------------------------------
# lab-taxonomy layer: explicit spelling-variant merges
# ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["% SATURATION", "TSAT", "transferrin saturation", "IRON SATURATION"],
)
def test_transferrin_saturation_variants_merge_onto_tsat(raw: str) -> None:
    assert canonicalize(raw) == "TSAT"


@pytest.mark.parametrize("raw", ["ACTH,PLASMA", "ACTH, Plasma", "ACTH"])
def test_acth_variants_merge(raw: str) -> None:
    assert canonicalize(raw) == "ACTH"


@pytest.mark.parametrize(
    "raw", ["ALKALINE PHOSPHATASE", "Alkaline Phosphatase", "Alkaline Phosphatase, S"]
)
def test_alkaline_phosphatase_variants_merge(raw: str) -> None:
    assert canonicalize(raw) == "Alkaline Phosphatase"


@pytest.mark.parametrize("raw", ["BILIRUBIN", "BILIRUBIN, TOTAL", "Bilirubin, Total"])
def test_bilirubin_total_variants_merge(raw: str) -> None:
    assert canonicalize(raw) == "Bilirubin, Total"


@pytest.mark.parametrize("raw", ["C-PEPTIDE, LC/MS/MS", "C-Peptide, Serum", "C-Peptide"])
def test_c_peptide_variants_merge(raw: str) -> None:
    assert canonicalize(raw) == "C-Peptide"


@pytest.mark.parametrize(
    "raw",
    [
        "ANTI-MULLERIAN HORMONE",
        "ANTI-MULLERIAN HORMONE (AMH), FEMALE",
        "Anti-Mullerian Hormone (AMH)",
    ],
)
def test_amh_variants_merge(raw: str) -> None:
    assert canonicalize(raw) == "AMH"


@pytest.mark.parametrize(
    "raw", ["ANA Direct", "ANA SCREEN, IFA", "ANA SCREEN, IMMUNOASSAY", "ANACHOICE SCREEN"]
)
def test_ana_screen_variants_merge_and_stay_separate_from_ana_titer(raw: str) -> None:
    assert canonicalize(raw) == "ANA Screen"
    assert canonicalize(raw) != "ANA titer"


@pytest.mark.parametrize(
    "raw", ["Babesia microti IgG", "BABESIA MICROTI AB (IGG)", "BABESIA MICROTI AB (IGG), SCREEN"]
)
def test_babesia_microti_igg_labcorp_vs_quest_spellings_merge(raw: str) -> None:
    assert canonicalize(raw) == "Babesia microti Antibody IgG"


@pytest.mark.parametrize("raw", ["CRP", "C-Reactive Protein", "C-Reactive Protein, Quant"])
def test_crp_variants_merge(raw: str) -> None:
    assert canonicalize(raw) == "CRP"


def test_hs_crp_is_not_merged_into_crp() -> None:
    """hs-CRP is a distinct, more-sensitive assay - never merged with
    ordinary CRP even though the names look similar."""
    assert canonicalize("HS CRP") == "hs-CRP"
    assert canonicalize("HS CRP") != "CRP"


# ----------------------------------------------------------------
# lab-taxonomy layer: generic specimen/method suffix-strip rule
# ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Chloride, Serum", "Chloride"),
        ("Alkaline Phosphatase, S", "Alkaline Phosphatase"),
        ("Copper, Serum or Plasma", "Copper"),
        ("17-OH-PROGESTERONE,LCMSMS", "17-Hydroxyprogesterone"),
    ],
)
def test_suffix_strip_rule_resolves_specimen_and_method_suffixes(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


def test_suffix_strip_rule_does_not_collapse_a_clinically_distinct_specimen() -> None:
    """RBC magnesium is a different assay from serum magnesium - the
    suffix-strip rule must never merge them (this is why "RBC" isn't in
    the curated suffix list)."""
    assert canonicalize("Magnesium, RBC") == "Magnesium, RBC"
    assert canonicalize("Magnesium, Plasma") == "Magnesium"
    assert canonicalize("Magnesium, RBC") != canonicalize("Magnesium, Plasma")


# ----------------------------------------------------------------
# lab-taxonomy layer: panel/derived_from metadata
# ----------------------------------------------------------------


def test_panel_field_is_set_for_curated_panel_members() -> None:
    assert ANALYTE_SPECS["WBC"].panel == "CBC"
    assert ANALYTE_SPECS["TSH"].panel == "Thyroid"
    assert ANALYTE_SPECS["TSAT"].panel == "Iron Studies"


def test_panel_field_defaults_to_none_for_unpanelled_analytes() -> None:
    assert ANALYTE_SPECS["Tryptase"].panel is None


def test_derived_from_recorded_for_tsat_and_ag_ratio() -> None:
    assert set(ANALYTE_SPECS["TSAT"].derived_from) == {"Iron", "TIBC"}
    assert set(ANALYTE_SPECS["A/G Ratio"].derived_from) == {"albumin", "Globulin"}


def test_labcorp_long_form_unit_spellings_are_accepted() -> None:
    """LabCorp prints 'Million/uL'/'Thousand/uL' (real-corpus finding); these
    are the same units as M/uL / K/uL and must not queue. Comparison is
    case-insensitive."""
    for name, unit, value in (
        ("RBC", "Million/uL", 5.04),
        ("RBC", "million/ul", 5.04),
        ("WBC", "Thousand/uL", 6.2),
        ("Platelets", "thousand/uL", 250.0),
    ):
        row = _lab(name=name, ucum_unit=unit, value=value)
        issues = validate_row(row)
        assert not any(i.code == IssueCode.UNKNOWN_UNIT for i in issues), (name, unit)

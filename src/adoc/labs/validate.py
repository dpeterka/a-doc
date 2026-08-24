"""Deterministic lab validation — NO LLM (CLAUDE.md: deterministic logic is plain code).

Three layers, per PLAN.md's ingestion loop ("deterministic validation:
per-analyte unit whitelist, physiologic bounds, trend-outlier check"):

1. `canonicalize` — alias-table name normalization for this patient's
   recurring analytes (a starter set of ~25; PLAN.md: "canonical-name
   mapping table ... never block ingestion on coding" — unknown analytes
   simply skip validation, they are not rejected).
2. `validate_row` — unit whitelist, physiologic plausibility bounds,
   flag/value-vs-reference-range consistency, titer format.
3. `trend_outlier` — flags a >40% jump vs. the patient's own recent median
   once >=3 priors exist (catches decimal-shift extraction errors, e.g. a
   potassium of 4.1 misread as 41).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from adoc.labs.models import LabFlag, LabResult

if TYPE_CHECKING:
    from adoc.labs.db import LabsDb

ValueKind = Literal["numeric", "titer", "qualitative"]

# Trend-outlier: relative deviation from the patient's own recent median that
# triggers a flag (PLAN.md: "decimal-error detection", e.g. K 4.1 -> 41).
TREND_OUTLIER_RATIO = 0.4
TREND_OUTLIER_MIN_PRIORS = 3
# A >=10x-class shift is the decimal-misread signature (4.1 -> 41). Below
# this, a cross-pass-agreed value is treated as a real physiological spike
# (this patient spikes frequently — confirmed against source PDFs) and the
# outlier becomes an annotation, not an auto-accept blocker.
DECIMAL_SIGNATURE_RATIO = 9.0


class IssueCode(StrEnum):
    UNKNOWN_UNIT = "unknown_unit"
    OUT_OF_BOUNDS = "out_of_bounds"
    FLAG_INCONSISTENT = "flag_inconsistent"
    TITER_FORMAT = "titer_format"
    TREND_OUTLIER = "trend_outlier"


@dataclass(frozen=True)
class ValidationIssue:
    code: IssueCode
    message: str


@dataclass(frozen=True)
class AnalyteSpec:
    """Deterministic validation spec for one canonical analyte."""

    canonical_name: str
    aliases: tuple[str, ...]
    kind: ValueKind
    allowed_units: tuple[str, ...] = field(default_factory=tuple)
    bounds: tuple[float, float] | None = None  # hard physiologic plausibility bounds


def _normalize(text: str) -> str:
    """Case/punctuation-insensitive normalization for alias lookup."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


_TITER_PATTERN = re.compile(r"^1\s*:\s*\d+$")

# ~25 analytes common to autoimmune workups: CBC, CMP, inflammation, thyroid,
# autoimmune serology, and iron/vitamin panels frequently ordered alongside
# them. Bounds are hard *plausibility* limits (beyond which a value is almost
# certainly an extraction error), not clinical reference ranges.
ANALYTE_SPECS: dict[str, AnalyteSpec] = {
    spec.canonical_name: spec
    for spec in (
        # --- CBC ---
        AnalyteSpec(
            "WBC",
            ("wbc", "white blood cell count", "white blood cells", "leukocytes"),
            "numeric",
            ("10*3/uL", "x10^3/uL", "K/uL", "Thousand/uL"),
            (0.1, 100.0),
        ),
        AnalyteSpec(
            "RBC",
            ("rbc", "red blood cell count", "red blood cells", "erythrocytes"),
            "numeric",
            ("10*6/uL", "x10^6/uL", "M/uL", "Million/uL"),
            (0.5, 10.0),
        ),
        AnalyteSpec(
            "hemoglobin",
            ("hemoglobin", "haemoglobin", "hgb", "hb"),
            "numeric",
            ("g/dL",),
            (2.0, 24.0),
        ),
        AnalyteSpec(
            "hematocrit",
            ("hematocrit", "haematocrit", "hct"),
            "numeric",
            ("%",),
            (5.0, 75.0),
        ),
        AnalyteSpec(
            "platelets",
            ("platelets", "platelet count", "plt"),
            "numeric",
            ("10*3/uL", "x10^3/uL", "K/uL", "Thousand/uL"),
            (1.0, 2000.0),
        ),
        # --- CMP ---
        AnalyteSpec(
            "sodium",
            ("sodium", "na", "na+"),
            "numeric",
            ("mmol/L",),
            (100.0, 180.0),
        ),
        AnalyteSpec(
            "potassium",
            ("potassium", "k", "k+"),
            "numeric",
            ("mmol/L",),
            (1.0, 10.0),
        ),
        AnalyteSpec(
            "creatinine",
            ("creatinine", "creat"),
            "numeric",
            ("mg/dL",),
            (0.1, 20.0),
        ),
        AnalyteSpec(
            "ALT",
            ("alt", "alanine aminotransferase", "sgpt"),
            "numeric",
            ("U/L",),
            (1.0, 2000.0),
        ),
        AnalyteSpec(
            "AST",
            ("ast", "aspartate aminotransferase", "sgot"),
            "numeric",
            ("U/L",),
            (1.0, 2000.0),
        ),
        AnalyteSpec(
            "glucose",
            ("glucose", "blood glucose", "fasting glucose"),
            "numeric",
            ("mg/dL",),
            (10.0, 1000.0),
        ),
        AnalyteSpec(
            "calcium",
            ("calcium", "ca", "total calcium"),
            "numeric",
            ("mg/dL",),
            (4.0, 16.0),
        ),
        AnalyteSpec(
            "albumin",
            ("albumin", "alb"),
            "numeric",
            ("g/dL",),
            (1.0, 7.0),
        ),
        # --- Inflammation ---
        AnalyteSpec(
            "CRP",
            ("crp", "c-reactive protein", "c reactive protein"),
            "numeric",
            ("mg/L",),
            (0.0, 500.0),
        ),
        AnalyteSpec(
            "ESR",
            ("esr", "erythrocyte sedimentation rate", "sed rate"),
            "numeric",
            ("mm/hr", "mm/h"),
            (0.0, 150.0),
        ),
        # --- Thyroid ---
        AnalyteSpec(
            "TSH",
            ("tsh", "thyroid stimulating hormone", "thyrotropin"),
            "numeric",
            ("mIU/L", "uIU/mL"),
            (0.001, 100.0),
        ),
        AnalyteSpec(
            "free T4",
            ("free t4", "ft4", "free thyroxine"),
            "numeric",
            ("ng/dL",),
            (0.1, 10.0),
        ),
        # --- Autoimmune serology ---
        AnalyteSpec(
            "ANA titer",
            ("ana", "ana titer", "antinuclear antibody", "antinuclear antibodies"),
            "titer",
        ),
        AnalyteSpec(
            "anti-dsDNA",
            ("anti-dsdna", "anti dsdna", "dsdna", "double stranded dna antibody"),
            "numeric",
            ("IU/mL",),
            (0.0, 1000.0),
        ),
        AnalyteSpec(
            "RF",
            ("rf", "rheumatoid factor"),
            "numeric",
            ("IU/mL",),
            (0.0, 1000.0),
        ),
        AnalyteSpec(
            "anti-CCP",
            ("anti-ccp", "anti ccp", "ccp antibody", "cyclic citrullinated peptide antibody"),
            "numeric",
            ("U/mL",),
            (0.0, 1000.0),
        ),
        AnalyteSpec(
            "C3",
            ("c3", "complement c3"),
            "numeric",
            ("mg/dL",),
            (5.0, 300.0),
        ),
        AnalyteSpec(
            "C4",
            ("c4", "complement c4"),
            "numeric",
            ("mg/dL",),
            (1.0, 100.0),
        ),
        # --- Vitamin / iron ---
        AnalyteSpec(
            "vitamin D",
            ("vitamin d", "25-hydroxyvitamin d", "25-oh vitamin d", "vit d"),
            "numeric",
            ("ng/mL",),
            (1.0, 200.0),
        ),
        AnalyteSpec(
            "ferritin",
            ("ferritin",),
            "numeric",
            ("ng/mL",),
            (0.5, 5000.0),
        ),
        AnalyteSpec(
            "TSAT",
            ("tsat", "transferrin saturation", "iron saturation"),
            "numeric",
            ("%",),
            (1.0, 100.0),
        ),
    )
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _spec in ANALYTE_SPECS.values():
    _ALIAS_TO_CANONICAL[_normalize(_spec.canonical_name)] = _spec.canonical_name
    for _alias in _spec.aliases:
        _ALIAS_TO_CANONICAL[_normalize(_alias)] = _spec.canonical_name


def canonicalize(name_raw: str) -> str | None:
    """Map a raw analyte name to its canonical name, or `None` if unknown.

    Lookup is case/punctuation-insensitive (`_normalize`). Unknown analytes
    return `None` rather than raising — per PLAN.md, ingestion is never
    blocked on coding an analyte we don't yet recognize.
    """
    return _ALIAS_TO_CANONICAL.get(_normalize(name_raw))


def _flag_issue(row: LabResult) -> ValidationIssue | None:
    if row.value is None or row.flag is None:
        return None
    if row.flag in (LabFlag.HIGH, LabFlag.CRITICAL_HIGH):
        if row.ref_high is not None and row.value <= row.ref_high:
            return ValidationIssue(
                IssueCode.FLAG_INCONSISTENT,
                f"flag {row.flag.value} but value {row.value} <= ref_high {row.ref_high}",
            )
    elif (
        row.flag in (LabFlag.LOW, LabFlag.CRITICAL_LOW)
        and row.ref_low is not None
        and row.value >= row.ref_low
    ):
        return ValidationIssue(
            IssueCode.FLAG_INCONSISTENT,
            f"flag {row.flag.value} but value {row.value} >= ref_low {row.ref_low}",
        )
    return None


def validate_row(row: LabResult) -> list[ValidationIssue]:
    """Deterministic checks against `ANALYTE_SPECS`: unit, bounds, flag, titer.

    Analytes not in `ANALYTE_SPECS` (canonicalize returns `None`, or `row.name`
    otherwise doesn't match a known spec) produce no issues — coding gaps
    never block ingestion.
    """
    issues: list[ValidationIssue] = []
    canonical = canonicalize(row.name) or (row.name if row.name in ANALYTE_SPECS else None)
    spec = ANALYTE_SPECS.get(canonical) if canonical else None
    if spec is None:
        return issues

    if spec.kind == "numeric":
        allowed = {u.lower() for u in spec.allowed_units}
        if row.ucum_unit is None or row.ucum_unit.lower() not in allowed:
            issues.append(
                ValidationIssue(
                    IssueCode.UNKNOWN_UNIT,
                    f"{spec.canonical_name}: unit {row.ucum_unit!r} not in "
                    f"whitelist {spec.allowed_units}",
                )
            )
        if row.value is not None and spec.bounds is not None:
            low, high = spec.bounds
            if not (low <= row.value <= high):
                issues.append(
                    ValidationIssue(
                        IssueCode.OUT_OF_BOUNDS,
                        f"{spec.canonical_name}: value {row.value} outside plausible "
                        f"bounds [{low}, {high}] - likely extraction error",
                    )
                )
        flag_issue = _flag_issue(row)
        if flag_issue is not None:
            issues.append(flag_issue)

    elif spec.kind == "titer":
        if row.value_text is not None and not _TITER_PATTERN.match(row.value_text.strip()):
            issues.append(
                ValidationIssue(
                    IssueCode.TITER_FORMAT,
                    f"{spec.canonical_name}: value_text {row.value_text!r} does not "
                    "match titer format (e.g. '1:80')",
                )
            )

    return issues


def trend_deviation(db: LabsDb, row: LabResult) -> float | None:
    """Relative deviation of `row.value` from the median of all EARLIER
    readings (by collection date) of the same canonical analyte AND THE
    SAME SPECIMEN as `row`; None when fewer than TREND_OUTLIER_MIN_PRIORS
    priors exist or the value is non-numeric.

    Scoping priors to `row.specimen` keeps a urinalysis GLUCOSE
    "NEGATIVE" reading from ever being compared against a serum glucose
    trend (or vice versa) even though both canonicalize to the same
    `name` — see `labs/models.py`'s `Specimen` docstring. A row whose own
    specimen is `"unknown"` (true of every row before this dimension
    existed, and of any newly-extracted row whose report didn't state
    one) compares against other `"unknown"`-specimen priors of the same
    canonical name — i.e. exactly today's behavior, unchanged, since
    pre-migration data is entirely `"unknown"`.
    """
    if row.value is None:
        return None
    canonical = canonicalize(row.name) or row.name
    priors = [
        r.value
        for r in db.series(canonical, row.specimen)
        if r.value is not None and r.id != row.id and r.date < row.date
    ]
    if len(priors) < TREND_OUTLIER_MIN_PRIORS:
        return None
    median = statistics.median(priors)
    if median == 0:
        return None
    return abs(row.value - median) / abs(median)


def trend_outlier(db: LabsDb, row: LabResult) -> ValidationIssue | None:
    """Flag a >40% jump vs. the median of the patient's earlier readings
    (>=3 priors) OF THE SAME SPECIMEN. Catches decimal-shift extraction
    errors (e.g. potassium 4.1 misread as 41) without any clinical
    knowledge — pure statistics on this patient's own history for the same
    canonical analyte and specimen (see `trend_deviation`'s docstring).
    """
    ratio = trend_deviation(db, row)
    if ratio is not None and ratio > TREND_OUTLIER_RATIO:
        canonical = canonicalize(row.name) or row.name
        return ValidationIssue(
            IssueCode.TREND_OUTLIER,
            f"{canonical}: value {row.value} is {ratio:.0%} away from the median of "
            f"earlier readings - possible decimal error",
        )
    return None

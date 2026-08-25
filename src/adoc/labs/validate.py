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

## Matching vs. renaming (feature/taxonomy-distinctions)

A real-data sweep found `canonicalize`'s permissive suffix-strip and
score-suffix rules had been used to *rename* stored rows, silently
discarding a site/side ("LEFT HIP" vs "RIGHT HIP" hip BMD), specimen
("Manganese, Plasma" vs "..., RBC"), or stratification ("eGFR If Africn
Am" vs "...NonAfricn Am") those rules were never meant to erase - two
CLINICALLY DISTINCT measurements ended up sharing one trend series. The
fix splits "how do I group/validate/trend-scope this row" from "may I
overwrite this row's stored name" into two functions:

- `canonicalize(name) -> spec-or-None` stays fully permissive (exact
  alias; the generic suffix-strip retry; the score-suffix rule) - it
  drives read-time panel grouping (`labs.panels`), `validate_row`'s spec
  lookup, and `trend_deviation`'s prior-series lookup. Widening what
  reads as "the same family" here is safe: it never touches a stored row.
- `canonical_rename_target(name_raw, name) -> str-or-None` is used ONLY
  to decide whether `adoc labs-recanonicalize` (`labs.recanonicalize`) may
  physically overwrite a row's stored `name`. It considers an EXACT alias
  match only (no suffix-strip retry, no score-suffix rule) - a human
  explicitly reviewed and listed that alias as denoting the identical
  measurement (e.g. "ACTH,PLASMA" / "ACTH, Plasma"). Every other match
  returns `None`, meaning "leave the stored name exactly as it is" - the
  row still gets full `canonicalize` benefits (panel, validation, trend
  scoping) at read time, just under its own distinct stored name.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from adoc.labs.models import LabFlag, LabResult

if TYPE_CHECKING:
    from adoc.labs.db import LabsDb

ValueKind = Literal["numeric", "titer", "qualitative", "score"]
"""`"score"` (queue-ergonomics slice item 2) is for analytes that are a
computed score by their nature - a DEXA T-score/Z-score or a FRAX
fracture-probability percentage - and so legitimately carry no unit (T/Z)
or only "%" (FRAX) and no clinical reference range at all. `validate_row`
treats it like `"numeric"` (bounds + flag checks) but never complains
about a missing unit - see `AnalyteSpec.allowed_units`'s docstring below."""

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
    """Deterministic validation spec for one canonical analyte.

    `panel`/`derived_from` (lab-taxonomy layer, feature/lab-taxonomy) are
    display/grouping metadata only - neither is consulted by `validate_row`.
    `panel` is the curated clinical grouping label a spec belongs to (e.g.
    `"CBC"`, `"Iron Studies"`); `None` (the default) means the analyte isn't
    part of any curated panel - `labs.panels`'s grouping helpers call that
    bucket `"Other"`. `derived_from` names the OTHER canonical analyte(s) a
    calculated value is computed from (e.g. TSAT from Iron + TIBC, A/G Ratio
    from albumin + Globulin) - purely informational, for a UI note like
    "calculated from Iron and TIBC"; nothing here recomputes or validates
    the arithmetic.
    """

    canonical_name: str
    aliases: tuple[str, ...]
    kind: ValueKind
    # For kind="score": the units a *printed* score is allowed to carry
    # besides no unit at all (always fine for a score - see `validate_row`).
    # T-score/Z-score: empty (unitless by nature). FRAX: `("%",)`.
    #
    # For kind="numeric": an EMPTY tuple means "no unit whitelist" - the
    # unit is simply never checked (see `validate_row`'s numeric branch).
    # This matters for the lab-taxonomy layer's many curation-only specs
    # (panel members added purely so their analyte canonicalizes/groups
    # correctly, with no curated real-world unit knowledge behind them):
    # `allowed_units=()` on one of THOSE must never manufacture a new
    # `UNKNOWN_UNIT` issue against rows that were already accepted before
    # the spec existed. A non-empty `allowed_units` keeps behaving exactly
    # as before (unit must match one of the whitelisted spellings/families).
    allowed_units: tuple[str, ...] = field(default_factory=tuple)
    bounds: tuple[float, float] | None = None  # hard physiologic plausibility bounds
    panel: str | None = None
    derived_from: tuple[str, ...] = field(default_factory=tuple)


def _normalize(text: str) -> str:
    """Case/punctuation-insensitive normalization for alias lookup."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


_TITER_PATTERN = re.compile(r"^1\s*:\s*\d+$")

# Unit spelling families (feature/semantic-compare): real-corpus reconcile
# false-positives showed extraction passes printing the SAME unit in
# different, equally-valid spellings (e.g. "Million/uL" vs "M/uL" vs
# "x10^6/uL"). Each inner tuple is one family of interchangeable spellings,
# pre-normalized (casefold, no internal whitespace - see `_normalize_unit_text`).
# Deliberately does NOT merge families a clinician would NOT treat as
# interchangeable even though they look similar: "IU/mL" and "U/mL" stay
# separate (different assay standardization), and "units"/"U" (a bare,
# unit-less flag on some printed ranges) stays its own family too.
UNIT_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("10*6/ul", "x10^6/ul", "m/ul", "million/ul", "x10e6/ul"),
    ("10*3/ul", "x10^3/ul", "k/ul", "thousand/ul", "x10e3/ul"),
    ("mg/dl",),
    ("g/dl",),
    ("mmol/l",),
    ("mcg/dl", "ug/dl", "µg/dl"),
    ("miu/l", "uiu/ml"),  # numerically equivalent for TSH reporting
    ("ng/ml",),
    ("pg/ml",),
    ("%",),
    ("mm/hr", "mm/h"),
    ("iu/ml",),  # kept separate from U/mL - not assumed equal
    ("u/ml",),
    ("units", "u"),
)


def _normalize_unit_text(text: str) -> str:
    """Case/whitespace-insensitive key for `UNIT_SYNONYMS`/`canonical_unit`
    lookup. Internal whitespace is removed entirely (not just collapsed) -
    unit spellings never carry semantically meaningful spaces."""
    return re.sub(r"\s+", "", text.strip().casefold())


_UNIT_SYNONYM_INDEX: dict[str, str] = {
    _normalize_unit_text(member): family[0] for family in UNIT_SYNONYMS for member in family
}


def canonical_unit(raw: str | None) -> str | None:
    """The canonical family token for `raw` (module docstring), or `None`
    if `raw` doesn't match any known spelling family - never a guess."""
    if raw is None:
        return None
    normalized = _normalize_unit_text(raw)
    if not normalized:
        return None
    return _UNIT_SYNONYM_INDEX.get(normalized)


def _unit_in_whitelist(unit: str | None, allowed: tuple[str, ...]) -> bool:
    """`unit` matches one of `allowed`'s printed spellings, or shares its
    `canonical_unit` family (feature/semantic-compare: `validate_row`'s
    whitelist accepts any synonym of a whitelisted unit, not just the exact
    spellings enumerated in `AnalyteSpec.allowed_units`)."""
    if unit is None:
        return False
    unit_key = canonical_unit(unit) or _normalize_unit_text(unit)
    for allowed_unit in allowed:
        allowed_key = canonical_unit(allowed_unit) or _normalize_unit_text(allowed_unit)
        if unit_key == allowed_key:
            return True
    return False


# Hand-curated analyte specs: CBC, CMP, inflammation, thyroid, autoimmune
# serology, iron/vitamin/hormone panels, and the other lab-taxonomy-layer
# panels curated from this patient's real analyte vocabulary (feature/
# lab-taxonomy). Bounds are hard *plausibility* limits (beyond which a value
# is almost certainly an extraction error), not clinical reference ranges -
# most lab-taxonomy-only additions carry NO bounds and NO unit whitelist
# (`allowed_units=()`, which `validate_row` now treats as "don't check the
# unit at all" - see `AnalyteSpec`'s docstring) since they exist purely so
# their analyte canonicalizes/groups correctly, not because their unit
# conventions or plausible ranges have been curated. `_MOLD_IGG_SPECS`/
# `_ALLERGEN_IGE_SPECS`/`_LYME_WB_SPECS` (generated, below) are merged in
# alongside these to build the final `ANALYTE_SPECS`.
_HAND_CURATED_SPECS: tuple[AnalyteSpec, ...] = (
    # --- CBC ---
    AnalyteSpec(
        "WBC",
        ("wbc", "white blood cell count", "white blood cells", "leukocytes"),
        "numeric",
        ("10*3/uL", "x10^3/uL", "K/uL", "Thousand/uL"),
        (0.1, 100.0),
        panel="CBC",
    ),
    AnalyteSpec(
        "RBC",
        ("rbc", "red blood cell count", "red blood cells", "erythrocytes"),
        "numeric",
        ("10*6/uL", "x10^6/uL", "M/uL", "Million/uL"),
        (0.5, 10.0),
        panel="CBC",
    ),
    AnalyteSpec(
        "hemoglobin",
        ("hemoglobin", "haemoglobin", "hgb", "hb"),
        "numeric",
        ("g/dL",),
        (2.0, 24.0),
        panel="CBC",
    ),
    AnalyteSpec(
        "hematocrit",
        ("hematocrit", "haematocrit", "hct"),
        "numeric",
        ("%",),
        (5.0, 75.0),
        panel="CBC",
    ),
    AnalyteSpec(
        "platelets",
        ("platelets", "platelet count", "plt"),
        "numeric",
        ("10*3/uL", "x10^3/uL", "K/uL", "Thousand/uL"),
        (1.0, 2000.0),
        panel="CBC",
    ),
    # --- CBC differential (lab-taxonomy layer): percentage and
    # absolute-count forms are DIFFERENT measurements (different units,
    # different clinical use) and so stay separate canonicals, never
    # merged into one - only genuine same-measurement spelling variants
    # (e.g. "Baso (Absolute)" vs "ABSOLUTE BASOPHILS") are merged via
    # aliases on the ONE canonical for that measurement.
    AnalyteSpec("MCH", ("mch",), "numeric", (), None, panel="CBC"),
    AnalyteSpec("MCHC", ("mchc",), "numeric", (), None, panel="CBC"),
    AnalyteSpec("MCV", ("mcv",), "numeric", (), None, panel="CBC"),
    AnalyteSpec("RDW", ("rdw",), "numeric", (), None, panel="CBC"),
    AnalyteSpec("MPV", ("mpv",), "numeric", (), None, panel="CBC"),
    AnalyteSpec("Neutrophils", ("neutrophils",), "numeric", (), None, panel="CBC"),
    AnalyteSpec(
        "Neutrophils, Absolute",
        ("absolute neutrophils", "neutrophils (absolute)"),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    AnalyteSpec("Lymphocytes", ("lymphocytes", "lymphs"), "numeric", (), None, panel="CBC"),
    AnalyteSpec(
        "Lymphocytes, Absolute",
        ("absolute lymphocytes", "lymphs (absolute)"),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    AnalyteSpec("Monocytes", ("monocytes",), "numeric", (), None, panel="CBC"),
    AnalyteSpec(
        "Monocytes, Absolute",
        ("absolute monocytes", "monocytes(absolute)"),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    AnalyteSpec("Eosinophils", ("eosinophils", "eos"), "numeric", (), None, panel="CBC"),
    AnalyteSpec(
        "Eosinophils, Absolute",
        ("absolute eosinophils", "eos (absolute)"),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    # Merge family named explicitly in the lab-taxonomy plan: "Basos" /
    # "Baso (Absolute)" / "ABSOLUTE BASOPHILS" - the last two are the
    # SAME (absolute-count) measurement under two spellings and merge
    # onto one canonical; "Basos" (percent) is a genuinely different
    # measurement and gets its own.
    AnalyteSpec("Basophils", ("basophils", "basos"), "numeric", (), None, panel="CBC"),
    AnalyteSpec(
        "Basophils, Absolute",
        ("absolute basophils", "baso (absolute)"),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    AnalyteSpec(
        "Immature Granulocytes",
        ("immature granulocytes",),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    AnalyteSpec(
        "Immature Granulocytes, Absolute",
        ("immature grans (abs)",),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    AnalyteSpec(
        "Reticulocyte Count",
        ("reticulocyte count, automated",),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    AnalyteSpec(
        "Reticulocyte Count, Absolute",
        ("reticulocyte, absolute",),
        "numeric",
        (),
        None,
        panel="CBC",
    ),
    # --- CMP ---
    AnalyteSpec(
        "sodium",
        ("sodium", "na", "na+"),
        "numeric",
        ("mmol/L",),
        (100.0, 180.0),
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "potassium",
        ("potassium", "k", "k+"),
        "numeric",
        ("mmol/L",),
        (1.0, 10.0),
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "creatinine",
        ("creatinine", "creat"),
        "numeric",
        ("mg/dL",),
        (0.1, 20.0),
        panel="Comprehensive Metabolic Panel",
    ),
    # Random urine creatinine (used to normalize a spot urine protein/
    # albumin ratio) is a different specimen, on a wildly different
    # numeric scale, from serum creatinine above - a real collision-
    # family finding (feature/taxonomy-distinctions) showed both merging
    # onto one bare "creatinine" canonical via a shared alias list, which
    # would have applied serum creatinine's plausibility bounds to urine
    # values and silently combined two different trend series.
    AnalyteSpec(
        "Creatinine, Urine",
        ("creatinine, random urine",),
        "numeric",
        (),
        None,
        panel="Urinalysis",
    ),
    AnalyteSpec(
        "ALT",
        ("alt", "alanine aminotransferase", "sgpt", "alt (sgpt)"),
        "numeric",
        ("U/L",),
        (1.0, 2000.0),
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "AST",
        ("ast", "aspartate aminotransferase", "sgot", "ast (sgot)"),
        "numeric",
        ("U/L",),
        (1.0, 2000.0),
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "glucose",
        ("glucose", "blood glucose", "fasting glucose"),
        "numeric",
        ("mg/dL",),
        (10.0, 1000.0),
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "calcium",
        ("calcium", "ca", "total calcium"),
        "numeric",
        ("mg/dL",),
        (4.0, 16.0),
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "albumin",
        ("albumin", "alb"),
        "numeric",
        ("g/dL",),
        (1.0, 7.0),
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "Calcium, Ionized",
        ("calcium, ionized, serum",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "Chloride",
        ("chloride",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "Carbon Dioxide",
        ("carbon dioxide", "carbon dioxide, total"),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "BUN",
        ("bun", "urea nitrogen (bun)"),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "BUN/Creatinine Ratio",
        ("bun/creatinine ratio",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
        derived_from=("BUN", "creatinine"),
    ),
    AnalyteSpec(
        "Globulin",
        ("globulin", "globulin, total"),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    # Explicit merge family: "A/G Ratio" / "ALBUMIN/GLOBULIN RATIO".
    AnalyteSpec(
        "A/G Ratio",
        ("albumin/globulin ratio",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
        derived_from=("albumin", "Globulin"),
    ),
    AnalyteSpec(
        "Total Protein",
        ("protein, total", "protein, total, serum"),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    # Explicit merge family: "ALKALINE PHOSPHATASE" / "Alkaline
    # Phosphatase" / "Alkaline Phosphatase, S" (the last strips its
    # ", S" specimen-abbreviation suffix - see `_strip_known_suffix`).
    AnalyteSpec(
        "Alkaline Phosphatase",
        ("alkaline phosphatase",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    # Explicit merge family: "BILIRUBIN" / "BILIRUBIN, TOTAL" /
    # "Bilirubin, Total".
    AnalyteSpec(
        "Bilirubin, Total",
        ("bilirubin", "bilirubin, total"),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "Bilirubin, Direct",
        ("bilirubin, direct",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "Phosphorus",
        ("phosphorus",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "eGFR",
        ("egfr",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    # Race-stratified eGFR variants report DIFFERENT computed values from
    # the same creatinine/demographic inputs (different equation
    # coefficients) - a real collision-family finding (feature/taxonomy-
    # distinctions) showed both LabCorp abbreviated spellings merging onto
    # plain "eGFR" via one shared alias list, silently overwriting one
    # stratified value's trend series with the other's. Each
    # stratification gets its own canonical and its own exact alias for
    # the LabCorp abbreviated spelling.
    AnalyteSpec(
        "eGFR (African American)",
        ("egfr if africn am",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "eGFR (Non-African American)",
        ("egfr if nonafricn am",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    AnalyteSpec(
        "Uric Acid",
        ("uric acid",),
        "numeric",
        (),
        None,
        panel="Comprehensive Metabolic Panel",
    ),
    # --- Inflammation ---
    AnalyteSpec(
        "CRP",
        ("crp", "c-reactive protein", "c reactive protein", "c-reactive protein, quant"),
        "numeric",
        ("mg/L",),
        (0.0, 500.0),
        panel="Inflammation",
    ),
    AnalyteSpec(
        "ESR",
        (
            "esr",
            "erythrocyte sedimentation rate",
            "sed rate",
            "sed rate by modified westergren",
            "sedimentation rate-westergren",
        ),
        "numeric",
        ("mm/hr", "mm/h"),
        (0.0, 150.0),
        panel="Inflammation",
    ),
    # hs-CRP is a distinct, more-sensitive assay (different reference
    # range/clinical use, cardiac-risk stratification) from ordinary
    # CRP above - deliberately its own canonical, not merged.
    AnalyteSpec("hs-CRP", ("hs crp",), "numeric", (), None, panel="Inflammation"),
    # --- Thyroid ---
    AnalyteSpec(
        "TSH",
        ("tsh", "thyroid stimulating hormone", "thyrotropin"),
        "numeric",
        ("mIU/L", "uIU/mL"),
        (0.001, 100.0),
        panel="Thyroid",
    ),
    AnalyteSpec(
        "free T4",
        ("free t4", "ft4", "free thyroxine", "t4, free", "t4,free(direct)"),
        "numeric",
        ("ng/dL",),
        (0.1, 10.0),
        panel="Thyroid",
    ),
    AnalyteSpec(
        "Free T3",
        ("t3, free", "triiodothyronine (t3), free"),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    AnalyteSpec("T3 Uptake", ("t3 uptake",), "numeric", (), None, panel="Thyroid"),
    AnalyteSpec("Thyroxine (T4)", ("thyroxine (t4)",), "numeric", (), None, panel="Thyroid"),
    AnalyteSpec(
        "Triiodothyronine (T3)",
        ("triiodothyronine (t3)",),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    AnalyteSpec(
        "Free Thyroxine Index",
        ("free thyroxine index",),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    AnalyteSpec(
        "Reverse T3",
        ("t3 reverse, lc/ms/ms", "reverse t3, serum"),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    AnalyteSpec(
        "TPO Antibody",
        (
            "thyroid peroxidase antibodies",
            "thyroid peroxidase (tpo) ab",
        ),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    AnalyteSpec(
        "Thyroglobulin Antibody",
        ("thyroglobulin antibodies", "thyroglobulin antibody"),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    AnalyteSpec(
        "Thyroid Stimulating Immunoglobulin",
        ("thyroid stim immunoglobulin",),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    AnalyteSpec(
        "Thyrotropin Receptor Antibody",
        ("thyrotropin receptor ab, serum",),
        "numeric",
        (),
        None,
        panel="Thyroid",
    ),
    # --- Autoimmune serology ---
    AnalyteSpec(
        "ANA titer",
        ("ana", "ana titer", "antinuclear antibody", "antinuclear antibodies"),
        "titer",
        panel="Autoimmune Serology",
    ),
    # ANA "screen" results are typically pos/neg or an index, not a
    # titer - kept as its own qualitative canonical rather than merged
    # into "ANA titer" (which would wrongly run titer-format checking
    # against a non-titer value). Explicit merge family: "ANA Direct" /
    # "ANA SCREEN, IFA" / "ANA SCREEN, IMMUNOASSAY" / "ANACHOICE SCREEN".
    AnalyteSpec(
        "ANA Screen",
        (
            "ana direct",
            "ana screen, ifa",
            "ana screen, immunoassay",
            "anachoice screen",
        ),
        "qualitative",
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "anti-dsDNA",
        (
            "anti-dsdna",
            "anti dsdna",
            "dsdna",
            "double stranded dna antibody",
            "dna (ds) antibody",
        ),
        "numeric",
        ("IU/mL",),
        (0.0, 1000.0),
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "RF",
        ("rf", "rheumatoid factor"),
        "numeric",
        ("IU/mL",),
        (0.0, 1000.0),
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Rheumatoid Factor IgA",
        ("rheumatoid factor levels iga",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "anti-CCP",
        (
            "anti-ccp",
            "anti ccp",
            "ccp antibody",
            "cyclic citrullinated peptide antibody",
            "cyclic citrullinated peptide (ccp) ab (igg)",
        ),
        "numeric",
        ("U/mL",),
        (0.0, 1000.0),
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "C3",
        ("c3", "complement c3"),
        "numeric",
        ("mg/dL",),
        (5.0, 300.0),
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "C4",
        ("c4", "complement c4"),
        "numeric",
        ("mg/dL",),
        (1.0, 100.0),
        panel="Autoimmune Serology",
    ),
    # Complement C4c is an activation FRAGMENT of C4, a distinct assay
    # from native C4 above - deliberately not merged.
    AnalyteSpec(
        "Complement C4c",
        ("complement component c4c",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Cardiolipin Antibody IgA",
        ("cardiolipin ab (iga)",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Cardiolipin Antibody IgG",
        ("cardiolipin ab (igg)",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Cardiolipin Antibody IgM",
        ("cardiolipin ab (igm)",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec("ANCA Screen", ("anca screen",), "qualitative", panel="Autoimmune Serology"),
    AnalyteSpec(
        "Myeloperoxidase (MPO) Antibody",
        ("myeloperoxidase antibody",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Proteinase-3 (PR3) Antibody",
        ("proteinase-3 antibody",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Smith (Sm) Antibody",
        ("sm antibody",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Sm/RNP Antibody",
        ("sm/rnp antibody",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "SS-A (Ro) Antibody",
        ("sjogren's antibody (ss-a)",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "SS-B (La) Antibody",
        ("sjogren's antibody (ss-b)",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Scl-70 Antibody",
        ("scl-70 antibody",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Centromere B Antibody",
        ("centromere b antibody",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "RNA Polymerase III Antibody",
        ("rna polymerase iii ab",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "14-3-3 Eta Protein",
        ("14.3.3 eta, rheum. arthritis",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "21-Hydroxylase Antibody",
        ("21-hydroxylase antibodies",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Adrenal Antibody",
        ("adrenal ab", "adrenal ab, titer", "antiadrenal antibodies, quant"),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Anti-Ovary Antibody",
        ("anti-ovary ab", "anti-ovary ab titer"),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "HLA-B27 Antigen",
        ("hla-b27 antigen",),
        "qualitative",
        panel="Autoimmune Serology",
    ),
    AnalyteSpec(
        "Tissue Transglutaminase (tTG) IgA Antibody",
        ("tissue transglutam ab iga",),
        "numeric",
        (),
        None,
        panel="Autoimmune Serology",
    ),
    # --- Vitamin / iron ---
    AnalyteSpec(
        "vitamin D",
        (
            "vitamin d",
            "25-hydroxyvitamin d",
            "25-oh vitamin d",
            "vit d",
            "vitamin d, 25-oh, total",
            "vitamin d,25-oh,total,ia",
            "vitamin d, 25-hydroxy",
        ),
        "numeric",
        ("ng/mL",),
        (1.0, 200.0),
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Vitamin D, 25-OH D2",
        ("vitamin d, 25-oh, d2",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Vitamin D, 25-OH D3",
        ("vitamin d, 25-oh, d3",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Vitamin D, 1,25-Dihydroxy (Calcitriol)",
        ("calcitriol(1,25 di-oh vit d)",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Vitamin B12",
        ("vitamin b12",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Folate",
        ("folate, serum", "folate (folic acid), serum", "folate (folic acid)"),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Biotin",
        ("biotin (vitamin b7)",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec("Zinc", ("zinc",), "numeric", (), None, panel="Vitamins & Nutrition"),
    AnalyteSpec("Magnesium", ("magnesium",), "numeric", (), None, panel="Vitamins & Nutrition"),
    # RBC (intracellular) magnesium is a distinct assay from serum
    # magnesium above, with its own reference range - deliberately kept
    # as its own canonical, never merged with plain "Magnesium" (also
    # why "RBC" is excluded from the generic suffix-strip list).
    AnalyteSpec(
        "Magnesium, RBC",
        ("magnesium, rbc",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec("Vitamin A", ("vitamin a",), "numeric", (), None, panel="Vitamins & Nutrition"),
    AnalyteSpec(
        "Vitamin B1 (Thiamine)",
        ("vitamin b1 (thiamine), blood, lc/ms/ms", "vit. b1, whole blood"),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec("Vitamin B6", ("vitamin b6",), "numeric", (), None, panel="Vitamins & Nutrition"),
    AnalyteSpec(
        "Vitamin E, Alpha Tocopherol",
        ("vitamin e, alpha tocopherol", "vitamin e(alpha tocopherol)"),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Vitamin E, Beta/Gamma Tocopherol",
        ("vitamin e, beta gamma tocopherol",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Vitamin E, Gamma Tocopherol",
        ("vitamin e(gamma tocopherol)",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Beta Carotene",
        ("carotene, beta",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    # Blood and serum/plasma copper are distinct specimen types with
    # different reference ranges (same shape as "Manganese, Plasma"/",
    # RBC" below) - a real-collision-shaped bug: merging them onto one
    # bare "Copper" canonical (via one shared alias list) would silently
    # combine two different trend series.
    AnalyteSpec(
        "Copper, Blood",
        ("copper, blood",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Copper, Serum or Plasma",
        ("copper, serum or plasma",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Selenium, Blood",
        ("selenium, blood",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Selenium, Serum/Plasma",
        ("selenium, serum/plasma",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Iodine",
        ("iodine, serum/plasma", "iodine, serum or plasma"),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    # Plasma and RBC (intracellular) manganese are distinct assays with
    # different reference ranges (same shape as "Magnesium, RBC" above) -
    # a real collision-family finding (feature/taxonomy-distinctions)
    # showed both merging onto one bare "Manganese" canonical via a
    # shared alias list, silently combining two different trend series.
    AnalyteSpec(
        "Manganese, Plasma",
        ("manganese, plasma",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Manganese, RBC",
        ("manganese, rbc",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Homocysteine",
        ("homocysteine",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Coenzyme Q10",
        ("coenzyme q10",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Nicotinamide",
        ("nicotinamide",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Nicotinic Acid",
        ("nicotinic acid",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Arachidonic Acid",
        ("arachidonic acid",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Arachidonic Acid/EPA Ratio",
        ("arachidonic acid/epa ratio",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
        derived_from=("Arachidonic Acid", "EPA"),
    ),
    AnalyteSpec("DHA", ("dha",), "numeric", (), None, panel="Vitamins & Nutrition"),
    AnalyteSpec("DPA", ("dpa",), "numeric", (), None, panel="Vitamins & Nutrition"),
    AnalyteSpec("EPA", ("epa",), "numeric", (), None, panel="Vitamins & Nutrition"),
    AnalyteSpec(
        "EPA+DPA+DHA Total",
        ("epa+dpa+dha",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Linoleic Acid",
        ("linoleic acid",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Omega-3 Total",
        ("omega-3 total",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Omega-6 Total",
        ("omega-6 total",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
    ),
    AnalyteSpec(
        "Omega-6/Omega-3 Ratio",
        ("omega-6/omega-3 ratio",),
        "numeric",
        (),
        None,
        panel="Vitamins & Nutrition",
        derived_from=("Omega-6 Total", "Omega-3 Total"),
    ),
    AnalyteSpec(
        "ferritin",
        ("ferritin", "ferritin, serum"),
        "numeric",
        ("ng/mL",),
        (0.5, 5000.0),
        panel="Iron Studies",
    ),
    AnalyteSpec(
        "TSAT",
        ("tsat", "transferrin saturation", "iron saturation", "% saturation"),
        "numeric",
        ("%",),
        (1.0, 100.0),
        panel="Iron Studies",
        derived_from=("Iron", "TIBC"),
    ),
    AnalyteSpec(
        "Iron",
        ("iron", "iron, total", "iron, serum"),
        "numeric",
        (),
        None,
        panel="Iron Studies",
    ),
    AnalyteSpec(
        "TIBC",
        ("iron binding capacity", "iron bind.cap.(tibc)"),
        "numeric",
        (),
        None,
        panel="Iron Studies",
    ),
    AnalyteSpec("UIBC", ("uibc",), "numeric", (), None, panel="Iron Studies"),
    # --- Lipid Panel ---
    AnalyteSpec(
        "Cholesterol, Total",
        ("cholesterol, total",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec("Triglycerides", ("triglycerides",), "numeric", (), None, panel="Lipid Panel"),
    AnalyteSpec(
        "HDL Cholesterol",
        ("hdl cholesterol",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec(
        "LDL Cholesterol",
        ("ldl-cholesterol", "ldl chol calc (nih)"),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec(
        "Non-HDL Cholesterol",
        ("non hdl cholesterol",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec(
        "VLDL Cholesterol",
        ("vldl cholesterol cal",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec(
        "Cholesterol/HDL Ratio",
        ("chol/hdlc ratio",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
        derived_from=("Cholesterol, Total", "HDL Cholesterol"),
    ),
    AnalyteSpec(
        "Apolipoprotein B",
        ("apolipoprotein b",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec("Lipoprotein(a)", ("lipoprotein (a)",), "numeric", (), None, panel="Lipid Panel"),
    AnalyteSpec(
        "Lp-PLA2 Activity",
        ("lp pla2 activity",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec(
        "LDL Particle Number",
        ("ldl particle number",),
        "numeric",
        (),
        None,
        panel="Lipid Panel",
    ),
    AnalyteSpec("LDL Pattern", ("ldl pattern",), "qualitative", panel="Lipid Panel"),
    AnalyteSpec("LDL Peak Size", ("ldl peak size",), "numeric", (), None, panel="Lipid Panel"),
    AnalyteSpec("LDL Small", ("ldl small",), "numeric", (), None, panel="Lipid Panel"),
    AnalyteSpec("LDL Medium", ("ldl medium",), "numeric", (), None, panel="Lipid Panel"),
    AnalyteSpec("HDL Large", ("hdl large",), "numeric", (), None, panel="Lipid Panel"),
    # --- Heavy Metals ---
    AnalyteSpec("Arsenic, Blood", ("arsenic, blood",), "numeric", (), None, panel="Heavy Metals"),
    AnalyteSpec("Arsenic, Urine", ("arsenic, urine",), "numeric", (), None, panel="Heavy Metals"),
    AnalyteSpec(
        "Cadmium, Urine",
        ("cadmium, random urine",),
        "numeric",
        (),
        None,
        panel="Heavy Metals",
    ),
    AnalyteSpec("Lead, Blood", ("lead (venous)",), "numeric", (), None, panel="Heavy Metals"),
    AnalyteSpec("Lead, Urine", ("lead, urine",), "numeric", (), None, panel="Heavy Metals"),
    AnalyteSpec("Mercury, Blood", ("mercury, blood",), "numeric", (), None, panel="Heavy Metals"),
    AnalyteSpec(
        "Mercury, Urine",
        ("mercury, random urine",),
        "numeric",
        (),
        None,
        panel="Heavy Metals",
    ),
    # --- Hormones ---
    AnalyteSpec("Cortisol", ("cortisol, total", "cortisol"), "numeric", (), None, panel="Hormones"),
    AnalyteSpec(
        "Cortisol AM",
        ("cortisol, a.m.", "cortisol - am"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    # Explicit merge family: "ACTH,PLASMA" / "ACTH, Plasma".
    AnalyteSpec("ACTH", ("acth", "acth, plasma"), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("Aldosterone", ("aldosterone",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("ADH (Vasopressin)", ("adh",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("Androstenedione", ("androstenedione",), "numeric", (), None, panel="Hormones"),
    # Explicit merge family: AMH.
    AnalyteSpec(
        "AMH",
        (
            "anti-mullerian hormone",
            "anti-mullerian hormone (amh), female",
            "anti-mullerian hormone (amh)",
            "amh",
        ),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "17-Hydroxyprogesterone",
        ("17-oh-progesterone,lcmsms", "17-oh-progesterone"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "Progesterone",
        ("progesterone", "progesterone, lc/ms"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "Estradiol",
        ("estradiol", "estradiol, ultrasensitive lc/ms"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec("Estriol", ("estriol, serum",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec(
        "Estrogens, Total",
        ("estrogens, total, ia", "estrogens, total"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec("Estrone", ("estrone", "estrone, serum"), "numeric", (), None, panel="Hormones"),
    AnalyteSpec(
        "Testosterone, Total",
        ("testosterone, total, ms", "testosterone,tot,lc/ms/ms", "testosterone"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "Testosterone, Free",
        ("testosterone, free",),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "Testosterone, Bioavailable",
        ("testosterone, bioavailable", "testosterone,bioavailable"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec("LH", ("lh",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("FSH", ("fsh",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("Prolactin", ("prolactin",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("DHEA-S", ("dhea sulfate", "dhea-sulfate"), "numeric", (), None, panel="Hormones"),
    # Explicit merge family: C-Peptide.
    AnalyteSpec(
        "C-Peptide",
        ("c-peptide, lc/ms/ms", "c-peptide, serum"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "Insulin",
        ("insulin", "insulin, intact, lc/ms/ms"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "Insulin Resistance Score",
        ("insulin resistance score",),
        "score",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec("SHBG", ("sex hormone binding globulin",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec(
        "Renin Activity", ("renin activity, plasma",), "numeric", (), None, panel="Hormones"
    ),
    AnalyteSpec("Metanephrine", ("metanephrine, pl",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("Normetanephrine", ("normetanephrine, pl",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("Erythropoietin", ("erythropoietin",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec(
        "PTH, Intact",
        ("parathyroid hormone, intact", "pth, intact"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec(
        "hCG, Total",
        ("hcg, total, qn", "hcg, beta subunit, qnt, serum"),
        "numeric",
        (),
        None,
        panel="Hormones",
    ),
    AnalyteSpec("IGF-1", ("insulin-like growth factor i",), "numeric", (), None, panel="Hormones"),
    AnalyteSpec("IGF-1 Z-Score", ("igf-1, z score",), "score", (), None, panel="Hormones"),
    # --- Urinalysis ---
    AnalyteSpec("Appearance", ("appearance",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("Color", ("color",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("pH", ("ph",), "numeric", (), None, panel="Urinalysis"),
    AnalyteSpec("Specific Gravity", ("specific gravity",), "numeric", (), None, panel="Urinalysis"),
    AnalyteSpec("Protein", ("protein",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("Ketones", ("ketones",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("Occult Blood", ("occult blood",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("Leukocyte Esterase", ("leukocyte esterase",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("Nitrite", ("nitrite",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("Bacteria", ("bacteria",), "qualitative", panel="Urinalysis"),
    AnalyteSpec("Hyaline Cast", ("hyaline cast",), "qualitative", panel="Urinalysis"),
    AnalyteSpec(
        "Squamous Epithelial Cells",
        ("squamous epithelial cells",),
        "qualitative",
        panel="Urinalysis",
    ),
    AnalyteSpec(
        "Osmolality, Urine", ("osmolality,urine",), "numeric", (), None, panel="Urinalysis"
    ),
    AnalyteSpec(
        "Urine Culture",
        ("culture, urine, routine", "reflexive urine culture"),
        "qualitative",
        panel="Urinalysis",
    ),
    # --- Stool Studies ---
    AnalyteSpec(
        "Fecal Calprotectin",
        ("calprotectin, stool", "calprotectin, fecal"),
        "numeric",
        (),
        None,
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Fecal Pancreatic Elastase",
        ("pancreatic elastase 1", "pancreatic elastase, fecal"),
        "numeric",
        (),
        None,
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "C. difficile Toxin",
        (
            "c difficile toxin a/b",
            "c difficile toxin gene naa",
            "clostridium difficile toxin/gdh w/refl to pcr",
            "toxin a and b",
        ),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec("C. difficile GDH Antigen", ("gdh antigen",), "qualitative", panel="Stool Studies"),
    AnalyteSpec(
        "Cryptosporidium Antigen",
        ("cryptosporidium antigen, eia", "cryptosporidium", "cryptosporidium eia"),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Giardia Antigen",
        (
            "giardia ag, eia, stool",
            "giardia lamblia",
            "giardia lamblia ag, eia",
            "giardia result 1",
        ),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Ova and Parasites Exam",
        ("ova and parasites, conc/perm smear, 3 spec", "ova + parasite exam"),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Ova and Parasites Trichrome Stain",
        ("trichrome 1", "trichrome 2", "trichrome 3"),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Ova and Parasites Concentration Exam",
        ("concentration 1", "concentration 2", "concentration 3"),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "H. pylori Stool Antigen",
        ("h. pylori stool ag, eia",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec("Campylobacter", ("campylobacter",), "qualitative", panel="Stool Studies"),
    AnalyteSpec(
        "Entamoeba histolytica",
        ("entamoeba histolytica",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec("E. coli O157", ("e coli o157",), "qualitative", panel="Stool Studies"),
    AnalyteSpec(
        "Enteroaggregative E. coli",
        ("enteroaggregative e coli",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Enteropathogenic E. coli",
        ("enteropathogenic e coli",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Enterotoxigenic E. coli",
        ("enterotoxigenic e coli",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec("Salmonella", ("salmonella",), "qualitative", panel="Stool Studies"),
    AnalyteSpec(
        "Shiga-toxin-producing E. coli",
        ("shiga-toxin-producing e coli",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Shigella/Enteroinvasive E. coli",
        ("shigella/enteroinvasive e coli",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec("Vibrio", ("vibrio",), "qualitative", panel="Stool Studies"),
    AnalyteSpec("Vibrio cholerae", ("vibrio cholerae",), "qualitative", panel="Stool Studies"),
    AnalyteSpec(
        "Yersinia enterocolitica",
        ("yersinia enterocolitica",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec(
        "Plesiomonas shigelloides",
        ("plesiomonas shigelloides",),
        "qualitative",
        panel="Stool Studies",
    ),
    AnalyteSpec("Norovirus GI/GII", ("norovirus gi/gii",), "qualitative", panel="Stool Studies"),
    AnalyteSpec("Rotavirus A", ("rotavirus a",), "qualitative", panel="Stool Studies"),
    AnalyteSpec("Sapovirus", ("sapovirus",), "qualitative", panel="Stool Studies"),
    AnalyteSpec("Astrovirus", ("astrovirus",), "qualitative", panel="Stool Studies"),
    AnalyteSpec(
        "Adenovirus F 40/41", ("adenovirus f 40/41",), "qualitative", panel="Stool Studies"
    ),
    AnalyteSpec(
        "Cyclospora cayetanensis",
        ("cyclospora cayetanensis",),
        "qualitative",
        panel="Stool Studies",
    ),
    # --- Tumor Markers ---
    AnalyteSpec("CA-125", ("cancer antigen (ca) 125",), "numeric", (), None, panel="Tumor Markers"),
    AnalyteSpec(
        "AFP (Alpha-Fetoprotein)",
        ("afp, serum, tumor marker",),
        "numeric",
        (),
        None,
        panel="Tumor Markers",
    ),
    # --- Immunology / Flow Cytometry ---
    AnalyteSpec(
        "Immunoglobulin A (IgA)",
        ("immunoglobulin a",),
        "numeric",
        (),
        None,
        panel="Immunology/Flow Cytometry",
    ),
    AnalyteSpec(
        "Immunoglobulin E (IgE), Total",
        ("immunoglobulin e, total",),
        "numeric",
        (),
        None,
        panel="Immunology/Flow Cytometry",
    ),
    AnalyteSpec(
        "NK Cells (CD3-/CD16+CD56+), %",
        ("cd3-/cd16+cd56+ (%)",),
        "numeric",
        (),
        None,
        panel="Immunology/Flow Cytometry",
    ),
    AnalyteSpec(
        "NK Cells (CD3-/CD16+CD56+), Absolute",
        ("natural killer cells cd3-cd16+cd56+ (abs)",),
        "numeric",
        (),
        None,
        panel="Immunology/Flow Cytometry",
    ),
    AnalyteSpec("Tryptase", ("tryptase",), "numeric", (), None),
    AnalyteSpec("Histamine", ("histamine, plasma",), "numeric", (), None),
    AnalyteSpec("Candida glabrata", ("candida glabrata",), "qualitative"),
    AnalyteSpec("Candida species", ("candida species",), "qualitative"),
    # --- Tick-Borne Serology ---
    AnalyteSpec(
        "Lyme WB IgG Interpretation",
        ("lyme disease ab(igg),blot", "lyme igg wb interp."),
        "qualitative",
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Lyme WB IgM Interpretation",
        ("lyme disease ab(igm),blot", "lyme igm wb interp."),
        "qualitative",
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "B. miyamotoi Antibody IgG",
        ("b. miyamotoi ab (igg)",),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "B. miyamotoi Antibody IgM",
        ("b. miyamotoi ab (igm)",),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Babesia microti Antibody IgG",
        (
            "babesia microti ab (igg)",
            "babesia microti ab (igg), screen",
            "babesia microti igg",
        ),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Babesia microti Antibody IgM",
        (
            "babesia microti ab (igm)",
            "babesia microti ab (igm), screen",
            "babesia microti igm",
        ),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Babesia microti Interpretation",
        ("interpretation (babesia microti)",),
        "qualitative",
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Babesia duncani Antibody IgG",
        ("babesia duncani (wa1) antibody (igg), ifa",),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Anaplasma phagocytophilum Antibody IgG",
        (
            "a. phagocytophilum ab (igg)",
            "a. phagocytophilum ab (igg), screen",
        ),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Anaplasma phagocytophilum Antibody IgM",
        (
            "a. phagocytophilum ab (igm)",
            "a. phagocytophilum ab (igm), screen",
        ),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Anaplasma phagocytophilum PCR",
        ("anaplasma phagocytophilum dna, ql real time pcr",),
        "qualitative",
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Ehrlichia chaffeensis Antibody IgG",
        (
            "e. chaffeensis ab (igg), screen",
            "e. chaffeensis ab igg",
            "e. chaffeensis (hme) igg titer",
        ),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Ehrlichia chaffeensis Antibody IgM",
        (
            "e. chaffeensis ab (igm), screen",
            "e. chaffeensis ab igm",
            "e. chaffeensis (hme) igm titer",
        ),
        "numeric",
        (),
        None,
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "Ehrlichia chaffeensis PCR",
        ("ehrlichia chaffeensis dna real time pcr",),
        "qualitative",
        panel="Tick-Borne Serology",
    ),
    AnalyteSpec(
        "RMSF Antibody IgG", ("rmsf igg",), "numeric", (), None, panel="Tick-Borne Serology"
    ),
    AnalyteSpec(
        "RMSF Antibody IgM", ("rmsf igm",), "numeric", (), None, panel="Tick-Borne Serology"
    ),
    # --- Scores (queue-ergonomics slice item 2: FRAX/T-score/Z-score
    # have no unit/reference range by nature - `kind="score"` skips
    # `validate_row`'s unit-whitelist complaint when the printed unit
    # is None (or "%" for FRAX), and no ref range is ever expected).
    # `canonicalize`'s suffix-match rule (below) lets a site-prefixed
    # DEXA row name like "LEFT HIP femoral neck T-Score" resolve to
    # this spec even though the alias table itself only matches whole
    # names.
    AnalyteSpec(
        "T-score",
        ("t-score", "t score"),
        "score",
        (),
        (-6.0, 6.0),
        panel="Bone Density",
    ),
    AnalyteSpec(
        "Z-score",
        ("z-score", "z score"),
        "score",
        (),
        (-6.0, 6.0),
        panel="Bone Density",
    ),
    AnalyteSpec(
        "FRAX 10-year probability of hip fracture",
        ("10-year probability of hip fracture is",),
        "score",
        ("%",),
        (0.0, 100.0),
        panel="Bone Density",
    ),
    AnalyteSpec(
        "FRAX 10-year probability of major osteoporotic fracture",
        (
            "frax analysis shows 10-year probability of major osteoporotic "
            "fracture (clinical spine, forearm, hip or shoulder) is",
        ),
        "score",
        ("%",),
        (0.0, 100.0),
        panel="Bone Density",
    ),
    # Left/right hip BMD are different anatomic measurements with their
    # own independent trend series - a real collision-family finding
    # (feature/taxonomy-distinctions) showed both sides merging onto one
    # bare "Total Hip BMD"/"Femoral Neck BMD" canonical via a shared alias
    # list, silently overwriting one hip's series with the other's. Note
    # score-kind DEXA rows ("... T-Score"/"... Z-Score") are unaffected by
    # this split - those resolve via `_SCORE_SUFFIX_TO_CANONICAL` (below),
    # a `canonicalize`-only (read-time) rule that `canonical_rename_target`
    # never uses, so a site-prefixed score row's STORED name is already
    # preserved distinct without needing its own per-side spec.
    AnalyteSpec(
        "Left Hip Total BMD",
        ("left hip total bmd",),
        "numeric",
        (),
        None,
        panel="Bone Density",
    ),
    AnalyteSpec(
        "Right Hip Total BMD",
        ("right hip total bmd",),
        "numeric",
        (),
        None,
        panel="Bone Density",
    ),
    AnalyteSpec(
        "Left Hip Femoral Neck BMD",
        ("left hip femoral neck bmd",),
        "numeric",
        (),
        None,
        panel="Bone Density",
    ),
    AnalyteSpec(
        "Right Hip Femoral Neck BMD",
        ("right hip femoral neck bmd",),
        "numeric",
        (),
        None,
        panel="Bone Density",
    ),
    AnalyteSpec(
        "Lumbar Spine BMD",
        ("lumbar spine ap(l1-l4) bmd",),
        "numeric",
        (),
        None,
        panel="Bone Density",
    ),
    # --- Infectious disease serology (spelling-variant merges;
    # deliberately not forced into a curated panel - see module docstring
    # "don't force one-off exotic names into fake panels" - these merge
    # for trend continuity but show under "Other" in the UI) ---
    AnalyteSpec(
        "EBV VCA Antibody IgG",
        ("ebv ab vca, igg", "ebv viral capsid ag (vca) ab (igg)"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "EBV VCA Antibody IgM",
        ("ebv ab vca, igm", "ebv viral capsid ag (vca) ab (igm)"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "EBV Early Antigen Antibody IgG",
        ("ebv early antigen d ab (igg)", "ebv early antigen ab, igg"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "EBV Nuclear Antigen (EBNA) Antibody IgG",
        ("ebv nuclear ag (ebna) ab (igg)", "ebv nuclear antigen ab, igg"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "CMV Antibody IgG",
        ("cytomegalovirus antibody (igg)", "cytomegalovirus (cmv) ab, igg"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "CMV Antibody IgM",
        ("cytomegalovirus antibody (igm)", "cytomegalovirus (cmv) ab, igm"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "HSV-1 Antibody IgG",
        ("hsv 1 igg, type specific ab", "hsv 1 igg, type spec"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "HSV-2 Antibody IgG",
        ("hsv 2 igg, type specific ab", "hsv 2 igg, type spec"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "Varicella Zoster Antibody IgG",
        ("varicella zoster virus antibody (igg)", "varicella zoster igg"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "Rubella Antibody IgG",
        ("rubella antibody (igg)", "rubella antibodies, igg"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec("Rubeola (Measles) Antibody IgG", ("rubeola ab, igg",), "numeric", (), None),
    AnalyteSpec("Mumps Antibody IgG", ("mumps abs, igg",), "numeric", (), None),
    AnalyteSpec(
        "HHV-6 Antibody IgG",
        ("herpesvirus 6 ab (igg)", "hhv 6 igg antibodies"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec("HHV-6 Antibody IgM", ("herpesvirus 6 ab (igm)",), "numeric", (), None),
    AnalyteSpec(
        "Mycoplasma pneumoniae Antibody IgG",
        ("mycoplasma pneumoniae antibody (igg)", "m pneumoniae igg abs"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "Mycoplasma pneumoniae Antibody IgM",
        ("mycoplasma pneumoniae antibody (igm)", "m pneumoniae igm abs"),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec("Hepatitis A Antibody IgM", ("hepatitis a igm",), "numeric", (), None),
    AnalyteSpec(
        "Hepatitis B Core Antibody IgM",
        ("hepatitis b core antibody (igm)",),
        "numeric",
        (),
        None,
    ),
    AnalyteSpec(
        "Hepatitis B Surface Antigen",
        ("hepatitis b surface antigen",),
        "qualitative",
    ),
    AnalyteSpec("Hepatitis B Surface Antibody", ("hep b surface ab, qual",), "qualitative"),
    AnalyteSpec("Hepatitis C Antibody", ("hepatitis c antibody",), "qualitative"),
    AnalyteSpec("HIV Ag/Ab (4th Gen)", ("hiv ag/ab, 4th gen",), "qualitative"),
    AnalyteSpec(
        "RPR (Syphilis)",
        ("rpr (dx) w/refl titer and confirmatory testing",),
        "qualitative",
    ),
    AnalyteSpec(
        "QuantiFERON-TB Gold Plus",
        ("quantiferon®-tb gold plus, 1 tube",),
        "qualitative",
    ),
    AnalyteSpec("NIL", ("nil",), "numeric", (), None),
    AnalyteSpec("Mitogen-Nil", ("mitogen-nil",), "numeric", (), None),
    AnalyteSpec("TB1-Nil", ("tb1-nil",), "numeric", (), None),
    AnalyteSpec("TB2-Nil", ("tb2-nil",), "numeric", (), None),
    AnalyteSpec(
        "Chlamydia trachomatis RNA (NAAT)",
        ("chlamydia trachomatis rna, tma, urogenital",),
        "qualitative",
    ),
    AnalyteSpec(
        "Neisseria gonorrhoeae RNA (NAAT)",
        ("neisseria gonorrhoeae rna, tma, urogenital",),
        "qualitative",
    ),
    AnalyteSpec(
        "Trichomonas vaginalis RNA (NAAT)",
        ("trichomonas vaginalis (tv), tma",),
        "qualitative",
    ),
    AnalyteSpec(
        "Bacterial Vaginosis PCR Panel",
        ("sureswab adv bacterial vaginosis (bv), tma",),
        "qualitative",
    ),
    AnalyteSpec(
        "Monospot (Heterophile) Screen",
        ("heterophile, mono screen", "mononucleosis test, qual"),
        "qualitative",
    ),
    AnalyteSpec("Hemoglobin A1c", ("hemoglobin a1c",), "numeric", (), None),
    AnalyteSpec("Hemoglobin A (electrophoresis fraction)", ("hemoglobin a",), "numeric", (), None),
    AnalyteSpec("Hemoglobin A2", ("hemoglobin a2 (quant)",), "numeric", (), None),
    AnalyteSpec("Hemoglobin F", ("hemoglobin f",), "numeric", (), None),
)

# --- Mold IgG Panel (generated: one AnalyteSpec per M0xx code) ---
# Each raw vocab string is truncated mid-species-name by the source PDF's
# fixed column width (e.g. "M001-IgG Penicillium chrysog") and sometimes
# printed with a leading "**" flag - `_normalize` already strips ALL
# punctuation everywhere (not just leading/trailing), so the "**"-prefixed
# and bare forms normalize identically and need only one alias each.
_MOLD_IGG_TABLE: tuple[tuple[str, str], ...] = (
    ("M001", "Penicillium chrysog"),
    ("M002", "Cladosporium herbar"),
    ("M003", "Aspergillus fumigat"),
    ("M004", "Mucor racemosus"),
    ("M005", "Candida albican"),
    ("M006", "Alternaria alternat"),
    ("M007", "Botrytis cinerea"),
    ("M008", "Setomelanomma rosta"),
    ("M009", "Fusarium proliferat"),
    ("M010", "Stemphylium herbaru"),
    ("M011", "Rhizopus nigricans"),
    ("M012", "Aureobasidi pullula"),
    ("M014", "Epicoccum purpur"),
    ("M207", "Aspergillus niger"),
)
_MOLD_IGG_SPECS: tuple[AnalyteSpec, ...] = tuple(
    AnalyteSpec(
        f"Mold IgG {code} {label}",
        (f"{code}-IgG {label}",),
        "numeric",
        (),
        None,
        panel="Mold IgG Panel",
    )
    for code, label in _MOLD_IGG_TABLE
)

# --- Allergen IgE (generated: one AnalyteSpec per allergen code) ---
# Most allergens are printed under only one raw spelling ("F001-IgE Egg
# White"); Pork/Beef/Lamb are ALSO printed LabCorp-style ("PORK (F26)
# IGE") - both spellings alias onto the same canonical. The "... IGE
# CLASS" reading (a 0-6 category, not the same value) is a genuinely
# different measurement and gets its own canonical, not merged in.
_ALLERGEN_IGE_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Egg White", ("F001-IgE Egg White",)),
    ("Milk", ("F002-IgE Milk",)),
    ("Codfish", ("F003-IgE Codfish",)),
    ("Wheat", ("F004-IgE Wheat",)),
    ("Rye", ("F005-IgE Rye",)),
    ("Barley", ("F006-IgE Barley",)),
    ("Oat", ("F007-IgE Oat",)),
    ("Corn", ("F008-IgE Corn",)),
    ("Rice", ("F009-IgE Rice",)),
    ("Peanut", ("F013-IgE Peanut",)),
    ("Soybean", ("F014-IgE Soybean",)),
    ("Crab", ("F023-IgE Crab",)),
    ("Shrimp", ("F024-IgE Shrimp",)),
    ("Tomato", ("F025-IgE Tomato",)),
    ("Pork", ("F026-IgE Pork", "PORK (F26) IGE")),
    ("Beef", ("F027-IgE Beef", "BEEF (F27) IGE")),
    ("Orange", ("F033-IgE Orange",)),
    ("Potato, White", ("F035-IgE Potato, White",)),
    ("Yeast", ("F045-IgE Yeast",)),
    ("Garlic", ("F047-IgE Garlic",)),
    ("Chicken", ("F083-IgE Chicken",)),
    ("Lamb", ("F088-IgE Lamb", "LAMB (F88) IGE")),
    ("D. pteronyssinus", ("D001-IgE D pteronyssinus",)),
    ("D. farinae", ("D002-IgE D farinae",)),
    ("Cat Dander", ("E001-IgE Cat Dander",)),
    ("Dog Dander", ("E005-IgE Dog Dander",)),
    ("Bermuda Grass", ("G002-IgE Bermuda Grass",)),
    ("Bluegrass, Kentucky", ("G008-IgE Bluegrass, Kentucky",)),
    ("Bahia Grass", ("G017-IgE Bahia Grass",)),
    ("Cockroach, American", ("I206-IgE Cockroach, American",)),
    ("Maple/Box Elder", ("T001-IgE Maple/Box Elder",)),
    ("Common Silver Birch", ("T003-IgE Common Silver Birch",)),
    ("Hazelnut Tree", ("T004-IgE Hazelnut Tree",)),
    ("Cedar, Mountain", ("T006-IgE Cedar, Mountain",)),
    ("Oak, White", ("T007-IgE Oak, White",)),
    ("Elm, American", ("T008-IgE Elm, American",)),
    ("Ash, White", ("T015-IgE Ash, White",)),
    ("Hickory, White", ("T041-IgE Hickory, White",)),
    ("White Mulberry", ("T070-IgE White Mulberry",)),
    ("Ragweed, Short", ("W001-IgE Ragweed, Short",)),
    ("Mugwort", ("W006-IgE Mugwort",)),
    ("Plantain, English", ("W009-IgE Plantain, English",)),
    ("Pigweed, Common", ("W014-IgE Pigweed, Common",)),
    ("Sheep Sorrel", ("W018-IgE Sheep Sorrel",)),
    ("Nettle", ("W020-IgE Nettle",)),
    ("Alpha-Gal", ("O215-IgE Alpha-Gal", "GALACTOSE ALPHA 1,3 GALACTOSE IGE")),
    # Mold allergens tested by IgE (distinct from the Mold IgG Panel above -
    # same species, different Ig class/clinical use) - same truncated,
    # PDF-column-width-limited species names as their IgG counterparts.
    ("Penicillium chrysogen (M1)", ("M001-IgE Penicillium chrysogen",)),
    ("Cladosporium herbarum (M2)", ("M002-IgE Cladosporium herbarum",)),
    ("Aspergillus fumigatus (M3)", ("M003-IgE Aspergillus fumigatus",)),
    ("Mucor racemosus (M4)", ("M004-IgE Mucor racemosus",)),
    ("Alternaria alternata (M6)", ("M006-IgE Alternaria alternata",)),
    ("Stemphylium herbarum (M10)", ("M010-IgE Stemphylium herbarum",)),
)
_ALLERGEN_IGE_CLASS_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Pork", ("PORK (F26) IGE CLASS",)),
    ("Beef", ("BEEF (F27) IGE CLASS",)),
    ("Lamb", ("LAMB (F88) IGE CLASS",)),
)
_ALLERGEN_IGE_SPECS: tuple[AnalyteSpec, ...] = tuple(
    AnalyteSpec(f"{label} IgE", aliases, "numeric", (), None, panel="Allergen IgE")
    for label, aliases in _ALLERGEN_IGE_TABLE
) + tuple(
    AnalyteSpec(f"{label} IgE Class", aliases, "numeric", (), None, panel="Allergen IgE")
    for label, aliases in _ALLERGEN_IGE_CLASS_TABLE
)

# --- Tick-Borne Serology: Lyme Western Blot bands (generated) ---
# Each band+Ig-class is its own canonical (CDC interpretation criteria
# count specific bands, so collapsing them into one series would erase
# clinically meaningful information) - the "one sub-family" the
# lab-taxonomy plan describes is the shared "Lyme WB {N} kDa {Ig}" naming
# convention and shared `panel`, not a single collapsed canonical. Each
# band is printed under two spelling conventions ("23 KD (IGG) BAND" and
# "IgG P23 Ab.") that both alias onto the same canonical.
_LYME_WB_IGG_BANDS: tuple[int, ...] = (18, 23, 28, 30, 39, 41, 45, 58, 66, 93)
_LYME_WB_IGM_BANDS: tuple[int, ...] = (23, 39, 41)
_LYME_WB_SPECS: tuple[AnalyteSpec, ...] = tuple(
    AnalyteSpec(
        f"Lyme WB {band} kDa IgG",
        (f"{band} KD (IGG) BAND", f"IgG P{band} Ab."),
        "qualitative",
        panel="Tick-Borne Serology",
    )
    for band in _LYME_WB_IGG_BANDS
) + tuple(
    AnalyteSpec(
        f"Lyme WB {band} kDa IgM",
        (f"{band} KD (IGM) BAND", f"IgM P{band} Ab."),
        "qualitative",
        panel="Tick-Borne Serology",
    )
    for band in _LYME_WB_IGM_BANDS
)

ANALYTE_SPECS: dict[str, AnalyteSpec] = {
    spec.canonical_name: spec
    for spec in (
        *_HAND_CURATED_SPECS,
        *_MOLD_IGG_SPECS,
        *_ALLERGEN_IGE_SPECS,
        *_LYME_WB_SPECS,
    )
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _spec in ANALYTE_SPECS.values():
    _ALIAS_TO_CANONICAL[_normalize(_spec.canonical_name)] = _spec.canonical_name
    for _alias in _spec.aliases:
        _ALIAS_TO_CANONICAL[_normalize(_alias)] = _spec.canonical_name

# Suffix-match table for `kind="score"` analytes ONLY (queue-ergonomics
# slice item 2): a DEXA row is commonly printed with a site prefix the
# exact-alias table above can't see through (e.g. "LEFT HIP femoral neck
# T-Score", "L1-L4 Z-Score"). Sorted longest-normalized-alias-first so a
# more specific suffix always wins a match before a shorter one gets the
# chance. Deliberately scoped to score-kind aliases only - every other
# analyte keeps exact-alias matching, unchanged, to avoid a same-suffix
# false positive (e.g. nothing here is short/generic enough to
# accidentally suffix-match an unrelated test name).
_SCORE_SUFFIX_TO_CANONICAL: list[tuple[str, str]] = sorted(
    (
        (_normalize(_alias), _spec.canonical_name)
        for _spec in ANALYTE_SPECS.values()
        if _spec.kind == "score"
        for _alias in (_spec.canonical_name, *_spec.aliases)
    ),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


# Generic specimen/method suffix strip (lab-taxonomy layer, item 3): a
# curated set of trailing ", <suffix>" qualifiers labs print on an
# otherwise-plain analyte name - a specimen restatement ("Chloride,
# Serum"), a short specimen abbreviation ("Alkaline Phosphatase, S"), or an
# assay-method tag ("C-Peptide, LC/MS/MS") - that carry no separate
# clinical meaning from the bare analyte for the analytes this strips.
# Applied to the RAW name (not the already-punctuation-stripped
# `_normalize`d one) so the match only fires on an actual ", <suffix>"
# boundary - never against a same-looking trailing substring that happens
# to share letters with a real word (this is why the pattern requires the
# leading comma). Deliberately excludes anything that IS a clinically
# distinct sub-test from its "bare" form - most notably "RBC" (RBC
# magnesium is a different assay from serum magnesium, not a specimen
# restatement of the same one - see the "Magnesium, RBC" spec) and "Total"
# (kept as part of several canonical names themselves, e.g. "Bilirubin,
# Total", "Cholesterol, Total" - stripping it would collapse those into a
# bare form that isn't what was actually measured).
_SUFFIX_STRIP_PATTERN = re.compile(
    r",\s*("
    r"serum(\s*(or|/)\s*plasma)?"
    r"|plasma(\s*(or|/)\s*serum)?"
    r"|s|p"
    r"|quant|quantitative"
    r"|lcmsms|lc\s*/\s*ms(\s*/\s*ms)?"
    r"|ia|ms"
    r")\s*$",
    re.IGNORECASE,
)


def _strip_known_suffix(name_raw: str) -> str | None:
    """`name_raw` with one trailing curated specimen/method suffix removed
    (`_SUFFIX_STRIP_PATTERN`), or `None` if no such suffix is present."""
    match = _SUFFIX_STRIP_PATTERN.search(name_raw)
    if match is None:
        return None
    return name_raw[: match.start()]


def canonicalize(name_raw: str) -> str | None:
    """Map a raw analyte name to its canonical name, or `None` if unknown.

    Lookup is case/punctuation-insensitive (`_normalize`). Resolution order:

      1. An exact whole-name alias match.
      2. A `kind="score"` suffix match (module-level
         `_SCORE_SUFFIX_TO_CANONICAL` - site-prefixed DEXA rows like "LEFT
         HIP femoral neck T-Score" resolve to the "T-score" spec this way).
         Every non-score analyte is exact-alias-only at this step,
         unchanged.
      3. A curated specimen/method suffix strip (`_strip_known_suffix`,
         item 3 above) followed by a retried exact alias match on the
         stripped name - e.g. "Chloride, Serum" strips to "Chloride" and
         then matches that spec's own alias.

    Unknown analytes return `None` rather than raising — per PLAN.md,
    ingestion is never blocked on coding an analyte we don't yet recognize.
    """
    normalized = _normalize(name_raw)
    exact = _ALIAS_TO_CANONICAL.get(normalized)
    if exact is not None:
        return exact
    for suffix, canonical in _SCORE_SUFFIX_TO_CANONICAL:
        if normalized.endswith(suffix):
            return canonical
    stripped = _strip_known_suffix(name_raw)
    if stripped is not None:
        stripped_exact = _ALIAS_TO_CANONICAL.get(_normalize(stripped))
        if stripped_exact is not None:
            return stripped_exact
    return None


def canonical_rename_target(name_raw: str, name: str) -> str | None:
    """The name `adoc labs-recanonicalize` (`labs.recanonicalize`) may
    overwrite a row's stored `name` with, or `None` to mean "leave the
    stored name exactly as it is" (module docstring, "Matching vs.
    renaming").

    Unlike `canonicalize`, this considers ONLY an EXACT alias match (the
    same case/punctuation normalization `canonicalize` uses) - never the
    generic suffix-strip retry, never the score-suffix rule. Those two
    rules are safe to use for read-time matching (`canonicalize`) but NOT
    for physically renaming a stored row: they can each discard a
    site/side, specimen, or stratification distinction an exact alias
    entry never would, because an exact alias is a human-reviewed
    statement that two spellings denote the IDENTICAL measurement (e.g.
    "ACTH,PLASMA" / "ACTH, Plasma" - both merge, still, via this
    function), where a suffix-strip/score-suffix match is only a
    heuristic resemblance.

    Tried against `name_raw` first (the least-processed form, most likely
    to match an alias verbatim), then `name` - mirrors `canonicalize`'s
    own resolution order for the exact-alias step.
    """
    for candidate in (name_raw, name):
        exact = _ALIAS_TO_CANONICAL.get(_normalize(candidate))
        if exact is not None:
            return exact
    return None


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
        # An empty `allowed_units` means "no unit whitelist" (AnalyteSpec's
        # docstring) - many lab-taxonomy specs exist purely so their
        # analyte canonicalizes/groups under a panel, with no curated unit
        # knowledge behind them, and must never manufacture a brand-new
        # UNKNOWN_UNIT issue against already-accepted rows just because
        # they now canonicalize to a real spec. Only check when a
        # whitelist was actually curated.
        if spec.allowed_units and not _unit_in_whitelist(row.ucum_unit, spec.allowed_units):
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

    elif spec.kind == "score":
        # A score is unitless by nature (T-score/Z-score) or "%"-only
        # (FRAX) - `row.ucum_unit is None` never complains (unlike
        # "numeric", where a missing unit itself IS the complaint); only
        # an actually-printed, non-whitelisted unit does. No reference
        # range is ever expected for a score, so nothing here checks one.
        if row.ucum_unit is not None and not _unit_in_whitelist(row.ucum_unit, spec.allowed_units):
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

    return issues


def trend_deviation(
    db: LabsDb, row: LabResult, *, series: Sequence[LabResult] | None = None
) -> float | None:
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

    `series`, when given, is used in place of querying
    `db.series(canonical, row.specimen)` — identical rows, identical
    filtering, but supplied by a caller that already bulk-fetched every
    analyte's rows in one query (e.g. `LabsDb.series_by_key()`) instead of
    paying a fresh `labs.sqlite` round trip per row. `labs.sqlite` lives on
    EFS/NFS in the deployed app, where each query costs milliseconds, so a
    sweep over every current analyte
    (`reason.review.deterministic_trend_scan`) or every pending row
    (`labs.reclassify.reclassify_pending`) turns into real latency without
    this. Defaults to `None`, which queries exactly as before.
    """
    if row.value is None:
        return None
    canonical = canonicalize(row.name) or row.name
    candidates = db.series(canonical, row.specimen) if series is None else series
    priors = [
        r.value for r in candidates if r.value is not None and r.id != row.id and r.date < row.date
    ]
    if len(priors) < TREND_OUTLIER_MIN_PRIORS:
        return None
    median = statistics.median(priors)
    if median == 0:
        return None
    return abs(row.value - median) / abs(median)


def outlier_issue_from_deviation(row: LabResult, ratio: float | None) -> ValidationIssue | None:
    """The `ValidationIssue` `trend_outlier` would build from an
    already-computed `trend_deviation` ratio.

    Split out so a caller that needs BOTH the outlier gate and the raw
    ratio for something else (`ingest.reconcile._evaluate_pair` also uses
    the ratio for its cross-pass decimal-signature check) computes
    `trend_deviation` exactly ONCE per row instead of once via
    `trend_outlier` and once again directly — each `trend_deviation` call
    is a `labs.sqlite` query when no pre-fetched `series` is supplied.
    """
    if ratio is not None and ratio > TREND_OUTLIER_RATIO:
        canonical = canonicalize(row.name) or row.name
        return ValidationIssue(
            IssueCode.TREND_OUTLIER,
            f"{canonical}: value {row.value} is {ratio:.0%} away from the median of "
            f"earlier readings - possible decimal error",
        )
    return None


def trend_outlier(
    db: LabsDb, row: LabResult, *, series: Sequence[LabResult] | None = None
) -> ValidationIssue | None:
    """Flag a >40% jump vs. the median of the patient's earlier readings
    (>=3 priors) OF THE SAME SPECIMEN. Catches decimal-shift extraction
    errors (e.g. potassium 4.1 misread as 41) without any clinical
    knowledge — pure statistics on this patient's own history for the same
    canonical analyte and specimen (see `trend_deviation`'s docstring).

    `series` is forwarded to `trend_deviation` unchanged — see its
    docstring.
    """
    return outlier_issue_from_deviation(row, trend_deviation(db, row, series=series))

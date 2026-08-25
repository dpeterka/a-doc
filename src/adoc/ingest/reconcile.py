"""Cross-pass reconciliation (PLAN.md "Ingestion", session loop (a)).

Matches `ExtractedResult` rows across the two extraction passes by
canonicalized analyte name (`labs.validate.canonicalize`, falling back to a
normalized raw name for analytes not yet in `ANALYTE_SPECS`) and
page-tolerant position (+/- `PAGE_TOLERANCE` pages, since pass A reads the
whole PDF and pass B reads per-page images - the two passes need not agree
exactly on which page a value "belongs to" for a splitting/merged table).

A row is **AUTO** only if ALL of the following gates pass (otherwise it is
**PENDING**, with every failing gate's reason recorded):

  1. matched  - both passes produced a row for this analyte (a row that
     exists in only one pass is PENDING, reason `single_pass`).
  2. value    - numeric `value` is exactly equal between passes (and
     `value_text` is equal after case/whitespace normalization).
  3. unit     - `unit_raw` is semantically equivalent between passes
     (`units_equivalent`: case/whitespace-normalized equality, or both a
     member of the same `labs.validate.UNIT_SYNONYMS` spelling family, e.g.
     "Million/uL" vs. "M/uL").
  4. ref      - `ref_range_raw` is semantically equivalent between passes
     (`ref_ranges_equivalent`: unicode-dash-unified/whitespace/case
     normalized, a trailing unit token stripped, then compared as a parsed
     numeric range/threshold/titer-threshold/qualitative-word shape rather
     than as literal text - e.g. `"<20"` vs. `"<20 Units"`).
  5. flag     - `flag_raw` is semantically equivalent between passes
     (`flags_equivalent`: absent/`""`/`"N"`/`"normal"` are all "unflagged"
     and equivalent to each other; word forms like `"high"` equal their
     letter codes - but absent is NEVER equivalent to an actual abnormal
     code, that stays a real disagreement).
  6. specimen - both passes agree on `specimen` (otherwise reason
     `specimen_mismatch`) - this is what keeps a urinalysis GLUCOSE
     "NEGATIVE" reading from ever being silently merged with a serum
     glucose reading just because one pass misread which section a row
     belonged to.
  7. confidence - both passes report `confidence == "high"`.
  8. validate - `labs.validate.validate_row` on EITHER pass's reading
     yields zero `ValidationIssue`s (unit whitelist, physiologic bounds,
     flag/value consistency, titer format) - checking both, not just one
     pass, catches an implausible misread even when the other pass got it
     right.
  9. trend    - `labs.validate.trend_outlier` returns `None` for EITHER
     pass's reading (no >40% jump vs. this patient's own recent median,
     scoped to that reading's own specimen - catches decimal-shift errors
     like potassium 4.1 misread as 41).
  10. dated   - the document's date (collection_date, falling back to
     report_date, from either pass) resolved to a real date.

Both passes' raw extracted rows plus the computed reasons are serialized
verbatim into `ReconciledRow.raw_json` for the confirm-queue UI and for
audit (PLAN.md "Provenance").

**RESCUE pass** — the two extraction passes sometimes name the SAME
measurement differently, e.g. "FRAX 10-year probability of hip fracture"
vs. a sentence-fragment "10-year probability of hip fracture is", so they
never land in the same `_match_key` group and never get a chance at
`_pair_rows` in the first place. After the normal per-group pairing
above, every leftover single-pass `ExtractedResult` (across ALL groups) is
run through one more greedy pairing pass, `_rescue_pair`, against a looser
but still fully deterministic compatibility test: same page (+/-
`PAGE_TOLERANCE`), identical value (or identical value_text), a compatible
unit (equal once normalized, or one side simply unstated), and the same
specimen-or-unknown. A rescued pair reconciles through the SAME checks
`_reconcile_matched_pair` runs (value/unit/ref-range/flag/specimen/
confidence, `validate_row`, `trend_outlier`) but ALWAYS ends up PENDING
with `name_variant` as its first reason - the differing names are
themselves reason enough for one quick human look, even when every other
field lines up - and always lands in the confirm queue's "agreed" bucket
(`name_variant` is deliberately not in `DISAGREEMENT_REASON_PREFIXES`),
since this is "the same result, just worded differently", not a genuine
cross-pass disagreement. The row's stored name is whichever of the two
(cleaned) names is LONGER/more specific; both original names are kept in
`raw_json` for audit. Every extracted name - not just rescued ones - is
first run through `clean_result_name`, which strips a trailing sentence-
fragment verb/punctuation (e.g. "... is", "... was", a trailing ":") an
extractor prompt might still emit, so canonicalize/grouping/pairing/audit
never see a raw fragment.

A residual risk is accepted deliberately: two genuinely DIFFERENT analytes
that happen to print the identical value on the same page (e.g. two
distinct tests both reading "0.0") could be rescued together if their
units are also compatible - this is why unit compatibility is required
rather than dropped; a real unit mismatch (e.g. "mg/dL" vs "ng/mL") still
blocks the rescue outright.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from adoc.ingest.schema import DocumentExtraction, ExtractedResult
from adoc.labs.models import LabFlag, LabResult, Specimen
from adoc.labs.validate import (
    DECIMAL_SIGNATURE_RATIO,
    UNIT_SYNONYMS,
    canonical_rename_target,
    canonical_unit,
    canonicalize,
    outlier_issue_from_deviation,
    trend_deviation,
    trend_outlier,
    validate_row,
)

if TYPE_CHECKING:
    from adoc.labs.db import LabsDb

PAGE_TOLERANCE = 1
_PLACEHOLDER_SHA = "0" * 64
_REF_RANGE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*[-–]\s*([0-9]*\.?[0-9]+)\s*$")

ReconcileStatus = Literal["auto", "pending"]


class ReconciledRow(BaseModel):
    """One reconciled analyte row, ready for `LabsDb.insert_results` (via
    `ingest.pipeline`'s conversion to `LabResult`).
    """

    name_raw: str
    canonical_name: str | None
    date: date
    value: float | None
    value_text: str | None
    unit_raw: str | None
    ref_range_raw: str | None
    flag_raw: str | None
    specimen: Specimen
    source_page: int | None
    status: ReconcileStatus
    reasons: list[str] = Field(default_factory=list)
    raw_json: str


def parse_ref_range(ref_range_raw: str | None) -> tuple[float | None, float | None]:
    """Parse a printed `"10 - 20"`-shaped reference range into `(low, high)`.

    Anything that doesn't match the simple two-number-with-dash shape
    (e.g. `"<5"`, `"positive/negative"`) yields `(None, None)` - the raw
    text is preserved separately in `ref_text`/`ref_range_raw`.
    """
    if not ref_range_raw:
        return None, None
    match = _REF_RANGE_RE.match(ref_range_raw)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def parse_flag(flag_raw: str | None) -> LabFlag | None:
    """Map a printed flag string onto `LabFlag`, or `None` if unrecognized."""
    if not flag_raw:
        return None
    try:
        return LabFlag(flag_raw.strip().upper())
    except ValueError:
        return None


def _normalize_str(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    return normalized or None


# --------------------------------------------------------------------------
# Semantic comparators: two extraction passes can print the SAME reading
# with a cosmetic transcription difference - a trailing unit token on a
# reference range, a unicode dash vs. a hyphen, "None" vs. "" for an
# unflagged result. `_reconcile_matched_pair` uses these in place of literal
# (`_normalize_str`-only) comparison for ref_range/unit/flag.
# value/value_text/specimen/confidence stay literal on purpose: loosening
# them risks masking a genuine extraction disagreement rather than a
# cosmetic one.
# --------------------------------------------------------------------------

_UNIT_SYNONYM_MEMBERS: tuple[str, ...] = tuple(
    member for family in UNIT_SYNONYMS for member in family
)

# Unicode dash variants (en dash, em dash, minus sign, ...) an extractor
# pass might print instead of a plain hyphen-minus in a numeric range.
_DASH_CHARS = "‐‑‒–—―−"
_DASH_TRANSLATION = str.maketrans({c: "-" for c in _DASH_CHARS})

_RANGE_PATTERN = re.compile(r"^([0-9]*\.?[0-9]+)\s*-\s*([0-9]*\.?[0-9]+)$")
_THRESHOLD_PATTERN = re.compile(r"^(<=|>=|<|>)\s*(.+)$")
_TITER_VALUE_PATTERN = re.compile(r"^(\d+)\s*:\s*(\d+)$")
_NUMERIC_PATTERN = re.compile(r"^[0-9]*\.?[0-9]+$")

# Single qualitative words/phrases treated as equivalent to one another when
# they appear as an entire (normalized) reference range - e.g. one pass
# transcribes a qualitative result's reference range as "negative", another
# as "none seen" or "not detected".
_QUALITATIVE_RANGE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("negative", "none seen", "not detected", "none detected"),
)
_QUALITATIVE_RANGE_INDEX: dict[str, str] = {
    word: group[0] for group in _QUALITATIVE_RANGE_GROUPS for word in group
}


def _normalize_range_text(text: str) -> str:
    """Unicode-dash-unify, collapse whitespace, casefold, and strip a
    leading "reference range:"/"ref range:"/"range:" label (real corpus:
    '"Reference Range: NEGATIVE" vs "NEGATIVE"') - the first normalization
    pass `ref_ranges_equivalent` applies before trying to strip a trailing
    unit token or parse the result's semantics."""
    unified = text.translate(_DASH_TRANSLATION)
    collapsed = re.sub(r"\s+", " ", unified.strip()).casefold()
    unlabeled = re.sub(r"^(reference range|ref\.? range|range)\s*:\s*", "", collapsed)
    # A trailing "see note N"/"see comments" pointer adds no range semantics
    # (real corpus: "Not Detected" vs "Not Detected See Note 1").
    return re.sub(r"[\s,;.]*see (note|comment)s?( \d+)?\.?$", "", unlabeled).strip()


_SEE_NOTE_RE = re.compile(r"^see (note|comment)s?\b", re.IGNORECASE)


def _is_range_pointer(text: str | None) -> bool:
    """A "See Note 2"-style pointer carries no range semantics of its own -
    treated as absent (the note's content lives in narrative_findings)."""
    return text is not None and bool(_SEE_NOTE_RE.match(text.strip()))


def _normalize_unit_token(text: str) -> str:
    return re.sub(r"\s+", "", text.strip()).casefold()


def _strip_trailing_unit(normalized_text: str, *units: str | None) -> str:
    """Strip a trailing unit token from an already-`_normalize_range_text`-ed
    string, when that token matches one of `units` (either pass's own
    printed unit) or any known `UNIT_SYNONYMS` spelling - e.g. `"<20 units"`
    -> `"<20"`, `"3.80 - 5.10 million/ul"` -> `"3.80 - 5.10"`. Tried
    longest-candidate-first so a multi-word unit isn't left partially
    stripped. A no-match is a no-op."""
    candidates = {_normalize_unit_token(u) for u in units if u} | {
        _normalize_unit_token(u) for u in _UNIT_SYNONYM_MEMBERS
    }
    for candidate in sorted((c for c in candidates if c), key=len, reverse=True):
        suffix = f" {candidate}"
        if normalized_text.endswith(suffix):
            return normalized_text[: -len(suffix)].strip()
    return normalized_text


def _parse_number_or_titer(text: str) -> float | Fraction | None:
    text = text.strip()
    titer_match = _TITER_VALUE_PATTERN.match(text)
    if titer_match:
        return Fraction(int(titer_match.group(1)), int(titer_match.group(2)))
    if _NUMERIC_PATTERN.match(text):
        return float(text)
    return None


def _parse_ref_semantics(normalized_text: str) -> tuple[object, ...] | None:
    """Parse an already-normalized (dash-unified/casefolded/unit-stripped)
    reference-range string into a semantic shape, or `None` if it doesn't
    match any recognized form - a numeric range, a threshold (`<20`,
    `<=20`, `>59`, a titer threshold like `>=1:80`), or a single
    qualitative word/phrase equivalence class."""
    if not normalized_text:
        return None
    if normalized_text in _QUALITATIVE_RANGE_INDEX:
        return ("qual", _QUALITATIVE_RANGE_INDEX[normalized_text])
    threshold_match = _THRESHOLD_PATTERN.match(normalized_text)
    if threshold_match:
        op, raw_value = threshold_match.groups()
        value = _parse_number_or_titer(raw_value)
        if value is None:
            return None
        return ("threshold", op, value)
    range_match = _RANGE_PATTERN.match(normalized_text)
    if range_match:
        return ("range", float(range_match.group(1)), float(range_match.group(2)))
    return None


_RANGE_TOKEN_RE = re.compile(
    r"(?:<=|>=|<|>)\s*\d+(?:\.\d+)?(?::\d+)?"  # thresholds, incl. titer thresholds
    r"|\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?"  # low-high pairs
)


def _multi_tier_range_tokens(normalized_text: str) -> list[str]:
    """The ordered numeric range tokens inside a multi-tier conditional
    reference set (real corpus: progesterone's phase-dependent tiers -
    "Follicular <1.0; Luteal 2.6-21.5; ... 3rd 52.0-302.0"). Tier LABELS are
    prose the two extractors word differently ("Postmenopausal" vs
    "Post menopausal"); the numeric sequence is the semantics.
    """
    return [re.sub(r"\s+", "", tok) for tok in _RANGE_TOKEN_RE.findall(normalized_text)]


def ref_ranges_equivalent(
    a_raw: str | None,
    b_raw: str | None,
    *,
    a_unit: str | None = None,
    b_unit: str | None = None,
) -> bool:
    """Semantic equivalence of two printed reference ranges (feature/
    semantic-compare) - the ref_range_mismatch false-positive family (real
    corpus: 783 of 1159 queued rows), e.g. `"<20"` vs. `"<20 Units"`, or
    `"3.80-5.10"` vs. `"3.80 - 5.10 Million/uL"`.

    Both None/empty -> equivalent (nothing printed by either pass); a
    "See Note N"-style pointer is treated as empty (no range semantics of
    its own). Exactly one side empty -> NOT equivalent here, but the
    caller downgrades that shape to the non-disagreement reason
    `ref_range_single_source` when the provided side parses (a
    completeness difference, not a conflict). Otherwise: normalize both
    (unicode dashes -> '-', collapse whitespace, casefold, strip a trailing
    unit token that matches either pass's own unit or a known synonym), then
    parse each side's semantics (numeric range / threshold / titer-threshold
    / qualitative word). Equivalent iff both parse and their parsed
    semantics are equal (numeric tolerance 0) - ONLY when BOTH sides fail to
    parse does this fall back to normalized-string equality; one side
    parsing and the other not is treated as a real difference.
    """
    a_empty = a_raw is None or not a_raw.strip() or _is_range_pointer(a_raw)
    b_empty = b_raw is None or not b_raw.strip() or _is_range_pointer(b_raw)
    if a_empty and b_empty:
        return True
    if a_empty != b_empty:
        return False

    assert a_raw is not None and b_raw is not None  # both non-empty, per the checks above
    a_norm = _strip_trailing_unit(_normalize_range_text(a_raw), a_unit, b_unit)
    b_norm = _strip_trailing_unit(_normalize_range_text(b_raw), a_unit, b_unit)

    a_parsed = _parse_ref_semantics(a_norm)
    b_parsed = _parse_ref_semantics(b_norm)
    if a_parsed is not None and b_parsed is not None:
        return a_parsed == b_parsed
    if a_parsed is None and b_parsed is None:
        if a_norm == b_norm:
            return True
        # Multi-tier conditional sets: compare the ordered numeric range
        # sequence; two+ identical ranges in identical order is equivalence
        # regardless of how the tier labels are worded.
        a_tokens = _multi_tier_range_tokens(a_norm)
        b_tokens = _multi_tier_range_tokens(b_norm)
        return len(a_tokens) >= 2 and a_tokens == b_tokens
    return False


def units_equivalent(a_raw: str | None, b_raw: str | None) -> bool:
    """Semantic equivalence of two printed units: equal once
    casefold/whitespace-normalized, or both members of the same
    `labs.validate.UNIT_SYNONYMS` spelling family (e.g. "Million/uL" vs.
    "M/uL", or the TSH "mIU/L"/"uIU/mL" family)."""
    a_norm = _normalize_str(a_raw)
    b_norm = _normalize_str(b_raw)
    if a_norm == b_norm:
        return True
    if a_norm is None or b_norm is None:
        return False
    a_canon = canonical_unit(a_raw)
    b_canon = canonical_unit(b_raw)
    return a_canon is not None and a_canon == b_canon


_FLAG_WORD_TO_CODE: dict[str, str] = {
    "high": "H",
    "h": "H",
    "low": "L",
    "l": "L",
    "abnormal": "A",
    "a": "A",
    "critical high": "HH",
    "hh": "HH",
    "critical low": "LL",
    "ll": "LL",
}
# Explicitly-unflagged spellings - equivalent to "absent" (module docstring's
# flags_equivalent note), never to an abnormal code.
_UNFLAGGED_WORDS = frozenset({"n", "normal"})


# Single letters LabCorp prints as performing-site/footnote markers, NOT
# result flags (real corpus: 'B'/'C'/'D'/'F' vs None on 127 rows). 'A' is
# deliberately NOT here: it is a legitimate Abnormal code, so an
# 'A'-vs-absent pair stays a real, human-reviewable disagreement even
# though some reports use 'A' as a footnote letter too - safety first.
_FOOTNOTE_LETTERS = frozenset({"b", "c", "d", "e", "f", "g"})


def _resolve_flag_token(raw: str | None) -> str:
    """`raw`'s canonical flag code, `""` for absent/unflagged, or the
    (casefolded) raw token itself if unrecognized - so two unrecognized-but-
    identical spellings still compare equal without silently treating them
    as unflagged.

    A comma/space-separated multi-token field is resolved token-wise
    ('High, H' == 'High' == 'H'): recognized flag codes win; footnote
    letters and bare digits carry no flag information and are dropped.
    """
    if raw is None:
        return ""
    text = re.sub(r"\s+", " ", raw.strip()).casefold()
    if not text or text in _UNFLAGGED_WORDS:
        return ""
    if text in _FLAG_WORD_TO_CODE:
        return _FLAG_WORD_TO_CODE[text]
    codes: set[str] = set()
    informative: list[str] = []
    for token in re.split(r"[,;/ ]+", text):
        if not token or token in _UNFLAGGED_WORDS:
            continue
        if token in _FLAG_WORD_TO_CODE:
            codes.add(_FLAG_WORD_TO_CODE[token])
        elif token in _FOOTNOTE_LETTERS or token.isdigit():
            continue  # site/footnote marker - no flag information
        else:
            informative.append(token)
    if codes:
        return max(codes, key=len) if len(codes) > 1 else next(iter(codes))
    return " ".join(informative)


def flags_equivalent(a_raw: str | None, b_raw: str | None) -> bool:
    """Semantic equivalence of two printed abnormal-value flags (feature/
    semantic-compare): `None`/`""`/whitespace-only and the words `"N"`/
    `"normal"` are all "absent" and equivalent to EACH OTHER; word forms
    (`"high"`, `"low"`, `"abnormal"`, `"critical high"`, `"critical low"`)
    are equivalent to their letter codes, case-insensitively. Absent vs. an
    actual abnormal code (H/L/A/HH/LL) is deliberately NOT equivalent - one
    pass saw an abnormal flag the other pass missed entirely, and that stays
    a real, human-reviewable disagreement."""
    return _resolve_flag_token(a_raw) == _resolve_flag_token(b_raw)


# Trailing sentence-fragment tokens `clean_result_name` strips (module
# docstring's RESCUE-pass note): a verb an extractor prompt might still
# tack onto a result name when transcribing a sentence like "10-year
# probability of hip fracture is 12%" without splitting the value off
# first, or trailing punctuation left over from a colon-terminated label.
# Applied repeatedly (a name could end in more than one, e.g. "... is:")
# until nothing more matches.
_TRAILING_FRAGMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\s+is$", re.IGNORECASE),
    re.compile(r"\s+was$", re.IGNORECASE),
    re.compile(r"\s+were$", re.IGNORECASE),
    re.compile(r"\s*:$"),
    re.compile(r"\s*-$"),
)


def clean_result_name(name_raw: str) -> str:
    """Strip a trailing sentence-fragment verb/punctuation and collapse
    internal whitespace (module docstring's RESCUE-pass note). Applied to
    EVERY extracted result name at the top of `reconcile()`, not just
    rescued ones, so canonicalize/grouping/pairing/audit never see a raw
    fragment. Never returns an empty string - falls back to the original
    (whitespace-collapsed) name if stripping would leave nothing.
    """
    cleaned = re.sub(r"\s+", " ", name_raw.strip())
    if not cleaned:
        return name_raw
    changed = True
    while changed:
        changed = False
        for pattern in _TRAILING_FRAGMENT_PATTERNS:
            stripped = pattern.sub("", cleaned).strip()
            if stripped and stripped != cleaned:
                cleaned = stripped
                changed = True
    return cleaned


def _clean_results(results: Sequence[ExtractedResult]) -> list[ExtractedResult]:
    """`clean_result_name`, applied to a whole pass's `results` list."""
    cleaned: list[ExtractedResult] = []
    for row in results:
        name = clean_result_name(row.name_raw)
        cleaned.append(row if name == row.name_raw else row.model_copy(update={"name_raw": name}))
    return cleaned


def _stored_name(*name_raws: str) -> str | None:
    """The canonical name a row may be PERSISTED under, or `None` to store
    the raw name verbatim.

    `ReconciledRow.canonical_name` used to take `canonicalize(...)`'s
    permissive result (suffix-strip retry, score-suffix rule) - the same
    over-merge `labs.validate`'s "Matching vs. renaming" docstring
    describes for `adoc labs-recanonicalize`: a site-prefixed DEXA score
    ("LEFT HIP Total Z-Score") was stored as bare "Z-score" AT INGESTION,
    silently discarding the site. Only an EXACT alias match
    (`canonical_rename_target`) may name a persisted row; permissive
    `canonicalize` remains correct for everything read-time in this module
    (grouping/pairing via `_match_key`, spec lookup for
    `validate_row`/`trend_outlier` candidates).
    """
    for raw in name_raws:
        target = canonical_rename_target(raw, raw)
        if target is not None:
            return target
    return None


def _match_key(name_raw: str) -> str:
    canonical = canonicalize(name_raw)
    if canonical:
        return canonical
    return re.sub(r"[^a-z0-9]+", "", name_raw.lower())


def _group_by_key(results: Sequence[ExtractedResult]) -> dict[str, list[ExtractedResult]]:
    groups: dict[str, list[ExtractedResult]] = defaultdict(list)
    for row in results:
        groups[_match_key(row.name_raw)].append(row)
    for rows in groups.values():
        rows.sort(key=lambda r: r.page)
    return groups


def _pair_rows(
    a_rows: list[ExtractedResult], b_rows: list[ExtractedResult]
) -> list[tuple[ExtractedResult | None, ExtractedResult | None]]:
    """Greedy page-tolerant pairing within one analyte-name group.

    Page distance is still the primary criterion (unchanged), but the
    specimen now breaks ties: when two `b` candidates are equally close in
    page to `a`, the one reporting the SAME specimen as `a` wins. This
    matters for a document that legitimately prints the same analyte name
    under two different specimens close together (e.g. a combined
    urinalysis + serum panel) — it keeps a same-specimen pass-A/pass-B pair
    from being split apart by an equidistant, different-specimen row. A
    genuine specimen disagreement between the two passes' readings of what
    IS otherwise the same result is still paired (so it can be flagged
    `specimen_mismatch` in `_reconcile_matched_pair`) — this only re-orders
    which candidate wins a page-distance tie, it never refuses to pair.
    """
    pairs: list[tuple[ExtractedResult | None, ExtractedResult | None]] = []
    remaining_b = list(b_rows)
    for a in a_rows:
        best_idx: int | None = None
        for idx, b in enumerate(remaining_b):
            distance = abs(b.page - a.page)
            if distance > PAGE_TOLERANCE:
                continue
            if best_idx is None:
                best_idx = idx
                continue
            current = remaining_b[best_idx]
            current_distance = abs(current.page - a.page)
            if distance < current_distance or (
                distance == current_distance
                and b.specimen == a.specimen
                and current.specimen != a.specimen
            ):
                best_idx = idx
        if best_idx is not None:
            pairs.append((a, remaining_b.pop(best_idx)))
        else:
            pairs.append((a, None))
    pairs.extend((None, b) for b in remaining_b)
    return pairs


def _units_rescue_compatible(a_unit: str | None, b_unit: str | None) -> bool:
    """Compatible for the RESCUE pass (module docstring): equal once
    normalized, or one side simply didn't state a unit at all. A real
    mismatch (e.g. "mg/dL" vs "ng/mL") still blocks the rescue - this is
    what keeps two different-analyte same-value coincidences on one page
    from being wrongly rescued together (module docstring's residual-risk
    note)."""
    na, nb = _normalize_str(a_unit), _normalize_str(b_unit)
    return na is None or nb is None or na == nb


def _specimen_rescue_compatible(a_specimen: str, b_specimen: str) -> bool:
    return a_specimen == b_specimen or a_specimen == "unknown" or b_specimen == "unknown"


def _rescue_compatible(a: ExtractedResult, b: ExtractedResult) -> bool:
    """The RESCUE pass's compatibility test (module docstring): same page
    (+/- `PAGE_TOLERANCE`), identical value or identical (normalized)
    value_text, a compatible unit, and the same specimen-or-unknown. Never
    considers name - that is the entire point of this pass."""
    if abs(a.page - b.page) > PAGE_TOLERANCE:
        return False
    value_match = a.value is not None and b.value is not None and a.value == b.value
    value_text_match = (
        a.value_text is not None
        and b.value_text is not None
        and _normalize_str(a.value_text) == _normalize_str(b.value_text)
    )
    if not (value_match or value_text_match):
        return False
    if not _units_rescue_compatible(a.unit_raw, b.unit_raw):
        return False
    return _specimen_rescue_compatible(a.specimen, b.specimen)


def _rescue_pair(
    leftover_a: list[ExtractedResult], leftover_b: list[ExtractedResult]
) -> tuple[
    list[tuple[ExtractedResult, ExtractedResult]], list[ExtractedResult], list[ExtractedResult]
]:
    """Greedy page-tolerant pairing of RESCUE candidates ACROSS different
    name-groups (module docstring) - the counterpart to `_pair_rows`'s
    within-group pairing, run over what it left unmatched. Returns
    `(rescued_pairs, still_unmatched_a, still_unmatched_b)`."""
    remaining_b = list(leftover_b)
    rescued: list[tuple[ExtractedResult, ExtractedResult]] = []
    still_a: list[ExtractedResult] = []
    for a in leftover_a:
        best_idx: int | None = None
        for idx, b in enumerate(remaining_b):
            if not _rescue_compatible(a, b):
                continue
            if best_idx is None or abs(b.page - a.page) < abs(remaining_b[best_idx].page - a.page):
                best_idx = idx
        if best_idx is not None:
            rescued.append((a, remaining_b.pop(best_idx)))
        else:
            still_a.append(a)
    return rescued, still_a, remaining_b


def _longer_name(name_a: str, name_b: str) -> str:
    """The LONGER/more specific of two (already-cleaned) result names
    (module docstring's RESCUE-pass note) - ties keep `name_a`."""
    return name_a if len(name_a) >= len(name_b) else name_b


# D5: a rescued pair's residual risk is two genuinely DIFFERENT analytes
# that happen to print the identical value on the same page (module
# docstring's accepted-risk note) - rescue still pairs them (never leaves
# them stranded as twin single_pass rows), but a pair whose names share NO
# meaningful token gets a different, disagreement-bucketed reason so a
# human actually looks instead of it being bulk-OK'd alongside genuine
# same-measurement name variants.
_NAME_OVERLAP_STOPWORDS: frozenset[str] = frozenset(
    {
        # generic English function words
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "were",
        "was",
        "are",
        "not",
        # generic lab-report filler nouns - shared ONLY because both names
        # happen to be a "<analyte> <filler>" shape, not because the
        # analytes are related (e.g. "Iron Test" vs "Copper Test")
        "test",
        "tests",
        "level",
        "levels",
        "count",
        "counts",
        "panel",
        "result",
        "results",
        "value",
        "values",
    }
)


def _meaningful_name_tokens(name: str) -> set[str]:
    """Tokens of `name` (casefolded alnum runs) long enough and common
    enough to be a "meaningful" overlap signal (D5): longer than 2
    characters and not a generic English stopword - short/filler tokens
    like "of"/"is"/"the" would manufacture a false shared-token match
    between two genuinely unrelated analyte names."""
    tokens = re.findall(r"[a-z0-9]+", name.casefold())
    return {t for t in tokens if len(t) > 2 and t not in _NAME_OVERLAP_STOPWORDS}


def _names_share_a_meaningful_token(name_a: str, name_b: str) -> bool:
    return bool(_meaningful_name_tokens(name_a) & _meaningful_name_tokens(name_b))


def _reconcile_rescued_pair(
    a: ExtractedResult, b: ExtractedResult, *, doc_date: date, missing_date: bool, db: LabsDb
) -> ReconciledRow:
    """Reconcile one RESCUE-paired A/B row (module docstring).

    Deliberately does NOT run `_reconcile_matched_pair`'s cross-pass
    field-comparison gates (value/unit/ref-range/flag/specimen/confidence)
    - the rescue's OWN compatibility test (`_rescue_compatible`) already
    covers value/unit/specimen with its own, looser definitions, and
    ref_range/flag/confidence were never part of it, so comparing them
    here would manufacture disagreement reasons (`ref_range_mismatch`,
    ...) for two readings the rescue pass itself judged compatible.

    The first reason is `name_variant` when the two names share at least
    one meaningful token (`_names_share_a_meaningful_token`) - the common
    case, "the same result, just worded differently" - staying out of
    `DISAGREEMENT_REASON_PREFIXES` so it lands in the confirm queue's
    "agreed" bucket, same as before. When the names share NO token at all
    (D5: the module docstring's accepted residual risk - two different
    analytes coincidentally sharing a value/page), the reason is
    `name_variant_unverified` instead - IN `DISAGREEMENT_REASON_PREFIXES`,
    so it needs a real human look rather than a bulk OK - the pair is still
    rescued (never left stranded as twin single_pass rows), just bucketed
    honestly. Either way, whatever `validate_row`/`trend_outlier` find on
    the representative reading is appended after (the same single-source
    annotations `_reconcile_single_pass` would add). Field values (value/
    unit/ref range/flag/specimen/page) are taken from whichever of the
    two readings carries the LONGER/more specific name - the same
    reading the stored `name_raw` comes from; the other reading's full
    payload is still kept in `raw_json` for audit.
    """
    chosen_name = _longer_name(a.name_raw, b.name_raw)
    representative = a if chosen_name == a.name_raw else b
    canonical = canonicalize(a.name_raw) or canonicalize(b.name_raw)

    first_reason = (
        "name_variant"
        if _names_share_a_meaningful_token(a.name_raw, b.name_raw)
        else "name_variant_unverified"
    )
    reasons: list[str] = [first_reason]
    if missing_date:
        reasons.append("missing_date")

    candidate = _candidate_lab_result(representative, canonical=canonical, doc_date=doc_date)
    reasons.extend(issue.message for issue in validate_row(candidate))
    if (outlier := trend_outlier(db, candidate)) is not None:
        reasons.append(outlier.message)

    raw_json = json.dumps(
        {
            "pass_a": a.model_dump(mode="json"),
            "pass_b": b.model_dump(mode="json"),
            "reasons": reasons,
            "name_variant": {"pass_a_name": a.name_raw, "pass_b_name": b.name_raw},
        }
    )
    return ReconciledRow(
        name_raw=chosen_name,
        canonical_name=_stored_name(chosen_name, a.name_raw, b.name_raw),
        date=doc_date,
        value=representative.value,
        value_text=representative.value_text,
        unit_raw=representative.unit_raw,
        ref_range_raw=representative.ref_range_raw,
        flag_raw=representative.flag_raw,
        specimen=representative.specimen,
        source_page=representative.page,
        status="pending",
        reasons=reasons,
        raw_json=raw_json,
    )


# Collection dates outside this window are extraction misreads (a real
# document seen here carried year 0906) — treated as missing so the row
# queues and a human supplies the true date from the page image.
_EARLIEST_PLAUSIBLE_DATE = date(1900, 1, 1)


def _plausible(d: date | None) -> date | None:
    if d is None:
        return None
    if d < _EARLIEST_PLAUSIBLE_DATE or d.year > date.today().year + 1:
        return None
    return d


def _doc_date(pass_a: DocumentExtraction, pass_b: DocumentExtraction) -> date | None:
    return (
        _plausible(pass_a.collection_date)
        or _plausible(pass_a.report_date)
        or _plausible(pass_b.collection_date)
        or _plausible(pass_b.report_date)
    )


def _candidate_lab_result(
    row: ExtractedResult, *, canonical: str | None, doc_date: date
) -> LabResult:
    """A throwaway `LabResult` used only to run `validate_row`/`trend_outlier`
    for AUTO-gating - never persisted as-is (`source_doc` is a placeholder;
    `ingest.pipeline` builds the real, insertable `LabResult`).
    """
    ref_low, ref_high = parse_ref_range(row.ref_range_raw)
    return LabResult(
        date=doc_date,
        name=canonical or row.name_raw,
        name_raw=row.name_raw,
        value=row.value,
        value_text=row.value_text,
        ucum_unit=row.unit_raw,
        ref_low=ref_low,
        ref_high=ref_high,
        ref_text=row.ref_range_raw,
        flag=parse_flag(row.flag_raw),
        specimen=row.specimen,
        source_doc=_PLACEHOLDER_SHA,
        source_page=row.page,
        raw_json="{}",
    )


def _reconcile_single_pass(
    a: ExtractedResult | None,
    b: ExtractedResult | None,
    *,
    doc_date: date,
    missing_date: bool,
    db: LabsDb,
) -> ReconciledRow:
    present = a if a is not None else b
    assert present is not None, "_reconcile_single_pass requires exactly one of a/b"

    canonical = canonicalize(present.name_raw)
    reasons = ["single_pass"]
    if missing_date:
        reasons.append("missing_date")

    candidate = _candidate_lab_result(present, canonical=canonical, doc_date=doc_date)
    reasons.extend(issue.message for issue in validate_row(candidate))
    if (outlier := trend_outlier(db, candidate)) is not None:
        reasons.append(outlier.message)

    raw_json = json.dumps(
        {
            "pass_a": a.model_dump(mode="json") if a is not None else None,
            "pass_b": b.model_dump(mode="json") if b is not None else None,
            "reasons": reasons,
        }
    )
    return ReconciledRow(
        name_raw=present.name_raw,
        canonical_name=_stored_name(present.name_raw),
        date=doc_date,
        value=present.value,
        value_text=present.value_text,
        unit_raw=present.unit_raw,
        ref_range_raw=present.ref_range_raw,
        flag_raw=present.flag_raw,
        specimen=present.specimen,
        source_page=present.page,
        status="pending",
        reasons=reasons,
        raw_json=raw_json,
    )


@dataclass(frozen=True)
class _PairEvaluation:
    """The full outcome of comparing one matched A/B pair (`_evaluate_pair`):
    the reason list (for `raw_json`/audit - includes non-blocking
    annotations like a sub-decimal-signature trend spike) plus the
    AUTO/PENDING gate boolean, which needs a little more than "reasons is
    empty" (an annotation-only trend spike on an otherwise-fully-agreeing
    pair still AUTOs - see `reconcile.py`'s module docstring)."""

    reasons: list[str]
    gates_pass: bool


def _evaluate_pair(
    a: ExtractedResult, b: ExtractedResult, *, doc_date: date, missing_date: bool, db: LabsDb
) -> _PairEvaluation:
    """Shared core of `_reconcile_matched_pair` (real-time ingest) and
    `labs.reclassify.reclassify_pending` (retro-reclassification of
    already-PENDING rows under the current comparators/specs) - see
    `compute_pair_reasons`, the pure `-> list[str]` wrapper reclassify
    calls directly.

    ref_range/unit/flag comparisons use the semantic comparators above
    (`ref_ranges_equivalent`/`units_equivalent`/`flags_equivalent`) in place
    of literal (`_normalize_str`-only) equality - value/value_text/specimen/
    confidence are unchanged, per those comparators' own docstrings.
    """
    reasons: list[str] = ["missing_date"] if missing_date else []

    value_match = a.value == b.value
    value_text_match = _normalize_str(a.value_text) == _normalize_str(b.value_text)
    unit_match = units_equivalent(a.unit_raw, b.unit_raw)
    ref_match = ref_ranges_equivalent(
        a.ref_range_raw, b.ref_range_raw, a_unit=a.unit_raw, b_unit=b.unit_raw
    )
    flag_match = flags_equivalent(a.flag_raw, b.flag_raw)
    specimen_match = a.specimen == b.specimen
    confidence_ok = a.confidence == "high" and b.confidence == "high"

    if not value_match:
        reasons.append(f"value_mismatch: {a.value!r} vs {b.value!r}")
    if not value_text_match:
        reasons.append(f"value_text_mismatch: {a.value_text!r} vs {b.value_text!r}")
    if not unit_match:
        reasons.append(f"unit_mismatch: {a.unit_raw!r} vs {b.unit_raw!r}")
    if not ref_match:
        a_ref_empty = not (a.ref_range_raw or "").strip() or _is_range_pointer(a.ref_range_raw)
        b_ref_empty = not (b.ref_range_raw or "").strip() or _is_range_pointer(b.ref_range_raw)
        if a_ref_empty != b_ref_empty:
            # Exactly one pass transcribed a range while value/unit agreed:
            # a completeness difference, not a conflict - lands in the
            # agreed (bulk-OK) bucket rather than "needs your eyes".
            provided = a.ref_range_raw if b_ref_empty else b.ref_range_raw
            reasons.append(f"ref_range_single_source: {provided!r}")
        else:
            reasons.append(f"ref_range_mismatch: {a.ref_range_raw!r} vs {b.ref_range_raw!r}")
    if not flag_match:
        reasons.append(f"flag_mismatch: {a.flag_raw!r} vs {b.flag_raw!r}")
    if not specimen_match:
        reasons.append(f"specimen_mismatch: {a.specimen!r} vs {b.specimen!r}")
    if a.confidence != "high":
        reasons.append(f"pass_a_confidence:{a.confidence}")
    if b.confidence != "high":
        reasons.append(f"pass_b_confidence:{b.confidence}")

    # Validate/trend-check BOTH passes' readings (not just pass A's) so a
    # decimal-shift misread in *either* pass (PLAN.md's potassium "4.1 vs
    # 41" example) is caught even when the other pass got it right.
    canonical = canonicalize(a.name_raw) or canonicalize(b.name_raw)
    candidate_a = _candidate_lab_result(a, canonical=canonical, doc_date=doc_date)
    candidate_b = _candidate_lab_result(b, canonical=canonical, doc_date=doc_date)
    issues = validate_row(candidate_a) + validate_row(candidate_b)
    reasons.extend(issue.message for issue in issues)
    # `trend_deviation` is a `labs.sqlite` query (per candidate) when no
    # pre-fetched series is supplied — compute it ONCE per candidate here
    # and derive both the outlier gate (`outlier_issue_from_deviation`)
    # and the raw ratio (for the decimal-signature check below) from that
    # single result, instead of querying once via `trend_outlier` and
    # again via a direct `trend_deviation` call for the same row.
    deviation_a = trend_deviation(db, candidate_a)
    deviation_b = trend_deviation(db, candidate_b)
    outliers = [
        outlier
        for outlier in (
            outlier_issue_from_deviation(candidate_a, deviation_a),
            outlier_issue_from_deviation(candidate_b, deviation_b),
        )
        if outlier is not None
    ]
    reasons.extend(outlier.message for outlier in outliers)

    # Trend spikes on a cross-pass-AGREED value are treated as real
    # physiology (this patient spikes frequently; agreement is the stronger
    # extraction-correctness signal) — they annotate the row but do not
    # block AUTO. The one exception: a >=10x-class shift, the decimal-
    # misread signature both passes could plausibly share, still queues.
    deviations = [d for d in (deviation_a, deviation_b) if d is not None]
    decimal_signature = any(d >= DECIMAL_SIGNATURE_RATIO for d in deviations)

    gates_pass = (
        not missing_date
        and value_match
        and value_text_match
        and unit_match
        and ref_match
        and flag_match
        and specimen_match
        and confidence_ok
        and not issues
        and not decimal_signature
    )
    return _PairEvaluation(reasons=reasons, gates_pass=gates_pass)


def compute_pair_reasons(
    a: ExtractedResult, b: ExtractedResult, *, doc_date: date, missing_date: bool, db: LabsDb
) -> list[str]:
    """Pure reason-list computation for one matched A/B pair, shared by
    real-time reconciliation (`_reconcile_matched_pair`) and
    `labs.reclassify.reclassify_pending`'s retro-reclassification of
    already-PENDING rows - see `_evaluate_pair`, which this wraps. Always
    recomputed against the CURRENT semantic comparators, `ANALYTE_SPECS`,
    and (via `db`) this patient's current trend history - never a
    duplicated copy of `_reconcile_matched_pair`'s logic.
    """
    return _evaluate_pair(a, b, doc_date=doc_date, missing_date=missing_date, db=db).reasons


def _reconcile_matched_pair(
    a: ExtractedResult, b: ExtractedResult, *, doc_date: date, missing_date: bool, db: LabsDb
) -> ReconciledRow:
    evaluation = _evaluate_pair(a, b, doc_date=doc_date, missing_date=missing_date, db=db)

    raw_json = json.dumps(
        {
            "pass_a": a.model_dump(mode="json"),
            "pass_b": b.model_dump(mode="json"),
            "reasons": evaluation.reasons,
        }
    )
    # Both passes agreeing on specimen is the common case, and that agreed
    # value is what carries into the persisted LabResult. When they
    # disagree the row is PENDING regardless (`specimen_mismatch` above) —
    # pass A's reading is kept as a placeholder pending human correction,
    # never presented as "the" agreed specimen.
    return ReconciledRow(
        name_raw=a.name_raw,
        canonical_name=_stored_name(a.name_raw, b.name_raw),
        date=doc_date,
        value=a.value,
        value_text=a.value_text,
        unit_raw=a.unit_raw,
        ref_range_raw=a.ref_range_raw,
        flag_raw=a.flag_raw,
        specimen=a.specimen,
        source_page=a.page,
        status="auto" if evaluation.gates_pass else "pending",
        reasons=evaluation.reasons,
        raw_json=raw_json,
    )


DISAGREEMENT_REASON_PREFIXES: tuple[str, ...] = (
    "value_mismatch",
    "value_text_mismatch",
    "unit_mismatch",
    "ref_range_mismatch",
    "flag_mismatch",
    "specimen_mismatch",
    "single_pass",
    "pass_a_confidence:",
    "pass_b_confidence:",
    "name_variant_unverified",
)
"""Reason prefixes (see the module docstring's gate list) that reflect a
genuine cross-pass disagreement, or a pass that couldn't even be
compared against the other - as opposed to a single-source
validation/dating issue that both passes' readings share (unknown
analyte, missing date, an out-of-bounds value, a trend outlier, ...).
`name_variant_unverified` (D5) is the RESCUE pass's zero-name-overlap
case: still paired (never left as stranded twins), but with no shared
token between the two names to back up "same result, different wording",
so it needs a real look rather than the bulk-OK `name_variant` bucket.

The confirm-queue UI (`web.routes.confirm`) buckets PENDING rows on this
distinction: a row with none of these prefixes among its reasons only
needs a quick human OK ("models agreed"); a row with any of them needs a
real look ("models disagreed") - see `row_is_agreed`.
"""


def is_disagreement_reason(reason: str) -> bool:
    """True if `reason` (one entry of a `ReconciledRow`/pending row's
    `reasons`) reflects a cross-pass disagreement rather than a
    single-source issue - see `DISAGREEMENT_REASON_PREFIXES`."""
    return reason.startswith(DISAGREEMENT_REASON_PREFIXES)


def row_is_agreed(reasons: Sequence[str]) -> bool:
    """True iff none of `reasons` reflect a cross-pass disagreement.

    An "agreed" PENDING row only failed a single-source deterministic
    check that both extraction passes' readings shared - unknown
    analyte, missing date, an out-of-bounds value, a trend outlier, and
    so on - so a quick human OK is enough. Anything else (a value/unit/
    reference-range/flag mismatch between the two passes, a row only one
    pass could read at all, or either pass reporting low confidence)
    needs genuine cross-model reconciliation by a human.
    """
    return not any(is_disagreement_reason(r) for r in reasons)


def reconcile(
    pass_a: DocumentExtraction, pass_b: DocumentExtraction, db: LabsDb
) -> list[ReconciledRow]:
    """Reconcile two independent extraction passes into per-analyte rows.

    See the module docstring for the full AUTO-gate list, and its
    "RESCUE pass" note for what happens to rows still unmatched after the
    normal per-name-group pairing below. `db` is used read-only, for
    `trend_outlier`'s comparison against this patient's own prior values
    of the same analyte.
    """
    resolved_date = _doc_date(pass_a, pass_b)
    missing_date = resolved_date is None
    doc_date = resolved_date or date.today()

    # Every extracted name is cleaned before anything else touches it
    # (module docstring) - canonicalize/grouping/pairing/audit only ever
    # see `clean_result_name`'s output.
    a_results = _clean_results(pass_a.results)
    b_results = _clean_results(pass_b.results)

    groups_a = _group_by_key(a_results)
    groups_b = _group_by_key(b_results)

    rows: list[ReconciledRow] = []
    leftover_a: list[ExtractedResult] = []
    leftover_b: list[ExtractedResult] = []
    for key in sorted(set(groups_a) | set(groups_b)):
        for a, b in _pair_rows(groups_a.get(key, []), groups_b.get(key, [])):
            if a is not None and b is not None:
                rows.append(
                    _reconcile_matched_pair(
                        a, b, doc_date=doc_date, missing_date=missing_date, db=db
                    )
                )
            elif a is not None:
                leftover_a.append(a)
            else:
                assert b is not None  # _pair_rows never yields (None, None)
                leftover_b.append(b)

    # RESCUE pass (module docstring): try to pair what's left across
    # different name-groups before giving up and calling each one
    # single_pass.
    rescued_pairs, still_a, still_b = _rescue_pair(leftover_a, leftover_b)
    for a, b in rescued_pairs:
        rows.append(
            _reconcile_rescued_pair(a, b, doc_date=doc_date, missing_date=missing_date, db=db)
        )
    for a in still_a:
        rows.append(
            _reconcile_single_pass(a, None, doc_date=doc_date, missing_date=missing_date, db=db)
        )
    for b in still_b:
        rows.append(
            _reconcile_single_pass(None, b, doc_date=doc_date, missing_date=missing_date, db=db)
        )
    return rows

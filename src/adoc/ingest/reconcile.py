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
  3. unit     - `unit_raw` is equal after case/whitespace normalization.
  4. ref      - `ref_range_raw` is equal after case/whitespace
     normalization.
  5. flag     - `flag_raw` is equal after case/whitespace normalization.
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

**RESCUE pass** (queue-ergonomics slice item 3b - root cause of the twin
single-pass-row problem: the two extraction passes sometimes name the SAME
measurement differently, e.g. "FRAX 10-year probability of hip fracture"
vs. a sentence-fragment "10-year probability of hip fracture is" - so they
never land in the same `_match_key` group and never get a chance at
`_pair_rows` in the first place). After the normal per-group pairing
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
from datetime import date
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from adoc.ingest.schema import DocumentExtraction, ExtractedResult
from adoc.labs.models import LabFlag, LabResult, Specimen
from adoc.labs.validate import (
    DECIMAL_SIGNATURE_RATIO,
    canonicalize,
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
    ...) for two readings the rescue pass itself judged compatible. This
    always queues PENDING with `name_variant` first, plus whatever
    `validate_row`/`trend_outlier` find on the representative reading
    (the same single-source annotations `_reconcile_single_pass` would
    add) - never a `DISAGREEMENT_REASON_PREFIXES` reason - so it always
    lands in the confirm queue's "agreed" bucket. Field values (value/
    unit/ref range/flag/specimen/page) are taken from whichever of the
    two readings carries the LONGER/more specific name - the same
    reading the stored `name_raw` comes from; the other reading's full
    payload is still kept in `raw_json` for audit.
    """
    chosen_name = _longer_name(a.name_raw, b.name_raw)
    representative = a if chosen_name == a.name_raw else b
    canonical = canonicalize(a.name_raw) or canonicalize(b.name_raw)

    reasons: list[str] = ["name_variant"]
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
        canonical_name=canonical,
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
        canonical_name=canonical,
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


def _reconcile_matched_pair(
    a: ExtractedResult, b: ExtractedResult, *, doc_date: date, missing_date: bool, db: LabsDb
) -> ReconciledRow:
    reasons: list[str] = ["missing_date"] if missing_date else []

    value_match = a.value == b.value
    value_text_match = _normalize_str(a.value_text) == _normalize_str(b.value_text)
    unit_match = _normalize_str(a.unit_raw) == _normalize_str(b.unit_raw)
    ref_match = _normalize_str(a.ref_range_raw) == _normalize_str(b.ref_range_raw)
    flag_match = _normalize_str(a.flag_raw) == _normalize_str(b.flag_raw)
    specimen_match = a.specimen == b.specimen
    confidence_ok = a.confidence == "high" and b.confidence == "high"

    if not value_match:
        reasons.append(f"value_mismatch: {a.value!r} vs {b.value!r}")
    if not value_text_match:
        reasons.append(f"value_text_mismatch: {a.value_text!r} vs {b.value_text!r}")
    if not unit_match:
        reasons.append(f"unit_mismatch: {a.unit_raw!r} vs {b.unit_raw!r}")
    if not ref_match:
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
    outliers = [
        outlier
        for outlier in (trend_outlier(db, candidate_a), trend_outlier(db, candidate_b))
        if outlier is not None
    ]
    reasons.extend(outlier.message for outlier in outliers)

    # Trend spikes on a cross-pass-AGREED value are treated as real
    # physiology (this patient spikes frequently; agreement is the stronger
    # extraction-correctness signal) — they annotate the row but do not
    # block AUTO. The one exception: a >=10x-class shift, the decimal-
    # misread signature both passes could plausibly share, still queues.
    deviations = [
        d
        for d in (trend_deviation(db, candidate_a), trend_deviation(db, candidate_b))
        if d is not None
    ]
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

    raw_json = json.dumps(
        {
            "pass_a": a.model_dump(mode="json"),
            "pass_b": b.model_dump(mode="json"),
            "reasons": reasons,
        }
    )
    # Both passes agreeing on specimen is the common case, and that agreed
    # value is what carries into the persisted LabResult. When they
    # disagree the row is PENDING regardless (`specimen_mismatch` above) —
    # pass A's reading is kept as a placeholder pending human correction,
    # never presented as "the" agreed specimen.
    return ReconciledRow(
        name_raw=a.name_raw,
        canonical_name=canonical,
        date=doc_date,
        value=a.value,
        value_text=a.value_text,
        unit_raw=a.unit_raw,
        ref_range_raw=a.ref_range_raw,
        flag_raw=a.flag_raw,
        specimen=a.specimen,
        source_page=a.page,
        status="auto" if gates_pass else "pending",
        reasons=reasons,
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
)
"""Reason prefixes (see the module docstring's gate list) that reflect a
genuine cross-pass disagreement, or a pass that couldn't even be
compared against the other - as opposed to a single-source
validation/dating issue that both passes' readings share (unknown
analyte, missing date, an out-of-bounds value, a trend outlier, ...).

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

"""Is this result abnormal? A comparison, not a flag (ADR 0051).

Every layer that asks "is this analyte high/low/normal" asked the lab's
`flag` column and nothing else. Measured in production 2026-09-08:

    2079 rows, 187 flagged   (H 120, L 21, A 46)
    1892 unflagged           - 91% of the record

So the question was being answered from 9% of the data, and the other 91%
read as "not abnormal" rather than as "nobody said". The damage was
everywhere at once:

- `knowledge.criteria` scored 17 rules whose analyte was present and whose
  condition could never be met.
- ADR 0044's `lab_phenotype` derived **1** HPO term from 461 analytes, so
  the phenotype engines never received the serology that ADR was written to
  give them.
- `retirement.evaluate_rule_out`'s `normal` operator returned **True for an
  empty flag**, reporting "is within the lab's reference range" without
  having looked at a range.
- `reason.context`'s Abnormal section — what every model call sees — is
  built from the same predicate, and renders no reference range at all. The
  model was not outperforming the deterministic layer here; both were
  reading the same impoverished view.

## Why the ranges were missing

`labs.db._parse_ref_range` is anchored and rejects a trailing unit:

    '140-400'      -> (140.0, 400.0)
    '16-232 ng/mL' -> (None, None)

920 unflagged rows carry `ref_text` and no parsed bounds for exactly that
reason.

## The rule

Flag first — a lab that says `H` has applied its own judgement and knows
more about its assay than this code does. Then the numeric bounds. Then
`ref_text`, parsed at read time so historical rows are fixed without a
migration.

**`None` means cannot-tell and is never "normal".** That distinction is the
whole point: an unflagged row with no usable range says nothing, and
treating it as normal is what let a rule-out fire on absent information.
"""

from __future__ import annotations

import re
from typing import Literal

from adoc.labs.models import LabResult, flag_is_high, flag_is_low

Position = Literal["high", "low", "normal"]

# `16-232 ng/mL`, `140-400`, `0.01 - 2.99`. The trailing unit is optional and
# ignored: the caller compares against `LabResult.ucum_unit`, and a range
# whose unit disagrees with the value's is a different bug (see
# `criteria._count_threshold_item`) that this parser must not paper over.
_RANGE_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*[-–—]\s*([0-9]*\.?[0-9]+)\s*(?:[A-Za-z/%µ°^0-9.\s]*)$"
)
# `<5`, `<=5`, `> 60` — one-sided ranges, common for antibodies and markers.
_UPPER_RE = re.compile(r"^\s*[<≤]\s*=?\s*([0-9]*\.?[0-9]+)")
_LOWER_RE = re.compile(r"^\s*[>≥]\s*=?\s*([0-9]*\.?[0-9]+)")


def parse_reference_range(text: str | None) -> tuple[float | None, float | None]:
    """`(low, high)` from a reference-range string, either bound optional.

    Widened from `labs.db._parse_ref_range`, which is anchored with `$` and
    so rejects every range carrying its unit — the single reason 920 stored
    rows have a `ref_text` and no bounds.
    """
    if not text or not text.strip():
        return None, None
    match = _RANGE_RE.match(text)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        # A reversed range is a parse artefact, not a fact about the assay.
        return (low, high) if low <= high else (high, low)
    upper = _UPPER_RE.match(text)
    if upper:
        return None, float(upper.group(1))
    lower = _LOWER_RE.match(text)
    if lower:
        return float(lower.group(1)), None
    return None, None


def reference_bounds(row: LabResult) -> tuple[float | None, float | None]:
    """The row's numeric bounds, parsing `ref_text` when the columns are
    empty.

    At read time rather than by migration, so the 920 rows already stored
    with unparsed text are fixed the moment this ships.
    """
    if row.ref_low is not None or row.ref_high is not None:
        return row.ref_low, row.ref_high
    return parse_reference_range(row.ref_text)


def range_position(row: LabResult) -> Position | None:
    """`high`, `low`, `normal` — or `None` when the record cannot say.

    `None` is the important return. An unflagged row with no usable range
    carries no information about whether it is abnormal, and every caller
    here must treat that differently from `normal`. Conflating the two is
    what let `evaluate_rule_out` retire a hypothesis on a flag that was
    simply never set.

    `A` (abnormal, direction unrecorded) resolves through the range when one
    is available and otherwise returns `None` — never a guessed direction.
    Measured: 46 `A` rows on this record, 0 of them resolvable, so they stay
    honest rather than becoming a coin flip.
    """
    if flag_is_high(row.flag):
        return "high"
    if flag_is_low(row.flag):
        return "low"

    if row.value is None:
        return None
    low, high = reference_bounds(row)
    if low is None and high is None:
        return None
    if high is not None and row.value > high:
        return "high"
    if low is not None and row.value < low:
        return "low"
    return "normal"


def is_high(row: LabResult) -> bool:
    """Whether the row reads high. Cannot-tell is not high."""
    return range_position(row) == "high"


def is_low(row: LabResult) -> bool:
    """Whether the row reads low. Cannot-tell is not low."""
    return range_position(row) == "low"


def is_abnormal(row: LabResult) -> bool:
    """Whether the row reads out of range in either direction.

    Deliberately excludes an `A` flag with no resolvable range — it says
    something is off without saying what, which a caller ranking or
    rendering by direction cannot use. `range_position` returning `None`
    keeps that visible instead of inventing a side.
    """
    return range_position(row) in ("high", "low")

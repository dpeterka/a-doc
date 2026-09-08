"""Which analytes are heading back toward their reference range (ADR 0049).

The arithmetic half of resolution detection. `casefile.resolution` holds the
judgement half and imports nothing from `labs` — the one-directional rule
`knowledge.criteria` states about `PhenotypeLookup`, and the reason
`ResolutionSignal` is plain data rather than a `LabResult`.

Reference bounds come from `labs.reference` (ADR 0051), so a row with no
usable range yields no signal rather than a signal read off a missing flag.
"""

from __future__ import annotations

import logging

from adoc.casefile.resolution import MIN_SERIES_POINTS, ResolutionSignal, classify_series
from adoc.labs.db import LabsDb
from adoc.labs.reference import reference_bounds

logger = logging.getLogger(__name__)


def detect_resolution_signals(db: LabsDb) -> list[ResolutionSignal]:
    """Every analyte whose series started outside its reference range and has
    since closed a meaningful part of that gap.

    One bulk fetch (`series_by_key`) rather than a query per analyte: this
    runs over every stored analyte, and `labs.sqlite` lives on EFS/NFS in
    production where each query costs milliseconds of round trip — the same
    reason `_trend_scan_fn` was written that way.
    """
    signals: list[ResolutionSignal] = []
    for (name, _specimen), series in db.series_by_key().items():
        numeric = [(row.date, row.value) for row in series if row.value is not None]
        if len(numeric) < MIN_SERIES_POINTS:
            continue

        # Bounds from the FIRST row that has any. A lab can change its
        # reported range between draws; the range in force when the value was
        # out of range is the one the excursion was measured against.
        ref_low = ref_high = None
        unit = ""
        for row in series:
            low, high = reference_bounds(row)
            if low is not None or high is not None:
                ref_low, ref_high = low, high
                unit = row.ucum_unit or ""
                break
        if ref_low is None and ref_high is None:
            continue

        direction, closed, bound, side = classify_series(
            numeric, ref_low=ref_low, ref_high=ref_high
        )
        if side is None:
            continue

        ordered = sorted(numeric)
        signals.append(
            ResolutionSignal(
                analyte=name,
                direction=direction,
                first_date=ordered[0][0],
                first_value=ordered[0][1],
                last_date=ordered[-1][0],
                last_value=ordered[-1][1],
                unit=unit,
                bound=bound,
                side=side,
                fraction_closed=closed,
            )
        )
    signals.sort(key=lambda s: (-s.fraction_closed, s.analyte))
    return signals

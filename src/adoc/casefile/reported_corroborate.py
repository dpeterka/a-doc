"""Check remembered results against the measured series.

A reported result is a claim. When a document carrying that analyte near that
date is already on file — or arrives later — the claim can be checked, and
the answer is useful in both directions:

- **corroborated** closes a loop the patient opened, and tells the reasoner
  the memory is reliable enough to weigh.
- **contradicted** is at least as valuable. A remembered "high iron" against a
  measured normal is not a nuisance; it may mean she is thinking of a
  different analyte, a different year, or a different person's result — and
  a differential built on the memory would be built on sand.

Deterministic and offline: this compares stored numbers and flags, never asks
a model to adjudicate.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from adoc.casefile.reported import ReportedResult, ReportedResults
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult, flag_is_high, flag_is_low
from adoc.labs.validate import canonicalize

logger = logging.getLogger(__name__)

MATCH_WINDOW_DAYS = 45
"""How far from the remembered date a measured row may sit and still be the
same event.

A patient saying "November 2024" rarely means a specific day, and a panel
drawn in late October or early December is plainly the one she means. Wider
than this and unrelated draws start matching; narrower and a
month-precision memory matches nothing.
"""


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _direction_of(row: LabResult) -> str:
    if flag_is_high(row.flag):
        return "high"
    if flag_is_low(row.flag):
        return "low"
    if not row.flag:
        return "normal"
    # `A` (abnormal, direction unrecorded) lands here rather than being
    # guessed into a direction — see `labs.models.flag_is_low`.
    return "unknown"


def _candidates(entry: ReportedResult, rows: list[LabResult]) -> list[LabResult]:
    """Measured rows that could be the result she is remembering."""
    if entry.when is None:
        # Undated memories are not matched. Picking the nearest row for an
        # undated claim would manufacture a correspondence out of nothing.
        return []
    target = _normalize(entry.canonical_name or entry.analyte)
    if not target:
        return []
    window = timedelta(days=MATCH_WINDOW_DAYS)
    matches = []
    for row in rows:
        names = {_normalize(row.name), _normalize(row.name_raw or "")}
        mapped = canonicalize(row.name)
        if mapped:
            names.add(_normalize(mapped))
        if target not in names:
            continue
        if abs(row.date - entry.when) <= window:
            matches.append(row)
    return matches


def corroborate_reported(results: ReportedResults, db: LabsDb) -> ReportedResults:
    """Mark each unverified entry against the measured series.

    Only `unverified` entries are examined: a result already adjudicated is
    not re-litigated every run, so a human decision (or an earlier match)
    stays put.
    """
    rows = list(db.all_non_rejected_rows())
    for entry in results.entries:
        if entry.verification != "unverified":
            continue
        matches = _candidates(entry, rows)
        if not matches:
            continue
        nearest = min(matches, key=lambda r: abs(r.date - entry.when))  # type: ignore[operator]
        ref = f"labs:{nearest.name}:{nearest.date.isoformat()}"
        measured = _direction_of(nearest)

        if entry.direction in {"unknown", ""} or measured == "unknown":
            # A document exists for the claim, but nothing comparable was
            # remembered. That is corroboration of the EVENT, not of a value.
            entry.verification = "corroborated"
            entry.corroborating_source = ref
            entry.note = (entry.note + " Measured result found for this date.").strip()
            continue

        if measured == entry.direction:
            entry.verification = "corroborated"
        else:
            entry.verification = "contradicted"
            entry.note = (
                entry.note
                + f" Remembered as {entry.direction}; the measured result reads {measured}."
            ).strip()
            logger.info(
                "reported-results: %r remembered as %s but measured %s (%s)",
                entry.analyte,
                entry.direction,
                measured,
                ref,
            )
        entry.corroborating_source = ref
    return results

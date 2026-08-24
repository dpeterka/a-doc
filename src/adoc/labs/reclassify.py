"""Retro-reclassification of already-PENDING rows under the current
reconcile comparators (feature/semantic-compare) — NO new LLM calls (this
is a maintenance pass over already-extracted `raw_json`, not a new
extraction).

Real-corpus finding motivating this module: 1,153 of 1,159 queued PENDING
rows turned out to be false "disagreements" from `ingest.reconcile`'s old
literal (`_normalize_str`-only) field comparisons — the same printed
reading, transcribed with a cosmetic difference (a trailing unit token on a
reference range, a unicode dash, `None` vs. `""` for an unflagged result,
...) one extraction pass happened to introduce. `ingest.reconcile` now
compares ref_range/unit/flag semantically going forward
(`ref_ranges_equivalent`/`units_equivalent`/`flags_equivalent`); this module
is `adoc labs-reclassify`'s one-time/periodic sweep to fix up rows that
queued BEFORE that change under the old, stricter comparison.

For every still-PENDING row whose `raw_json` holds BOTH extraction passes'
full payloads (`pass_a` and `pass_b` - i.e. a row `ingest.reconcile.
_reconcile_matched_pair` produced, not a `single_pass`/`name_variant`
rescued-pair row - see the module docstring note below):

  1. Recompute the reason list with `ingest.reconcile.compute_pair_reasons`
     — the SAME pure function `_reconcile_matched_pair` uses at ingest
     time, reused rather than duplicated, so this sweep can never drift
     from real-time reconciliation behavior. `validate_row`/`trend_outlier`
     gates are recomputed against the CURRENT `labs.sqlite` (this
     patient's trend history may have grown since the row was first
     queued) and the CURRENT `ANALYTE_SPECS`/`UNIT_SYNONYMS` — never
     against a frozen snapshot.
  2. No reasons at all → flip the row to `auto` (every reason it queued
     under was a comparator false positive).
  3. Reasons remain → the row stays PENDING, but `raw_json["reasons"]` is
     rewritten to the recomputed list so the confirm queue's agreed-vs-
     disagreed bucketing (`ingest.reconcile.row_is_agreed`) reflects the
     current comparators (a row that queued as "disagreed" under a false
     ref-range mismatch, but also carries a genuine `validate_row` issue,
     moves into the "agreed" bucket once the false mismatch is gone).

`raw_json` is only ever rewritten when the recomputed reason list actually
differs from what's stored — a row recompute finds unchanged (a genuine
`value_mismatch`, a real `unit_mismatch`, `single_pass`, ...) is left
completely untouched, no audit stamp added. A row that IS rewritten gets
`previous_reasons` (the original list) and `reclassified_at` (an ISO
timestamp) added alongside its untouched `pass_a`/`pass_b` payloads
(`LabsDb.reclassify_row`).

Rows produced by the RESCUE pass (`ingest.reconcile._reconcile_rescued_pair`
— `single_pass`/`name_variant` in their reasons) are left completely
untouched: that path deliberately never ran `_reconcile_matched_pair`'s
field-comparison gates in the first place (its own, looser rescue-
compatibility test already covers value/unit/specimen), so recomputing
`compute_pair_reasons` against them would manufacture disagreement reasons
a human never needed to see.

`--dry-run` (`adoc labs-reclassify --dry-run`) computes and reports exactly
what a real run would find without mutating anything — no `reclassify_row`
calls, no export, no commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError

from adoc.ingest.reconcile import compute_pair_reasons, row_is_agreed
from adoc.ingest.schema import ExtractedResult
from adoc.labs.db import LabsDb


@dataclass
class ReclassifyReport:
    """Outcome of one `reclassify_pending` pass.

    Counts reflect the FINAL classification of every eligible (both-passes-
    present, non-rescued) PENDING row checked this run, whether or not that
    row's `raw_json` actually needed rewriting - `auto_flipped` +
    `rebucketed_agreed` + `still_disagreed` == `checked`.
    """

    checked: int = 0
    auto_flipped: int = 0
    rebucketed_agreed: int = 0
    still_disagreed: int = 0
    # Rows whose raw_json was actually rewritten this run (a subset of
    # `checked` - a row recomputed to an IDENTICAL reason list, e.g. a
    # genuine value_mismatch/single_pass, is left untouched). The CLI
    # commits/exports iff this is nonzero.
    rewritten: int = 0
    auto_flipped_ids: list[int] = field(default_factory=list)


def _both_passes_present(payload: dict[str, object]) -> bool:
    return payload.get("pass_a") is not None and payload.get("pass_b") is not None


def reclassify_pending(db: LabsDb, *, dry_run: bool = False) -> ReclassifyReport:
    """Run the retro-reclassification sweep (module docstring) over every
    currently-PENDING row.

    `dry_run=True` computes and reports exactly what a real run would do
    without calling `LabsDb.reclassify_row` at all - `db` is left
    completely unmutated. Idempotent: a row this run flips to `auto` is no
    longer PENDING, so a second run's `db.pending()` simply won't see it
    again; a row left PENDING with an unchanged reason list is recomputed
    identically (and left untouched) on every subsequent run.
    """
    report = ReclassifyReport()
    now = datetime.now(UTC)

    for row in db.pending():
        payload = row.raw_payload()
        original_reasons = list(payload.get("reasons", []))
        if "single_pass" in original_reasons or "name_variant" in original_reasons:
            continue
        if not _both_passes_present(payload):
            continue

        try:
            pass_a = ExtractedResult.model_validate(payload["pass_a"])
            pass_b = ExtractedResult.model_validate(payload["pass_b"])
        except ValidationError:
            # A malformed legacy payload should never crash the sweep -
            # just leave that row for a human to handle directly.
            continue

        assert row.id is not None  # rows read back from the db always have one
        report.checked += 1

        missing_date = "missing_date" in original_reasons
        new_reasons = compute_pair_reasons(
            pass_a, pass_b, doc_date=row.date, missing_date=missing_date, db=db
        )
        changed = new_reasons != original_reasons

        if not new_reasons:
            report.auto_flipped += 1
            report.auto_flipped_ids.append(row.id)
            auto = True
        elif row_is_agreed(new_reasons):
            report.rebucketed_agreed += 1
            auto = False
        else:
            report.still_disagreed += 1
            auto = False

        if changed:
            report.rewritten += 1
            if not dry_run:
                db.reclassify_row(row.id, reasons=new_reasons, auto=auto, at=now)

    return report

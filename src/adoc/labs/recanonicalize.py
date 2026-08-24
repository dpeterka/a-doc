"""Retro-recanonicalization of stored `name`s under the CURRENT
`labs.validate.canonicalize` (lab-taxonomy layer, feature/lab-taxonomy) - NO
LLM (a maintenance pass over already-extracted rows, exactly like
`labs/reclassify.py`/`labs/specimen.py`).

`ANALYTE_SPECS` has grown a great deal since many existing rows were first
ingested: a row's stored `name` reflects whatever `canonicalize` returned
(or its own raw name verbatim, if `canonicalize` returned `None`) AT
INGESTION TIME. `adoc labs-recanonicalize` (`recanonicalize_rows` below)
re-runs `canonicalize` against the CURRENT spec table for every non-rejected
row and, wherever the result differs from what's stored, updates it - so a
row ingested before, say, "Alkaline Phosphatase, S" had a spec now
canonicalizes and joins that analyte's trend series instead of sitting
under its raw name forever.

For every non-rejected row, in `id` order:

  1. Compute `new_name = canonicalize(row.name_raw) or canonicalize(row.name)
     or row.name` (trying `name_raw` first since that's the least-processed
     form and most likely to match a spec's alias table verbatim). If this
     equals the row's current `name`, the row is untouched (already
     canonical, or still unrecognized).
  2. Otherwise, check whether another row already occupies the target
     `(date, new_name, specimen, source_doc)` key - including a rename
     THIS SAME pass already made earlier (rows are processed in `id`
     order, and the pass tracks its own renames even in `--dry-run` mode,
     where the db itself is never touched):
       a. No collision -> plain rename
          (`LabsDb.rename_for_recanonicalization`).
       b. Collision, IDENTICAL reading (`_readings_identical`, borrowed
          from `db.py`'s `insert_results` conflict logic) -> this row is
          an exact duplicate of the survivor already at that key: reject
          it (`reject_row_as_recanonicalization_duplicate`), never
          renamed - the survivor is untouched.
       c. Collision, DIFFERING reading -> ambiguous: the survivor (the row
          already at the target key) flips to PENDING with BOTH readings
          preserved in its `raw_json`
          (`flip_to_pending_for_recanonicalization_conflict` - the same
          move `insert_results`'s case (d) makes for a re-extraction
          conflict); this row is rejected too, but as SUPERSEDED rather
          than a plain duplicate - its differing reading is never lost,
          only merged into the now-PENDING survivor's payload for a human
          to resolve via the confirm queue.

A row already occupying the target key with `extraction_status ==
"rejected"` is not treated as a collision (rejected rows are excluded from
the scan entirely) - an exceedingly rare same-key rejected-row clash would
surface as a plain sqlite UNIQUE-constraint error rather than being silently
absorbed; this tool does not attempt to resolve that case.

`--dry-run` computes and reports the same counts without calling ANY
db-mutating method above, and without exporting/committing.

Idempotent: a renamed row's `name` already equals what `canonicalize`
returns on the next run, so it's untouched; a rejected row is excluded from
`LabsDb.all_non_rejected_rows()` entirely on the next run; a flipped-to-
PENDING survivor's `name` also already equals the canonical result, so
re-running recomputes nothing further for it (a human still resolves the
queued conflict via the confirm queue, same as any other PENDING row).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adoc.labs.db import LabsDb, _readings_identical
from adoc.labs.models import ExtractionStatus, LabResult
from adoc.labs.validate import canonicalize


@dataclass
class RecanonicalizeReport:
    """Outcome of one `recanonicalize_rows` pass.

    `checked` counts every non-rejected row examined; `renamed` +
    `merged_duplicates` + `conflicts_queued` + `untouched` == `checked`.
    """

    checked: int = 0
    renamed: int = 0
    merged_duplicates: int = 0
    conflicts_queued: int = 0
    untouched: int = 0
    renamed_ids: list[int] = field(default_factory=list)


def _new_name_for(row: LabResult) -> str:
    """The name `row` would canonicalize to under the CURRENT spec table -
    `name_raw` first (least-processed, most likely to match an alias
    verbatim), falling back to the currently-stored `name`, falling back
    to `row.name` itself when neither canonicalizes."""
    return canonicalize(row.name_raw) or canonicalize(row.name) or row.name


def _find_collision(
    db: LabsDb,
    row: LabResult,
    new_name: str,
    current_name: dict[int, str],
    rows_by_id: dict[int, LabResult],
) -> int | None:
    """The id of a DIFFERENT non-rejected row already occupying `row`'s
    target `(date, new_name, specimen, source_doc)` key, if any.

    Checks THIS PASS's own renames-so-far first (`current_name`, updated
    even in `--dry-run` mode so a dry run reports the same collisions a
    real run would find), then falls back to a live db lookup for a row
    that already carried `new_name` before this pass started."""
    for other_id, other_current_name in current_name.items():
        if other_id == row.id:
            continue
        other = rows_by_id.get(other_id)
        if other is None:
            continue
        if (
            other_current_name == new_name
            and other.date == row.date
            and other.specimen == row.specimen
            and other.source_doc == row.source_doc
        ):
            return other_id

    probe = row.model_copy(update={"name": new_name})
    existing = db._find_at_key(probe)  # noqa: SLF001 - same-package reuse, see module docstring
    if (
        existing is not None
        and existing.id != row.id
        and existing.extraction_status != ExtractionStatus.REJECTED
    ):
        assert existing.id is not None
        return existing.id
    return None


def recanonicalize_rows(db: LabsDb, *, dry_run: bool = False) -> RecanonicalizeReport:
    """Run the recanonicalization sweep (module docstring) over every
    currently non-rejected row.

    `dry_run=True` computes and reports exactly what a real run would do
    without calling any of `LabsDb.rename_for_recanonicalization` /
    `reject_row_as_recanonicalization_duplicate` /
    `flip_to_pending_for_recanonicalization_conflict` /
    `reject_row_as_superseded_by_recanonicalize_conflict` - `db` is left
    completely unmutated.
    """
    report = RecanonicalizeReport()
    current_name: dict[int, str] = {}
    rows_by_id: dict[int, LabResult] = {}

    for row in db.all_non_rejected_rows():
        assert row.id is not None  # rows read back from the db always have one
        rows_by_id[row.id] = row
        current_name[row.id] = row.name

        new_name = _new_name_for(row)
        report.checked += 1
        if new_name == row.name:
            report.untouched += 1
            continue

        collision_id = _find_collision(db, row, new_name, current_name, rows_by_id)
        if collision_id is None:
            report.renamed += 1
            report.renamed_ids.append(row.id)
            current_name[row.id] = new_name
            if not dry_run:
                db.rename_for_recanonicalization(row.id, new_name)
            continue

        survivor = rows_by_id.get(collision_id) or db.get_row(collision_id)
        assert survivor is not None and survivor.id is not None
        if _readings_identical(survivor, row):
            report.merged_duplicates += 1
            if not dry_run:
                db.reject_row_as_recanonicalization_duplicate(row.id, kept_id=survivor.id)
        else:
            report.conflicts_queued += 1
            if not dry_run:
                db.flip_to_pending_for_recanonicalization_conflict(survivor.id, conflicting=row)
                db.reject_row_as_superseded_by_recanonicalize_conflict(
                    row.id, survivor_id=survivor.id
                )
        # Either way this row is rejected (or would be, for real), never
        # renamed - it keeps its OLD name/key so no UNIQUE collision is
        # ever created, and it's excluded from `all_non_rejected_rows()`
        # on the next pass either way.
        current_name.pop(row.id, None)

    return report

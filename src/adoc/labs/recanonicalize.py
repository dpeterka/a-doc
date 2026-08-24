"""Retro-recanonicalization of stored `name`s under the CURRENT
`labs.validate.canonical_rename_target` (lab-taxonomy layer, feature/
lab-taxonomy; rename policy split, feature/taxonomy-distinctions) - NO
LLM (a maintenance pass over already-extracted rows, exactly like
`labs/reclassify.py`/`labs/specimen.py`).

`ANALYTE_SPECS` has grown a great deal since many existing rows were first
ingested: a row's stored `name` reflects whatever it resolved to (or its
own raw name verbatim) AT INGESTION TIME. `adoc labs-recanonicalize`
(`recanonicalize_rows` below) re-runs `canonical_rename_target` against
the CURRENT spec table for every non-rejected row and, wherever the
result differs from what's stored, updates it - so a row ingested before,
say, "ACTH,PLASMA" had an exact alias onto "ACTH" joins that analyte's
trend series instead of sitting under its raw name forever.

IMPORTANT: `_new_name_for` uses `validate.canonical_rename_target`, NOT
the more permissive `validate.canonicalize` - the latter also matches via
a generic suffix-strip retry and a score-suffix rule (see `validate`'s
module docstring, "Matching vs. renaming"), both of which are safe for
read-time grouping/validation/trend-scoping but were found to have
silently merged CLINICALLY DISTINCT stored series when used to rename
(e.g. "LEFT HIP Total BMD" and "RIGHT HIP Total BMD" both renaming onto
one shared name). `canonical_rename_target` only ever renames on an EXACT
alias match - a human-reviewed statement that two spellings denote the
identical measurement - so a suffix/score-assisted match now leaves a
row's stored name untouched forever, while it still gets full
`canonicalize` benefits (panel, validation, trend scoping) at read time.

Two-phase design (plan, then execute) - the sweep robustness this module
is named for:

  1. **Plan**: for every non-rejected row, compute the name it would end
     up with (`_new_name_for`) - purely in memory, no db mutation yet.
  2. **Group**: bucket EVERY non-rejected row (whether its planned name
     changes or not) by the `(date, planned-name, specimen, source_doc)`
     key it would occupy once the plan is applied. Any key shared by 2+
     rows - including a to-be-renamed row landing on a key an untouched
     row already occupies - is a collision, full stop.
  3. **Execute**: a key with exactly one row is a plain rename (or no-op,
     if its planned name already equals its stored name). A key with 2+
     rows routes ALL of them through the existing merge/conflict handlers
     BEFORE a single `UPDATE` touching that key is issued - so a sqlite
     UNIQUE-constraint violation on `(date, name, specimen, source_doc)`
     is structurally impossible: no row is ever renamed onto a key
     another live row (in this pass's plan) also targets.

     The SURVIVOR of a group is whichever row already holds that exact
     key today (an "incumbent" - at most one can exist, since the table's
     own UNIQUE constraint already forbids two live rows sharing a key
     before this pass ever runs), if one exists; otherwise the lowest-`id`
     row (mirroring "rows are processed in `id` order" from the original
     single-pass design). This distinction matters: a naive "lowest id
     always wins" rule would let a LOWER-id row being renamed try to
     overwrite the exact key a HIGHER-id, already-canonical row already
     occupies - still a UNIQUE violation, just moved rather than avoided.
     Preferring the incumbent means that row is never renamed and never
     double-counted, and the actually-moving row is the one routed
     through merge/conflict instead.

For a collision group:

  a. IDENTICAL reading (`_readings_identical`, borrowed from `db.py`'s
     `insert_results` conflict logic) between a loser and the survivor ->
     the loser is an exact duplicate of the survivor: reject it
     (`reject_row_as_recanonicalization_duplicate`), never renamed - the
     survivor is untouched apart from its own (possible) rename.
  b. DIFFERING reading -> ambiguous: the survivor flips to PENDING with
     both readings preserved in its `raw_json`
     (`flip_to_pending_for_recanonicalization_conflict` - the same move
     `insert_results`'s case (d) makes for a re-extraction conflict); the
     loser is rejected too, but as SUPERSEDED rather than a plain
     duplicate - its differing reading is never lost, only merged into
     the now-PENDING survivor's payload for a human to resolve via the
     confirm queue.

A row already occupying the target key with `extraction_status ==
"rejected"` is not treated as a collision (rejected rows are excluded from
the scan entirely) - an exceedingly rare same-key rejected-row clash would
surface as a plain sqlite UNIQUE-constraint error rather than being silently
absorbed; this tool does not attempt to resolve that case.

`--dry-run` computes and reports the exact same plan/groups without
calling ANY db-mutating method above, and without exporting/committing -
dry-run and live share the identical in-memory plan/group computation, so
their reported counts can never drift apart (only the `if not dry_run:`-
guarded db calls differ).

Idempotent: a renamed row's `name` already equals what
`canonical_rename_target` returns on the next run, so it's untouched; a
rejected row is excluded from `LabsDb.all_non_rejected_rows()` entirely on
the next run; a flipped-to-PENDING survivor's `name` also already equals
its target, so re-running recomputes nothing further for it (a human
still resolves the queued conflict via the confirm queue, same as any
other PENDING row).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from adoc.labs.db import LabsDb, _readings_identical
from adoc.labs.models import LabResult
from adoc.labs.validate import canonical_rename_target


@dataclass
class RecanonicalizeReport:
    """Outcome of one `recanonicalize_rows` pass.

    `checked` counts every non-rejected row examined; `renamed` +
    `merged_duplicates` + `conflicts_queued` + `untouched` +
    `blocked_by_tombstone` == `checked`.
    """

    checked: int = 0
    renamed: int = 0
    merged_duplicates: int = 0
    conflicts_queued: int = 0
    untouched: int = 0
    blocked_by_tombstone: int = 0
    renamed_ids: list[int] = field(default_factory=list)


def _new_name_for(row: LabResult) -> str:
    """The name `row` would be renamed to under the CURRENT spec table's
    EXACT aliases only (`validate.canonical_rename_target`) - `name_raw`
    tried first (least-processed, most likely to match an alias
    verbatim), then the currently-stored `name`, falling back to `row.name`
    itself when neither is an exact alias of any spec (module docstring:
    a suffix/score-assisted match is deliberately NOT a rename target)."""
    return canonical_rename_target(row.name_raw, row.name) or row.name


_GroupKey = tuple[str, str, str, str]  # (date, planned-name, specimen, source_doc)


def _group_key(row: LabResult, planned_name: str) -> _GroupKey:
    return (row.date.isoformat(), planned_name, row.specimen, row.source_doc)


def recanonicalize_rows(db: LabsDb, *, dry_run: bool = False) -> RecanonicalizeReport:
    """Run the recanonicalization sweep (module docstring) over every
    currently non-rejected row.

    `dry_run=True` computes and reports exactly what a real run would do
    without calling any of `LabsDb.rename_for_recanonicalization` /
    `reject_row_as_recanonicalization_duplicate` /
    `flip_to_pending_for_recanonicalization_conflict` /
    `reject_row_as_superseded_by_recanonicalize_conflict` - `db` is left
    completely unmutated. The plan/grouping computation itself never
    depends on `dry_run`, so a dry run's counts are exactly what a live
    run would produce.
    """
    rows = db.all_non_rejected_rows()
    report = RecanonicalizeReport(checked=len(rows))
    rows_by_id = {row.id: row for row in rows}

    # Phase 1 (plan): every row's target name, computed purely in memory -
    # no report counts are assigned yet, since which row is a group's
    # survivor (and therefore "renamed"/"untouched") vs. a loser can only
    # be decided once every row sharing a key is known (phase 2/3 below).
    planned_name: dict[int, str] = {}
    for row in rows:
        assert row.id is not None  # rows read back from the db always have one
        planned_name[row.id] = _new_name_for(row)

    # Phase 1b (tombstone check): the table's UNIQUE constraint spans
    # REJECTED rows too, so a planned rename whose target key a tombstone
    # still occupies would raise IntegrityError despite no LIVE row
    # sitting there (found live: curation-rejected FRAX duplicates already
    # held the canonical-name key their kept siblings were being renamed
    # to). Such a rename simply doesn't happen - the row keeps its stored
    # name (read-time `canonicalize` still groups it correctly) and is
    # counted in `blocked_by_tombstone`. Reverting the plan BEFORE phase 2
    # keeps grouping faithful to the keys rows will actually occupy.
    rejected_keys = db.rejected_row_keys()
    blocked_ids: set[int] = set()
    for row in rows:
        assert row.id is not None
        target = planned_name[row.id]
        if target != row.name and _group_key(row, target) in rejected_keys:
            planned_name[row.id] = row.name
            blocked_ids.add(row.id)

    # Phase 2 (group): bucket every row by the FINAL key it would occupy
    # once the plan is applied - collisions (including a to-be-renamed
    # row landing on a key an untouched row already holds) fall out of
    # this for free, since both rows are in the same `rows` list.
    groups: dict[_GroupKey, list[int]] = defaultdict(list)
    for row in rows:
        assert row.id is not None
        groups[_group_key(row, planned_name[row.id])].append(row.id)

    # Phase 3 (execute): resolve each group. The survivor is the
    # incumbent (a row whose CURRENT name already equals the group's
    # target - at most one can exist, since the table's own UNIQUE
    # constraint already forbids two live rows sharing a key), or else
    # the lowest-id row (module docstring). A singleton group is simply
    # that row, renamed or left untouched; a 2+ group routes every
    # non-survivor row through the merge/conflict handlers BEFORE any
    # rename is applied, so no UPDATE can ever collide with another row
    # in this pass's plan.
    for ids in groups.values():
        ids_sorted = sorted(ids)
        incumbent_id = next((i for i in ids_sorted if rows_by_id[i].name == planned_name[i]), None)
        survivor_id = incumbent_id if incumbent_id is not None else ids_sorted[0]
        survivor = rows_by_id[survivor_id]
        survivor_name = planned_name[survivor_id]
        if survivor_name == survivor.name:
            if survivor_id in blocked_ids:
                report.blocked_by_tombstone += 1
            else:
                report.untouched += 1
        else:
            report.renamed += 1
            report.renamed_ids.append(survivor_id)
            if not dry_run:
                db.rename_for_recanonicalization(survivor_id, survivor_name)

        for loser_id in ids_sorted:
            if loser_id == survivor_id:
                continue
            loser = rows_by_id[loser_id]
            if _readings_identical(survivor, loser):
                report.merged_duplicates += 1
                if not dry_run:
                    db.reject_row_as_recanonicalization_duplicate(loser_id, kept_id=survivor_id)
            else:
                report.conflicts_queued += 1
                if not dry_run:
                    db.flip_to_pending_for_recanonicalization_conflict(
                        survivor_id, conflicting=loser
                    )
                    db.reject_row_as_superseded_by_recanonicalize_conflict(
                        loser_id, survivor_id=survivor_id
                    )
            # Either way this row is rejected (or would be, for real),
            # never renamed - it keeps its OLD name/key so no UNIQUE
            # collision is ever created, and it's excluded from
            # `all_non_rejected_rows()` on the next pass either way.

    report.renamed_ids.sort()
    return report

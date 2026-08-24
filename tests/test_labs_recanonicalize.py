"""Tests for adoc.labs.recanonicalize: retro-recanonicalization of stored
`name`s under the current `labs.validate.canonicalize` (`adoc
labs-recanonicalize`, feature/lab-taxonomy).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.labs.recanonicalize import recanonicalize_rows

SHA = "e" * 64


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="doc.pdf",
            doc_type="lab-result",
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )
    return store


def _row(
    *,
    name: str,
    name_raw: str,
    value: float | None = 1.0,
    lab_date: date = date(2026, 1, 1),
    specimen: str = "unknown",
    extraction_status: ExtractionStatus = ExtractionStatus.AUTO,
) -> LabResult:
    return LabResult(
        date=lab_date,
        name=name,
        name_raw=name_raw,
        value=value,
        source_doc=SHA,
        specimen=specimen,
        extraction_status=extraction_status,
        raw_json=json.dumps({"name_raw": name_raw, "value": value}),
    )


def test_dry_run_reports_without_mutating(db: LabsDb) -> None:
    # "ACTH,PLASMA" is an EXACT alias of "ACTH" (module docstring: only an
    # exact alias is a rename target - a generic suffix-strip match like
    # "Alkaline Phosphatase, S" -> "Alkaline Phosphatase" no longer is).
    (row_id,) = db.insert_results([_row(name="ACTH,PLASMA", name_raw="ACTH,PLASMA")])
    assert row_id is not None

    report = recanonicalize_rows(db, dry_run=True)

    assert report.checked == 1
    assert report.renamed == 1
    assert report.renamed_ids == [row_id]
    # Nothing actually changed.
    stored = db.get_row(row_id)
    assert stored is not None
    assert stored.name == "ACTH,PLASMA"


def test_plain_rename_updates_the_stored_name(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_row(name="ACTH,PLASMA", name_raw="ACTH,PLASMA")])
    assert row_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 1
    stored = db.get_row(row_id)
    assert stored is not None
    assert stored.name == "ACTH"
    assert stored.extraction_status == ExtractionStatus.AUTO  # untouched otherwise


def test_suffix_derived_match_is_never_renamed(db: LabsDb) -> None:
    """ "Alkaline Phosphatase, S" resolves to "Alkaline Phosphatase" only via
    the generic suffix-strip retry (`validate.canonicalize`), not an exact
    alias - `canonical_rename_target` never fires for it, so the row's
    stored name is left exactly as-is (it still gets full `canonicalize`
    benefits at read time - see `validate`'s module docstring)."""
    (row_id,) = db.insert_results(
        [_row(name="Alkaline Phosphatase, S", name_raw="Alkaline Phosphatase, S")]
    )
    assert row_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 0
    assert report.untouched == 1
    stored = db.get_row(row_id)
    assert stored is not None
    assert stored.name == "Alkaline Phosphatase, S"


def test_already_canonical_row_is_left_untouched(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_row(name="Alkaline Phosphatase", name_raw="ALP")])
    assert row_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.untouched == 1
    assert report.renamed == 0


def test_unrecognized_analyte_is_left_untouched(db: LabsDb) -> None:
    db.insert_results([_row(name="some made up marker", name_raw="some made up marker")])

    report = recanonicalize_rows(db, dry_run=False)

    assert report.untouched == 1
    assert report.renamed == 0


def test_identical_reading_collision_merges_the_duplicate(db: LabsDb) -> None:
    """Two EXACT-alias spelling variants of the SAME analyte ("transferrin
    saturation" and "iron saturation" both alias "TSAT" - see
    `ANALYTE_SPECS`), same date/specimen/doc, with the SAME reading - one
    survives (renamed), the other is rejected as a duplicate, never
    renamed itself."""
    ids = db.insert_results(
        [
            _row(name="transferrin saturation", name_raw="transferrin saturation", value=80.0),
            _row(name="iron saturation", name_raw="iron saturation", value=80.0),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 1
    assert report.merged_duplicates == 1

    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    # The first-processed row (lower id) becomes the survivor.
    assert first.name == "TSAT"
    assert first.extraction_status == ExtractionStatus.AUTO
    assert second.extraction_status == ExtractionStatus.REJECTED
    assert second.raw_payload()["recanonicalization_duplicate_of"] == first_id


def test_differing_reading_collision_queues_both_for_review(db: LabsDb) -> None:
    """Two exact-alias spelling variants, same date/specimen/doc, DIFFERING
    readings - the survivor flips to PENDING carrying both payloads; the
    loser is rejected as superseded, never silently dropped."""
    ids = db.insert_results(
        [
            _row(name="transferrin saturation", name_raw="transferrin saturation", value=80.0),
            _row(name="iron saturation", name_raw="iron saturation", value=95.0),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 1
    assert report.conflicts_queued == 1

    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    assert first.name == "TSAT"
    assert first.extraction_status == ExtractionStatus.PENDING
    assert first.raw_payload()["recanonicalize_conflict"]["value"] == 95.0
    assert second.extraction_status == ExtractionStatus.REJECTED
    assert second.raw_payload()["superseded_by_recanonicalize_conflict"] == first_id


def test_idempotent_second_run_finds_nothing_left_to_do(db: LabsDb) -> None:
    db.insert_results(
        [
            _row(name="transferrin saturation", name_raw="transferrin saturation", value=80.0),
            _row(name="iron saturation", name_raw="iron saturation", value=80.0),
        ]
    )
    first_report = recanonicalize_rows(db, dry_run=False)
    assert first_report.renamed == 1
    assert first_report.merged_duplicates == 1

    second_report = recanonicalize_rows(db, dry_run=False)

    assert second_report.renamed == 0
    assert second_report.merged_duplicates == 0
    assert second_report.conflicts_queued == 0
    # The one surviving non-rejected row is now already canonical.
    assert second_report.untouched == 1
    assert second_report.checked == 1


def test_planner_handles_three_way_exact_alias_collision_without_crashing(db: LabsDb) -> None:
    """Three EXACT-alias spelling variants of the SAME analyte, same date/
    specimen/doc, all landing on one target key at once - the planner
    (module docstring: plan-then-execute, grouped by final key) must
    resolve all of them via the merge/conflict handlers before issuing any
    rename, so sqlite's UNIQUE constraint on (date, name, specimen,
    source_doc) is never violated (no IntegrityError)."""
    ids = db.insert_results(
        [
            _row(name="TSAT", name_raw="TSAT", value=80.0),
            _row(name="transferrin saturation", name_raw="transferrin saturation", value=80.0),
            _row(name="iron saturation", name_raw="iron saturation", value=91.0),
        ]
    )
    first_id, second_id, third_id = ids
    assert first_id is not None and second_id is not None and third_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    # The already-canonical row is the incumbent survivor; both others
    # are routed off of it (one identical duplicate, one differing
    # conflict) - never renamed themselves, no crash.
    assert report.checked == 3
    assert report.untouched == 1
    assert report.merged_duplicates == 1
    assert report.conflicts_queued == 1
    assert report.renamed == 0

    survivor = db.get_row(first_id)
    duplicate = db.get_row(second_id)
    conflict_loser = db.get_row(third_id)
    assert survivor is not None and duplicate is not None and conflict_loser is not None
    assert survivor.name == "TSAT"
    assert survivor.extraction_status == ExtractionStatus.PENDING
    assert survivor.raw_payload()["recanonicalize_conflict"]["value"] == 91.0
    assert duplicate.extraction_status == ExtractionStatus.REJECTED
    assert duplicate.raw_payload()["recanonicalization_duplicate_of"] == first_id
    assert conflict_loser.extraction_status == ExtractionStatus.REJECTED
    assert conflict_loser.raw_payload()["superseded_by_recanonicalize_conflict"] == first_id


def test_dry_run_matches_live_outcome_on_a_fixture_with_every_case(tmp_path: Path) -> None:
    """dry-run and live must report byte-for-byte identical counts on the
    same starting fixture (module docstring: "dry-run and live share the
    identical in-memory plan/group computation") - built from two
    identical copies of a fixture covering: an exact-alias plain rename, a
    suffix-derived non-rename, an identical-reading collision, a
    differing-reading collision, and an already-canonical untouched row.
    """

    def _seed(store: LabsDb) -> None:
        store.upsert_document(
            LabDocument(
                sha256=SHA,
                filename="doc.pdf",
                doc_type="lab-result",
                page_count=1,
                status=DocumentStatus.COMPLETE,
            )
        )
        store.insert_results(
            [
                _row(name="ACTH,PLASMA", name_raw="ACTH,PLASMA", lab_date=date(2026, 1, 1)),
                _row(
                    name="Alkaline Phosphatase, S",
                    name_raw="Alkaline Phosphatase, S",
                    lab_date=date(2026, 1, 2),
                ),
                _row(
                    name="transferrin saturation",
                    name_raw="transferrin saturation",
                    value=80.0,
                    lab_date=date(2026, 1, 3),
                ),
                _row(
                    name="iron saturation",
                    name_raw="iron saturation",
                    value=80.0,
                    lab_date=date(2026, 1, 3),
                ),
                _row(
                    name="transferrin saturation",
                    name_raw="transferrin saturation",
                    value=50.0,
                    lab_date=date(2026, 1, 4),
                ),
                _row(
                    name="iron saturation",
                    name_raw="iron saturation",
                    value=65.0,
                    lab_date=date(2026, 1, 4),
                ),
                _row(name="ACTH", name_raw="ACTH", lab_date=date(2026, 1, 5)),
            ]
        )

    dry_db = LabsDb(tmp_path / "dry.sqlite")
    _seed(dry_db)
    dry_report = recanonicalize_rows(dry_db, dry_run=True)

    live_db = LabsDb(tmp_path / "live.sqlite")
    _seed(live_db)
    live_report = recanonicalize_rows(live_db, dry_run=False)

    assert dry_report == live_report


# ----------------------------------------------------------------
# Real collision-family regressions (feature/taxonomy-distinctions): each
# of these pairs was found silently merging onto one shared stored name
# via a suffix-strip or score-suffix MATCH being reused as a RENAME. A
# sweep must never collapse them onto the same stored name again, whether
# that means no rename at all (Z-score) or two distinct rename targets
# (BMD/eGFR/Manganese, once their specs were split).
# ----------------------------------------------------------------


def test_left_right_hip_z_scores_never_collapse_onto_one_stored_name(db: LabsDb) -> None:
    ids = db.insert_results(
        [
            _row(
                name="LEFT HIP femoral neck Z-Score",
                name_raw="LEFT HIP femoral neck Z-Score",
                value=-1.1,
            ),
            _row(
                name="LEFT HIP Total Z-Score",
                name_raw="LEFT HIP Total Z-Score",
                value=-0.8,
                lab_date=date(2026, 1, 2),
            ),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 0
    assert report.untouched == 2
    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    assert first.name == "LEFT HIP femoral neck Z-Score"
    assert second.name == "LEFT HIP Total Z-Score"


def test_left_right_hip_bmd_never_collapse_onto_one_stored_name(db: LabsDb) -> None:
    ids = db.insert_results(
        [
            _row(name="LEFT HIP Total BMD", name_raw="LEFT HIP Total BMD", value=0.85),
            _row(
                name="RIGHT HIP Total BMD",
                name_raw="RIGHT HIP Total BMD",
                value=0.90,
                lab_date=date(2026, 1, 2),
            ),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 2
    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    assert first.name == "Left Hip Total BMD"
    assert second.name == "Right Hip Total BMD"
    assert first.name != second.name


def test_manganese_plasma_and_rbc_never_collapse_onto_one_stored_name(db: LabsDb) -> None:
    ids = db.insert_results(
        [
            _row(name="Manganese, Plasma", name_raw="Manganese, Plasma", value=1.2),
            _row(
                name="Manganese, RBC",
                name_raw="Manganese, RBC",
                value=15.0,
                lab_date=date(2026, 1, 2),
            ),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    recanonicalize_rows(db, dry_run=False)

    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    assert first.name == "Manganese, Plasma"
    assert second.name == "Manganese, RBC"


def test_egfr_race_stratified_variants_never_collapse_onto_one_stored_name(db: LabsDb) -> None:
    ids = db.insert_results(
        [
            _row(name="eGFR If Africn Am", name_raw="eGFR If Africn Am", value=90.0),
            _row(
                name="eGFR If NonAfricn Am",
                name_raw="eGFR If NonAfricn Am",
                value=78.0,
                lab_date=date(2026, 1, 2),
            ),
        ]
    )
    first_id, second_id = ids
    assert first_id is not None and second_id is not None

    recanonicalize_rows(db, dry_run=False)

    first = db.get_row(first_id)
    second = db.get_row(second_id)
    assert first is not None and second is not None
    assert first.name == "eGFR (African American)"
    assert second.name == "eGFR (Non-African American)"


def test_rename_blocked_when_target_key_held_by_rejected_row(db: LabsDb) -> None:
    """The table's UNIQUE(date, name, specimen, source_doc) spans REJECTED
    rows too (found live: curation-rejected FRAX duplicates already held
    the canonical-name key their kept siblings were being renamed to). A
    planned rename onto a tombstone's key must simply not happen - stored
    name kept, counted in `blocked_by_tombstone`, no IntegrityError."""
    tombstone_id, live_id = db.insert_results(
        [
            _row(name="ACTH", name_raw="ACTH"),
            _row(name="ACTH,PLASMA", name_raw="ACTH,PLASMA", value=2.0),
        ]
    )
    assert tombstone_id is not None and live_id is not None
    db.reject_row(tombstone_id)

    dry = recanonicalize_rows(db, dry_run=True)
    live = recanonicalize_rows(db, dry_run=False)

    for report in (dry, live):
        assert report.checked == 1
        assert report.renamed == 0
        assert report.blocked_by_tombstone == 1
        assert report.untouched == 0
    stored = db.get_row(live_id)
    assert stored is not None
    assert stored.name == "ACTH,PLASMA"
    assert stored.extraction_status is not ExtractionStatus.REJECTED

    # Idempotent: a second pass reports the same block, still no crash.
    again = recanonicalize_rows(db, dry_run=False)
    assert again.blocked_by_tombstone == 1


def test_legacy_permissive_stored_name_is_restored_to_raw(db: LabsDb) -> None:
    """Before feature/taxonomy-distinctions, `ingest.reconcile` persisted
    permissive `canonicalize(...)` results as stored names - a site-
    prefixed DEXA score could sit under bare "Z-score" (real rows found
    locally). With no exact alias vouching for it, the sweep restores the
    stored name to `name_raw`; a stored name that is NOT the permissive
    artifact (e.g. a human correction) is left alone."""
    legacy_id, corrected_id = db.insert_results(
        [
            _row(name="Z-score", name_raw="LEFT HIP Total Z-Score", value=-1.2),
            _row(name="My Custom Label", name_raw="LEFT HIP femoral neck Z-Score", value=-0.5),
        ]
    )
    assert legacy_id is not None and corrected_id is not None

    report = recanonicalize_rows(db, dry_run=False)

    assert report.renamed == 1
    assert report.renamed_ids == [legacy_id]
    restored = db.get_row(legacy_id)
    assert restored is not None
    assert restored.name == "LEFT HIP Total Z-Score"
    untouched = db.get_row(corrected_id)
    assert untouched is not None
    assert untouched.name == "My Custom Label"

    again = recanonicalize_rows(db, dry_run=True)
    assert again.renamed == 0

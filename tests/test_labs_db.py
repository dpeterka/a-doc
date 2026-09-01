"""Tests for adoc.labs.db.LabsDb: schema, confirm-queue flow, dedupe, FTS, JSONL round-trip."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adoc.labs.db import DocumentTextPage, LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabFlag, LabResult

SHA_A = "a" * 64
SHA_B = "b" * 64


def _doc(sha256: str = SHA_A, **overrides: object) -> LabDocument:
    fields: dict[str, object] = {
        "sha256": sha256,
        "filename": "quest-2026-05-02.pdf",
        "doc_type": "lab-result",
        "doc_date": date(2026, 5, 2),
        "page_count": 3,
        "ingested_at": datetime(2026, 5, 3, 12, 0, 0),
        "status": DocumentStatus.COMPLETE,
    }
    fields.update(overrides)
    return LabDocument.model_validate(fields)


def _lab(
    name: str = "potassium",
    value: float | None = 4.1,
    lab_date: date = date(2026, 5, 2),
    source_doc: str = SHA_A,
    **overrides: object,
) -> LabResult:
    fields: dict[str, object] = {
        "date": lab_date,
        "name": name,
        "name_raw": name,
        "value": value,
        "ucum_unit": "mmol/L",
        "ref_low": 3.5,
        "ref_high": 5.1,
        "source_doc": source_doc,
        "extraction_status": ExtractionStatus.AUTO,
        "raw_json": json.dumps({"name_raw": name, "value": value}),
    }
    fields.update(overrides)
    return LabResult.model_validate(fields)


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(_doc())
    store.upsert_document(_doc(sha256=SHA_B, filename="labcorp-2026-06-01.pdf"))
    return store


def test_schema_created_with_user_version(db: LabsDb) -> None:
    version = db._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 5  # 5: encounter_text corpus (ADR 0015 extended to encounters)
    tables = {
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    assert {"documents", "labs", "labs_fts", "document_text", "document_text_fts"} <= tables


def test_foreign_keys_and_wal_enabled(db: LabsDb) -> None:
    assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_journal_mode_defaults_to_wal(tmp_path: Path) -> None:
    """Local/dev/test default: WAL is fast on a normal local filesystem."""
    store = LabsDb(tmp_path / "labs.sqlite")
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_journal_mode_truncate_for_nfs_efs(tmp_path: Path) -> None:
    """Deployed (ECS/EFS) mode: TRUNCATE avoids WAL's shared-memory index
    file, which is unsafe over NFS/EFS (see LabsDb.__init__'s docstring and
    ADR 0006)."""
    store = LabsDb(tmp_path / "labs.sqlite", journal_mode="TRUNCATE")
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"


def test_busy_timeout_is_set_so_cross_process_contention_waits_not_fails(
    tmp_path: Path,
) -> None:
    """`sqlite3.connect()`'s default timeout is 5.0s. On EFS, separate ECS
    tasks (the always-on web service and the scheduled ingest/review/backup
    jobs) are separate PROCESSES sharing this one file — the in-process
    `RLock` (see `LabsDb.__init__`'s docstring) does not help across them at
    all. A web request landing mid-way through a batch write used to raise
    `sqlite3.OperationalError: database is locked` after 5s rather than
    simply waiting the extra few seconds for the writer to finish."""
    store = LabsDb(tmp_path / "labs.sqlite")

    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000


def test_insert_results_returns_ids(db: LabsDb) -> None:
    ids = db.insert_results([_lab(), _lab(name="sodium", value=140.0, ucum_unit="mmol/L")])
    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == 2


def test_upsert_document_updates_in_place(db: LabsDb) -> None:
    db.upsert_document(_doc(status=DocumentStatus.NEEDS_REVIEW, page_count=5))
    fetched = db.get_document(SHA_A)
    assert fetched is not None
    assert fetched.status == DocumentStatus.NEEDS_REVIEW
    assert fetched.page_count == 5
    assert len(db.list_documents()) == 2  # no duplicate row


def test_confirm_correct_reject_flow(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_lab(extraction_status=ExtractionStatus.PENDING)])
    assert row_id is not None

    pending = db.pending()
    assert len(pending) == 1
    assert pending[0].id == row_id
    assert pending[0].extraction_status == ExtractionStatus.PENDING

    db.confirm_row(row_id)
    confirmed = db.get_row(row_id)
    assert confirmed is not None
    assert confirmed.extraction_status == ExtractionStatus.CONFIRMED
    assert db.pending() == []

    # a second row is corrected instead of confirmed
    (row_id_2,) = db.insert_results(
        [
            _lab(
                name="sodium",
                value=41.0,
                lab_date=date(2026, 5, 9),
                extraction_status=ExtractionStatus.PENDING,
            )
        ]
    )
    assert row_id_2 is not None
    db.correct_row(row_id_2, value=140.0, flag=LabFlag.HIGH)
    corrected = db.get_row(row_id_2)
    assert corrected is not None
    assert corrected.extraction_status == ExtractionStatus.CORRECTED
    assert corrected.value == 140.0
    assert corrected.flag == LabFlag.HIGH

    with pytest.raises(ValueError):
        db.correct_row(row_id_2, not_a_column="x")
    with pytest.raises(ValueError):
        db.correct_row(row_id_2)

    # a third row is rejected
    (row_id_3,) = db.insert_results(
        [
            _lab(
                name="glucose",
                value=95.0,
                lab_date=date(2026, 5, 10),
                extraction_status=ExtractionStatus.PENDING,
            )
        ]
    )
    assert row_id_3 is not None
    db.reject_row(row_id_3)
    rejected = db.get_row(row_id_3)
    assert rejected is not None
    assert rejected.extraction_status == ExtractionStatus.REJECTED


def test_unique_constraint_dedupes_on_insert(db: LabsDb) -> None:
    first = _lab()
    duplicate = _lab()  # identical (date, name, source_doc)

    ids = db.insert_results([first, duplicate])
    assert ids[0] is not None
    assert ids[1] is None  # skipped as a duplicate

    rows = db.series("potassium")
    assert len(rows) == 1


def test_unique_constraint_allows_same_analyte_different_doc(db: LabsDb) -> None:
    ids = db.insert_results([_lab(source_doc=SHA_A), _lab(source_doc=SHA_B)])
    assert all(i is not None for i in ids)
    assert len(db.series("potassium")) == 2


def test_series_is_time_ordered(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(lab_date=date(2026, 6, 1), value=4.2),
            _lab(lab_date=date(2026, 1, 1), value=4.0),
            _lab(lab_date=date(2026, 3, 1), value=4.1),
        ]
    )
    series = db.series("potassium")
    assert [r.date for r in series] == [date(2026, 1, 1), date(2026, 3, 1), date(2026, 6, 1)]


def test_latest_panel_returns_most_recent_per_analyte(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(name="potassium", value=4.0, lab_date=date(2026, 1, 1)),
            _lab(name="potassium", value=4.4, lab_date=date(2026, 6, 1)),
            _lab(name="sodium", value=140.0, lab_date=date(2026, 2, 1), ucum_unit="mmol/L"),
        ]
    )
    panel = {row.name: row for row in db.latest_panel()}
    assert panel["potassium"].value == 4.4
    assert panel["potassium"].date == date(2026, 6, 1)
    assert panel["sodium"].value == 140.0


def test_latest_panel_excludes_rejected(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_lab(value=9.9, lab_date=date(2026, 7, 1))])
    assert row_id is not None
    db.reject_row(row_id)
    panel = {row.name: row for row in db.latest_panel()}
    assert "potassium" not in panel


def test_abnormal_since_filters_by_flag_and_date(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(value=6.0, lab_date=date(2026, 1, 1), flag=LabFlag.HIGH),
            _lab(value=6.0, lab_date=date(2026, 8, 1), flag=LabFlag.HIGH),
            _lab(name="sodium", value=140.0, lab_date=date(2026, 8, 1), ucum_unit="mmol/L"),
        ]
    )
    abnormal = db.abnormal_since(date(2026, 6, 1))
    assert len(abnormal) == 1
    assert abnormal[0].flag == LabFlag.HIGH
    assert abnormal[0].date == date(2026, 8, 1)


def test_fts_search_matches_name_and_name_raw(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(name="ANA titer", name_raw="Antinuclear Antibody", value=None, value_text="1:320"),
            _lab(name="sodium", value=140.0, ucum_unit="mmol/L"),
        ]
    )
    by_canonical = db.search("ANA")
    assert any(r.name == "ANA titer" for r in by_canonical)

    by_raw = db.search("Antinuclear")
    assert any(r.name_raw == "Antinuclear Antibody" for r in by_raw)

    assert db.search("nonexistent-analyte-zzz") == []


def test_jsonl_export_rebuild_round_trip_is_lossless(db: LabsDb, tmp_path: Path) -> None:
    db.insert_results(
        [
            _lab(name="potassium", value=4.1, lab_date=date(2026, 5, 2)),
            _lab(
                name="ANA titer",
                name_raw="ANA",
                value=None,
                value_text="1:160",
                ucum_unit=None,
                ref_low=None,
                ref_high=None,
                lab_date=date(2026, 5, 2),
                extraction_status=ExtractionStatus.PENDING,
            ),
            _lab(name="sodium", value=140.0, ucum_unit="mmol/L", source_doc=SHA_B),
        ]
    )
    # exercise every status path so the export covers the full row shape
    rows = db.series("potassium")
    db.confirm_row(rows[0].id)  # type: ignore[arg-type]

    export_a = tmp_path / "export-a.jsonl"
    db.export_jsonl(export_a)

    rebuilt = LabsDb(tmp_path / "rebuilt.sqlite")
    rebuilt.rebuild_from_jsonl(export_a)

    export_b = tmp_path / "export-b.jsonl"
    rebuilt.export_jsonl(export_b)

    assert export_a.read_text(encoding="utf-8") == export_b.read_text(encoding="utf-8")

    # and the rebuilt db is behaviorally identical, not just byte-identical on export
    assert [r.model_dump() for r in rebuilt.list_documents()] == [
        d.model_dump() for d in db.list_documents()
    ]
    assert [r.model_dump() for r in rebuilt.series("potassium")] == [
        r.model_dump() for r in db.series("potassium")
    ]
    assert [r.id for r in rebuilt.pending()] == [r.id for r in db.pending()]


def test_rebuild_from_jsonl_is_idempotent_and_replaces_existing_content(
    db: LabsDb, tmp_path: Path
) -> None:
    db.insert_results([_lab()])
    export_path = tmp_path / "export.jsonl"
    db.export_jsonl(export_path)

    other = LabsDb(tmp_path / "other.sqlite")
    other.upsert_document(_doc(sha256="c" * 64, filename="stale.pdf"))
    other.rebuild_from_jsonl(export_path)
    other.rebuild_from_jsonl(export_path)  # rebuilding twice must not duplicate/error

    assert {d.sha256 for d in other.list_documents()} == {SHA_A, SHA_B}
    assert len(other.series("potassium")) == 1


# --------------------------------------------------------------------------
# Specimen dimension: migration/schema, unique constraint, latest_panel,
# series filtering, and old/new-format JSONL rebuild tolerance.
# --------------------------------------------------------------------------


def test_migration_adds_specimen_column_with_unknown_default(db: LabsDb) -> None:
    version = db._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 5  # 5: encounter_text corpus (ADR 0015 extended to encounters)
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(labs)").fetchall()}
    assert "specimen" in columns

    (row_id,) = db.insert_results([_lab()])
    row = db.get_row(row_id)  # type: ignore[arg-type]
    assert row is not None
    assert row.specimen == "unknown"


def test_unique_constraint_allows_same_analyte_different_specimen_same_doc(db: LabsDb) -> None:
    """The real finding this whole slice is about: a urinalysis GLUCOSE
    "NEGATIVE" reading and a serum glucose mg/dL reading, same document,
    same date, same canonical name - now distinguished by specimen instead
    of colliding on the UNIQUE(date, name, source_doc) constraint."""
    ids = db.insert_results(
        [
            _lab(
                name="glucose",
                value=None,
                value_text="NEGATIVE",
                ucum_unit=None,
                ref_low=None,
                ref_high=None,
                specimen="urine",
            ),
            _lab(name="glucose", value=92.0, ucum_unit="mg/dL", specimen="serum"),
        ]
    )
    assert all(i is not None for i in ids)
    assert len(db.series("glucose")) == 2
    assert len(db.series("glucose", "urine")) == 1
    assert len(db.series("glucose", "serum")) == 1


def test_unique_constraint_still_dedupes_within_same_specimen(db: LabsDb) -> None:
    first = _lab(specimen="urine")
    duplicate = _lab(specimen="urine")  # identical (date, name, specimen, source_doc)

    ids = db.insert_results([first, duplicate])
    assert ids[0] is not None
    assert ids[1] is None
    assert len(db.series("potassium")) == 1


def test_series_specimen_none_returns_all_specimens(db: LabsDb) -> None:
    db.insert_results(
        [
            _lab(name="glucose", value=None, value_text="NEGATIVE", specimen="urine"),
            _lab(name="glucose", value=92.0, ucum_unit="mg/dL", specimen="serum"),
        ]
    )
    assert len(db.series("glucose")) == 2
    assert len(db.series("glucose", None)) == 2


def test_latest_panel_splits_by_specimen_not_just_name(db: LabsDb) -> None:
    """Before this dimension existed, `latest_panel` grouped by `name`
    alone, so a urine glucose reading and a serum glucose reading (same
    canonical name) would collide - only the later-dated one would ever
    surface. Grouping by (name, specimen) fixes that."""
    db.insert_results(
        [
            _lab(
                name="glucose",
                value=None,
                value_text="NEGATIVE",
                ucum_unit=None,
                ref_low=None,
                ref_high=None,
                lab_date=date(2026, 1, 1),
                specimen="urine",
            ),
            _lab(
                name="glucose",
                value=92.0,
                ucum_unit="mg/dL",
                lab_date=date(2026, 6, 1),
                specimen="serum",
            ),
        ]
    )
    panel = {(row.name, row.specimen): row for row in db.latest_panel()}
    assert panel[("glucose", "urine")].value_text == "NEGATIVE"
    assert panel[("glucose", "serum")].value == 92.0


def test_update_specimen_does_not_change_extraction_status(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_lab(extraction_status=ExtractionStatus.AUTO)])
    assert row_id is not None
    db.update_specimen(row_id, "urine")
    row = db.get_row(row_id)
    assert row is not None
    assert row.specimen == "urine"
    assert row.extraction_status == ExtractionStatus.AUTO


def test_rows_with_unknown_specimen_excludes_already_known(db: LabsDb) -> None:
    ids = db.insert_results([_lab(specimen="urine"), _lab(name="sodium", value=140.0)])
    assert all(i is not None for i in ids)
    unknown = db.rows_with_unknown_specimen()
    assert [r.name for r in unknown] == ["sodium"]


def test_jsonl_rebuild_tolerates_old_format_lines_missing_specimen(
    db: LabsDb, tmp_path: Path
) -> None:
    """A pre-migration export line has no `specimen` key at all in its `row`
    payload - `rebuild_from_jsonl` must still load it, defaulting to
    `"unknown"` (the pydantic model default), not raise."""
    (row_id,) = db.insert_results([_lab()])
    export_path = tmp_path / "export.jsonl"
    db.export_jsonl(export_path)

    lines = export_path.read_text(encoding="utf-8").splitlines()
    old_format_lines = []
    for line in lines:
        payload = json.loads(line)
        if payload["table"] == "lab":
            payload["row"].pop("specimen", None)  # simulate a pre-migration export
        old_format_lines.append(json.dumps(payload))
    old_export = tmp_path / "old-export.jsonl"
    old_export.write_text("\n".join(old_format_lines) + "\n", encoding="utf-8")

    rebuilt = LabsDb(tmp_path / "rebuilt-old.sqlite")
    rebuilt.rebuild_from_jsonl(old_export)

    row = rebuilt.get_row(row_id)  # type: ignore[arg-type]
    assert row is not None
    assert row.specimen == "unknown"


def test_jsonl_export_rebuild_round_trip_preserves_specimen(db: LabsDb, tmp_path: Path) -> None:
    db.insert_results(
        [
            _lab(name="glucose", value=None, value_text="NEGATIVE", specimen="urine"),
            _lab(name="glucose", value=92.0, ucum_unit="mg/dL", specimen="serum"),
        ]
    )
    export_a = tmp_path / "export-a.jsonl"
    db.export_jsonl(export_a)

    rebuilt = LabsDb(tmp_path / "rebuilt-new.sqlite")
    rebuilt.rebuild_from_jsonl(export_a)

    export_b = tmp_path / "export-b.jsonl"
    rebuilt.export_jsonl(export_b)
    assert export_a.read_text(encoding="utf-8") == export_b.read_text(encoding="utf-8")

    specimens = {row.specimen for row in rebuilt.series("glucose")}
    assert specimens == {"urine", "serum"}


# --------------------------------------------------------------------------
# resolve_with_pass (queue-ergonomics slice item 1): the confirm queue's
# "Use reading A"/"Use reading B" actions on a disagreement row.
# --------------------------------------------------------------------------


def _disagreement_row(db: LabsDb, *, pass_a: dict, pass_b: dict, **overrides: object) -> int:
    raw_json = json.dumps({"pass_a": pass_a, "pass_b": pass_b, "reasons": ["value_mismatch"]})
    fields: dict[str, object] = {
        "date": date(2026, 5, 2),
        "name": "ferritin",
        "name_raw": "ferritin",
        "value": pass_a["value"],
        "ucum_unit": pass_a.get("unit_raw"),
        "source_doc": SHA_A,
        "extraction_status": ExtractionStatus.PENDING,
        "raw_json": raw_json,
    }
    fields.update(overrides)
    (row_id,) = db.insert_results([LabResult.model_validate(fields)])
    assert row_id is not None
    return row_id


def test_resolve_with_pass_a_applies_pass_as_fields(db: LabsDb) -> None:
    row_id = _disagreement_row(
        db,
        pass_a={
            "name_raw": "ferritin",
            "value": 8.0,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": "L",
            "specimen": "serum",
        },
        pass_b={
            "name_raw": "ferritin",
            "value": 9.5,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": None,
            "specimen": "serum",
        },
    )

    db.resolve_with_pass(row_id, "a")

    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 8.0
    assert row.ucum_unit == "ng/mL"
    assert row.ref_low == 10.0
    assert row.ref_high == 200.0
    assert row.flag == LabFlag.LOW
    assert row.specimen == "serum"
    assert row.extraction_status == ExtractionStatus.CORRECTED
    payload = row.raw_payload()
    assert payload["resolved_with"] == "pass_a"
    # the original extraction (both passes) is left intact for audit
    assert payload["pass_a"]["value"] == 8.0
    assert payload["pass_b"]["value"] == 9.5


def test_resolve_with_pass_b_applies_pass_bs_fields(db: LabsDb) -> None:
    row_id = _disagreement_row(
        db,
        pass_a={
            "name_raw": "ferritin",
            "value": 8.0,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": "L",
            "specimen": "serum",
        },
        pass_b={
            "name_raw": "ferritin",
            "value": 9.5,
            "unit_raw": "ng/mL",
            "ref_range_raw": "10-200",
            "flag_raw": None,
            "specimen": "serum",
        },
    )

    db.resolve_with_pass(row_id, "b")

    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 9.5
    assert row.flag is None
    assert row.extraction_status == ExtractionStatus.CORRECTED
    payload = row.raw_payload()
    assert payload["resolved_with"] == "pass_b"


def test_resolve_with_pass_recanonicalizes_name(db: LabsDb) -> None:
    """The chosen pass's OWN name is what recomputes the canonical `name`
    - not whatever the row happened to carry before (a contrived case:
    the two passes disagreeing on the name outright, to prove
    recanonicalization actually runs per-pass rather than being carried
    over from the row's prior state)."""
    row_id = _disagreement_row(
        db,
        pass_a={"name_raw": "Sodium", "value": 140.0, "unit_raw": "mmol/L", "specimen": "serum"},
        pass_b={"name_raw": "Potassium", "value": 4.1, "unit_raw": "mmol/L", "specimen": "serum"},
        name="sodium",
        name_raw="Sodium",
    )

    db.resolve_with_pass(row_id, "b")

    row = db.get_row(row_id)
    assert row is not None
    assert row.name == "potassium"
    assert row.name_raw == "Potassium"
    assert row.value == 4.1


def test_resolve_with_pass_rejects_bad_which(db: LabsDb) -> None:
    row_id = _disagreement_row(
        db,
        pass_a={"name_raw": "ferritin", "value": 8.0},
        pass_b={"name_raw": "ferritin", "value": 9.5},
    )
    with pytest.raises(ValueError):
        db.resolve_with_pass(row_id, "c")  # type: ignore[arg-type]


def test_resolve_with_pass_raises_when_that_pass_has_no_data(db: LabsDb) -> None:
    raw_json = json.dumps(
        {
            "pass_a": {"name_raw": "ferritin", "value": 8.0},
            "pass_b": None,
            "reasons": ["single_pass"],
        }
    )
    (row_id,) = db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="ferritin",
                name_raw="ferritin",
                value=8.0,
                source_doc=SHA_A,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=raw_json,
            )
        ]
    )
    assert row_id is not None
    with pytest.raises(ValueError):
        db.resolve_with_pass(row_id, "b")


# --------------------------------------------------------------------------
# reject_row_as_twin / resolved_rows_for_document (queue-ergonomics slice
# item 4: the labs-dedupe-twins sweep).
# --------------------------------------------------------------------------


def test_reject_row_as_twin_marks_rejected_with_audit_note(db: LabsDb) -> None:
    (auto_id,) = db.insert_results([_lab(extraction_status=ExtractionStatus.AUTO)])
    (pending_id,) = db.insert_results(
        [_lab(name="sodium", value=140.0, extraction_status=ExtractionStatus.PENDING)]
    )
    assert auto_id is not None and pending_id is not None

    db.reject_row_as_twin(pending_id, twin_of=auto_id, method="rule")

    row = db.get_row(pending_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.REJECTED
    payload = row.raw_payload()
    assert payload["auto_rejected_twin_of"] == auto_id
    assert payload["method"] == "rule"


def test_reject_row_as_twin_stamps_llm_provenance_when_given(db: LabsDb) -> None:
    """CONFIRMED bug fix (CLAUDE.md provenance rule): an `llm`-method
    rejection persists `model_id`/`prompt_template_version`/`rejected_at`
    when the caller supplies them (`labs/twins.py` always does for its
    `method="llm"` path) - a `rule`-method rejection never gets a
    model_id/prompt_template_version stamped (no model was ever called).
    """
    (auto_id,) = db.insert_results([_lab(extraction_status=ExtractionStatus.AUTO)])
    (pending_id,) = db.insert_results(
        [_lab(name="sodium", value=140.0, extraction_status=ExtractionStatus.PENDING)]
    )
    assert auto_id is not None and pending_id is not None
    at = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)

    db.reject_row_as_twin(
        pending_id,
        twin_of=auto_id,
        method="llm",
        model_id="claude-fake-haiku",
        prompt_template_version="twin-classifier-v1",
        at=at,
    )

    row = db.get_row(pending_id)
    assert row is not None
    payload = row.raw_payload()
    assert payload["method"] == "llm"
    assert payload["model_id"] == "claude-fake-haiku"
    assert payload["prompt_template_version"] == "twin-classifier-v1"
    assert datetime.fromisoformat(payload["rejected_at"]) == at


def test_resolved_rows_for_document_excludes_pending_and_other_documents(db: LabsDb) -> None:
    (auto_id,) = db.insert_results([_lab(extraction_status=ExtractionStatus.AUTO)])
    (confirmed_id,) = db.insert_results(
        [_lab(name="sodium", value=140.0, extraction_status=ExtractionStatus.CONFIRMED)]
    )
    (pending_id,) = db.insert_results(
        [_lab(name="calcium", value=9.5, extraction_status=ExtractionStatus.PENDING)]
    )
    (other_doc_id,) = db.insert_results(
        [
            _lab(
                name="glucose",
                value=95.0,
                source_doc=SHA_B,
                extraction_status=ExtractionStatus.AUTO,
            )
        ]
    )
    assert all(i is not None for i in (auto_id, confirmed_id, pending_id, other_doc_id))

    resolved = db.resolved_rows_for_document(SHA_A)
    resolved_ids = {r.id for r in resolved}
    assert resolved_ids == {auto_id, confirmed_id}


# --------------------------------------------------------------------------
# insert_results re-extraction handling (D1): a row colliding with one
# already occupying its (date, name, specimen, source_doc) UNIQUE key must
# never be a silent no-op against a row a human already resolved one way or
# another - see `LabsDb.insert_results`'s docstring for the four cases.
# --------------------------------------------------------------------------


def test_insert_results_case_a_no_existing_row_inserts_normally(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_lab(value=4.1)])
    assert row_id is not None
    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 4.1
    assert row.extraction_status == ExtractionStatus.AUTO


def test_insert_results_case_b_revives_a_rejected_row_with_corrected_reading(db: LabsDb) -> None:
    """A rejected row occupies the UNIQUE key; a later re-extraction of the
    same document with a corrected reading must revive it (overwrite its
    fields, requeue PENDING), never silently drop the correction."""
    (row_id,) = db.insert_results([_lab(value=8.0)])
    assert row_id is not None
    db.reject_row(row_id)

    (result_id,) = db.insert_results([_lab(value=9.5)])

    assert result_id == row_id  # revived in place, not a new row
    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 9.5
    assert row.extraction_status == ExtractionStatus.PENDING
    payload = row.raw_payload()
    assert "re_extraction_after_rejection" in payload["reasons"]
    assert payload["superseded_rejection"]["value"] == 8.0
    assert len(db.series("potassium")) == 1


def test_insert_results_case_c_dedupes_identical_reading_over_confirmed_row(db: LabsDb) -> None:
    (row_id,) = db.insert_results([_lab(value=4.1)])
    assert row_id is not None
    db.confirm_row(row_id)

    result = db.insert_results([_lab(value=4.1)])  # identical value/unit

    assert result == [None]
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.CONFIRMED  # untouched
    assert len(db.series("potassium")) == 1


def test_insert_results_case_d_differing_reextraction_flips_confirmed_row_to_pending(
    db: LabsDb,
) -> None:
    """A confirmed row's key gets a re-extraction with a DIFFERENT value -
    never silently dropped: the existing (human-confirmed) reading is left
    untouched, but the row flips back to PENDING with the new reading
    merged in for a human to resolve."""
    (row_id,) = db.insert_results([_lab(value=4.1)])
    assert row_id is not None
    db.confirm_row(row_id)

    (result_id,) = db.insert_results([_lab(value=4.4)])

    assert result_id == row_id  # never silently dropped
    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 4.1  # the confirmed reading is untouched
    assert row.extraction_status == ExtractionStatus.PENDING
    payload = row.raw_payload()
    assert payload["re_extraction_conflict"]["value"] == 4.4
    assert any(r.startswith("re_extraction_conflict") for r in payload["reasons"])
    assert len(db.series("potassium")) == 1  # still one row, not duplicated


def test_insert_results_case_d_differing_reextraction_flips_pending_row_too(db: LabsDb) -> None:
    """Case (d) applies to a still-PENDING existing row too, not just a
    resolved one."""
    (row_id,) = db.insert_results([_lab(value=4.1, extraction_status=ExtractionStatus.PENDING)])
    assert row_id is not None

    (result_id,) = db.insert_results([_lab(value=41.0)])

    assert result_id == row_id
    row = db.get_row(row_id)
    assert row is not None
    assert row.value == 4.1
    assert row.extraction_status == ExtractionStatus.PENDING
    payload = row.raw_payload()
    assert payload["re_extraction_conflict"]["value"] == 41.0


# --------------------------------------------------------------------------
# document_text / document_text_fts (docs/adr/0015-document-text-corpus.md)
# --------------------------------------------------------------------------


def test_replace_document_text_stores_paginated_rows(db: LabsDb) -> None:
    db.replace_document_text(
        SHA_A,
        [DocumentTextPage(page=1, text="page one text"), DocumentTextPage(page=2, text="page two")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    rows = db._conn.execute(
        "SELECT page, text FROM document_text WHERE source_doc = ? ORDER BY page", (SHA_A,)
    ).fetchall()
    assert [(r["page"], r["text"]) for r in rows] == [(1, "page one text"), (2, "page two")]


def test_get_document_text_rejoins_pages_with_form_feed(db: LabsDb) -> None:
    db.replace_document_text(
        SHA_A,
        [DocumentTextPage(page=1, text="alpha"), DocumentTextPage(page=2, text="beta")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    assert db.get_document_text(SHA_A) == "alpha\fbeta"


def test_get_document_text_returns_none_when_never_stored(db: LabsDb) -> None:
    assert db.get_document_text(SHA_A) is None


def test_replace_document_text_is_idempotent(db: LabsDb) -> None:
    db.replace_document_text(
        SHA_A,
        [DocumentTextPage(page=None, text="first")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    db.replace_document_text(
        SHA_A,
        [DocumentTextPage(page=None, text="second, replacing the first")],
        extracted_at=datetime(2026, 5, 4, tzinfo=UTC),
    )
    rows = db._conn.execute(
        "SELECT text FROM document_text WHERE source_doc = ?", (SHA_A,)
    ).fetchall()
    assert [r["text"] for r in rows] == ["second, replacing the first"]


def test_document_text_shas_reflects_coverage_including_empty_text(db: LabsDb) -> None:
    assert db.document_text_shas() == set()
    db.replace_document_text(
        SHA_A, [DocumentTextPage(page=None, text="")], extracted_at=datetime(2026, 5, 3, tzinfo=UTC)
    )
    assert db.document_text_shas() == {SHA_A}


def test_search_document_text_finds_a_match_with_source_ref(db: LabsDb) -> None:
    db.replace_document_text(
        SHA_A,
        [
            DocumentTextPage(page=1, text="Nothing relevant here."),
            DocumentTextPage(page=2, text="Impression: findings consistent with early arthritis."),
        ],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    hits = db.search_document_text("arthritis")
    assert len(hits) == 1
    assert hits[0].source_doc == SHA_A
    assert hits[0].page == 2
    assert hits[0].source_ref == "doc:quest-2026-05-02.pdf#p2"
    assert "arthritis" in hits[0].snippet.lower()


def test_search_document_text_ref_has_no_page_suffix_when_unpaginated(db: LabsDb) -> None:
    db.replace_document_text(
        SHA_A,
        [DocumentTextPage(page=None, text="Patient-authored narrative mentions joint pain.")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    hits = db.search_document_text("joint pain")
    assert len(hits) == 1
    assert hits[0].page is None
    assert hits[0].source_ref == "doc:quest-2026-05-02.pdf"


def test_search_document_text_no_match_returns_empty(db: LabsDb) -> None:
    db.replace_document_text(
        SHA_A,
        [DocumentTextPage(page=None, text="unrelated content")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    assert db.search_document_text("nonexistent-token-xyz") == []


def test_search_document_text_respects_limit(db: LabsDb) -> None:
    pages = [DocumentTextPage(page=i, text=f"biopsy result {i}") for i in range(1, 8)]
    db.replace_document_text(SHA_A, pages, extracted_at=datetime(2026, 5, 3, tzinfo=UTC))
    hits = db.search_document_text("biopsy", limit=3)
    assert len(hits) == 3


def test_migration_adds_comparator_column_defaulting_to_null(db: LabsDb) -> None:
    """ADR 0025. Purely additive: an existing row keeps `comparator IS
    NULL`, which reads as "point measurement", so nothing already stored
    changes meaning when the column appears."""
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(labs)").fetchall()}

    assert "comparator" in columns


def test_a_bounded_result_round_trips_through_the_db(db: LabsDb) -> None:
    """ "<20 Units" is stored as value=20.0 with comparator="<" rather than
    as the string "<20", so it can be trended and range-checked at all."""
    db.insert_results([_lab(name="rna-pol-iii-ab", value=20.0, comparator="<", ucum_unit="Units")])

    row = db.series("rna-pol-iii-ab")[0]

    assert (row.value, row.comparator) == (20.0, "<")


def test_a_bounded_result_survives_a_jsonl_rebuild(db: LabsDb, tmp_path: Path) -> None:
    """`labs.sqlite` is DERIVED — `labs-export.jsonl` is the committed
    source of truth, so a comparator that did not survive the round trip
    would be silently lost on the next rebuild."""
    db.insert_results([_lab(name="estradiol", value=5.0, comparator="<", ucum_unit="pg/mL")])
    export = tmp_path / "labs-export.jsonl"
    db.export_jsonl(export)

    db.rebuild_from_jsonl(export)

    row = db.series("estradiol")[0]
    assert (row.value, row.comparator) == (5.0, "<")


def test_an_ordinary_result_keeps_a_null_comparator(db: LabsDb) -> None:
    """`None` means "point measurement" — the overwhelmingly common case,
    and what every pre-existing row migrates to."""
    db.insert_results([_lab()])

    assert db.series("potassium")[0].comparator is None

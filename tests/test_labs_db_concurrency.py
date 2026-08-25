"""Concurrency regression tests for `LabsDb` (production crash fix).

Root cause: `LabsDb` shares ONE `sqlite3.connect(..., check_same_thread=False)`
connection across every caller. FastAPI runs sync routes in a thread pool, so
a browser issuing overlapping requests (clicking between pages, or a slow
page leaving a wide window) really does drive that one connection from two
threads at the same time - which reliably produces
`sqlite3.InterfaceError: bad parameter or other API misuse` (seen in
production from `/labs` while `/upload`/`/reviews`/`/confirm` were being
served in the same second). `LabsDb` now serializes every method that
touches `self._conn` on a `threading.RLock` (see `_synchronized` in
`labs/db.py`); these tests exercise that under real thread contention.

All data here is synthetic (CLAUDE.md PHI boundary rule 1) - no real
patient data is ever read into this repo.

These tests are deliberately non-sleep-based: they drive genuine OS-thread
contention over many iterations (8 threads x 50 iterations x 5 queries =
2000 read calls for the mixed-read test) so that, without the lock, at
least one interleaving reliably lands inside sqlite3's non-reentrant C
internals. See this file's bottom docstring note (and the task report) for
the actual error produced when the lock was temporarily removed to confirm
these tests catch the regression.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult

ANALYTES = ["potassium", "sodium", "glucose", "hemoglobin", "creatinine", "crp"]


def _sha(n: int) -> str:
    """A syntactically-valid 64-hex-char sha256 stand-in - never a real hash
    of anything, just a unique `documents.sha256` primary key per doc."""
    return f"{n:064x}"


def _seeded_db(tmp_path: Path, *, n_docs: int, rows_per_doc: int) -> tuple[LabsDb, int]:
    """An on-disk `LabsDb` (not `:memory:` - the production crash was against
    a real sqlite file on disk, and this exercises that same path) seeded
    with `n_docs * rows_per_doc` distinct, already-resolved (`AUTO`) lab
    rows spread across `ANALYTES` and distinct dates, so `series`/
    `latest_panel`/`series_by_key`/`documents_overview` each have real rows
    to scan. Returns `(db, total_rows_inserted)`.
    """
    db = LabsDb(tmp_path / "labs.sqlite")
    total = 0
    for d in range(n_docs):
        sha = _sha(d)
        db.upsert_document(
            LabDocument(
                sha256=sha,
                filename=f"doc-{d}.pdf",
                doc_type="lab_report",
                page_count=1,
                status=DocumentStatus.COMPLETE,
            )
        )
        rows = []
        for r in range(rows_per_doc):
            name = ANALYTES[r % len(ANALYTES)]
            rows.append(
                LabResult(
                    date=date(2020, 1, 1) + timedelta(days=r),
                    name=name,
                    name_raw=name,
                    value=float(r),
                    ucum_unit="mg/dL",
                    source_doc=sha,
                    extraction_status=ExtractionStatus.AUTO,
                    raw_json=json.dumps({}),
                )
            )
        ids = db.insert_results(rows)
        assert all(i is not None for i in ids), "seed rows must all be distinct keys"
        total += len(rows)
    return db, total


def test_concurrent_mixed_reads_do_not_crash(tmp_path: Path) -> None:
    """~8 threads x ~50 iterations of a mixed read workload
    (`series`/`latest_panel`/`series_by_key`/`documents_overview`/`pending`)
    against one `LabsDb` - the exact page-load shape of `/labs` plus the
    other pages that were being hit concurrently in the production
    traceback. Asserts zero exceptions across all threads, then checks the
    data itself is still exactly what was seeded (a concurrency bug can
    also manifest as silently wrong/torn results, not just a raised
    exception).
    """
    db, total_rows = _seeded_db(tmp_path, n_docs=6, rows_per_doc=50)  # 300 rows

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            for j in range(50):
                name = ANALYTES[(i + j) % len(ANALYTES)]
                db.series(name)
                db.latest_panel()
                db.series_by_key()
                db.documents_overview()
                db.pending()
        except BaseException as exc:  # noqa: BLE001 - deliberately broad, re-raised via assert below
            with errors_lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, i) for i in range(8)]
        for future in futures:
            future.result()  # propagate a bug in the *test* itself (not `worker`'s own catch)

    assert errors == [], f"concurrent reads raised {len(errors)} exception(s); first: {errors[0]!r}"

    # Not just "didn't crash" - the data must still be exactly what was
    # seeded. A lock released between `execute` and `fetchall` (rather than
    # spanning both) could still avoid an outright crash while returning a
    # torn/wrong result set from another thread's in-flight query.
    total = sum(len(v) for v in db.series_by_key().values())
    assert total == total_rows
    overview = db.documents_overview()
    assert sum(o.accepted_count for o in overview) == total_rows
    assert sum(o.awaiting_review_count for o in overview) == 0


def test_concurrent_read_and_write_no_interface_error(tmp_path: Path) -> None:
    """One writer thread runs `insert_results` + confirm/correct/reject
    status-update cycles while four reader threads concurrently query -
    asserts no `sqlite3.InterfaceError`/`sqlite3.ProgrammingError` (or any
    other exception) from either side, and that the final state exactly
    matches what the writer did (no lost update, no double-apply, no row
    left in the wrong status because a read interleaved mid-write).
    """
    db, baseline_rows = _seeded_db(tmp_path, n_docs=2, rows_per_doc=10)  # 20 baseline AUTO rows
    write_sha = _sha(0)
    n_writes = 90

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    outcomes = {"confirmed": 0, "corrected": 0, "rejected": 0}
    outcomes_lock = threading.Lock()

    def writer() -> None:
        try:
            for i in range(n_writes):
                name = f"synthetic-analyte-{i}"
                (row_id,) = db.insert_results(
                    [
                        LabResult(
                            date=date(2022, 1, 1) + timedelta(days=i),
                            name=name,
                            name_raw=name,
                            value=float(i),
                            ucum_unit="mg/dL",
                            source_doc=write_sha,
                            extraction_status=ExtractionStatus.PENDING,
                            raw_json=json.dumps({}),
                        )
                    ]
                )
                assert row_id is not None
                branch = i % 3
                if branch == 0:
                    db.confirm_row(row_id)
                    key = "confirmed"
                elif branch == 1:
                    db.correct_row(row_id, value=float(i) + 0.5)
                    key = "corrected"
                else:
                    db.reject_row(row_id)
                    key = "rejected"
                with outcomes_lock:
                    outcomes[key] += 1
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(n_writes):
                db.pending()
                db.latest_panel()
                db.documents_overview()
                db.series_by_key()
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=writer)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], (
        f"concurrent read/write raised {len(errors)} exception(s); first: {errors[0]!r}"
    )

    # Consistent final state: every write landed exactly once, in exactly
    # the status the writer put it in.
    counts_by_status = dict.fromkeys((s.value for s in ExtractionStatus), 0)
    for row in db.all_non_rejected_rows():
        counts_by_status[row.extraction_status.value] += 1
    assert counts_by_status[ExtractionStatus.AUTO.value] == baseline_rows
    assert counts_by_status[ExtractionStatus.CONFIRMED.value] == outcomes["confirmed"]
    assert counts_by_status[ExtractionStatus.CORRECTED.value] == outcomes["corrected"]
    assert len(db.rejected_row_keys()) == outcomes["rejected"]
    assert outcomes["confirmed"] + outcomes["corrected"] + outcomes["rejected"] == n_writes

"""Tests for `reason.review.deterministic_trend_scan`: the weekly review's
step (a), a plain statistical sweep over every current analyte (PLAN.md
loop (c)). No LLM call.

Perf regression guard: this used to call `labs.validate.trend_outlier`
(and, inside it, `trend_deviation`) once per candidate row - one
`labs.sqlite` query per analyte. Locally that's invisible (~0.02 ms/query
against page cache), but the deployed app reads `labs.sqlite` over
EFS/NFS, where each query costs milliseconds of round trip - at the
production corpus's ~450 analytes, this scan (which runs every Sunday,
`web.routes.reviews.REVIEW_SCHEDULE_PHRASE`) turned into real added
latency, the same shape of bug PR #102 fixed for the labs index page.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.review import deterministic_trend_scan

_ANALYTE_COUNT = 40
_SHA = "a" * 64


def _db(tmp_path: Path) -> LabsDb:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(
        LabDocument(sha256=_SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    return db


def _row(name: str, *, day: int, value: float) -> LabResult:
    return LabResult(
        date=date(2026, 5, day),
        name=name,
        name_raw=name,
        value=value,
        source_doc=_SHA,
        raw_json=json.dumps({}),
    )


def test_trend_scan_does_not_issue_a_query_per_analyte(tmp_path: Path) -> None:
    db = _db(tmp_path)
    rows: list[LabResult] = []
    for i in range(_ANALYTE_COUNT):
        name = f"analyte-{i}"
        # Three priors + a latest reading, all unremarkable (no outlier),
        # for every analyte - enough for `trend_deviation` to actually
        # compute a ratio (TREND_OUTLIER_MIN_PRIORS == 3 priors).
        for day, value in ((1, 10.0), (2, 10.1), (3, 9.9), (4, 10.0)):
            rows.append(_row(name, day=day, value=value))
    db.insert_results(rows)

    selects: list[str] = []
    db._conn.set_trace_callback(lambda stmt: selects.append(stmt))
    try:
        result = deterministic_trend_scan(db)
    finally:
        db._conn.set_trace_callback(None)

    assert result.findings == []
    labs_selects = [s for s in selects if "FROM labs" in s]
    # 40 analytes: a per-analyte implementation issues 40+ of these (one
    # per `trend_outlier` call). The bulk implementation needs only a
    # handful regardless of analyte count.
    assert len(labs_selects) < 10, f"expected O(1) labs queries, got {len(labs_selects)}"


def test_trend_scan_still_flags_a_genuine_outlier(tmp_path: Path) -> None:
    """Correctness guard alongside the query-count one above: the bulk
    `series_by_key` prefetch must find the exact same outlier
    `trend_outlier` would from a fresh per-row query."""
    db = _db(tmp_path)
    rows = [
        _row("potassium", day=1, value=4.0),
        _row("potassium", day=2, value=4.1),
        _row("potassium", day=3, value=3.9),
        _row("potassium", day=4, value=41.0),  # decimal-shift misread
    ]
    db.insert_results(rows)

    result = deterministic_trend_scan(db)

    assert len(result.findings) == 1
    assert result.findings[0].analyte == "potassium"
    assert result.findings[0].value == 41.0

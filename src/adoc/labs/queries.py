"""Read-side query helpers over `LabsDb`, for later use by chat tools + UI.

Thin, deliberately dumb wrappers: all the real logic (schema, dedupe,
FTS) lives in `db.py`. This module exists so chat tools (`query_labs`,
PLAN.md "Reasoner integration") and the web UI (trend charts, confirm queue,
document listing) have one stable, read-only surface to import instead of
reaching into `LabsDb` internals directly.
"""

from __future__ import annotations

from datetime import date

from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult, Specimen
from adoc.labs.reference import is_abnormal


def trend_series(db: LabsDb, name: str, specimen: Specimen | None = None) -> list[LabResult]:
    """Time-ordered results for one canonical analyte, ref ranges included.

    Each `LabResult` already carries `ref_low`/`ref_high`/`ref_text`, so the
    trend chart / composer can render the reference band alongside values
    without a second query. `specimen=None` (default) returns every
    specimen's readings for `name`; pass a specimen to scope the series to
    just that one (see `LabsDb.series`).
    """
    return db.series(name, specimen)


def abnormal_summary(db: LabsDb, since: date | None = None) -> list[LabResult]:
    """Out-of-range results, most recent first.

    "Abnormal" is a COMPARISON, not a flag (ADR 0051). This used to be
    `row.flag is not None`, and only 187 of 2079 stored rows carry a flag —
    so the abnormal set, and with it the Abnormal section of every context
    pack a model ever reads, was drawn from 9% of the record. 42 rows sit
    measurably above their reference range with no flag at all.

    `range_position` falls back to the numeric bounds and then to `ref_text`,
    and returns `None` — never "normal" — when the record cannot say.

    With `since`, returns every FLAGGED row on/after that date
    (`db.abnormal_since`), which is a database query and still flag-based;
    that path is used for trend seeding rather than for judging a value.
    """
    if since is not None:
        return db.abnormal_since(since)
    return [row for row in db.latest_panel() if is_abnormal(row)]


def units_seen(db: LabsDb, name: str) -> list[str]:
    """Distinct units ever recorded for one canonical analyte, sorted.

    Useful for surfacing unit drift across documents/labs (e.g. a facility
    reporting glucose in mmol/L instead of mg/dL) even when
    `validate.ANALYTE_SPECS` doesn't (yet) cover the analyte.
    """
    units = {row.ucum_unit for row in db.series(name, include_rejected=True) if row.ucum_unit}
    return sorted(units)


def document_listing(db: LabsDb) -> list[LabDocument]:
    """All ingested source documents, most recently ingested first."""
    return db.list_documents()

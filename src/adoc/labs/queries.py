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
    """Abnormal (flagged) results, most recent first.

    With `since`, returns every flagged row on/after that date
    (`db.abnormal_since`). Without it, returns only each analyte's *latest*
    flagged result (from `db.latest_panel`) — a snapshot of what's currently
    abnormal rather than a full history.
    """
    if since is not None:
        return db.abnormal_since(since)
    return [row for row in db.latest_panel() if row.flag is not None]


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

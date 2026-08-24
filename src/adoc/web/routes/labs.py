"""Labs surface (PLAN.md "UI"): an analyte list with a sparkline overview,
and a per-analyte Plotly trend chart (values + reference-range band, flags
marked) plus a plain reading-by-reading table, fed by a small JSON endpoint.

Routing note (see `encode_analyte_id`/`decode_analyte_id`): real analyte
names carry characters a plain `{name}` path parameter can't survive - most
notably a literal "/" ("A/G Ratio"), which the ASGI server splits into two
path segments before Starlette's router ever sees the request, 404ing a
single-segment route even when the "/" arrives percent-encoded. Detail and
data routes key off a base64 identifier instead, which round-trips any
analyte name byte-for-byte and never contains a "/".
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, get_args

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult, Specimen
from adoc.labs.queries import trend_series
from adoc.labs.validate import ANALYTE_SPECS, canonicalize
from adoc.web.deps import get_db
from adoc.web.templating import templates

router = APIRouter(prefix="/labs")

_VALID_SPECIMENS = frozenset(get_args(Specimen))


def _parse_specimen(value: str | None) -> Specimen | None:
    """A `?specimen=` query value, if it's one of `Specimen`'s literal
    values - `None` (all specimens) for anything else, including an
    absent or unrecognized value. Never raises on bad client input."""
    if value in _VALID_SPECIMENS:
        return value  # type: ignore[return-value]
    return None


_SPARK_WIDTH = 120
_SPARK_HEIGHT = 28
_SPARK_PAD = 3

# Only the urlsafe-base64 alphabet (RFC 4648 sec. 5) - anything else can't
# possibly be one of our encoded ids, so `decode_analyte_id` treats it as a
# legacy literal analyte name instead of trying (and failing) to decode it.
_ANALYTE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def encode_analyte_id(name: str) -> str:
    """A urlsafe, slash-free, round-trip-stable identifier for an analyte's
    canonical `name`, used as `/labs/{id}`'s path segment.

    Base64 (urlsafe alphabet) never emits "/" and round-trips any input
    byte-for-byte, sidestepping the routing bug above for every analyte
    name - "A/G Ratio", "B. MIYAMOTOI AB (IGG)", names with "%", long
    FRAX-style names, all of it. Padding ("=") is stripped (some
    proxies/browsers mishandle a literal "=" in a path segment) and
    restored on decode.
    """
    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def decode_analyte_id(name_id: str) -> str | None:
    """The analyte name `name_id` decodes to, or `None` if it isn't a
    validly-encoded id.

    `None` also covers a *legacy* `/labs/{name}` bookmark saved before this
    fix (a literal, unencoded - or `quote()`-encoded - analyte name):
    callers fall back to trying `name_id` directly as an analyte name in
    that case (see `labs_detail`), so an old bookmark still resolves
    instead of hard-404ing forever.
    """
    if not _ANALYTE_ID_RE.match(name_id):
        return None
    padded = name_id + "=" * (-len(name_id) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _sparkline_svg(values: list[float]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        # A single reading isn't an error, just too few points for a trend
        # line - render one visible dot rather than leaving blank space.
        cx, cy = _SPARK_WIDTH / 2, _SPARK_HEIGHT / 2
        return (
            f'<svg viewBox="0 0 {_SPARK_WIDTH} {_SPARK_HEIGHT}" class="sparkline" '
            f'role="img" aria-label="one reading so far">'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="currentColor" /></svg>'
        )
    low, high = min(values), max(values)
    span = high - low or 1.0
    usable_w = _SPARK_WIDTH - 2 * _SPARK_PAD
    usable_h = _SPARK_HEIGHT - 2 * _SPARK_PAD
    step = usable_w / (len(values) - 1)

    points = []
    for index, value in enumerate(values):
        x = _SPARK_PAD + index * step
        y = _SPARK_PAD + usable_h - ((value - low) / span) * usable_h
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)
    return (
        f'<svg viewBox="0 0 {_SPARK_WIDTH} {_SPARK_HEIGHT}" class="sparkline" '
        f'preserveAspectRatio="none" role="img" aria-label="trend sparkline">'
        f'<polyline points="{polyline}" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" /></svg>'
    )


def _numeric_values(rows: list[LabResult]) -> list[float]:
    return [row.value for row in rows if row.value is not None]


def _is_score_analyte(name: str) -> bool:
    """True for a FRAX/T-score/Z-score-shaped analyte (mirrors
    `web.routes.confirm._is_score_row`, lifted to the whole-analyte level
    since the detail page labels an entire section, not one row): these
    carry neither a unit nor a clinical reference range by nature, so the
    detail page says so plainly instead of showing blank columns."""
    canonical = canonicalize(name) or name
    spec = ANALYTE_SPECS.get(canonical)
    return spec is not None and spec.kind == "score"


@router.get("")
def labs_index(request: Request, db: LabsDb = Depends(get_db)) -> Response:
    analytes: list[dict[str, Any]] = []
    # `latest_panel()` is keyed by (name, specimen), not just name, so a
    # serum glucose reading and a urinalysis GLUCOSE reading each get their
    # own row here instead of one silently hiding the other's latest
    # value. Each analyte's sparkline is scoped to its own specimen for
    # the same reason - a mixed-specimen sparkline would be meaningless.
    for latest in db.latest_panel():
        series = trend_series(db, latest.name, latest.specimen)
        values = _numeric_values(series)
        analytes.append(
            {
                "name": latest.name,
                "specimen": latest.specimen,
                "latest": latest,
                "sparkline_svg": _sparkline_svg(values),
                "url_name": encode_analyte_id(latest.name),
            }
        )
    return templates.TemplateResponse(request, "labs_index.html", {"analytes": analytes})


def _reading_rows(
    series: list[LabResult], doc_lookup: dict[str, LabDocument]
) -> list[dict[str, Any]]:
    """Plain-table view rows for one specimen's readings - qualitative
    (`value_text`-only) readings are included here (with their text) even
    though they're never plotted on the chart above."""
    rows: list[dict[str, Any]] = []
    for row in series:
        doc = doc_lookup.get(row.source_doc)
        rows.append(
            {
                "date": row.date,
                "value": row.value,
                "value_text": row.value_text,
                "unit": row.ucum_unit,
                "flag": row.flag.value if row.flag else None,
                "source_filename": doc.filename if doc else "a document",
                "source_url": f"/files/original/{row.source_doc}",
            }
        )
    return rows


@router.get("/{name_id}")
def labs_detail(request: Request, name_id: str, db: LabsDb = Depends(get_db)) -> Response:
    # A canonical name can carry more than one specimen (the urinalysis
    # GLUCOSE / serum glucose finding) - the detail page renders one chart
    # (plus its own reading table) per specimen actually present, rather
    # than merging them into a single misleading series.
    name = decode_analyte_id(name_id)
    if name is None:
        # Legacy `/labs/{name}` bookmark (pre-fix: a literal, unencoded
        # analyte name). If it still resolves to real data, send the
        # browser on to the canonical id-based URL instead of quietly
        # rendering under the raw name forever; if it doesn't, fall
        # through and render the ordinary "no data" page under `name_id`
        # as typed - this route's long-standing behavior for an
        # unrecognized analyte name.
        if trend_series(db, name_id):
            return RedirectResponse(f"/labs/{encode_analyte_id(name_id)}", status_code=308)
        name = name_id

    series = trend_series(db, name)
    specimens = sorted({row.specimen for row in series})
    doc_lookup = {doc.sha256: doc for doc in db.list_documents()}
    is_score = _is_score_analyte(name)

    sections = [
        {
            "specimen": specimen,
            "rows": _reading_rows([row for row in series if row.specimen == specimen], doc_lookup),
        }
        for specimen in specimens
    ]

    return templates.TemplateResponse(
        request,
        "labs_detail.html",
        {
            "name": name,
            "name_id": encode_analyte_id(name),
            "has_data": len(series) > 0,
            "specimens": specimens,
            "sections": sections,
            "is_score": is_score,
        },
    )


@router.get("/{name_id}/data")
def labs_data(name_id: str, specimen: str | None = None, db: LabsDb = Depends(get_db)) -> Response:
    name = decode_analyte_id(name_id) or name_id
    series = trend_series(db, name, _parse_specimen(specimen))
    payload = {
        "name": name,
        "specimen": specimen,
        "dates": [row.date.isoformat() for row in series],
        "values": [row.value for row in series],
        "value_text": [row.value_text for row in series],
        "units": [row.ucum_unit for row in series],
        "ref_low": [row.ref_low for row in series],
        "ref_high": [row.ref_high for row in series],
        "flags": [row.flag.value if row.flag else None for row in series],
    }
    return JSONResponse(payload)

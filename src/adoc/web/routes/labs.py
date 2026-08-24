"""Labs surface (PLAN.md "UI"): an analyte list with a sparkline overview,
and a per-analyte Plotly trend chart (values + reference-range band, flags
marked) fed by a small JSON endpoint.
"""

from __future__ import annotations

from typing import Any, get_args
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, Response

from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult, Specimen
from adoc.labs.queries import trend_series
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


def _sparkline_svg(values: list[float]) -> str:
    if len(values) < 2:
        return ""
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
                "url_name": quote(latest.name, safe=""),
            }
        )
    return templates.TemplateResponse(request, "labs_index.html", {"analytes": analytes})


@router.get("/{name}")
def labs_detail(request: Request, name: str, db: LabsDb = Depends(get_db)) -> Response:
    # A canonical name can carry more than one specimen (the urinalysis
    # GLUCOSE / serum glucose finding) - the detail page renders one chart
    # per specimen actually present, rather than merging them into a
    # single misleading series.
    series = trend_series(db, name)
    specimens = sorted({row.specimen for row in series})
    return templates.TemplateResponse(
        request,
        "labs_detail.html",
        {"name": name, "has_data": len(series) > 0, "specimens": specimens},
    )


@router.get("/{name}/data")
def labs_data(name: str, specimen: str | None = None, db: LabsDb = Depends(get_db)) -> Response:
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

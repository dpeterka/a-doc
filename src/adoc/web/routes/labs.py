"""Labs surface (PLAN.md "UI"): an analyte list with a sparkline overview,
and a per-analyte Plotly trend chart (values + reference-range band, flags
marked) fed by a small JSON endpoint.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, Response

from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.labs.queries import trend_series
from adoc.web.deps import get_db
from adoc.web.templating import templates

router = APIRouter(prefix="/labs")

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
    for latest in db.latest_panel():
        series = trend_series(db, latest.name)
        values = _numeric_values(series)
        analytes.append(
            {
                "name": latest.name,
                "latest": latest,
                "sparkline_svg": _sparkline_svg(values),
                "url_name": quote(latest.name, safe=""),
            }
        )
    return templates.TemplateResponse(request, "labs_index.html", {"analytes": analytes})


@router.get("/{name}")
def labs_detail(request: Request, name: str, db: LabsDb = Depends(get_db)) -> Response:
    series = trend_series(db, name)
    return templates.TemplateResponse(
        request,
        "labs_detail.html",
        {"name": name, "has_data": len(series) > 0},
    )


@router.get("/{name}/data")
def labs_data(name: str, db: LabsDb = Depends(get_db)) -> Response:
    series = trend_series(db, name)
    payload = {
        "name": name,
        "dates": [row.date.isoformat() for row in series],
        "values": [row.value for row in series],
        "value_text": [row.value_text for row in series],
        "units": [row.ucum_unit for row in series],
        "ref_low": [row.ref_low for row in series],
        "ref_high": [row.ref_high for row in series],
        "flags": [row.flag.value if row.flag else None for row in series],
    }
    return JSONResponse(payload)

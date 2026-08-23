"""Shared Jinja2 environment for every route module.

One `Jinja2Templates` instance so every template shares the same globals
(the persistent disclaimer text) and filters (`markdown_lite`, chip
class/label helpers for ledger tiers/status/origin).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from adoc.web.markdown_lite import render_markdown_lite

TEMPLATES_DIR = Path(__file__).parent / "templates"

DISCLAIMER_TEXT = (
    "a-doc is not a doctor and does not diagnose. It organizes your records and "
    "turns them into leads to discuss with your own doctor. If this is a "
    "medical emergency, call 911 (or your local emergency number) now."
)

_TIER_LABELS = {
    "most-likely": "Most Likely",
    "expanded": "Expanded",
    "cant-miss": "Can't-Miss",
}

_STATUS_LABELS = {
    "active": "Active",
    "patient-proposed": "Your idea",
    "challenged": "Being challenged",
    "ruled-out": "Ruled out",
    "confirmed-by-doctor": "Confirmed by your doctor",
    "parked": "Parked",
}

_ORIGIN_LABELS = {
    "model": "Suggested by a-doc",
    "patient": "Your own idea",
    "doctor": "From your doctor",
    "challenger": "Raised by the challenger check",
}


def tier_label(tier: str) -> str:
    return _TIER_LABELS.get(tier, tier)


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def origin_label(origin: str) -> str:
    return _ORIGIN_LABELS.get(origin, origin)


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["disclaimer_text"] = DISCLAIMER_TEXT
templates.env.filters["markdown_lite"] = render_markdown_lite
templates.env.filters["tier_label"] = tier_label
templates.env.filters["status_label"] = status_label
templates.env.filters["origin_label"] = origin_label

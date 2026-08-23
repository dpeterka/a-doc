"""Confirm queue surface (PLAN.md "Ingestion"): each pending row shown
beside its source page image, with Confirm / Correct / Reject actions
wired to `LabsDb`. Every action re-exports `labs-export.jsonl` and makes
one data-repo commit, per PLAN.md "State" (sqlite is derived; the JSONL
export + git history is the record of truth).
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.labs.db import LabsDb
from adoc.labs.models import LabFlag, LabResult
from adoc.web.casefile_helpers import page_image_url
from adoc.web.deps import get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/confirm")

LABS_EXPORT_RELPATH = "labs-export.jsonl"


def _row_view(repo: DataRepo, row: LabResult) -> dict[str, Any]:
    return {
        "row": row,
        "image_url": page_image_url(repo, row.source_doc, row.source_page),
    }


def _pending_views(repo: DataRepo, db: LabsDb) -> list[dict[str, Any]]:
    return [_row_view(repo, row) for row in db.pending()]


def _export_and_commit(repo: DataRepo, db: LabsDb, message: str) -> None:
    db.export_jsonl(repo.root / LABS_EXPORT_RELPATH)
    repo.commit(message, paths=[LABS_EXPORT_RELPATH])


@router.get("")
def confirm_queue(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request, "confirm.html", {"rows": _pending_views(repo, db), "error": None}
    )


@router.post("/{row_id}/confirm")
def confirm_row(
    request: Request,
    row_id: int,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    db.confirm_row(row_id)
    _export_and_commit(repo, db, f"confirm: row {row_id} confirmed")
    return templates.TemplateResponse(
        request, "_confirm_queue.html", {"rows": _pending_views(repo, db), "error": None}
    )


@router.post("/{row_id}/reject")
def reject_row(
    request: Request,
    row_id: int,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    db.reject_row(row_id)
    _export_and_commit(repo, db, f"confirm: row {row_id} rejected")
    return templates.TemplateResponse(
        request, "_confirm_queue.html", {"rows": _pending_views(repo, db), "error": None}
    )


def _parse_correction_fields(
    *,
    date: str | None,
    name: str | None,
    value: str | None,
    value_text: str | None,
    ucum_unit: str | None,
    ref_low: str | None,
    ref_high: str | None,
    flag: str | None,
) -> dict[str, Any]:
    """Build the `LabsDb.correct_row(**fields)` kwargs from the confirm form's
    (all-optional) inputs, skipping any left blank."""
    fields: dict[str, Any] = {}
    if date:
        fields["date"] = date_cls.fromisoformat(date)
    if name:
        fields["name"] = name
    if value:
        fields["value"] = float(value)
    if value_text:
        fields["value_text"] = value_text
    if ucum_unit:
        fields["ucum_unit"] = ucum_unit
    if ref_low:
        fields["ref_low"] = float(ref_low)
    if ref_high:
        fields["ref_high"] = float(ref_high)
    if flag:
        fields["flag"] = LabFlag(flag)
    return fields


@router.post("/{row_id}/correct")
def correct_row(
    request: Request,
    row_id: int,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
    date: str = Form(""),
    name: str = Form(""),
    value: str = Form(""),
    value_text: str = Form(""),
    ucum_unit: str = Form(""),
    ref_low: str = Form(""),
    ref_high: str = Form(""),
    flag: str = Form(""),
) -> Response:
    error: str | None = None
    try:
        fields = _parse_correction_fields(
            date=date or None,
            name=name or None,
            value=value or None,
            value_text=value_text or None,
            ucum_unit=ucum_unit or None,
            ref_low=ref_low or None,
            ref_high=ref_high or None,
            flag=flag or None,
        )
        if not fields:
            error = "Change at least one field before saving a correction."
        else:
            db.correct_row(row_id, **fields)
            _export_and_commit(repo, db, f"confirm: row {row_id} corrected")
    except ValueError as exc:
        error = f"Could not save that correction: {exc}"

    return templates.TemplateResponse(
        request, "_confirm_queue.html", {"rows": _pending_views(repo, db), "error": error}
    )

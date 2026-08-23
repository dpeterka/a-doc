"""Upload surface (PLAN.md session loop (a)): save into the data repo's
`inbox/`, then run the real ingestion pipeline and show its `IngestReport`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.ingest.archive import PageRenderer
from adoc.ingest.pipeline import ingest_file
from adoc.ingest.vision import VisionClient, VisionError
from adoc.labs.db import LabsDb
from adoc.web.deps import get_db, get_renderer, get_repo, get_vision
from adoc.web.templating import templates

router = APIRouter(prefix="/upload")


@router.get("")
def upload_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "upload.html", {"report": None, "error": None})


@router.post("")
async def upload_submit(
    request: Request,
    file: UploadFile = File(...),
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
    vision: VisionClient = Depends(get_vision),
    renderer: PageRenderer = Depends(get_renderer),
) -> Response:
    filename = Path(file.filename or "upload").name
    inbox_dir = repo.root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = inbox_dir / filename
    contents = await file.read()
    dest.write_bytes(contents)

    error: str | None = None
    report = None
    try:
        report = ingest_file(dest, repo=repo, db=db, vision=vision, renderer=renderer)
    except VisionError as exc:
        error = f"Could not read that document: {exc}"

    return templates.TemplateResponse(request, "upload.html", {"report": report, "error": error})

"""Upload surface (PLAN.md session loop (a)): save into the data repo's
`inbox/`, then run the real ingestion pipeline and show its `IngestReport`.

Before the pipeline ever runs, `detect_intake_kind` checks the saved file's
actual content (user decision: PDF, Word `.docx`, plain text `.txt`, and
`.zip` archives are supported — see `upload.html`'s note and the `accept`
attribute on the file input; genomic files such as 23andMe raw exports and
VCF/BCF are also accepted but are archived, never read as documents — see
`ingest.genomics`). An unsupported file is removed from `inbox/` right
away — never archived into `sources/`, never run through the pipeline —
and a warm, plain-language error names the file's type and what a-doc
does accept. A file that IS a supported type is ingested with
`inbox_root=inbox_dir`, opting it into `ingest.pipeline`'s post-ingest
inbox hygiene (deleted on success/duplicate, moved to `work/failed/` +
logged on error — see that module's docstring).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.ingest.archive import PageRenderer
from adoc.ingest.filetypes import detect_intake_kind
from adoc.ingest.pipeline import ingest_file
from adoc.ingest.vision import VisionClient, VisionError
from adoc.labs.db import LabsDb
from adoc.web.deps import get_db, get_renderer, get_repo, get_vision
from adoc.web.templating import templates

router = APIRouter(prefix="/upload")

SUPPORTED_TYPES_NOTE = "PDF, Word (.docx), text (.txt), and zip (.zip) files"

GENOMICS_NOTE = (
    "Genetic data files (23andMe exports, VCF/BCF) are stored for later "
    "genomic analysis, not read as documents."
)


def _unsupported_file_message(filename: str) -> str:
    """A warm, plain-language rejection that names the file's own type
    (best guess from its extension - `detect_intake_kind` only tells us
    "not one we support", not what it actually is) and what a-doc does
    accept. Never a stack trace or an internal exception string."""
    suffix = Path(filename).suffix.lower().lstrip(".")
    kind_desc = f"a .{suffix} file" if suffix else "a file of an unrecognized type"
    return (
        f"{filename} looks like {kind_desc}, but a-doc can only read {SUPPORTED_TYPES_NOTE} — "
        "lab reports, doctor letters, imaging reports, or health summaries. Please convert it "
        "or choose a different file."
    )


@router.get("")
def upload_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request, "upload.html", {"report": None, "error": None, "genomics_note": GENOMICS_NOTE}
    )


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

    if detect_intake_kind(dest) is None:
        dest.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request,
            "upload.html",
            {
                "report": None,
                "error": _unsupported_file_message(filename),
                "genomics_note": GENOMICS_NOTE,
            },
        )

    error: str | None = None
    report = None
    try:
        report = ingest_file(
            dest, repo=repo, db=db, vision=vision, renderer=renderer, inbox_root=inbox_dir
        )
    except VisionError as exc:
        error = f"Could not read that document: {exc}"

    return templates.TemplateResponse(
        request,
        "upload.html",
        {"report": report, "error": error, "genomics_note": GENOMICS_NOTE},
    )

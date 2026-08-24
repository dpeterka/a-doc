"""Failed-ingestion surface (PLAN.md "Ingestion" post-ingest inbox hygiene):
a document that failed to ingest is moved out of `inbox/` into
`work/failed/` by `ingest.pipeline`'s hygiene and recorded in
`work/failed/failures.jsonl` (see `ingest.failures`) so a failure is never
silently lost. This page lists those records with two actions per file:

- **Retry**: move the file back into `inbox/` and run `ingest_file` on it
  right now, showing the outcome; a success clears its failures.jsonl
  record (the pipeline's own hygiene handles re-recording it on a repeat
  failure - see `ingest.pipeline._apply_inbox_hygiene`).
- **Remove**: delete the file and its record for good (client-side confirm
  dialog; there is no undo).

Like every other route in this app (except `/login`, `/healthz`, and
`/static/*`), `SessionAuthMiddleware` requires a valid session cookie here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.ingest.archive import PageRenderer
from adoc.ingest.failures import (
    FailureRecord,
    failed_file_path,
    find_failure,
    flatten_relpath,
    read_failures,
    remove_failure,
    restore_to_inbox,
)
from adoc.ingest.pipeline import ingest_file
from adoc.ingest.vision import VisionClient
from adoc.labs.db import LabsDb
from adoc.web.deps import get_db, get_renderer, get_repo, get_vision
from adoc.web.templating import templates

router = APIRouter(prefix="/failed")


def _friendly_reason(reason: str) -> str:
    """A warm, plain-language gloss for a failure's raw exception string —
    never shown as a stack trace, matching the upload route's tone."""
    lowered = reason.lower()
    if "pdftoppm" in lowered or "poppler" in lowered:
        return "a-doc couldn't render this document's pages"
    if "docx" in lowered:
        return "a-doc couldn't read this Word document's text"
    if "not a pdf" in lowered or "unsupported" in lowered:
        return "a-doc doesn't recognize this file's type"
    return "a-doc ran into a problem reading this document"


@dataclass
class FailureView:
    record: FailureRecord
    flat_name: str
    friendly_reason: str


def _failure_views(repo: DataRepo) -> list[FailureView]:
    return [
        FailureView(
            record=record,
            flat_name=flatten_relpath(Path(record.original_inbox_path)),
            friendly_reason=_friendly_reason(record.reason),
        )
        for record in read_failures(repo)
    ]


def _context(repo: DataRepo, *, message: str | None = None) -> dict[str, Any]:
    return {"failures": _failure_views(repo), "message": message}


@router.get("")
def failed_list(request: Request, repo: DataRepo = Depends(get_repo)) -> Response:
    return templates.TemplateResponse(request, "failed.html", _context(repo))


@router.post("/{flat_name}/retry")
def retry_failure(
    request: Request,
    flat_name: str,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
    vision: VisionClient = Depends(get_vision),
    renderer: PageRenderer = Depends(get_renderer),
) -> Response:
    record = find_failure(repo, flat_name)
    if record is None:
        return templates.TemplateResponse(
            request, "failed.html", _context(repo, message="That file isn't on the list anymore.")
        )

    inbox_dir = repo.root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = restore_to_inbox(repo, record, inbox_dir)

    report = ingest_file(
        dest, repo=repo, db=db, vision=vision, renderer=renderer, inbox_root=inbox_dir
    )
    outcome = report.files[0]
    if outcome.outcome == "error":
        reason = _friendly_reason(outcome.issues[0] if outcome.issues else record.reason)
        message = f"{record.filename} still couldn't be processed: {reason}."
    else:
        # The pipeline's own hygiene already deleted the re-ingested inbox
        # copy; it doesn't know about this *pre-existing* failure record,
        # so clearing it is this route's job.
        remove_failure(repo, record.original_inbox_path)
        message = f"{record.filename}: {outcome.outcome}. It's off the failed list now."

    return templates.TemplateResponse(request, "failed.html", _context(repo, message=message))


@router.post("/{flat_name}/remove")
def remove_failed_file(
    request: Request, flat_name: str, repo: DataRepo = Depends(get_repo)
) -> Response:
    record = find_failure(repo, flat_name)
    message: str | None = None
    if record is not None:
        failed_file_path(repo, record).unlink(missing_ok=True)
        remove_failure(repo, record.original_inbox_path)
        message = f"Removed {record.filename}."

    return templates.TemplateResponse(request, "failed.html", _context(repo, message=message))


__all__ = ["router"]

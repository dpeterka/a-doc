"""Ledger surface (PLAN.md "UI"): a read-only view of the full differential
ledger with status/origin/tier chips, and evidence source-refs linked back
to their documents where a link is resolvable (`labs:`, `doc:`, `pmid:`).
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument
from adoc.web.casefile_helpers import find_document_by_filename, page_image_url
from adoc.web.deps import get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/ledger")

_LABS_REF_RE = re.compile(r"^labs:(?P<slug>[a-z0-9-]+):(?P<date>\d{4}-\d{2}-\d{2})$")
_DOC_REF_RE = re.compile(r"^doc:(?P<filename>[^\s#]+)#p(?P<page>\d+)$")
_PMID_REF_RE = re.compile(r"^pmid:(?P<pmid>\d+)$")


def _source_ref_href(
    source: str,
    *,
    repo: DataRepo,
    db: LabsDb,
    documents: list[LabDocument],
    image_cache: dict[str, list[Path]],
) -> str | None:
    if match := _LABS_REF_RE.match(source):
        return f"/labs/{quote(match.group('slug'), safe='')}"
    if match := _DOC_REF_RE.match(source):
        doc = find_document_by_filename(db, match.group("filename"), documents=documents)
        if doc is None:
            return None
        return page_image_url(repo, doc.sha256, int(match.group("page")), cache=image_cache)
    if match := _PMID_REF_RE.match(source):
        return f"https://pubmed.ncbi.nlm.nih.gov/{match.group('pmid')}/"
    return None


@router.get("")
def ledger_view(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    ledger = load_ledger(repo.root / LEDGER_RELPATH)

    # Fetched/listed once per page render, not once per evidence source-ref:
    # a ledger with many hypotheses/evidence items would otherwise re-run
    # `db.list_documents()` (a full-table query) and re-list a document's
    # page-image directory (a filesystem `iterdir()`) once per `doc:` ref,
    # even when several refs point at the same document — see
    # `find_document_by_filename`/`list_page_images`'s docstrings.
    documents = db.list_documents()
    image_cache: dict[str, list[Path]] = {}

    def href(source: str) -> str | None:
        return _source_ref_href(
            source, repo=repo, db=db, documents=documents, image_cache=image_cache
        )

    return templates.TemplateResponse(
        request, "ledger.html", {"ledger": ledger, "source_ref_href": href}
    )

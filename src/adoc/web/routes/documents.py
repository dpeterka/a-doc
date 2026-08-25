"""Consumed-documents surface (nav: "Documents > Consumed"): a plain list
of every document a-doc has ever read, newest first — filename, when it
was added, a plain-language document type, its status, and (for anything
that isn't a genomic file) how many of its lab rows were accepted
outright versus are still waiting on a human ("Documents"'s "Review"
item). `LabsDb.documents_overview()` is the sole SQL query behind this;
this route only reshapes it for the template (friendly type labels, a
lab-document's link to its archived original).

`/documents/consumed/{sha}/text` (docs/adr/0015-document-text-corpus.md) is
a read-only view of a document's extracted plain text, when any is on file
(`LabsDb.get_document_text`) — never offered for a genomic document, which
never has text extracted (CRITICAL DESIGN RULE, ADR 0010).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.labs.db import LabsDb
from adoc.web.deps import get_db
from adoc.web.templating import templates

router = APIRouter(prefix="/documents")

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Plain, patient-facing words for `documents.doc_type` (`ingest.schema.DocType`
# plus `ingest.genomics.GENOMIC_DOC_TYPE`) - never the raw snake_case value.
_DOC_TYPE_LABELS: dict[str, str] = {
    "lab_report": "lab report",
    "imaging_report": "imaging report",
    "clinical_note": "clinical note",
    GENOMIC_DOC_TYPE: "genomic data",
    "other": "other",
}


def _doc_type_label(doc_type: str) -> str:
    return _DOC_TYPE_LABELS.get(doc_type, doc_type)


@router.get("/consumed")
def consumed(request: Request, db: LabsDb = Depends(get_db)) -> Response:
    text_shas = db.document_text_shas()
    rows: list[dict[str, Any]] = []
    for overview in db.documents_overview():
        doc = overview.document
        is_genomic = doc.doc_type == GENOMIC_DOC_TYPE
        rows.append(
            {
                "sha256": doc.sha256,
                "filename": doc.filename,
                "ingested_at": doc.ingested_at,
                "doc_type_label": _doc_type_label(doc.doc_type),
                "status": doc.status.value,
                "is_genomic": is_genomic,
                "accepted_count": overview.accepted_count,
                "awaiting_review_count": overview.awaiting_review_count,
                # Defense in depth, belt-and-suspenders with `ingest.doctext`'s
                # structural exclusion: a genomic document is NEVER offered a
                # text link here, even in the (never-happens-in-practice) case
                # that a `document_text` row somehow existed for its sha.
                "has_text": (not is_genomic) and doc.sha256 in text_shas,
            }
        )
    return templates.TemplateResponse(request, "documents_consumed.html", {"rows": rows})


@router.get("/consumed/{sha}/text")
def consumed_text(request: Request, sha: str, db: LabsDb = Depends(get_db)) -> Response:
    """Read-only view of one document's extracted plain text
    (docs/adr/0015-document-text-corpus.md). 404s for an unknown/unsafe
    sha, a document with no text on file, or a genomic document (defense in
    depth — see `consumed()` above; never happens in practice, since a
    genomic document never has a `document_text` row to begin with)."""
    if not _SHA_RE.match(sha):
        return Response(status_code=404)
    doc = db.get_document(sha)
    if doc is None or doc.doc_type == GENOMIC_DOC_TYPE:
        return Response(status_code=404)
    text = db.get_document_text(sha)
    if text is None:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request,
        "document_text.html",
        {"filename": doc.filename, "sha256": sha, "text": text},
    )


__all__ = ["router"]

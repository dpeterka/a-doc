"""Consumed-documents surface (nav: "Documents > Consumed"): a plain list
of every document a-doc has ever read, newest first — filename, when it
was added, a plain-language document type, its status, and (for anything
that isn't a genomic file) how many of its lab rows were accepted
outright versus are still waiting on a human ("Documents"'s "Review"
item). `LabsDb.documents_overview()` is the sole SQL query behind this;
this route only reshapes it for the template (friendly type labels, a
lab-document's link to its archived original).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.labs.db import LabsDb
from adoc.web.deps import get_db
from adoc.web.templating import templates

router = APIRouter(prefix="/documents")

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
    rows: list[dict[str, Any]] = []
    for overview in db.documents_overview():
        doc = overview.document
        rows.append(
            {
                "sha256": doc.sha256,
                "filename": doc.filename,
                "ingested_at": doc.ingested_at,
                "doc_type_label": _doc_type_label(doc.doc_type),
                "status": doc.status.value,
                "is_genomic": doc.doc_type == GENOMIC_DOC_TYPE,
                "accepted_count": overview.accepted_count,
                "awaiting_review_count": overview.awaiting_review_count,
            }
        )
    return templates.TemplateResponse(request, "documents_consumed.html", {"rows": rows})


__all__ = ["router"]

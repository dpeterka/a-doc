"""Pydantic v2 extraction models (PLAN.md "Key schemas": "Extraction schema").

These are the structured-output payloads for the vision extraction passes
(`ingest/extract.py`) and the classifier call (`ingest/pipeline.py`). They
are cross-boundary payloads (CLAUDE.md "Code conventions": "Pydantic v2
models for every cross-boundary payload") and are never constructed by hand
from real patient data outside `tests/fixtures/` (CLAUDE.md PHI boundary).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]
DocType = Literal["lab_report", "clinical_note", "imaging_report", "other"]


class ExtractedResult(BaseModel):
    """One discrete result row transcribed from a document page.

    Mirrors PLAN.md's `results[]` shape: `(name_raw, value|value_text,
    unit_raw, ref_range_raw, flag_raw, page, confidence)`. Exactly what the
    extractor transcribed — no canonicalization, unit conversion, or
    validation happens here (that is `labs.validate`'s job, applied in
    `ingest/reconcile.py`).
    """

    name_raw: str
    value: float | None = None
    value_text: str | None = None
    unit_raw: str | None = None
    ref_range_raw: str | None = None
    flag_raw: str | None = None
    page: int = Field(ge=1)
    confidence: Confidence


class IllegibleRegion(BaseModel):
    """One page region the extractor could not read with confidence."""

    page: int = Field(ge=1)
    description: str


class DocumentExtraction(BaseModel):
    """The full structured extraction for one document (one extractor pass).

    PLAN.md "Extraction schema": `doc_type, facility, collection/report
    dates, results[], narrative_findings[], illegible_regions[]`.
    """

    doc_type: DocType
    facility: str | None = None
    collection_date: date | None = None
    report_date: date | None = None
    results: list[ExtractedResult] = Field(default_factory=list)
    narrative_findings: list[str] = Field(default_factory=list)
    illegible_regions: list[IllegibleRegion] = Field(default_factory=list)


class ClassifyResult(BaseModel):
    """The `classifier` role's structured output: doc type + a date guess."""

    doc_type: DocType
    doc_date: date | None = None

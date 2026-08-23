"""Pydantic v2 models mirroring the `labs` table DDL (PLAN.md "Key schemas").

These are the cross-boundary payloads for the labs slice: `LabDocument` mirrors
the `documents` table, `LabResult` mirrors the `labs` table. `db.py` converts
between these models and sqlite rows; nothing else should construct raw rows.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DocumentStatus(StrEnum):
    """Lifecycle of an ingested source document (see `documents.status` CHECK)."""

    PROCESSING = "processing"
    COMPLETE = "complete"
    NEEDS_REVIEW = "needs-review"
    FAILED = "failed"


class ExtractionStatus(StrEnum):
    """Lifecycle of one extracted lab row (see `labs.extraction_status` CHECK).

    `auto` = agreed cross-model extraction, auto-accepted. `pending` = sits in
    the human confirm queue. `confirmed`/`corrected`/`rejected` are the three
    possible outcomes of a human review.
    """

    AUTO = "auto"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    PENDING = "pending"
    REJECTED = "rejected"


class LabFlag(StrEnum):
    """Abnormal-value flag, mirroring the `labs.flag` CHECK constraint."""

    HIGH = "H"
    LOW = "L"
    CRITICAL_HIGH = "HH"
    CRITICAL_LOW = "LL"
    ABNORMAL = "A"


class LabDocument(BaseModel):
    """One ingested source document, mirrors the `documents` table."""

    model_config = ConfigDict(frozen=False)

    sha256: str = Field(min_length=64, max_length=64)
    filename: str
    doc_type: str
    doc_date: date | None = None
    page_count: int = Field(ge=1)
    ingested_at: datetime = Field(default_factory=_utcnow)
    status: DocumentStatus = DocumentStatus.PROCESSING


class LabResult(BaseModel):
    """One extracted/confirmed lab result, mirrors the `labs` table.

    `id` is `None` for a not-yet-persisted row; `db.py` assigns it on insert.
    `value` (numeric) and `value_text` (titers, "positive"/"negative", free
    text) are both optional but at least one must be present — validated
    below to mirror the intent of the DDL even though sqlite itself only
    enforces `value_text` via application code (see PLAN.md ingestion notes).
    """

    model_config = ConfigDict(frozen=False)

    id: int | None = None
    date: date
    loinc_code: str | None = None
    name: str
    name_raw: str
    value: float | None = None
    value_text: str | None = None
    ucum_unit: str | None = None
    ref_low: float | None = None
    ref_high: float | None = None
    ref_text: str | None = None
    flag: LabFlag | None = None
    source_doc: str = Field(min_length=64, max_length=64)
    source_page: int | None = None
    extraction_status: ExtractionStatus = ExtractionStatus.AUTO
    raw_json: str
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _value_or_text_required(self) -> LabResult:
        if self.value is None and self.value_text is None:
            raise ValueError("LabResult requires at least one of value/value_text")
        return self

    def raw_payload(self) -> dict[str, Any]:
        """Decode `raw_json` into a plain dict (the extractor's raw payload)."""
        result: dict[str, Any] = json.loads(self.raw_json)
        return result

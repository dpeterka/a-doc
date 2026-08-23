"""Tests for adoc.ingest.schema: extraction Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from adoc.ingest.schema import ClassifyResult, DocumentExtraction, ExtractedResult

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "extractions"


def test_extracted_result_requires_page_and_confidence() -> None:
    result = ExtractedResult(
        name_raw="Potassium",
        value=4.1,
        unit_raw="mmol/L",
        ref_range_raw="3.5-5.1",
        page=1,
        confidence="high",
    )
    assert result.value_text is None
    assert result.flag_raw is None


def test_extracted_result_page_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        ExtractedResult(name_raw="Potassium", page=0, confidence="high")


def test_document_extraction_defaults_to_empty_lists() -> None:
    extraction = DocumentExtraction(doc_type="lab_report")
    assert extraction.results == []
    assert extraction.narrative_findings == []
    assert extraction.illegible_regions == []
    assert extraction.facility is None


def test_classify_result_doc_date_optional() -> None:
    result = ClassifyResult(doc_type="clinical_note")
    assert result.doc_date is None


@pytest.mark.parametrize(
    "fixture_name",
    [
        "clean_agreement.json",
        "value_disagreement.json",
        "unit_mismatch.json",
        "single_pass_only.json",
        "low_confidence.json",
        "non_lab_clinical_note.json",
    ],
)
def test_fixtures_parse_as_document_extractions(fixture_name: str) -> None:
    payload = json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))

    pass_a = DocumentExtraction.model_validate(payload["pass_a"])
    pass_b = DocumentExtraction.model_validate(payload["pass_b"])

    assert pass_a.doc_type in ("lab_report", "clinical_note", "imaging_report", "other")
    assert pass_b.doc_type in ("lab_report", "clinical_note", "imaging_report", "other")

"""Tests for adoc.ingest.extract: role/prompt wiring for both the vision
double-pass (`double_pass_extract`) and the docx text double-pass
(`double_pass_extract_text`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from adoc.config import ModelBinding
from adoc.ingest.archive import ArchivedDoc
from adoc.ingest.extract import (
    DOCX_PROMPT_A,
    DOCX_PROMPT_A_VERSION,
    DOCX_PROMPT_B,
    DOCX_PROMPT_B_VERSION,
    PROMPT_A,
    PROMPT_A_VERSION,
    PROMPT_B,
    PROMPT_B_VERSION,
    double_pass_extract,
    double_pass_extract_text,
)
from adoc.ingest.schema import DocumentExtraction, ExtractedResult
from adoc.ingest.vision import ImagePart, PdfPart, TextPart, VisionClient
from adoc.reason.client import (
    AnthropicProvider,
    LlmClient,
    OpenAIProvider,
    TransportRequest,
    TransportResponse,
)


class _FakeVisionClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def extract(self, role, *, system, parts, schema, binding_index=0, max_tokens=4096):  # type: ignore[no-untyped-def]
        self.calls.append({"role": role, "system": system, "parts": list(parts)})
        if role == "extractor_pass_a":
            return DocumentExtraction(
                doc_type="lab_report",
                results=[
                    ExtractedResult(name_raw="Potassium", value=4.1, page=1, confidence="high")
                ],
            )
        return DocumentExtraction(
            doc_type="lab_report",
            results=[ExtractedResult(name_raw="Potassium", value=4.1, page=1, confidence="high")],
        )


def test_double_pass_extract_sends_pdf_to_pass_a_and_pages_to_pass_b(
    tmp_path: Path, tiny_pdf_bytes: bytes
) -> None:
    pdf_path = tmp_path / "sources" / "sha__doc.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(tiny_pdf_bytes)

    page_paths = []
    for i in range(1, 3):
        page_path = tmp_path / "sources" / "pages" / "sha" / f"p-{i}.png"
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_bytes(b"\x89PNG fake")
        page_paths.append(page_path)

    archived = ArchivedDoc(
        sha256="sha", original_path=pdf_path, page_paths=page_paths, already_ingested=False
    )

    fake = _FakeVisionClient()
    pass_a, pass_b = double_pass_extract(fake, archived)  # type: ignore[arg-type]

    assert isinstance(pass_a, DocumentExtraction)
    assert isinstance(pass_b, DocumentExtraction)
    assert fake.calls[0]["role"] == "extractor_pass_a"
    assert fake.calls[1]["role"] == "extractor_pass_b"

    pass_a_parts = fake.calls[0]["parts"]
    assert len(pass_a_parts) == 1
    assert isinstance(pass_a_parts[0], PdfPart)
    assert pass_a_parts[0].data == tiny_pdf_bytes

    pass_b_parts = fake.calls[1]["parts"]
    # one TextPart + one ImagePart per rendered page
    assert len(pass_b_parts) == 4
    assert isinstance(pass_b_parts[0], TextPart)
    assert isinstance(pass_b_parts[1], ImagePart)
    assert pass_b_parts[1].page == 1
    assert isinstance(pass_b_parts[3], ImagePart)
    assert pass_b_parts[3].page == 2

    assert PROMPT_A_VERSION in fake.calls[0]["system"]
    assert PROMPT_B_VERSION in fake.calls[1]["system"]
    assert fake.calls[0]["system"] != fake.calls[1]["system"]


def test_vision_client_is_the_declared_type() -> None:
    # documents that double_pass_extract's real signature is VisionClient,
    # even though the test above exercises it with a structurally-typed fake.
    assert VisionClient.extract.__name__ == "extract"


def test_double_pass_extract_text_sends_plain_text_to_both_roles(tmp_path: Path) -> None:
    """`double_pass_extract_text` goes through `LlmClient.complete`, not
    `VisionClient` - no binary parts, no page images, just the docx's
    extracted text as one user message per pass."""
    calls: list[TransportRequest] = []

    def anthropic_transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text="",
            tool_input={
                "doc_type": "lab_report",
                "results": [
                    {
                        "name_raw": "Potassium",
                        "value": 4.1,
                        "page": 1,
                        "confidence": "high",
                    }
                ],
                "narrative_findings": [],
                "illegible_regions": [],
            },
            input_tokens=10,
            output_tokens=10,
        )

    def openai_transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text="",
            tool_input={
                "doc_type": "lab_report",
                "results": [
                    {
                        "name_raw": "Potassium",
                        "value": 4.1,
                        "page": 1,
                        "confidence": "high",
                    }
                ],
                "narrative_findings": [],
                "illegible_regions": [],
            },
            input_tokens=10,
            output_tokens=10,
        )

    bindings: dict[str, list[ModelBinding]] = {
        "extractor_pass_a": [ModelBinding(provider="anthropic", model="fake-sonnet")],
        "extractor_pass_b": [ModelBinding(provider="openai", model="fake-gpt")],
    }
    providers = {
        "anthropic": AnthropicProvider(api_key=None, transport=anthropic_transport),
        "openai": OpenAIProvider(api_key=None, transport=openai_transport),
    }
    client = LlmClient(bindings, providers)

    pass_a, pass_b = double_pass_extract_text(client, "Home lab panel:\n\nPotassium 4.1")

    assert isinstance(pass_a, DocumentExtraction)
    assert isinstance(pass_b, DocumentExtraction)
    assert len(calls) == 2
    assert calls[0].messages[0].content == "Home lab panel:\n\nPotassium 4.1"
    assert calls[1].messages[0].content == "Home lab panel:\n\nPotassium 4.1"
    assert DOCX_PROMPT_A_VERSION in calls[0].system
    assert DOCX_PROMPT_B_VERSION in calls[1].system
    assert calls[0].system != calls[1].system


# --------------------------------------------------------------------------
# Specimen instruction + version bump (added alongside the `specimen`
# dimension on `ExtractedResult`) - every extractor prompt must tell the
# model to record a specimen per result from the report's own section
# headers/labels, defaulting to "unknown" rather than guessing, and each
# prompt's version constant must have moved past its pre-specimen value.
# --------------------------------------------------------------------------


def test_pdf_prompt_versions_are_bumped_past_v1() -> None:
    assert PROMPT_A_VERSION == "extractor-pass-a-v3"
    assert PROMPT_B_VERSION == "extractor-pass-b-v3"
    assert PROMPT_A_VERSION != "extractor-pass-a-v1"
    assert PROMPT_B_VERSION != "extractor-pass-b-v1"


def test_docx_prompt_versions_are_bumped_past_v1() -> None:
    assert DOCX_PROMPT_A_VERSION == "docx-extractor-pass-a-v3"
    assert DOCX_PROMPT_B_VERSION == "docx-extractor-pass-b-v3"
    assert DOCX_PROMPT_A_VERSION != "docx-extractor-pass-a-v1"
    assert DOCX_PROMPT_B_VERSION != "docx-extractor-pass-b-v1"


@pytest.mark.parametrize("prompt", [PROMPT_A, PROMPT_B, DOCX_PROMPT_A, DOCX_PROMPT_B])
def test_every_extractor_prompt_instructs_recording_specimen_per_result(prompt: str) -> None:
    lowered = prompt.lower()
    assert "specimen" in lowered
    # section-header/label examples the model should key off of
    assert "urinalysis" in lowered
    assert "stool" in lowered
    # the "don't guess, default unknown" instruction
    assert "unknown" in lowered

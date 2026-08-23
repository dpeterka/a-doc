"""Tests for adoc.ingest.vision.VisionClient: fake transports, no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from adoc.config import ModelBinding
from adoc.ingest.vision import (
    AnthropicVisionProvider,
    ImagePart,
    OpenAIVisionProvider,
    PdfPart,
    TextPart,
    VisionClient,
    VisionError,
    VisionTransportRequest,
    VisionTransportResponse,
)
from adoc.privacy import PatientIdentifiers, Scrubber
from adoc.reason.client import AnthropicProvider, LlmClient, OpenAIProvider


class Extraction(BaseModel):
    doc_type: str


def _llm_client(**roles: list[ModelBinding]) -> LlmClient:
    return LlmClient(
        roles,
        {
            "anthropic": AnthropicProvider(api_key=None, transport=lambda r: _unused(r)),
            "openai": OpenAIProvider(api_key=None, transport=lambda r: _unused(r)),
        },
    )


def _unused(request: object) -> None:  # pragma: no cover - guards against a real call
    raise AssertionError("the underlying LlmClient transport must never be invoked directly")


def _binding(provider: str, model: str) -> ModelBinding:
    return ModelBinding(provider=provider, model=model, params={})


def test_extract_dispatches_to_the_bound_providers_transport() -> None:
    seen: dict[str, object] = {}

    def fake_transport(request: VisionTransportRequest) -> VisionTransportResponse:
        seen["parts"] = request.parts
        seen["system"] = request.system
        seen["model"] = request.model
        return VisionTransportResponse(
            text="ok", tool_input={"doc_type": "lab_report"}, input_tokens=10, output_tokens=5
        )

    client = _llm_client(extractor_pass_a=[_binding("anthropic", "claude-sonnet-5")])
    vision = VisionClient(client, transports={"anthropic": fake_transport})

    result = vision.extract(
        "extractor_pass_a",
        system="extract this",
        parts=[TextPart(text="page 1"), PdfPart(data=b"%PDF-1.4")],
        schema=Extraction,
    )

    assert isinstance(result, Extraction)
    assert result.doc_type == "lab_report"
    assert seen["model"] == "claude-sonnet-5"
    assert len(seen["parts"]) == 2  # type: ignore[arg-type]


def test_scrubbing_applies_to_system_and_text_parts_but_not_binary_parts() -> None:
    seen: dict[str, object] = {}

    def fake_transport(request: VisionTransportRequest) -> VisionTransportResponse:
        seen["system"] = request.system
        seen["parts"] = request.parts
        return VisionTransportResponse(
            text="ok", tool_input={"doc_type": "other"}, input_tokens=1, output_tokens=1
        )

    scrubber = Scrubber(PatientIdentifiers.model_validate({"names": ["Jane Doe"]}))
    client = LlmClient(
        {"classifier": [_binding("anthropic", "claude-haiku-4-5")]},
        {"anthropic": AnthropicProvider(api_key=None, transport=lambda r: _unused(r))},
        scrubber=scrubber,
    )
    vision = VisionClient(client, transports={"anthropic": fake_transport})

    pdf_bytes = b"Jane Doe binary bytes that must not be scrubbed"
    vision.extract(
        "classifier",
        system="Patient is Jane Doe.",
        parts=[TextPart(text="Jane Doe reports fatigue."), PdfPart(data=pdf_bytes)],
        schema=Extraction,
    )

    assert seen["system"] == "Patient is [NAME]."
    parts = seen["parts"]
    assert isinstance(parts[0], TextPart)
    assert parts[0].text == "[NAME] reports fatigue."
    assert isinstance(parts[1], PdfPart)
    assert parts[1].data == pdf_bytes  # binary content bypasses the scrubber by design


def test_openai_vision_provider_rejects_pdf_parts_before_any_network_call() -> None:
    provider = OpenAIVisionProvider(api_key=None, transport=lambda r: _unused(r))
    request = VisionTransportRequest(
        model="gpt-5.2",
        system="s",
        parts=[PdfPart(data=b"%PDF-1.4")],
        schema=Extraction,
        params={},
        max_tokens=100,
    )

    with pytest.raises(VisionError, match="does not accept PdfPart"):
        provider.extract(request)


def test_openai_vision_provider_accepts_image_parts_via_transport() -> None:
    def fake_transport(request: VisionTransportRequest) -> VisionTransportResponse:
        return VisionTransportResponse(
            text="", tool_input={"doc_type": "lab_report"}, input_tokens=1, output_tokens=1
        )

    provider = OpenAIVisionProvider(api_key=None, transport=fake_transport)
    request = VisionTransportRequest(
        model="gpt-5.2",
        system="s",
        parts=[ImagePart(data=b"\x89PNG", page=1)],
        schema=Extraction,
        params={},
        max_tokens=100,
    )

    response = provider.extract(request)

    assert response.tool_input == {"doc_type": "lab_report"}


def test_no_structured_output_raises_vision_error() -> None:
    def no_tool_call(request: VisionTransportRequest) -> VisionTransportResponse:
        return VisionTransportResponse(
            text="plain text", tool_input=None, input_tokens=1, output_tokens=1
        )

    client = _llm_client(classifier=[_binding("anthropic", "claude-haiku-4-5")])
    vision = VisionClient(client, transports={"anthropic": no_tool_call})

    with pytest.raises(VisionError):
        vision.extract("classifier", system="s", parts=[TextPart(text="t")], schema=Extraction)


def test_audit_log_records_vision_calls(tmp_path: Path) -> None:
    def fake_transport(request: VisionTransportRequest) -> VisionTransportResponse:
        return VisionTransportResponse(
            text="", tool_input={"doc_type": "lab_report"}, input_tokens=3, output_tokens=2
        )

    audit_path = tmp_path / "audit.jsonl"
    client = LlmClient(
        {"classifier": [_binding("anthropic", "claude-haiku-4-5")]},
        {"anthropic": AnthropicProvider(api_key=None, transport=lambda r: _unused(r))},
        audit_log_path=audit_path,
    )
    vision = VisionClient(client, transports={"anthropic": fake_transport})

    vision.extract("classifier", system="s", parts=[TextPart(text="t")], schema=Extraction)

    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"role": "classifier"' in lines[0]


def test_anthropic_vision_provider_is_used_by_default_when_no_transport_injected() -> None:
    provider = AnthropicVisionProvider(api_key=None)
    assert provider._transport == provider._default_transport

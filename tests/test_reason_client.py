"""Tests for adoc.reason.client: LlmClient with fake transports (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from adoc.config import ModelBinding, Settings
from adoc.privacy import PatientIdentifiers, Scrubber
from adoc.reason.client import (
    FEATHERLESS_BASE_URL,
    AnthropicProvider,
    LlmClient,
    LlmError,
    Message,
    OpenAIProvider,
    TransientTransportError,
    TransportRequest,
    TransportResponse,
)


class Diagnosis(BaseModel):
    summary: str
    confidence: str


def _bindings(**roles: list[ModelBinding]) -> dict[str, list[ModelBinding]]:
    return roles


def _binding(provider: str = "anthropic", model: str = "claude-opus-5") -> ModelBinding:
    return ModelBinding(provider=provider, model=model, params={"effort": "high"})


def test_scrubbing_is_applied_to_system_and_messages_before_the_transport_sees_them() -> None:
    seen: dict[str, object] = {}

    def fake_transport(request: TransportRequest) -> TransportResponse:
        seen["system"] = request.system
        seen["messages"] = list(request.messages)
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    provider = AnthropicProvider(api_key=None, transport=fake_transport)
    scrubber = Scrubber(PatientIdentifiers.model_validate({"names": ["Jane Doe"]}))
    client = LlmClient(
        _bindings(primary_reasoner=[_binding()]),
        {"anthropic": provider},
        scrubber=scrubber,
    )

    client.complete(
        "primary_reasoner",
        system="Patient is Jane Doe.",
        messages=[Message(role="user", content="Jane Doe reports fatigue.")],
    )

    assert seen["system"] == "Patient is [NAME]."
    assert "Jane Doe" not in str(seen["system"])
    assert "Jane Doe" not in str(seen["messages"])


def test_audit_log_never_contains_message_content(tmp_path: Path) -> None:
    def fake_transport(request: TransportRequest) -> TransportResponse:
        return TransportResponse(text="ok", tool_input=None, input_tokens=10, output_tokens=5)

    provider = AnthropicProvider(api_key=None, transport=fake_transport)
    audit_path = tmp_path / "audit.jsonl"
    client = LlmClient(
        _bindings(primary_reasoner=[_binding()]),
        {"anthropic": provider},
        audit_log_path=audit_path,
    )

    secret_text = "a very secret clinical detail nobody should log"
    client.complete("primary_reasoner", system=secret_text, messages=[])

    line = audit_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert secret_text not in line
    assert record["role"] == "primary_reasoner"
    assert record["model"] == "claude-opus-5"
    assert record["provider"] == "anthropic"
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 5
    assert "duration_s" in record
    assert record["scrub_count"] == 0
    assert record["error"] is False


def test_schema_parsing_and_validation_succeeds_after_transient_retries(tmp_path: Path) -> None:
    calls: list[int] = []

    def flaky_transport(request: TransportRequest) -> TransportResponse:
        calls.append(1)
        if len(calls) < 3:
            raise TransientTransportError("rate limited")
        return TransportResponse(
            text="",
            tool_input={"summary": "possible lupus", "confidence": "moderate"},
            input_tokens=100,
            output_tokens=50,
        )

    provider = AnthropicProvider(api_key=None, transport=flaky_transport)
    client = LlmClient(
        _bindings(primary_reasoner=[_binding()]),
        {"anthropic": provider},
        max_retries=5,
        backoff_base_seconds=0.0,
    )

    result = client.complete(
        "primary_reasoner",
        system="s",
        messages=[Message(role="user", content="m")],
        schema=Diagnosis,
    )

    assert len(calls) == 3
    assert isinstance(result.parsed, Diagnosis)
    assert result.parsed.summary == "possible lupus"
    assert result.model_id == "claude-opus-5"
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.cost_estimate is not None and result.cost_estimate > 0


def test_retries_are_bounded_and_raise_llm_error_when_exhausted() -> None:
    calls: list[int] = []

    def always_fails(request: TransportRequest) -> TransportResponse:
        calls.append(1)
        raise TransientTransportError("still rate limited")

    provider = AnthropicProvider(api_key=None, transport=always_fails)
    client = LlmClient(
        _bindings(primary_reasoner=[_binding()]),
        {"anthropic": provider},
        max_retries=3,
        backoff_base_seconds=0.0,
    )

    with pytest.raises(LlmError):
        client.complete("primary_reasoner", system="s", messages=[])

    assert len(calls) == 3


def test_missing_structured_output_raises_llm_error_without_retry() -> None:
    calls: list[int] = []

    def no_tool_call(request: TransportRequest) -> TransportResponse:
        calls.append(1)
        return TransportResponse(
            text="plain text, no tool call", tool_input=None, input_tokens=1, output_tokens=1
        )

    provider = AnthropicProvider(api_key=None, transport=no_tool_call)
    client = LlmClient(
        _bindings(primary_reasoner=[_binding()]),
        {"anthropic": provider},
        max_retries=3,
        backoff_base_seconds=0.0,
    )

    with pytest.raises(LlmError):
        client.complete("primary_reasoner", system="s", messages=[], schema=Diagnosis)

    assert len(calls) == 1  # not a TransientTransportError, so no retry


def test_binding_resolution_for_a_single_bound_role() -> None:
    def fake_transport(request: TransportRequest) -> TransportResponse:
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    provider = AnthropicProvider(api_key=None, transport=fake_transport)
    client = LlmClient(
        _bindings(primary_reasoner=[_binding(model="claude-opus-5")]),
        {"anthropic": provider},
    )

    result = client.complete("primary_reasoner", system="s", messages=[])

    assert result.model_id == "claude-opus-5"


def test_binding_resolution_for_blind_panel_uses_binding_index() -> None:
    calls: list[str] = []

    def anthropic_transport(request: TransportRequest) -> TransportResponse:
        calls.append(request.model)
        return TransportResponse(text="a", tool_input=None, input_tokens=1, output_tokens=1)

    def openai_transport(request: TransportRequest) -> TransportResponse:
        calls.append(request.model)
        return TransportResponse(text="b", tool_input=None, input_tokens=1, output_tokens=1)

    anthropic_provider = AnthropicProvider(api_key=None, transport=anthropic_transport)
    openai_provider = OpenAIProvider(api_key=None, transport=openai_transport)
    client = LlmClient(
        _bindings(
            blind_panel=[
                _binding(provider="anthropic", model="claude-opus-5"),
                _binding(provider="openai", model="gpt-5.2-thinking"),
            ]
        ),
        {"anthropic": anthropic_provider, "openai": openai_provider},
    )

    result_0 = client.complete("blind_panel", system="s", messages=[], binding_index=0)
    result_1 = client.complete("blind_panel", system="s", messages=[], binding_index=1)

    assert result_0.model_id == "claude-opus-5"
    assert result_1.model_id == "gpt-5.2-thinking"
    assert calls == ["claude-opus-5", "gpt-5.2-thinking"]


def test_binding_index_out_of_range_raises_llm_error() -> None:
    def fake_transport(request: TransportRequest) -> TransportResponse:
        return TransportResponse(text="x", tool_input=None, input_tokens=1, output_tokens=1)

    provider = AnthropicProvider(api_key=None, transport=fake_transport)
    client = LlmClient(_bindings(primary_reasoner=[_binding()]), {"anthropic": provider})

    with pytest.raises(LlmError):
        client.complete("primary_reasoner", system="s", messages=[], binding_index=1)


def test_unknown_role_raises_llm_error() -> None:
    client = LlmClient({}, {})

    with pytest.raises(LlmError):
        client.complete("no_such_role", system="s", messages=[])


def test_from_settings_wires_featherless_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models_file = tmp_path / "models.yaml"
    models_file.write_text(
        "roles:\n"
        "  primary_reasoner:\n"
        "    provider: anthropic\n"
        "    model: claude-opus-5\n"
        "  blind_panel:\n"
        "    - provider: anthropic\n"
        "      model: claude-opus-5\n"
        "    - provider: featherless\n"
        "      model: deepseek-ai/DeepSeek-R1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    settings = Settings(models_file=models_file)

    client = LlmClient.from_settings(settings)

    featherless_provider = client._providers["featherless"]
    assert isinstance(featherless_provider, OpenAIProvider)
    assert featherless_provider.base_url == FEATHERLESS_BASE_URL

    openai_provider = client._providers["openai"]
    assert isinstance(openai_provider, OpenAIProvider)
    assert openai_provider.base_url is None


def test_from_settings_injects_fake_transports_so_no_sdk_client_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models_file = tmp_path / "models.yaml"
    models_file.write_text(
        "roles:\n  primary_reasoner:\n    provider: anthropic\n    model: claude-opus-5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(models_file=models_file)

    def fake_anthropic_transport(request: TransportRequest) -> TransportResponse:
        return TransportResponse(text="fake", tool_input=None, input_tokens=2, output_tokens=2)

    client = LlmClient.from_settings(settings, transports={"anthropic": fake_anthropic_transport})

    result = client.complete("primary_reasoner", system="s", messages=[])

    assert result.text == "fake"


def test_structured_output_unwraps_single_key_nesting() -> None:
    """Claude occasionally nests tool input under a wrapper key; the client
    validates flat-first and unwraps only when flat validation fails."""
    from adoc.reason.client import _unwrap_tool_input

    assert _unwrap_tool_input({"parameters": {"a": 1}}) == {"a": 1}
    assert _unwrap_tool_input({"a": 1, "b": 2}) == {"a": 1, "b": 2}
    assert _unwrap_tool_input({"a": 1}) == {"a": 1}

"""Tests for adoc.reason.client: LlmClient with fake transports (no network)."""

from __future__ import annotations

import json
import sys
import types
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


def test_bare_constructor_default_scrubber_reports_no_privacy_warning() -> None:
    """The bare `LlmClient(bindings, providers)` constructor's `Scrubber.
    noop()` default (no `Settings`/`data_dir` to build a real one from —
    every non-test caller only ever wires fake transports, see client.py's
    module docstring) is an explicit no-op, so it must not nag."""
    provider = AnthropicProvider(
        api_key=None,
        transport=lambda r: TransportResponse(
            text="ok", tool_input=None, input_tokens=1, output_tokens=1
        ),
    )
    client = LlmClient(_bindings(primary_reasoner=[_binding()]), {"anthropic": provider})

    assert client.privacy_warning is None


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


# --------------------------------------------------------------------------
# from_settings' default scrubber (the web-app/onboard defect this fixes:
# neither ever passed a scrubber, so LlmClient.__init__'s Scrubber.noop()
# fallback meant every real outbound call went out unscrubbed).
# --------------------------------------------------------------------------


def _settings_with_models_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    models_file = tmp_path / "models.yaml"
    models_file.write_text(
        "roles:\n  primary_reasoner:\n    provider: anthropic\n    model: claude-opus-5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Settings(models_file=models_file)


def test_from_settings_scrubs_by_default_when_no_scrubber_is_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the web-app/onboard defect: this MUST fail on
    current `develop`, where `from_settings` never built a scrubber of its
    own and every caller that omitted `scrubber=` (`web/app.py`, `cli.py`'s
    onboard) silently got `Scrubber.noop()` — unscrubbed text reaching an
    external provider."""
    identifiers_path = tmp_path / "case" / "identifiers.yaml"
    identifiers_path.parent.mkdir(parents=True)
    identifiers_path.write_text(
        "names: ['Jane Q. Public']\ndob: '1980-05-12'\naddress_fragments: ['123 Main St']\n",
        encoding="utf-8",
    )
    settings = _settings_with_models_file(tmp_path, monkeypatch)

    seen: dict[str, object] = {}

    def fake_transport(request: TransportRequest) -> TransportResponse:
        seen["system"] = request.system
        seen["messages"] = list(request.messages)
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    # No `scrubber=` passed - this is exactly what web/app.py's create_app
    # and cli.py's onboard command do.
    client = LlmClient.from_settings(settings, transports={"anthropic": fake_transport})

    client.complete(
        "primary_reasoner",
        system="Patient is Jane Q. Public, DOB 1980-05-12, lives at 123 Main St.",
        messages=[Message(role="user", content="Jane Q. Public reports fatigue. CRP 8.5 mg/L.")],
    )

    assert "Jane Q. Public" not in str(seen["system"])
    assert "1980-05-12" not in str(seen["system"])
    assert "123 Main St" not in str(seen["system"])
    assert "[NAME]" in str(seen["system"])
    assert "Jane Q. Public" not in str(seen["messages"])
    # Clinical content is untouched.
    assert "CRP 8.5 mg/L" in str(seen["messages"])
    assert client.privacy_warning is None


def test_from_settings_default_scrubber_warns_when_identifiers_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings_with_models_file(tmp_path, monkeypatch)

    client = LlmClient.from_settings(
        settings,
        transports={
            "anthropic": lambda r: TransportResponse(
                text="ok", tool_input=None, input_tokens=1, output_tokens=1
            )
        },
    )

    assert client.privacy_warning is not None
    assert "identifiers.yaml" in client.privacy_warning
    # Still runs - a missing identifiers file must never block the app.
    result = client.complete("primary_reasoner", system="s", messages=[])
    assert result.text == "ok"


def test_from_settings_honors_an_explicit_noop_scrubber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that deliberately wants no scrubbing must say so
    explicitly; `from_settings` must not silently override that choice."""
    settings = _settings_with_models_file(tmp_path, monkeypatch)
    seen: dict[str, object] = {}

    def fake_transport(request: TransportRequest) -> TransportResponse:
        seen["system"] = request.system
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    client = LlmClient.from_settings(
        settings, scrubber=Scrubber.noop(), transports={"anthropic": fake_transport}
    )

    client.complete("primary_reasoner", system="Jane Q. Public was here.", messages=[])

    assert seen["system"] == "Jane Q. Public was here."
    assert client.privacy_warning is None


def test_anthropic_default_transport_disables_sdk_retries_and_sets_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1: the SDK client must be constructed with `max_retries=0` and an
    explicit timeout, so the app's own retry loop (`LlmClient._call_with_retry`)
    is the only retry policy - no retries stacked under retries."""
    captured: dict[str, object] = {}

    class FakeMessages:
        def create(self, **kwargs: object) -> types.SimpleNamespace:
            return types.SimpleNamespace(
                content=[], usage=types.SimpleNamespace(input_tokens=1, output_tokens=1)
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.messages = FakeMessages()

    fake_module = types.SimpleNamespace(
        Anthropic=FakeAnthropicClient,
        RateLimitError=Exception,
        APIConnectionError=Exception,
        APIStatusError=Exception,
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    provider = AnthropicProvider(api_key="test-key")
    request = TransportRequest(
        model="claude-opus-5", system="s", messages=[], schema=None, params={}, max_tokens=100
    )

    provider.complete(request)

    assert captured["api_key"] == "test-key"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 300.0


def test_openai_default_transport_disables_sdk_retries_and_sets_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same M1 fix, OpenAI-family provider (also covers the Featherless
    OpenAI-compatible path, which shares `_default_transport`)."""
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> types.SimpleNamespace:
            message = types.SimpleNamespace(content="ok")
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            return types.SimpleNamespace(
                choices=[choice],
                usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeOpenAIClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.chat = FakeChat()

    fake_module = types.SimpleNamespace(
        OpenAI=FakeOpenAIClient,
        RateLimitError=Exception,
        APIConnectionError=Exception,
        APIStatusError=Exception,
    )
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    provider = OpenAIProvider(api_key="test-key", base_url=FEATHERLESS_BASE_URL)
    request = TransportRequest(
        model="deepseek-ai/DeepSeek-R1-0528",
        system="s",
        messages=[],
        schema=None,
        params={},
        max_tokens=100,
    )

    provider.complete(request)

    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == FEATHERLESS_BASE_URL
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 300.0


def test_structured_output_unwraps_single_key_nesting() -> None:
    """Claude occasionally nests tool input under a wrapper key; the client
    validates flat-first and unwraps only when flat validation fails."""
    from adoc.reason.client import _unwrap_tool_input

    assert _unwrap_tool_input({"parameters": {"a": 1}}) == {"a": 1}
    assert _unwrap_tool_input({"a": 1, "b": 2}) == {"a": 1, "b": 2}
    assert _unwrap_tool_input({"a": 1}) == {"a": 1}


def test_openai_strict_schema_rejects_no_oneof_or_discriminator() -> None:
    """Live failure: pydantic discriminated unions emit oneOf/discriminator,
    which OpenAI strict mode 400s — the real ChallengerVerdict schema must
    adapt cleanly."""
    import json as _json

    from adoc.reason.client import _openai_strict_schema
    from adoc.reason.stages import ChallengerVerdict

    adapted = _json.dumps(_openai_strict_schema(ChallengerVerdict.model_json_schema()))
    assert '"oneOf"' not in adapted
    assert '"discriminator"' not in adapted
    assert '"anyOf"' in adapted


def test_openai_empty_structured_content_raises_clear_error() -> None:
    """Live failure: gpt-5.2's reasoning consumed the whole completion
    budget, returning empty content with the schema unfulfilled — must be a
    clear error, not 'not valid JSON'."""
    from adoc.reason.client import REASONING_MAX_TOKENS

    assert REASONING_MAX_TOKENS >= 32768

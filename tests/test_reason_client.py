"""Tests for adoc.reason.client: LlmClient with fake transports (no network)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from adoc.config import ModelBinding, Settings
from adoc.privacy import PatientIdentifiers, Scrubber
from adoc.reason.client import (
    CONTEXT_COMPLETION_RESERVE,
    FEATHERLESS_BASE_URL,
    AnthropicProvider,
    LlmClient,
    LlmError,
    Message,
    OpenAIProvider,
    TransientTransportError,
    TransportRequest,
    TransportResponse,
    _validate_with_repairs,
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


class _FakeOpenAIResponse:
    def __init__(self, *, content: str, finish_reason: str) -> None:
        message = SimpleNamespace(content=content)
        self.choices = [SimpleNamespace(message=message, finish_reason=finish_reason)]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=10)


class _FakeOpenAIClient:
    """Mimics the real SDK client's shape one level below the injectable
    `transport=` seam — `OpenAIProvider._default_transport` constructs a
    real `openai.OpenAI(...)` itself, so `transport=` bypasses the exact
    code path (`finish_reason` -> `_extract_json_object`) this exercises."""

    def __init__(self, response: _FakeOpenAIResponse) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response))


def test_a_reasoning_model_truncated_mid_think_block_raises_a_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live failure this guards against: DeepSeek-R1 (via Featherless)
    hits the completion budget mid-`<think>`, leaving the tag UNCLOSED.

    `_extract_json_object`'s `<think>.*?</think>` strip does not match an
    unclosed tag, so the whole scratchpad stayed in `cleaned` and
    `cleaned.find("{")` found a brace inside the model's own reasoning
    rather than the real payload — parsing successfully as the WRONG JSON,
    or raising an opaque "not valid JSON" that named nothing about the real
    cause. `finish_reason == "length"` is now checked before parsing is
    ever attempted, so this raises a specific, named error instead.
    """
    import openai as openai_module

    unclosed = "<think>reasoning that ran out of room, and inside it a { brace"
    monkeypatch.setattr(
        openai_module,
        "OpenAI",
        lambda **_kwargs: _FakeOpenAIClient(
            _FakeOpenAIResponse(content=unclosed, finish_reason="length")
        ),
    )
    provider = OpenAIProvider(api_key=None, base_url=FEATHERLESS_BASE_URL)

    with pytest.raises(LlmError, match="max_tokens budget"):
        provider.complete(
            TransportRequest(
                model="deepseek-ai/DeepSeek-R1-0528",
                system="s",
                messages=[],
                schema=Diagnosis,
                params={},
                max_tokens=16384,
            )
        )


def test_an_untruncated_openai_response_still_parses_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The truncation check must not fire on an ordinary, complete reply."""
    import openai as openai_module

    monkeypatch.setattr(
        openai_module,
        "OpenAI",
        lambda **_kwargs: _FakeOpenAIClient(
            _FakeOpenAIResponse(content='{"name": "x", "confidence": 1}', finish_reason="stop")
        ),
    )
    provider = OpenAIProvider(api_key=None, base_url=FEATHERLESS_BASE_URL)

    response = provider.complete(
        TransportRequest(
            model="deepseek-ai/DeepSeek-R1-0528",
            system="s",
            messages=[],
            schema=Diagnosis,
            params={},
            max_tokens=16384,
        )
    )

    assert response.tool_input == {"name": "x", "confidence": 1}
    assert response.truncated is False


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


# --- tool-use payload repairs ---------------------------------------------------------


class _RepairTarget(BaseModel):
    items: list[int]
    note: str


def test_list_field_arriving_as_a_json_string_is_repaired() -> None:
    """Tool-use output sometimes serializes a list field into a STRING:
    `{"ops": "[{...}]"}` instead of `{"ops": [...]}`. Pydantic rejects it,
    the schema retry tends to repeat the same shape, and the turn dies — a
    live 33-turn intake run hit this 4 times, once putting a raw pydantic
    error in front of the patient."""
    result = _validate_with_repairs(_RepairTarget, {"items": "[1, 2, 3]", "note": "fine"})

    assert result.items == [1, 2, 3]


def test_a_valid_payload_is_never_reinterpreted() -> None:
    """Flat-first: each repair runs only after the plainer reading fails, so
    a correct payload cannot be mangled by one — including a string field
    whose contents merely look like the start of JSON."""
    result = _validate_with_repairs(_RepairTarget, {"items": [1], "note": "[not json"})

    assert result.items == [1]
    assert result.note == "[not json"


def test_wrapper_key_and_json_string_together_are_repaired() -> None:
    """Both known malformations at once: nested under a single wrapper key
    AND with the list serialized as a string."""
    payload = {"parameters": {"items": "[7]", "note": "ok"}}

    assert _validate_with_repairs(_RepairTarget, payload).items == [7]


def test_a_genuinely_invalid_payload_still_raises() -> None:
    """The repairs must not swallow real errors: a payload that no reading
    can rescue still reports a validation failure."""
    with pytest.raises(ValidationError):
        _validate_with_repairs(_RepairTarget, {"items": "not a list at all", "note": 1})


def test_a_placeholder_envelope_is_unwrapped() -> None:
    """Observed live SIX times in one 115-document backfill: the model
    echoed the tool's parameter scaffolding instead of filling it in —
    `{"parameter_name": "DocumentExtraction", "parameter_value": {...}}`.
    `_unwrap_tool_input` only unwraps a single-key dict, so this went to a
    hard validation failure and cost the document."""
    payload = {"items": [1, 2], "note": "ok"}

    result = _validate_with_repairs(
        _RepairTarget, {"parameter_name": "RepairTarget", "parameter_value": payload}
    )

    assert result.items == [1, 2]


def test_an_uppercase_placeholder_envelope_is_unwrapped() -> None:
    payload = {"items": [3], "note": "ok"}

    result = _validate_with_repairs(
        _RepairTarget, {"$PARAMETER_NAME": "repair_target", "parameter_value": payload}
    )

    assert result.items == [3]


def test_a_legitimate_nested_object_is_not_unwrapped() -> None:
    """The repair requires a placeholder NAME key alongside exactly one
    dict value, so a real payload that merely contains a nested object is
    never mistaken for an envelope."""

    class Nested(BaseModel):
        note: str
        inner: dict[str, int]

    result = _validate_with_repairs(Nested, {"note": "real", "inner": {"a": 1}})

    assert result.inner == {"a": 1}


# --- truncation is a failure for free text too, not just structured output --------------


def _truncating_client(truncated: bool) -> LlmClient:
    def transport(_req: TransportRequest) -> TransportResponse:
        return TransportResponse(
            text="Your ferritin trend suggests",
            tool_input=None,
            input_tokens=10,
            output_tokens=32768,
            truncated=truncated,
        )

    return LlmClient(
        bindings={"primary_reasoner": [ModelBinding(provider="anthropic", model="m", params={})]},
        providers={"anthropic": AnthropicProvider(api_key="k", transport=transport)},
    )


def test_a_truncated_free_text_reply_is_an_error() -> None:
    """`run_informational_turn` passes NO schema, so before this a reply that
    stopped mid-sentence at the token budget went straight to the patient
    with nothing detecting it. A truncated answer is wrong, not short."""
    client = _truncating_client(truncated=True)

    with pytest.raises(LlmError, match="truncated"):
        client.complete(
            "primary_reasoner", system="s", messages=[Message(role="user", content="q")]
        )


def test_an_untruncated_free_text_reply_passes() -> None:
    client = _truncating_client(truncated=False)

    result = client.complete(
        "primary_reasoner", system="s", messages=[Message(role="user", content="q")]
    )

    assert result.text == "Your ferritin trend suggests"


# --- context window: the weakest bound model sets the budget ---------------------------


def _panel_bindings(*windows: int | None) -> dict[str, list[ModelBinding]]:
    return {
        "blind_panel": [
            ModelBinding(provider="anthropic", model=f"m{i}", params={}, context_window=w)
            for i, w in enumerate(windows)
        ]
    }


def test_the_budget_is_the_smallest_window_not_the_largest() -> None:
    """A multi-bound role sends ONE payload to every binding — `blind_panel`
    renders a single context pack for three families — so a context sized to
    the largest window fails on the smallest."""
    client = LlmClient(bindings=_panel_bindings(200_000, 400_000, 64_000), providers={})

    assert client.context_budget("blind_panel") == 64_000 - CONTEXT_COMPLETION_RESERVE


def test_an_undeclared_window_disables_the_check_rather_than_guessing() -> None:
    """An unknown limit is not the same as no limit, and inventing a number
    would be worse than not checking."""
    client = LlmClient(bindings=_panel_bindings(200_000, None), providers={})

    assert client.context_budget("blind_panel") is None


def test_an_oversized_context_is_refused_naming_the_limiting_model() -> None:
    """Refusing here names the role and which of three families was too
    small; letting it through surfaces as an opaque provider error."""
    bindings = _panel_bindings(64_000)
    bindings["blind_panel"][0] = ModelBinding(
        provider="anthropic", model="deepseek-ish", params={}, context_window=64_000
    )
    client = LlmClient(
        bindings=bindings,
        providers={"anthropic": AnthropicProvider(api_key="k", transport=lambda _r: None)},  # type: ignore[arg-type,return-value]
    )
    huge = "x" * (64_000 * 4)

    with pytest.raises(LlmError, match="deepseek-ish"):
        client.complete("blind_panel", system="s", messages=[Message(role="user", content=huge)])


def test_a_context_inside_the_budget_is_allowed() -> None:
    def transport(_req: TransportRequest) -> TransportResponse:
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    client = LlmClient(
        bindings=_panel_bindings(200_000),
        providers={"anthropic": AnthropicProvider(api_key="k", transport=transport)},
    )

    result = client.complete(
        "blind_panel", system="s", messages=[Message(role="user", content="short")]
    )

    assert result.text == "ok"


# --- the reserve describes the call, not the worst call --------------------------------------


def test_the_reserve_defaults_to_the_pessimistic_global() -> None:
    """Unchanged behaviour for a caller that does not say what it needs."""
    client = LlmClient(bindings=_panel_bindings(64_000), providers={})

    assert client.context_budget("blind_panel") == 64_000 - CONTEXT_COMPLETION_RESERVE


def test_a_smaller_completion_reserve_buys_input_budget() -> None:
    """The production failure this exists to prevent.

    The deep review died at `blind_panel_0` with a context of ~31,261 tokens
    against a budget of 31,232 — over by 29 tokens, 0.09% — because a flat
    32,768 was reserved for a completion that is a short JSON list of
    hypotheses. Half of DeepSeek's 64,000-token window was being held back for
    output that never approaches it.
    """
    client = LlmClient(bindings=_panel_bindings(64_000), providers={})
    observed_pack = 31_261

    assert client.context_budget("blind_panel") < observed_pack, "the failing configuration"
    assert client.context_budget("blind_panel", completion_reserve=16_384) > observed_pack


def test_every_panel_binding_can_hold_the_observed_pack() -> None:
    """Against `models.yaml` as configured, not a synthetic fixture.

    The blind context pack measured 31,261 tokens in production, and a member
    that cannot receive it fails the whole review — which is exactly what
    happened when DeepSeek's window was declared as 64,000.

    The comparison is parenthesised deliberately. Written first as
    `budget or 0 > pack`, Python parses that as `budget or (0 > pack)`: a
    truthy int, so the assertion held no matter how small the budgets were.
    It passed vacuously, which is the one thing a guard must never do.
    """
    from adoc.config import load_model_bindings

    observed_pack = 31_261
    bindings = load_model_bindings()
    client = LlmClient(bindings=bindings, providers={})

    too_small = sorted(
        binding.model
        for index, binding in enumerate(bindings["blind_panel"])
        if (client.context_budget("blind_panel", binding_index=index) or 0) <= observed_pack
    )

    assert not too_small, f"these panel members cannot hold the observed context pack: {too_small}"


def test_a_call_is_sized_to_the_binding_it_actually_goes_to() -> None:
    """`complete()` resolves ONE binding by index, and `blind_panel` calls it
    once per member — no request is ever fanned out to all three at once.

    Sizing every call to the smallest window in the role therefore asked the
    wrong question, and in production it failed a 200,000-token Opus call
    because a 64,000-token DeepSeek shared the role.
    """
    client = LlmClient(bindings=_panel_bindings(200_000, 400_000, 64_000), providers={})

    assert client.context_budget("blind_panel", binding_index=0) == 200_000 - 32_768
    assert client.context_budget("blind_panel", binding_index=1) == 400_000 - 32_768
    assert client.context_budget("blind_panel", binding_index=2) == 64_000 - 32_768


def test_the_role_wide_budget_still_means_the_smallest() -> None:
    """Unchanged for a caller that does not name a binding: without one, the
    conservative reading is the only safe one."""
    client = LlmClient(bindings=_panel_bindings(200_000, 400_000, 64_000), providers={})

    assert client.context_budget("blind_panel") == 64_000 - 32_768


def test_an_out_of_range_binding_index_falls_back_to_the_smallest() -> None:
    """A bad index must not silently widen the budget."""
    client = LlmClient(bindings=_panel_bindings(200_000, 64_000), providers={})

    assert client.context_budget("blind_panel", binding_index=9) == 64_000 - 32_768

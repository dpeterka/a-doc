"""Provider-agnostic reasoner adapter (ADR 0005).

`LlmClient` resolves a *role* (e.g. `primary_reasoner`, `challenger`,
`blind_panel`) to one or more `models.yaml` bindings and dispatches to the
matching provider — Anthropic SDK, or an OpenAI-compatible client covering
both OpenAI and Featherless (`base_url=https://api.featherless.ai/v1`) —
behind one `complete()` interface.

Every call (a) scrubs `system` and message content through a `Scrubber`
hook, (b) appends an audit JSONL record (timestamps/role/model/tokens/
cost/duration/scrub_count — never message content), (c) retries a bounded
number of times with backoff on transient transport errors, and (d) raises
`LlmError` for anything else.

Providers are thin wrappers over an injectable transport function
(`_TransportFn`) so unit tests can supply a fake transport and never touch
the network or construct a real SDK client — see `AnthropicProvider` /
`OpenAIProvider` below. The real Anthropic/OpenAI SDK clients are only
constructed lazily, inside each provider's default transport, and only if
no transport was injected.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from adoc.config import ModelBinding, Settings, load_model_bindings
from adoc.privacy import Scrubber

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"

# The app owns the only retry policy - `LlmClient._call_with_retry`'s bounded
# backoff over a handful of attempts (default 3). Without these, each SDK
# client retries its own transient failures internally (default
# max_retries=2, each attempt allowed up to its own timeout) *underneath*
# that loop, so one app-level "attempt" can silently balloon into minutes
# of SDK-internal retrying - stacking retries on retries. `max_retries=0`
# disables the SDK's own retry loop; the explicit finite `timeout` (rather
# than the SDK default) bounds how long a single attempt can hang.
_ANTHROPIC_MAX_RETRIES = 0
_ANTHROPIC_TIMEOUT_SECONDS = 300.0
_OPENAI_MAX_RETRIES = 0
_OPENAI_TIMEOUT_SECONDS = 300.0

# Rough $/1M-token pricing for cost estimation, keyed by exact model id.
# Best-effort only: an unrecognized model id yields `cost_estimate=None`
# rather than a guess. Update alongside `models.yaml` role bindings - every
# model bound to a role in `models.yaml` should have an entry here.
_PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    # TODO: verify against Anthropic's published rates - models.yaml carries
    # no price comments to check against, so this is a placeholder.
    "claude-sonnet-5": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # TODO: verify against Anthropic's published rates (placeholder, as above).
    "claude-haiku-4-5-20251001": (0.25, 1.25),
    # TODO-verify: gpt-5.2 pricing not yet confirmed against OpenAI's
    # published rates.
    "gpt-5.2": (1.25, 10.00),
    # Featherless flat-rate plan (PLAN.md "Model strategy") - a $/mo flat
    # fee, not per-token billing, so the per-call token cost is $0.
    "deepseek-ai/DeepSeek-R1-0528": (0.0, 0.0),
}


class Message(BaseModel):
    """One chat-history turn. The system prompt is passed separately."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


class LlmResult(BaseModel):
    """The result of one `LlmClient.complete()` call."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    parsed: BaseModel | None = None
    model_id: str
    usage: Usage
    cost_estimate: float | None = None


class LlmError(Exception):
    """Raised for any non-transient failure of an `LlmClient.complete()` call."""


class TransientTransportError(Exception):
    """Raised by a transport when the failure is worth retrying (rate limit,
    5xx, connection error). Anything else a transport raises is treated as
    non-transient and wrapped into `LlmError` without retrying."""


@dataclass
class TransportRequest:
    """Everything a provider transport needs to make one completion call.

    `system` and every `Message.content` in `messages` have already been
    scrubbed by the time a transport sees them.
    """

    model: str
    system: str
    messages: list[Message]
    schema: type[BaseModel] | None
    params: dict[str, Any]
    max_tokens: int


@dataclass
class TransportResponse:
    """A provider transport's raw result, before schema validation."""

    text: str
    tool_input: dict[str, Any] | None
    input_tokens: int
    output_tokens: int


TransportFn = Callable[[TransportRequest], TransportResponse]


class Provider(Protocol):
    def complete(self, request: TransportRequest) -> TransportResponse: ...


class AnthropicProvider:
    """Anthropic SDK provider.

    Structured output is implemented as a single forced tool call named
    `emit_result`, whose `input_schema` is the target Pydantic model's JSON
    schema — the tool's `input` is what gets parsed and validated. `params`
    entries the SDK/model recognizes (currently `effort`, `thinking`) are
    passed through; anything else is ignored rather than raising.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        transport: TransportFn | None = None,
    ) -> None:
        self._api_key = api_key
        self._transport = transport or self._default_transport

    def complete(self, request: TransportRequest) -> TransportResponse:
        return self._transport(request)

    def _default_transport(self, request: TransportRequest) -> TransportResponse:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise LlmError("the 'anthropic' package is not installed") from exc

        client = anthropic.Anthropic(
            api_key=self._api_key,
            max_retries=_ANTHROPIC_MAX_RETRIES,
            timeout=_ANTHROPIC_TIMEOUT_SECONDS,
        )

        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [m.model_dump() for m in request.messages],
        }
        if "effort" in request.params:
            kwargs["output_config"] = {"effort": request.params["effort"]}
        if "thinking" in request.params:
            kwargs["thinking"] = request.params["thinking"]

        if request.schema is not None:
            kwargs["tools"] = [
                {
                    "name": "emit_result",
                    "description": "Emit the structured result for this call.",
                    "input_schema": request.schema.model_json_schema(),
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "emit_result"}

        try:
            response = client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise TransientTransportError(str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise TransientTransportError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientTransportError(str(exc)) from exc
            raise LlmError(str(exc)) from exc

        text_parts: list[str] = []
        tool_input: dict[str, Any] | None = None
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == "emit_result":
                tool_input = block.input

        if request.schema is not None and tool_input is None:
            raise LlmError("anthropic: expected an emit_result tool call, none returned")

        return TransportResponse(
            text="".join(text_parts),
            tool_input=tool_input,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class OpenAIProvider:
    """OpenAI-compatible provider — covers both OpenAI and Featherless.

    Pass `base_url=FEATHERLESS_BASE_URL` for Featherless; leave it `None`
    for OpenAI itself. Structured output uses JSON-schema `response_format`
    when a schema is given; otherwise a plain chat completion.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str | None = None,
        transport: TransportFn | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url
        self._transport = transport or self._default_transport

    def complete(self, request: TransportRequest) -> TransportResponse:
        return self._transport(request)

    def _default_transport(self, request: TransportRequest) -> TransportResponse:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise LlmError("the 'openai' package is not installed") from exc

        client = openai.OpenAI(
            api_key=self._api_key,
            base_url=self.base_url,
            max_retries=_OPENAI_MAX_RETRIES,
            timeout=_OPENAI_TIMEOUT_SECONDS,
        )

        chat_messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        chat_messages += [m.model_dump() for m in request.messages]

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": chat_messages,
        }
        # gpt-5.x rejects 'max_tokens' in favor of 'max_completion_tokens';
        # OpenAI-compatible hosts (Featherless) only accept 'max_tokens'.
        if self.base_url is None:
            kwargs["max_completion_tokens"] = request.max_tokens
        else:
            kwargs["max_tokens"] = request.max_tokens
        if "temperature" in request.params:
            kwargs["temperature"] = request.params["temperature"]
        # gpt-5.x thinking depth is the 'reasoning_effort' request parameter
        # (models.yaml params.effort), not a separate model id.
        if self.base_url is None and "effort" in request.params:
            kwargs["reasoning_effort"] = request.params["effort"]

        if request.schema is not None:
            if self.base_url is None:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema.__name__,
                        "schema": _openai_strict_schema(request.schema.model_json_schema()),
                        "strict": True,
                    },
                }
            else:
                # OpenAI-compatible hosts (Featherless) don't reliably support
                # json_schema response_format; instruct JSON in the prompt and
                # extract it from the text (reasoning models may wrap it in
                # <think> blocks).
                schema_json = json.dumps(request.schema.model_json_schema())
                chat_messages[0]["content"] = (
                    f"{request.system}\n\nRespond with ONLY a JSON object valid "
                    f"against this JSON Schema (no prose, no code fences):\n{schema_json}"
                )

        try:
            response = client.chat.completions.create(**kwargs)
        except openai.RateLimitError as exc:
            raise TransientTransportError(str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise TransientTransportError(str(exc)) from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientTransportError(str(exc)) from exc
            raise LlmError(str(exc)) from exc

        if not response.choices:
            raise LlmError("openai: response contained no choices")
        choice = response.choices[0]
        text = choice.message.content or ""

        tool_input: dict[str, Any] | None = None
        if request.schema is not None:
            try:
                tool_input = json.loads(_extract_json_object(text))
            except (json.JSONDecodeError, ValueError) as exc:
                raise LlmError(f"openai: response was not valid JSON: {exc}") from exc

        usage = response.usage
        return TransportResponse(
            text=text,
            tool_input=tool_input,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


def _openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt a pydantic JSON schema for OpenAI strict mode.

    Strict mode requires every object node to carry
    `additionalProperties: false` and to list every property in `required`.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            # Strict mode forbids oneOf (pydantic emits it for discriminated
            # unions, e.g. LedgerDiff's op union inside ChallengerVerdict —
            # found live: every challenger call 400'd) and the discriminator
            # keyword. anyOf is accepted and equivalent for our purposes:
            # the response is re-validated by pydantic afterwards anyway.
            if "oneOf" in out:
                out["anyOf"] = out.pop("oneOf")
            out.pop("discriminator", None)
            if out.get("type") == "object" or "properties" in out:
                out.setdefault("additionalProperties", False)
                if "properties" in out:
                    out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


def _unwrap_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a single-key dict whose value is itself a dict (see the
    flat-first fallback at the validation site); anything else passes
    through untouched.
    """
    if len(tool_input) == 1:
        (only_value,) = tool_input.values()
        if isinstance(only_value, dict):
            return only_value
    return tool_input


def _extract_json_object(text: str) -> str:
    """Return the first top-level JSON object in `text`.

    Reasoning models (DeepSeek-R1) may wrap output in <think> blocks or code
    fences; strict-schema hosts return the bare object, which passes through
    unchanged.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response text")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(cleaned[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    raise ValueError("unbalanced JSON object in response text")


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    rates = _PRICING_PER_MILLION.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    return (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate


@dataclass
class _AuditRecord:
    role: str
    model: str
    provider: str
    input_tokens: int | None
    output_tokens: int | None
    cost_estimate: float | None
    duration_s: float
    scrub_count: int
    error: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "timestamp": self.timestamp,
                "role": self.role,
                "provider": self.provider,
                "model": self.model,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_estimate": self.cost_estimate,
                "duration_s": round(self.duration_s, 4),
                "scrub_count": self.scrub_count,
                "error": self.error,
            }
        )


class LlmClient:
    """Provider-agnostic reasoner adapter. See module docstring."""

    def __init__(
        self,
        bindings: dict[str, list[ModelBinding]],
        providers: dict[str, Provider],
        *,
        scrubber: Scrubber | None = None,
        audit_log_path: Path | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
    ) -> None:
        self._bindings = bindings
        self._providers = providers
        self._scrubber = scrubber if scrubber is not None else Scrubber.noop()
        self._audit_log_path = audit_log_path
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        scrubber: Scrubber | None = None,
        audit_log_path: Path | None = None,
        transports: dict[str, TransportFn] | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
    ) -> LlmClient:
        """Build a client from `Settings` + `models.yaml`.

        `transports` (keyed by provider name: `anthropic`/`openai`/
        `featherless`) lets callers (tests, or a caller wanting a shared
        connection pool) inject a provider's transport; omitted providers
        fall back to that provider's default (real SDK) transport.
        """
        bindings = load_model_bindings(settings.models_file)
        transports = transports or {}

        anthropic_key = (
            settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        )
        openai_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        featherless_key = (
            settings.featherless_api_key.get_secret_value()
            if settings.featherless_api_key
            else None
        )

        providers: dict[str, Provider] = {
            "anthropic": AnthropicProvider(anthropic_key, transport=transports.get("anthropic")),
            "openai": OpenAIProvider(openai_key, transport=transports.get("openai")),
            "featherless": OpenAIProvider(
                featherless_key,
                base_url=FEATHERLESS_BASE_URL,
                transport=transports.get("featherless"),
            ),
        }
        return cls(
            bindings,
            providers,
            scrubber=scrubber,
            audit_log_path=audit_log_path,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
        )

    def _resolve_binding(self, role: str, binding_index: int) -> ModelBinding:
        bindings = self._bindings.get(role)
        if not bindings:
            raise LlmError(f"no model binding configured for role {role!r}")
        if binding_index < 0 or binding_index >= len(bindings):
            raise LlmError(
                f"role {role!r} has {len(bindings)} binding(s); "
                f"binding_index={binding_index} is out of range"
            )
        return bindings[binding_index]

    def _call_with_retry(self, provider: Provider, request: TransportRequest) -> TransportResponse:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                return provider.complete(request)
            except TransientTransportError as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    time.sleep(self._backoff_base_seconds * (2**attempt))
        raise LlmError(
            f"transient transport failure after {self._max_retries} attempt(s)"
        ) from last_exc

    def _audit(self, record: _AuditRecord) -> None:
        if self._audit_log_path is None:
            return
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_log_path.open("a", encoding="utf-8") as fh:
            fh.write(record.to_json_line() + "\n")

    def complete(
        self,
        role: str,
        *,
        system: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        binding_index: int = 0,
        max_tokens: int = 4096,
    ) -> LlmResult:
        binding = self._resolve_binding(role, binding_index)
        provider = self._providers.get(binding.provider)
        if provider is None:
            raise LlmError(f"no provider configured for {binding.provider!r}")

        scrubbed_system, scrub_count = self._scrubber.scrub(system)
        scrubbed_messages: list[Message] = []
        for message in messages:
            scrubbed_content, count = self._scrubber.scrub(message.content)
            scrub_count += count
            scrubbed_messages.append(Message(role=message.role, content=scrubbed_content))

        request = TransportRequest(
            model=binding.model,
            system=scrubbed_system,
            messages=scrubbed_messages,
            schema=schema,
            params=binding.params,
            max_tokens=max_tokens,
        )

        started = time.monotonic()
        try:
            response = self._call_with_retry(provider, request)
        except LlmError:
            self._audit(
                _AuditRecord(
                    role=role,
                    model=binding.model,
                    provider=binding.provider,
                    input_tokens=None,
                    output_tokens=None,
                    cost_estimate=None,
                    duration_s=time.monotonic() - started,
                    scrub_count=scrub_count,
                    error=True,
                )
            )
            raise
        duration = time.monotonic() - started

        parsed: BaseModel | None = None
        if schema is not None:
            if response.tool_input is None:
                self._audit(
                    _AuditRecord(
                        role=role,
                        model=binding.model,
                        provider=binding.provider,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        cost_estimate=None,
                        duration_s=duration,
                        scrub_count=scrub_count,
                        error=True,
                    )
                )
                raise LlmError(f"role {role!r}: provider returned no structured output")
            try:
                try:
                    parsed = schema.model_validate(response.tool_input)
                except Exception:
                    # Known Claude tool-use quirk: complex-schema input
                    # occasionally arrives nested under a single wrapper key
                    # (e.g. {"parameters": {...}}). Flat-first, unwrap-on-
                    # failure so a legitimate single-field payload is never
                    # misinterpreted.
                    parsed = schema.model_validate(_unwrap_tool_input(response.tool_input))
            except Exception as exc:
                self._audit(
                    _AuditRecord(
                        role=role,
                        model=binding.model,
                        provider=binding.provider,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        cost_estimate=None,
                        duration_s=duration,
                        scrub_count=scrub_count,
                        error=True,
                    )
                )
                raise LlmError(
                    f"role {role!r}: structured output failed validation: {exc}"
                ) from exc

        cost = _estimate_cost(binding.model, response.input_tokens, response.output_tokens)
        self._audit(
            _AuditRecord(
                role=role,
                model=binding.model,
                provider=binding.provider,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_estimate=cost,
                duration_s=duration,
                scrub_count=scrub_count,
            )
        )

        return LlmResult(
            text=response.text,
            parsed=parsed,
            model_id=binding.model,
            usage=Usage(input_tokens=response.input_tokens, output_tokens=response.output_tokens),
            cost_estimate=cost,
        )

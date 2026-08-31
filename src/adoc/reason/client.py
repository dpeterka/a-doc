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

`from_settings` — the real-wiring factory every production caller (`cli.py`,
`web/app.py`) uses — defaults `scrubber` to a real `Scrubber` built from
`settings.data_dir/case/identifiers.yaml` (`adoc.privacy.IDENTIFIERS_RELPATH`)
when the caller doesn't pass one explicitly. This is deliberate: the safe
path is the default, and a caller who wants no scrubbing must say so
explicitly (`scrubber=Scrubber.noop()`) rather than getting it by omission.
The bare `LlmClient(bindings, providers)` constructor still defaults
`scrubber` to `Scrubber.noop()` — that constructor has no `Settings`/
`data_dir` to build a real one from, and its callers are exclusively tests
and `evals/suites/*.py` (which only ever wire fake, non-network transports
— see those modules), never a real outbound call path.

Providers are thin wrappers over an injectable transport function
(`_TransportFn`) so unit tests can supply a fake transport and never touch
the network or construct a real SDK client — see `AnthropicProvider` /
`OpenAIProvider` below. The real Anthropic/OpenAI SDK clients are only
constructed lazily, inside each provider's default transport, and only if
no transport was injected.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from adoc.config import ModelBinding, Settings, load_model_bindings
from adoc.privacy import IDENTIFIERS_RELPATH, Scrubber

# A single call can legitimately block for minutes (`_ANTHROPIC_TIMEOUT_SECONDS`
# is 300s per attempt, over `_max_retries` attempts), and a diagnostic turn
# makes several in sequence. Without a log line on either side of the call,
# a slow turn is indistinguishable from a hung one — the audit log only
# gains its record once the call has already returned.
logger = logging.getLogger(__name__)

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"

# Default completion budget. Reasoning models (gpt-5.x with reasoning_effort,
# Claude adaptive thinking) spend REASONING tokens from this same budget -
# 4096 was fully consumed by gpt-5.2's internal reasoning over a large
# context, leaving EMPTY content (live challenger failure). Stages that
# expect small outputs may pass less explicitly.
REASONING_MAX_TOKENS = 32768

# A context window covers input AND output, so the usable input budget is
# the window minus whatever the completion may consume. Reserving the full
# `REASONING_MAX_TOKENS` is deliberately pessimistic: a reasoning model can
# spend the entire budget, and being wrong in this direction costs a little
# context, while being wrong the other way costs the call.
CONTEXT_COMPLETION_RESERVE = REASONING_MAX_TOKENS

# Token estimate without a tokenizer round-trip. ~4 chars/token is the usual
# English approximation; the divisor is deliberately low (3.5) so the
# estimate errs HIGH — a pre-flight check that under-estimates lets through
# exactly the call it exists to stop. Only ever used to decide whether to
# refuse a call, never to trim content.
_CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """Deliberately pessimistic character-based token estimate."""
    return int(len(text) / _CHARS_PER_TOKEN) + 1


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
    truncated: bool = False
    """Generation stopped because it hit the completion budget, not because
    the model finished (Anthropic `stop_reason == "max_tokens"`, OpenAI
    `finish_reason == "length"`).

    Reported by the transport, judged by `LlmClient.complete` — a provider
    should say what happened, not decide what it means. Defaults to `False`
    so existing fake transports keep working.
    """


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
            truncated=getattr(response, "stop_reason", None) == "max_tokens",
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
        if request.schema is not None and not text.strip():
            raise LlmError(
                f"model {request.model!r}: no structured content returned "
                f"(finish_reason={choice.finish_reason!r}) - reasoning tokens "
                "likely consumed the completion budget; raise max_tokens"
            )

        tool_input: dict[str, Any] | None = None
        if request.schema is not None:
            try:
                tool_input = json.loads(_extract_json_object(text))
            except (json.JSONDecodeError, ValueError) as exc:
                raise LlmError(f"openai: response was not valid JSON: {exc}") from exc

        usage = response.usage
        return TransportResponse(
            truncated=getattr(choice, "finish_reason", None) == "length",
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


# Keys that are a tool-schema PLACEHOLDER leaking into the output rather
# than a real field: the model echoed the parameter's name/value scaffolding
# instead of filling it in.
_PLACEHOLDER_NAME_KEYS = frozenset({"parameter_name", "$parameter_name", "parametername"})


def _unwrap_placeholder_envelope(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Lift a payload nested under a placeholder envelope.

    Observed live, six times in one 115-document backfill:

        {"parameter_name": "DocumentExtraction",
         "parameter_value": {...the real payload...}}

    `_unwrap_tool_input` only unwraps a SINGLE-key dict, so a two-key
    envelope like this went straight to a hard validation failure and cost
    the document. Requires exactly one dict-valued entry and a placeholder
    name key alongside it, so a legitimate payload that merely happens to
    contain one nested object is never unwrapped.
    """
    if not any(key.lower() in _PLACEHOLDER_NAME_KEYS for key in tool_input):
        return tool_input
    dict_values = [value for value in tool_input.values() if isinstance(value, dict)]
    if len(dict_values) != 1:
        return tool_input
    return dict_values[0]


def _decode_json_valued_strings(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Decode values that arrived as a JSON STRING instead of a structure.

    Tool-use output occasionally serializes a list or object field into a
    string — `{"ops": "[{\\"op\\": \\"update_fact\\", ...}]"}` instead of
    `{"ops": [...]}`. Pydantic rejects it (`Input should be a valid list`),
    the retry usually produces the same shape, and the turn dies. A live
    33-turn intake run hit it 4 times, once surfacing a raw pydantic error
    to the patient.

    Only strings that already look like JSON (`[`/`{`) are touched, and only
    when they parse; a field legitimately holding prose is never mangled.
    """
    repaired: dict[str, Any] = {}
    changed = False
    for key, value in tool_input.items():
        if isinstance(value, str):
            candidate = value.strip()
            if candidate[:1] in ("[", "{"):
                try:
                    repaired[key] = json.loads(candidate)
                    changed = True
                    continue
                except json.JSONDecodeError:
                    pass
        repaired[key] = value
    return repaired if changed else tool_input


def _validate_with_repairs(schema: type[BaseModel], tool_input: dict[str, Any]) -> BaseModel:
    """Validate `tool_input` against `schema`, trying known tool-use
    malformations in turn. Flat-first, so a payload that is already correct
    is never reinterpreted; each repair is attempted only after the plainer
    reading has failed. The LAST attempt's error is what propagates, so a
    genuinely bad payload still reports a real validation message.
    """
    candidates = [
        tool_input,
        _unwrap_tool_input(tool_input),
        _unwrap_placeholder_envelope(tool_input),
        _decode_json_valued_strings(tool_input),
        _decode_json_valued_strings(_unwrap_tool_input(tool_input)),
        _decode_json_valued_strings(_unwrap_placeholder_envelope(tool_input)),
    ]
    best_error: Exception | None = None
    best_count = -1
    for index, candidate in enumerate(candidates):
        if index and candidate is candidates[index - 1]:
            continue  # repair was a no-op; do not re-validate the same dict
        try:
            return schema.model_validate(candidate)
        except Exception as exc:  # noqa: PERF203 - each repair is a distinct attempt
            # Report the error from the candidate that got FURTHEST, not the
            # last one tried. The last candidate is the most heavily rewritten
            # and therefore the least informative: a live intake turn died
            # reporting "Input should be a valid list" for `ops` even though a
            # repair had already turned `ops` into a list, because a later
            # candidate re-raised the shallow error and masked whatever
            # actually failed. Two retries and a lost patient turn later, the
            # log still did not say what was wrong.
            count = len(exc.errors()) if isinstance(exc, ValidationError) else 1
            if best_error is None or count < best_count:
                best_error, best_count = exc, count
    assert best_error is not None
    raise best_error


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
        # NOTE: this bare-constructor default is intentionally a no-op — see
        # the module docstring. `from_settings` (the real-wiring path) does
        # NOT use this default; it builds a real `Scrubber` unless the
        # caller opts out explicitly.
        self._scrubber = scrubber if scrubber is not None else Scrubber.noop()
        self._audit_log_path = audit_log_path
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds

    @property
    def privacy_warning(self) -> str | None:
        """`None` if this client's scrubber is either an explicit no-op or
        has at least one name configured to match; otherwise a
        human-readable warning naming the exact identifiers file to
        create/populate. See `Scrubber.coverage_warning` — surfaced here so
        `cli.py`/`web/app.py` never have to reach into `_scrubber`."""
        return self._scrubber.coverage_warning

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

        `scrubber`, if omitted, defaults to a real `Scrubber` built from
        `settings.data_dir/case/identifiers.yaml` — see the module
        docstring. Pass `scrubber=Scrubber.noop()` explicitly to opt out.

        `transports` (keyed by provider name: `anthropic`/`openai`/
        `featherless`) lets callers (tests, or a caller wanting a shared
        connection pool) inject a provider's transport; omitted providers
        fall back to that provider's default (real SDK) transport.
        """
        bindings = load_model_bindings(settings.models_file)
        transports = transports or {}
        if scrubber is None:
            scrubber = Scrubber.from_file(settings.data_dir / IDENTIFIERS_RELPATH)

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

    def context_budget(
        self,
        role: str,
        *,
        binding_index: int | None = None,
        completion_reserve: int = CONTEXT_COMPLETION_RESERVE,
    ) -> int | None:
        """Usable input budget for `role`: the SMALLEST declared window among
        its bindings, minus what this call may actually spend on completion.

        `completion_reserve` defaults to the global pessimistic figure but
        should be the call's own `max_tokens`. Reserving a flat 32,768 for
        every call regardless of what it asked for took the deep review down
        in production: the blind context pack reached 31,261 tokens against a
        budget of 31,232 — over by 29 tokens, 0.09% — because half of
        DeepSeek's 64,000-token window was being held back for an output that
        is a short JSON list of hypotheses. The reserve has to describe the
        call, not the worst call the system can make.

        The weakest link, deliberately — a multi-bound role sends one payload
        to every binding (`blind_panel` renders a single context pack for
        three model families), so a context that fits the largest window but
        not the smallest fails on the smallest. Sizing to anything but the
        minimum means the pack is only *sometimes* valid.

        `None` when any binding leaves `context_window` undeclared: an
        unknown limit is not the same as no limit, and guessing one would be
        worse than not checking.
        """
        bindings = self._bindings.get(role) or []
        if binding_index is not None and 0 <= binding_index < len(bindings):
            # This call goes to exactly ONE model. `complete()` resolves a
            # single binding by index, and `blind_panel` calls it once per
            # member rather than fanning one request out to all three — so the
            # payload only has to fit the binding actually being called.
            #
            # Sizing every call to the smallest window in the role made a
            # 200,000-token Opus call fail because a 64,000-token DeepSeek
            # shared the role. That is not conservatism, it is the wrong
            # question: no request is ever sent to all three at once.
            window = bindings[binding_index].context_window
            return None if window is None else window - completion_reserve
        windows = [b.context_window for b in bindings]
        if not windows or any(w is None for w in windows):
            return None
        return min(w for w in windows if w is not None) - completion_reserve

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
        max_tokens: int = REASONING_MAX_TOKENS,
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

        # Pre-flight against the WEAKEST bound model's window (see
        # `context_budget`). Refusing here names the role and the limiting
        # model; letting it through surfaces as a provider error that says
        # nothing about which of three families was too small, or — worse on
        # some hosts — as a silently truncated input.
        budget = self.context_budget(
            role, binding_index=binding_index, completion_reserve=max_tokens
        )
        if budget is not None:
            estimated = estimate_tokens(scrubbed_system) + sum(
                estimate_tokens(m.content) for m in scrubbed_messages
            )
            if estimated > budget:
                smallest = min(
                    (b for b in self._bindings.get(role, []) if b.context_window is not None),
                    key=lambda b: b.context_window or 0,
                )
                raise LlmError(
                    f"role {role!r}: context is ~{estimated:,} tokens but the budget is "
                    f"{budget:,} — set by the smallest bound model "
                    f"({smallest.model}, {smallest.context_window:,}-token window) minus a "
                    f"{max_tokens:,}-token completion reserve for this call. Every binding "
                    "for this role receives the same payload, so it must fit the smallest."
                )

        # Content is never logged — only the routing metadata (role, provider,
        # model) and, afterwards, timing/token counts. Same rule the audit log
        # follows.
        logger.info(
            "llm: role=%s provider=%s model=%s calling", role, binding.provider, binding.model
        )
        started = time.monotonic()
        try:
            response = self._call_with_retry(provider, request)
        except LlmError:
            logger.warning(
                "llm: role=%s model=%s FAILED after %.1fs",
                role,
                binding.model,
                time.monotonic() - started,
            )
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

        # Truncation is a failure for EVERY call, structured or not. A
        # free-text reply that stopped mid-sentence at the budget is wrong,
        # not merely short — and `run_informational_turn` passes no schema,
        # so before this such a reply reached the patient undetected.
        # Judged here rather than in each provider so the rule is one rule,
        # and so it is reachable through the transport injection seam that
        # every test uses.
        if response.truncated:
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
                f"role {role!r} ({binding.model}): output hit the {max_tokens}-token budget "
                f"before completing ({response.output_tokens} tokens produced) - the response "
                "is truncated, not merely short"
            )

        logger.info(
            "llm: role=%s model=%s ok in %.1fs (in=%s out=%s tokens)",
            role,
            binding.model,
            duration,
            response.input_tokens,
            response.output_tokens,
        )

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
                    parsed = _validate_with_repairs(schema, response.tool_input)
                except ValidationError:
                    # Shape only — keys and types, never values. A payload
                    # that defeats every repair is undiagnosable from the
                    # error alone, and the values are the patient's.
                    logger.warning(
                        "llm: role=%s payload failed every repair; shape=%s",
                        role,
                        {
                            key: (
                                f"{type(value).__name__}[{len(value)}]"
                                if isinstance(value, (list, str, dict))
                                else type(value).__name__
                            )
                            for key, value in sorted(response.tool_input.items())
                        },
                    )
                    raise
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

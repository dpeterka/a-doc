"""Vision/document extension of the provider-agnostic LLM layer.

`reason.client.LlmClient.complete()` is text-only by design — its
`TransportRequest.messages` are `Message`s of plain `str` content (see that
module's docstring). Extraction over scanned documents needs binary
content blocks (a PDF, or page images), which is a different request
shape, not a different provider — so rather than change `client.py`'s
contract (out of scope for this slice, and every existing caller assumes
text-only), this module adds a *parallel* `VisionClient` that mirrors
`client.py`'s architecture (injectable-transport providers, forced
structured output, bounded retries, audit logging) for the document case,
and wraps an already-constructed `LlmClient` to reuse its role bindings,
scrubber, audit log path, and retry policy rather than duplicating that
configuration.

Provider behavior:
- **Anthropic**: `PdfPart` becomes a `document` content block (base64,
  `application/pdf`); `ImagePart` becomes an `image` content block (base64,
  `image/png`). Structured output uses the same forced `emit_result` tool
  call pattern as `AnthropicProvider` in `client.py`.
- **OpenAI**: `ImagePart` becomes an `image_url` data-URI content part.
  `PdfPart` is rejected with `VisionError` *before* any network call — by
  design, PDFs are never sent to the OpenAI-family model (PLAN.md's
  double-pass ingestion: pass A is the PDF-native Anthropic pass, pass B is
  the OpenAI-family pass over rendered page PNGs).

Scrubbing: only `system` and any `TextPart.text` pass through the wrapped
client's `Scrubber` before being sent. Binary document/image bytes bypass
the scrubber entirely and by design — PLAN.md's Privacy row accepts that
"vision extraction necessarily sends raw documents".
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from adoc.reason.client import (
    LlmClient,
    TransientTransportError,
    _openai_strict_schema,
    _unwrap_tool_input,
)

# --------------------------------------------------------------------------
# Document/image content parts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TextPart:
    """Plain instructional/labeling text (e.g. "Page 2 of 5:")."""

    text: str


@dataclass(frozen=True)
class PdfPart:
    """A whole PDF document, sent natively to a provider that supports it."""

    data: bytes
    filename: str = "document.pdf"


@dataclass(frozen=True)
class ImagePart:
    """One page rendered to a PNG image."""

    data: bytes
    page: int | None = None


Part = TextPart | PdfPart | ImagePart

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class VisionError(Exception):
    """Raised for a non-transient vision-extraction failure."""


# --------------------------------------------------------------------------
# Transport seam (mirrors reason.client.TransportRequest/Response/Fn)
# --------------------------------------------------------------------------


@dataclass
class VisionTransportRequest:
    model: str
    system: str
    parts: list[Part]
    schema: type[BaseModel]
    params: dict[str, Any]
    max_tokens: int


@dataclass
class VisionTransportResponse:
    text: str
    tool_input: dict[str, Any] | None
    input_tokens: int
    output_tokens: int
    truncated: bool = False  # provider hit its output-token limit


VisionTransportFn = Callable[[VisionTransportRequest], VisionTransportResponse]


class VisionProvider(Protocol):
    def extract(self, request: VisionTransportRequest) -> VisionTransportResponse: ...


# --------------------------------------------------------------------------
# Content-block builders
# --------------------------------------------------------------------------


def _anthropic_content_blocks(parts: Sequence[Part]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, PdfPart):
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(part.data).decode("ascii"),
                    },
                }
            )
        elif isinstance(part, ImagePart):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(part.data).decode("ascii"),
                    },
                }
            )
        else:  # pragma: no cover - Part is a closed union
            raise VisionError(f"unsupported part type: {type(part)!r}")
    return blocks


_OPENAI_NO_PDF_MESSAGE = (
    "the OpenAI vision path does not accept PdfPart by design - PLAN.md's "
    "double-pass ingestion always sends page PNGs (ImagePart) to the "
    "OpenAI-family pass; render pages first"
)


def _openai_content_parts(parts: Sequence[Part]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            result.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            encoded = base64.b64encode(part.data).decode("ascii")
            result.append(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
            )
        elif isinstance(part, PdfPart):  # pragma: no cover - extract() rejects PdfPart earlier
            raise VisionError(_OPENAI_NO_PDF_MESSAGE)
        else:  # pragma: no cover - Part is a closed union
            raise VisionError(f"unsupported part type: {type(part)!r}")
    return result


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class AnthropicVisionProvider:
    """Anthropic SDK vision provider: PDF `document` / `image` content blocks."""

    def __init__(self, api_key: str | None, *, transport: VisionTransportFn | None = None) -> None:
        self._api_key = api_key
        self._transport = transport or self._default_transport

    def extract(self, request: VisionTransportRequest) -> VisionTransportResponse:
        return self._transport(request)

    def _default_transport(self, request: VisionTransportRequest) -> VisionTransportResponse:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise VisionError("the 'anthropic' package is not installed") from exc

        client = anthropic.Anthropic(api_key=self._api_key)
        content = _anthropic_content_blocks(request.parts)

        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": content}],
            "tools": [
                {
                    "name": "emit_result",
                    "description": "Emit the structured extraction result for this call.",
                    "input_schema": request.schema.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": "emit_result"},
        }
        if "effort" in request.params:
            kwargs["output_config"] = {"effort": request.params["effort"]}
        if "thinking" in request.params:
            kwargs["thinking"] = request.params["thinking"]

        try:
            response = client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise TransientTransportError(str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise TransientTransportError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientTransportError(str(exc)) from exc
            raise VisionError(str(exc)) from exc

        text_parts: list[str] = []
        tool_input: dict[str, Any] | None = None
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == "emit_result":
                tool_input = block.input

        return VisionTransportResponse(
            text="".join(text_parts),
            tool_input=tool_input,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            truncated=response.stop_reason == "max_tokens",
        )


class OpenAIVisionProvider:
    """OpenAI-compatible vision provider: `image_url` data-URI content parts."""

    def __init__(self, api_key: str | None, *, transport: VisionTransportFn | None = None) -> None:
        self._api_key = api_key
        self._transport = transport or self._default_transport

    def extract(self, request: VisionTransportRequest) -> VisionTransportResponse:
        # Reject PdfPart here - before any transport (real or injected) is
        # ever invoked - so this holds even when a test/caller injects a
        # transport that bypasses `_default_transport`'s content-block
        # building below.
        if any(isinstance(part, PdfPart) for part in request.parts):
            raise VisionError(_OPENAI_NO_PDF_MESSAGE)
        return self._transport(request)

    def _default_transport(self, request: VisionTransportRequest) -> VisionTransportResponse:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise VisionError("the 'openai' package is not installed") from exc

        client = openai.OpenAI(api_key=self._api_key)
        content = _openai_content_parts(request.parts)

        kwargs: dict[str, Any] = {
            "model": request.model,
            # gpt-5.x rejects 'max_tokens'; this pass-B provider is
            # OpenAI-proper only (never Featherless), so use the new name
            # unconditionally — parity with reason.client's OpenAIProvider.
            "max_completion_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema.__name__,
                    # Strict mode requires additionalProperties/required on
                    # every object node — same adaptation as reason.client.
                    "schema": _openai_strict_schema(request.schema.model_json_schema()),
                    "strict": True,
                },
            },
        }

        try:
            response = client.chat.completions.create(**kwargs)
        except openai.RateLimitError as exc:
            raise TransientTransportError(str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise TransientTransportError(str(exc)) from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientTransportError(str(exc)) from exc
            raise VisionError(str(exc)) from exc

        choice = response.choices[0]
        text = choice.message.content or ""
        try:
            tool_input: dict[str, Any] | None = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisionError(f"openai: response was not valid JSON: {exc}") from exc

        usage = response.usage
        finish = response.choices[0].finish_reason if response.choices else None
        return VisionTransportResponse(
            text=text,
            tool_input=tool_input,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            truncated=finish == "length",
        )


def _wrapped_client_api_key(client: LlmClient, provider_name: str) -> str | None:
    """Best-effort reuse of the wrapped `LlmClient`'s provider API key.

    `LlmClient` does not expose its providers' API keys publicly (there is
    no public accessor, by design - see `client.py`), so this reaches into
    the provider objects it already built via `LlmClient.from_settings`.
    Returns `None` (letting the real SDK fall back to its own env-var
    convention) if the wrapped client has no provider under this name.
    """
    provider = client._providers.get(provider_name)  # noqa: SLF001
    return getattr(provider, "_api_key", None) if provider is not None else None


class VisionClient:
    """Document-extraction counterpart to `LlmClient`.

    Wraps an already-constructed `LlmClient` to reuse its role bindings,
    scrubber, audit log path, and retry policy. `providers` lets a caller
    (tests, mainly) fully replace the Anthropic/OpenAI vision providers
    with fakes; `transports` is the lighter-weight alternative that keeps
    the real provider wiring but swaps just the network call.
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        providers: dict[str, VisionProvider] | None = None,
        transports: dict[str, VisionTransportFn] | None = None,
    ) -> None:
        self._client = client
        if providers is not None:
            self._providers = providers
        else:
            transports = transports or {}
            self._providers = {
                "anthropic": AnthropicVisionProvider(
                    _wrapped_client_api_key(client, "anthropic"),
                    transport=transports.get("anthropic"),
                ),
                "openai": OpenAIVisionProvider(
                    _wrapped_client_api_key(client, "openai"),
                    transport=transports.get("openai"),
                ),
            }

    @property
    def client(self) -> LlmClient:
        """The wrapped `LlmClient` - reused by `ingest.pipeline`'s docx path,
        which has no binary pages to send and calls `LlmClient.complete`
        directly (classification + `extract.double_pass_extract_text`)
        rather than this class's vision-specific `extract`. Exposed rather
        than duplicated so both paths share one set of role bindings,
        scrubber, audit log, and retry policy."""
        return self._client

    def extract(
        self,
        role: str,
        *,
        system: str,
        parts: Sequence[Part],
        schema: type[SchemaT],
        binding_index: int = 0,
        max_tokens: int = 4096,
    ) -> SchemaT:
        binding = self._client._resolve_binding(role, binding_index)  # noqa: SLF001
        provider = self._providers.get(binding.provider)
        if provider is None:
            raise VisionError(f"no vision provider configured for {binding.provider!r}")

        scrubber = self._client._scrubber  # noqa: SLF001
        scrubbed_system, scrub_count = scrubber.scrub(system)
        scrubbed_parts: list[Part] = []
        for part in parts:
            if isinstance(part, TextPart):
                scrubbed_text, count = scrubber.scrub(part.text)
                scrub_count += count
                scrubbed_parts.append(TextPart(text=scrubbed_text))
            else:
                scrubbed_parts.append(part)

        request = VisionTransportRequest(
            model=binding.model,
            system=scrubbed_system,
            parts=scrubbed_parts,
            schema=schema,
            params=binding.params,
            max_tokens=max_tokens,
        )

        started = time.monotonic()
        try:
            response = self._call_with_retry(provider, request)
        except VisionError:
            self._audit(
                role,
                binding.provider,
                binding.model,
                None,
                time.monotonic() - started,
                scrub_count,
                error=True,
            )
            raise
        duration = time.monotonic() - started

        if response.tool_input is None:
            self._audit(
                role, binding.provider, binding.model, response, duration, scrub_count, error=True
            )
            raise VisionError(f"role {role!r}: provider returned no structured output")
        if response.truncated:
            # A truncated extraction is silent data loss (a real LabCorp
            # panel came back as one row) — fail hard so it lands in the
            # ingest report instead of the labs table.
            raise VisionError(
                f"role {role!r}: output hit the token limit; extraction is "
                "incomplete — raise max_tokens for this call"
            )
        try:
            try:
                parsed = schema.model_validate(response.tool_input)
            except Exception:
                # Same Claude wrapper-key quirk handled in reason.client:
                # flat-first, unwrap-on-failure.
                parsed = schema.model_validate(_unwrap_tool_input(response.tool_input))
        except Exception as exc:
            self._audit(
                role, binding.provider, binding.model, response, duration, scrub_count, error=True
            )
            raise VisionError(f"role {role!r}: structured output failed validation: {exc}") from exc

        self._audit(
            role, binding.provider, binding.model, response, duration, scrub_count, error=False
        )
        return parsed

    def _call_with_retry(
        self, provider: VisionProvider, request: VisionTransportRequest
    ) -> VisionTransportResponse:
        max_retries = self._client._max_retries  # noqa: SLF001
        backoff = self._client._backoff_base_seconds  # noqa: SLF001
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                return provider.extract(request)
            except TransientTransportError as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    time.sleep(backoff * (2**attempt))
        raise VisionError(
            f"transient transport failure after {max_retries} attempt(s)"
        ) from last_exc

    def _audit(
        self,
        role: str,
        provider_name: str,
        model: str,
        response: VisionTransportResponse | None,
        duration: float,
        scrub_count: int,
        *,
        error: bool,
    ) -> None:
        path: Path | None = self._client._audit_log_path  # noqa: SLF001
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "role": role,
            "provider": provider_name,
            "model": model,
            "input_tokens": response.input_tokens if response else None,
            "output_tokens": response.output_tokens if response else None,
            "cost_estimate": None,
            "duration_s": round(duration, 4),
            "scrub_count": scrub_count,
            "error": error,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

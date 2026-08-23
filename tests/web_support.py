"""Shared fake-building helpers for the web UI test suite (`tests/test_web_*.py`).

Not a test module itself (no `test_` prefix, so pytest never collects it) —
each `test_web_*.py` file imports these plain functions to build a
`create_app(...)` instance wired to a real (tmp_path) `DataRepo`, a real
in-memory `LabsDb`, and a fake-transport `LlmClient` that never touches the
network, mirroring the pattern in `tests/test_stages.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding, Settings
from adoc.ingest.archive import PageRenderer
from adoc.ingest.vision import VisionClient
from adoc.labs.db import LabsDb
from adoc.reason.client import (
    AnthropicProvider,
    LlmClient,
    OpenAIProvider,
    TransportRequest,
    TransportResponse,
)
from adoc.web.app import create_app
from adoc.web.users import USERS_RELPATH, add_user

DEFAULT_USERNAME = "patient"
DEFAULT_PASSWORD = "correct-horse-battery-staple"

Transport = Callable[[TransportRequest], TransportResponse]


def exploding_transport(calls: list[TransportRequest]) -> Transport:
    """A transport that fails the test loudly if ever invoked — used for
    the red-flag "zero client calls" assertions."""

    def _transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        raise AssertionError("the LLM transport must not be called in this test")

    return _transport


def make_primary_transport(
    ledger_ops: list[dict[str, Any]],
    patient_reply: dict[str, Any],
    calls: list[TransportRequest],
    *,
    route: str = "diagnostic",
) -> Transport:
    """Services `primary_reasoner` (ledger-maintainer + composer) and
    `classifier` (`TurnRoute`), dispatching on the requested schema — same
    pattern as `tests/test_stages.py`."""

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is not None
        name = request.schema.__name__
        if name == "_LedgerDiffPayload":
            tool_input: dict[str, Any] = {"rationale": "proposed diff", "ops": ledger_ops}
        elif name == "PatientReply":
            tool_input = patient_reply
        elif name == "TurnRoute":
            tool_input = {"route": route, "rationale": "test routing"}
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema for primary transport: {name}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=10, output_tokens=10)

    return transport


def make_informational_transport(text: str, calls: list[TransportRequest]) -> Transport:
    """Services `primary_reasoner`'s schema-less informational-turn call and
    the `classifier` role's `TurnRoute` call."""

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        if request.schema is not None and request.schema.__name__ == "TurnRoute":
            return TransportResponse(
                text="",
                tool_input={"route": "informational", "rationale": "test routing"},
                input_tokens=5,
                output_tokens=5,
            )
        return TransportResponse(text=text, tool_input=None, input_tokens=10, output_tokens=10)

    return transport


def make_challenger_transport(
    counter_arguments: list[dict[str, Any]],
    additional_ops: list[dict[str, Any]],
    calls: list[TransportRequest],
) -> Transport:
    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        tool_input = {
            "counter_arguments": counter_arguments,
            "additional_ops": additional_ops,
            "verdict_notes": "reviewed",
        }
        return TransportResponse(text="", tool_input=tool_input, input_tokens=10, output_tokens=10)

    return transport


def build_fake_client(primary_transport: Transport, challenger_transport: Transport) -> LlmClient:
    """`primary_reasoner`/`classifier` -> anthropic; `challenger` -> openai —
    same role/provider layout as `tests/test_stages.py`."""
    bindings: dict[str, list[ModelBinding]] = {
        "primary_reasoner": [ModelBinding(provider="anthropic", model="fake-primary")],
        "challenger": [ModelBinding(provider="openai", model="fake-challenger")],
        "classifier": [ModelBinding(provider="anthropic", model="fake-primary")],
    }
    providers = {
        "anthropic": AnthropicProvider(api_key=None, transport=primary_transport),
        "openai": OpenAIProvider(api_key=None, transport=challenger_transport),
    }
    return LlmClient(bindings, providers)


def build_app(
    tmp_path: Path,
    *,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    trust_forwarded_for: bool = False,
    primary_transport: Transport | None = None,
    challenger_transport: Transport | None = None,
    vision: VisionClient | None = None,
    renderer: PageRenderer | None = None,
) -> tuple[FastAPI, DataRepo, LabsDb, list[TransportRequest]]:
    """Build a fully-wired `create_app(...)` over a tmp_path data repo, an
    in-memory `LabsDb`, and a fake-transport `LlmClient`. Seeds one web
    login user (`username`/`password`) in the repo's `work/users.yaml`.
    Returns `(app, repo, db, calls)` — `calls` accumulates every
    `TransportRequest` any fake provider transport received, in order."""
    calls: list[TransportRequest] = []
    primary = primary_transport or exploding_transport(calls)
    challenger = challenger_transport or exploding_transport(calls)
    client = build_fake_client(primary, challenger)

    data_dir = tmp_path / "data"
    repo = DataRepo.init_at(data_dir)
    add_user(repo.root / USERS_RELPATH, username, password)
    db = LabsDb(":memory:")
    settings = Settings(data_dir=data_dir, trust_forwarded_for=trust_forwarded_for)

    app = create_app(settings, repo=repo, db=db, client=client, vision=vision, renderer=renderer)
    return app, repo, db, calls


def login(
    client: TestClient, username: str = DEFAULT_USERNAME, password: str = DEFAULT_PASSWORD
) -> None:
    response = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "adoc_session" in response.cookies

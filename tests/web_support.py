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
from adoc.intake.coverage import INTAKE_STATE_RELPATH, CoverageState, save_coverage_state
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


def default_visit_capture_transport(request: TransportRequest) -> TransportResponse:
    """Default `VisitCaptureResult` transport: empty ops, so a test that
    doesn't care about interval-history capture (most chat tests) never has
    to script one — every post-intake successful turn now also triggers
    `intake.agent.run_visit_capture` (`docs/adr/0013-fact-corroboration.md`),
    and this keeps that pass a silent no-op by default."""
    return TransportResponse(text="", tool_input={"ops": []}, input_tokens=1, output_tokens=1)


def build_fake_client(
    primary_transport: Transport,
    challenger_transport: Transport,
    *,
    intake_agent_transport: Transport | None = None,
    visit_capture_transport: Transport | None = None,
) -> LlmClient:
    """`primary_reasoner`/`classifier` -> anthropic; `challenger` -> openai —
    same role/provider layout as `tests/test_stages.py`. `intake_agent`
    also binds to the anthropic provider, defaulting to `primary_transport`
    so any test not exercising onboarding never has to think about it;
    `visit_capture_transport` defaults to `default_visit_capture_transport`
    (empty ops) so a test not exercising interval-history capture never has
    to think about it either."""
    bindings: dict[str, list[ModelBinding]] = {
        "primary_reasoner": [ModelBinding(provider="anthropic", model="fake-primary")],
        "challenger": [ModelBinding(provider="openai", model="fake-challenger")],
        "classifier": [ModelBinding(provider="anthropic", model="fake-primary")],
        "intake_agent": [ModelBinding(provider="anthropic", model="fake-intake-agent")],
    }
    providers = {
        "anthropic": AnthropicProvider(
            api_key=None,
            transport=_dispatching_anthropic_transport(
                primary_transport,
                intake_agent_transport or primary_transport,
                visit_capture_transport or default_visit_capture_transport,
            ),
        ),
        "openai": OpenAIProvider(api_key=None, transport=challenger_transport),
    }
    return LlmClient(bindings, providers)


def _dispatching_anthropic_transport(
    primary_transport: Transport,
    intake_agent_transport: Transport,
    visit_capture_transport: Transport,
) -> Transport:
    """`primary_reasoner`/`classifier`, `intake_agent`, and the interval-
    history visit-capture pass all share the anthropic provider slot in this
    test double; dispatch by schema name (`IntakeTurnResult`/
    `VisitCaptureResult`) so onboarding/capture tests can inject their own
    transport independently of whatever `primary_transport` a given test
    already set up for chat/ledger schemas."""

    def transport(request: TransportRequest) -> TransportResponse:
        if request.schema is not None:
            name = request.schema.__name__
            if name == "IntakeTurnResult":
                return intake_agent_transport(request)
            if name == "VisitCaptureResult":
                return visit_capture_transport(request)
        return primary_transport(request)

    return transport


def build_app(
    tmp_path: Path,
    *,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    trust_forwarded_for: bool = False,
    max_upload_mb: int | None = None,
    primary_transport: Transport | None = None,
    challenger_transport: Transport | None = None,
    intake_agent_transport: Transport | None = None,
    visit_capture_transport: Transport | None = None,
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
    client = build_fake_client(
        primary,
        challenger,
        intake_agent_transport=intake_agent_transport,
        visit_capture_transport=visit_capture_transport,
    )

    data_dir = tmp_path / "data"
    repo = DataRepo.init_at(data_dir)
    add_user(repo.root / USERS_RELPATH, username, password)
    db = LabsDb(":memory:")
    settings_kwargs: dict[str, Any] = {
        "data_dir": data_dir,
        "trust_forwarded_for": trust_forwarded_for,
    }
    if max_upload_mb is not None:
        settings_kwargs["max_upload_mb"] = max_upload_mb
    settings = Settings(**settings_kwargs)

    app = create_app(settings, repo=repo, db=db, client=client, vision=vision, renderer=renderer)
    return app, repo, db, calls


def mark_intake_complete(repo: DataRepo) -> None:
    """Test helper: seed `case/intake-state.yaml` as already-complete
    (`docs/adr/0012-initial-visit-conversation.md`) so a test can exercise
    the diagnostic/informational chat pipeline directly, without also
    scripting an `intake_agent` transport through a full initial-visit
    conversation first. Real completion only ever happens through
    `intake.agent.run_intake_turn`'s deterministic wrap-up gate — this
    helper exists only because most chat tests are about what happens
    *after* onboarding, not onboarding itself."""
    save_coverage_state(repo.root / INTAKE_STATE_RELPATH, CoverageState(intake_complete=True))
    repo.commit("chore: seed intake-complete state for test")


def login(
    client: TestClient, username: str = DEFAULT_USERNAME, password: str = DEFAULT_PASSWORD
) -> None:
    response = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "adoc_session" in response.cookies

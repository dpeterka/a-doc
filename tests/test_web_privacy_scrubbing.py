"""Regression tests for docs/adr/0017-default-scrubber-and-identifiers-file.md:
`web/app.py`'s `create_app` must wire a real, scrubbing `LlmClient` by
default — before this fix, `create_app` called `LlmClient.from_settings
(resolved_settings)` with no `scrubber=` argument, and `LlmClient.__init__`
silently defaulted a missing scrubber to `Scrubber.noop()`, so every chat
turn/intake turn/visit-capture pass sent unscrubbed patient text to
Anthropic/OpenAI/Featherless.

These tests build the app the same way `create_app` does (real `Settings`,
a seeded `case/identifiers.yaml`), inject a fake transport so no network
call is ever made, and assert the transport received scrub tokens rather
than the literal name/DOB/address — this must fail on current `develop`.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from adoc.casefile.repo import DataRepo
from adoc.config import Settings
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, Message, TransportRequest, TransportResponse
from adoc.web.app import create_app
from adoc.web.deps import get_privacy_warning


def _models_file(tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(
        "roles:\n  primary_reasoner:\n    provider: anthropic\n    model: claude-opus-5\n",
        encoding="utf-8",
    )
    return path


def test_create_app_default_client_scrubs_direct_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    DataRepo.init_at(data_dir)
    identifiers_path = data_dir / "case" / "identifiers.yaml"
    identifiers_path.write_text(
        "names: ['Jane Q. Public']\ndob: '1980-05-12'\naddress_fragments: ['123 Main St']\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(data_dir=data_dir, models_file=_models_file(tmp_path))

    seen: dict[str, object] = {}

    def fake_transport(request: TransportRequest) -> TransportResponse:
        seen["system"] = request.system
        seen["messages"] = list(request.messages)
        return TransportResponse(text="ok", tool_input=None, input_tokens=1, output_tokens=1)

    # Exactly `create_app`'s own default-construction shape
    # (`LlmClient.from_settings(resolved_settings)`), with only the
    # transport swapped so no real network call happens.
    client = LlmClient.from_settings(settings, transports={"anthropic": fake_transport})
    app = create_app(settings, repo=DataRepo(data_dir), db=LabsDb(":memory:"), client=client)

    assert app.state.client is client
    assert app.state.privacy_warning is None

    app.state.client.complete(
        "primary_reasoner",
        system="The patient is Jane Q. Public, DOB 1980-05-12, of 123 Main St.",
        messages=[
            Message(
                role="user",
                content="Jane Q. Public reports fatigue. CRP is 8.5 mg/L, ANA titer 1:640.",
            )
        ],
    )

    system_seen = str(seen["system"])
    messages_seen = str(seen["messages"])
    assert "Jane Q. Public" not in system_seen
    assert "1980-05-12" not in system_seen
    assert "123 Main St" not in system_seen
    assert "[NAME]" in system_seen
    assert "[DOB]" in system_seen
    assert "[ADDRESS]" in system_seen
    assert "Jane Q. Public" not in messages_seen
    # Clinical content must survive scrubbing untouched.
    assert "CRP is 8.5 mg/L" in messages_seen
    assert "ANA titer 1:640" in messages_seen


def test_create_app_default_wiring_is_not_a_noop_scrubber_even_without_an_explicit_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises `create_app`'s actual default branch
    (`client=None` -> `LlmClient.from_settings(resolved_settings)`, no
    transport override) and proves the resulting client's scrubber is a
    real, enabled one — not the previous silent `Scrubber.noop()`. This
    never calls `.complete()` (which would try to build a real Anthropic
    SDK client), so it stays offline."""
    data_dir = tmp_path / "data"
    DataRepo.init_at(data_dir)
    identifiers_path = data_dir / "case" / "identifiers.yaml"
    identifiers_path.write_text("names: ['Jane Q. Public']\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(data_dir=data_dir, models_file=_models_file(tmp_path))

    app = create_app(settings, repo=DataRepo(data_dir), db=LabsDb(":memory:"))

    assert app.state.client.privacy_warning is None
    text, count = app.state.client._scrubber.scrub(  # noqa: SLF001 - white-box regression check
        "Patient is Jane Q. Public."
    )
    assert count == 1
    assert text == "Patient is [NAME]."


def test_create_app_surfaces_a_loud_warning_when_identifiers_are_unpopulated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    DataRepo.init_at(data_dir)  # no case/identifiers.yaml seeded
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(data_dir=data_dir, models_file=_models_file(tmp_path))

    app = create_app(settings, repo=DataRepo(data_dir), db=LabsDb(":memory:"))

    assert app.state.privacy_warning is not None
    assert "identifiers.yaml" in app.state.privacy_warning

    # web.deps.get_privacy_warning is the seam a route/template can use to
    # surface this without reaching into app.state directly.
    fake_request = types.SimpleNamespace(app=types.SimpleNamespace(state=app.state))
    assert get_privacy_warning(fake_request) == app.state.privacy_warning  # type: ignore[arg-type]

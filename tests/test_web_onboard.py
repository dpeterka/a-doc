"""Onboarding surface tests: the web wizard drives `IntakeWizard` end to
end — submit produces a playback, confirm advances the section state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.reason.client import TransportRequest, TransportResponse


def _basics_transport(calls: list[TransportRequest]):
    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is not None
        if request.schema.__name__ == "BasicsSection":
            tool_input: dict[str, Any] = {
                "age": 40,
                "sex_at_birth": "female",
                "height_cm": None,
                "weight_kg": None,
                "occupation": None,
                "exposures": [],
            }
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema: {request.schema.__name__}")
        return TransportResponse(text="", tool_input=tool_input, input_tokens=5, output_tokens=5)

    return transport


def test_onboard_page_shows_progress_and_first_section(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/onboard")

    assert response.status_code == 200
    assert "[1/10] Basics" in response.text
    assert "0 of 10" in response.text


def test_submit_then_confirm_advances_the_section(tmp_path: Path) -> None:
    calls: list[TransportRequest] = []
    app, repo, _db, _ = build_app(tmp_path, primary_transport=_basics_transport(calls))
    client = TestClient(app)
    login(client)

    submit_response = client.post("/onboard/submit", data={"text": "I'm 40 and female."})
    assert submit_response.status_code == 200
    assert "age: 40" in submit_response.text
    assert "Confirm" in submit_response.text
    assert len(calls) == 1

    confirm_response = client.post("/onboard/confirm")
    assert confirm_response.status_code == 200
    assert "1 of 10" in confirm_response.text
    assert "[2/10] Current symptoms" in confirm_response.text

    # The section write + intake-state update is a real git commit.
    assert "Age: 40" in repo.read("case/case-summary.md")


def test_submit_with_blank_text_shows_an_error_without_calling_the_llm(
    tmp_path: Path,
) -> None:
    app, _repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/onboard/submit", data={"text": "   "})

    assert response.status_code == 200
    assert "Please write something" in response.text
    assert calls == []


def test_reopen_a_section_moves_the_cursor(tmp_path: Path) -> None:
    calls: list[TransportRequest] = []
    app, _repo, _db, _ = build_app(tmp_path, primary_transport=_basics_transport(calls))
    client = TestClient(app)
    login(client)

    client.post("/onboard/submit", data={"text": "I'm 40 and female."})
    client.post("/onboard/confirm")

    response = client.get("/onboard/section/basics")

    assert response.status_code == 200
    assert "[1/10] Basics" in response.text

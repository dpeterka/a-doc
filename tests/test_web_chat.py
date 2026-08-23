"""Chat surface tests: the red-flag screen runs before any model call, and
a diagnostic turn renders the three-tier differential.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login, make_challenger_transport, make_primary_transport

_SLE_MOST_LIKELY_OP = {
    "op": "add_hypothesis",
    "hypothesis": {
        "id": "sle-01",
        "name": "Systemic lupus erythematosus",
        "tier": "most-likely",
        "probability": "moderate",
        "status": "active",
        "origin": "model",
        "first_proposed": "2026-08-01",
        "evidence_for": [
            {"claim": "ANA elevated", "source": "labs:ana-titer:2026-05-02", "strength": "strong"}
        ],
    },
}

_PE_CANT_MISS_OP = {
    "op": "add_hypothesis",
    "hypothesis": {
        "id": "pe-01",
        "name": "Pulmonary embolism",
        "tier": "cant-miss",
        "probability": "low",
        "status": "active",
        "origin": "model",
        "first_proposed": "2026-08-01",
    },
}

_PATIENT_REPLY = {
    "tiers_rendered": (
        "Most Likely: a lupus-like presentation — this is a lead to discuss with your doctor.\n"
        "Expanded: a few other possibilities remain open.\n"
        "Can't-Miss: pulmonary embolism stays on the board."
    ),
    "tests_to_request": ["Complement C3/C4 panel"],
    "framing_ack": True,
}


def test_red_flag_message_returns_urgent_template_with_zero_llm_calls(
    tmp_path: Path,
) -> None:
    app, _repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send", data={"text": "I have crushing chest pain radiating to my left arm"}
    )

    assert response.status_code == 200
    assert "call 911" in response.text.lower() or "emergency" in response.text.lower()
    assert calls == []


def test_diagnostic_turn_renders_the_three_tiers(tmp_path: Path) -> None:
    calls = []
    primary = make_primary_transport([_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], _PATIENT_REPLY, calls)
    challenger = make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    app, _repo, _db, _calls = build_app(
        tmp_path, primary_transport=primary, challenger_transport=challenger
    )
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send",
        data={"text": "My joints have been aching for weeks and I'm constantly exhausted."},
    )

    assert response.status_code == 200
    body = response.text
    assert "Most Likely" in body
    assert "Expanded" in body
    assert "Can" in body and "Miss" in body
    assert "Complement C3/C4 panel" in body


def test_blank_message_shows_an_error_without_calling_the_llm(tmp_path: Path) -> None:
    app, _repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "   "})

    assert response.status_code == 200
    assert "type a message" in response.text.lower()
    assert calls == []


def test_chat_page_renders_without_error_when_transcript_is_empty(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/chat")

    assert response.status_code == 200

"""Onboarding surface tests (`docs/adr/0012-initial-visit-conversation.md`):
onboarding is no longer its own chat surface — `/onboard` and
`/onboard/send` are redirects to the one real surface, `/chat` (see
`tests/test_web_chat.py` for the actual conversation-routing tests).
`/onboard/review` survives as "Intake record": a read-only page listing
every active fact grouped by internal topic (fine for a record, unlike a
stepper), still with attribution/precision badges and a "Correct this"
affordance that now points back at `/chat`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.intake.agent import IntakeTurnResult
from adoc.reason.client import TransportRequest, TransportResponse


def _intake_transport(queue: list[dict[str, Any]]):
    remaining = list(queue)

    def transport(request: TransportRequest) -> TransportResponse:
        assert request.schema is IntakeTurnResult
        if not remaining:
            raise AssertionError("no scripted intake_agent response left")
        return TransportResponse(
            text="", tool_input=remaining.pop(0), input_tokens=5, output_tokens=5
        )

    return transport


def test_onboard_get_redirects_permanently_to_chat(tmp_path: Path) -> None:
    app, _repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/onboard", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "/chat"
    assert calls == []


def test_onboard_send_post_redirects_to_chat(tmp_path: Path) -> None:
    app, _repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/onboard/send", data={"text": "anything"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/chat"
    assert calls == []


def test_review_page_renders_active_fact_badges_and_retracted_facts_collapsed(
    tmp_path: Path,
) -> None:
    intake_transport = _intake_transport(
        [
            {
                "message": "Noted.",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "cancer-theory",
                            "section": "prior_diagnoses",
                            "kind": "diagnosis",
                            "statement": "Patient believes they have cancer.",
                            "attribution": "patient_assumption",
                            "fields": {"reasoning": "unexplained weight loss"},
                        },
                    },
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "old-theory",
                            "section": "prior_diagnoses",
                            "kind": "diagnosis",
                            "statement": "An old, since-withdrawn theory.",
                            "attribution": "patient_assumption",
                            "fields": {"reasoning": "n/a"},
                        },
                    },
                ],
                "topics_covered": [],
                "intake_complete": False,
            },
            {
                "message": "Got it, removed.",
                "ops": [
                    {
                        "op": "retract_fact",
                        "id": "old-theory",
                        "reason": "patient said this no longer applies",
                    }
                ],
                "topics_covered": [],
                "intake_complete": False,
            },
        ]
    )
    app, _repo, _db, _ = build_app(tmp_path, intake_agent_transport=intake_transport)
    client = TestClient(app)
    login(client)

    client.post("/chat/send", data={"text": "I think I have cancer, unexplained weight loss."})
    client.post("/chat/send", data={"text": "Actually forget the old theory."})

    response = client.get("/onboard/review")

    assert response.status_code == 200
    body = response.text
    assert "Intake record" in body
    assert "Patient believes they have cancer." in body
    assert "Patient&#39;s own suspicion" in body or "Patient's own suspicion" in body
    assert "Correct this" in body
    assert "1 retracted" in body


def test_review_page_never_shows_a_section_list_or_progress(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/onboard/review")

    assert response.status_code == 200
    assert "of 10" not in response.text
    assert "<progress" not in response.text


def test_review_page_correct_this_prefills_the_chat_page(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/onboard/review")

    assert response.status_code == 200
    assert '"/chat?prefill="' in response.text


def test_onboard_review_stays_reachable_and_shows_amend_banner_after_completion(
    tmp_path: Path,
) -> None:
    from web_support import mark_intake_complete

    app, repo, _db, _ = build_app(tmp_path)
    mark_intake_complete(repo)

    client = TestClient(app)
    login(client)

    response = client.get("/onboard/review")

    assert response.status_code == 200
    assert "Initial visit complete" in response.text

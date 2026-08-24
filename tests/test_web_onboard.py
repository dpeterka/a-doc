"""Onboarding surface tests (`docs/adr/0011-conversational-agentic-onboarding.md`):
the web UI is a chat surface driven entirely by `intake.agent.run_intake_turn`
— submit/send round-trips a turn, `/onboard/review` renders active facts with
badges, "Revisit" reopens a section through the normal turn loop, and
onboarding stays reachable (in amend mode) after every section completes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.intake.agent import IntakeTurnResult
from adoc.intake.sections import SECTIONS
from adoc.intake.wizard import INTAKE_STATE_RELPATH, IntakeState, SectionState, save_intake_state
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


def _basics_add_op(**field_overrides: Any) -> dict[str, Any]:
    fields = {"age": 40, "sex_at_birth": "female"}
    fields.update(field_overrides)
    return {
        "op": "add_fact",
        "fact": {
            "id": "basic-1",
            "section": "basics",
            "kind": "basic",
            "statement": "Patient is 40, female.",
            "fields": fields,
        },
    }


def test_onboard_page_shows_progress_and_first_section(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/onboard")

    assert response.status_code == 200
    assert "0 of 10" in response.text
    assert "Basics" in response.text


def test_send_a_turn_shows_the_reply_and_persists_facts(tmp_path: Path) -> None:
    intake_transport = _intake_transport(
        [
            {
                "message": "Got it — 40, female. What's your occupation?",
                "ops": [_basics_add_op()],
                "section_complete": False,
                "wants_section": None,
            }
        ]
    )
    app, repo, _db, _ = build_app(tmp_path, intake_agent_transport=intake_transport)
    client = TestClient(app)
    login(client)

    response = client.post("/onboard/send", data={"text": "I'm 40 and female."})

    assert response.status_code == 200
    assert "your occupation" in response.text

    from adoc.intake.facts import IntakeFactsStore

    store = IntakeFactsStore(repo.root)
    assert len(store.active_facts()) == 1


def test_send_with_blank_text_shows_an_error_without_calling_the_llm(tmp_path: Path) -> None:
    app, _repo, _db, _ = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/onboard/send", data={"text": "   "})

    assert response.status_code == 200
    assert "Please write something" in response.text


def test_section_complete_advances_progress(tmp_path: Path) -> None:
    intake_transport = _intake_transport(
        [
            {
                "message": "Got it, thanks.",
                "ops": [_basics_add_op(occupation="software engineer")],
                "section_complete": True,
                "wants_section": None,
            }
        ]
    )
    app, _repo, _db, _ = build_app(tmp_path, intake_agent_transport=intake_transport)
    client = TestClient(app)
    login(client)

    response = client.post("/onboard/send", data={"text": "40, female, software engineer."})

    assert response.status_code == 200
    assert "1 of 10" in response.text


def test_revisit_button_posts_a_canned_message_through_the_normal_turn_loop(
    tmp_path: Path,
) -> None:
    intake_transport = _intake_transport(
        [
            {
                "message": "Sure — let's revisit your basics. Anything to add or change?",
                "ops": [],
                "section_complete": False,
                "wants_section": "basics",
            }
        ]
    )
    app, repo, _db, _ = build_app(tmp_path, intake_agent_transport=intake_transport)
    client = TestClient(app)
    login(client)

    # Complete "basics" first so it's no longer current, then confirm the
    # onboarding page's revisit button targets it with the expected canned
    # text (rendered inside a hidden form field).
    state = IntakeState(
        sections={
            "basics": SectionState(status="complete", completed_at=datetime.now(UTC)),
            **{spec.key: SectionState() for spec in SECTIONS if spec.key != "basics"},
        },
        cursor="symptoms",
    )
    save_intake_state(repo.root / INTAKE_STATE_RELPATH, state)
    repo.commit("chore: seed state for test")

    response = client.get("/onboard")
    assert response.status_code == 200
    assert "like to revisit Basics." in response.text

    revisit = client.post("/onboard/send", data={"text": "I'd like to revisit Basics."})
    assert revisit.status_code == 200
    assert "revisit your basics" in revisit.text


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
                "section_complete": False,
                "wants_section": None,
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
                "section_complete": False,
                "wants_section": None,
            },
        ]
    )
    app, _repo, _db, _ = build_app(tmp_path, intake_agent_transport=intake_transport)
    client = TestClient(app)
    login(client)

    client.post("/onboard/send", data={"text": "I think I have cancer, unexplained weight loss."})
    client.post("/onboard/send", data={"text": "Actually forget the old theory."})

    response = client.get("/onboard/review")

    assert response.status_code == 200
    body = response.text
    assert "Patient believes they have cancer." in body
    assert "Patient&#39;s own suspicion" in body or "Patient's own suspicion" in body
    assert "Correct this" in body
    assert "1 retracted" in body


def test_onboard_stays_reachable_and_shows_amend_banner_after_completion(
    tmp_path: Path,
) -> None:
    repo_state = IntakeState(
        sections={
            spec.key: SectionState(status="complete", completed_at=datetime.now(UTC))
            for spec in SECTIONS
        },
        cursor=None,
    )
    app, repo, _db, _ = build_app(tmp_path)
    save_intake_state(repo.root / INTAKE_STATE_RELPATH, repo_state)
    repo.commit("chore: seed complete state for test")

    client = TestClient(app)
    login(client)

    response = client.get("/onboard")

    assert response.status_code == 200
    assert "Onboarding complete" in response.text

    review_response = client.get("/onboard/review")
    assert review_response.status_code == 200

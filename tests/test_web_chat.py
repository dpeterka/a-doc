"""Chat surface tests: the red-flag screen runs before any model call, and
a diagnostic turn renders the three-tier differential.

`docs/adr/0012-initial-visit-conversation.md` merged onboarding into this
same surface: while intake is incomplete every turn routes through
`intake.agent.run_intake_turn` instead of the diagnostic pipeline, so the
diagnostic-focused tests below seed `mark_intake_complete(repo)` first —
they are about what happens once the initial visit is done, not about
onboarding itself (that gets its own tests further down).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import (
    build_app,
    exploding_transport,
    login,
    make_challenger_transport,
    make_primary_transport,
    mark_intake_complete,
)

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH
from adoc.intake.agent import INTAKE_OPENER_MESSAGE, IntakeTurnResult, VisitCaptureResult
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.client import TransportRequest, TransportResponse

_ANA_SOURCE_DOC_SHA = "c" * 64


def _seed_ana_titer_row(db: LabsDb) -> None:
    """Seed the `labs:ana-titer:2026-05-02` row `_SLE_MOST_LIKELY_OP` cites
    so the Phase-2 citation checker (`reason.citations`) resolves it —
    without this, the citation-check DAG contract correctly rejects the
    diff as an unresolved evidence source ref."""
    db.upsert_document(
        LabDocument(
            sha256=_ANA_SOURCE_DOC_SHA, filename="quest.pdf", doc_type="lab-result", page_count=1
        )
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="ana-titer",
                name_raw="ANA",
                value_text="1:640",
                source_doc=_ANA_SOURCE_DOC_SHA,
                raw_json=json.dumps({"name_raw": "ANA"}),
            )
        ]
    )


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


def test_red_flag_message_during_intake_warns_but_the_turn_still_happens(
    tmp_path: Path,
) -> None:
    """Warn, don't block (ADR 0014): the warning rides along with a real
    reply instead of replacing it, so a patient recounting past history
    still gets her turn recorded."""
    intake_transport = _intake_transport(
        [
            {
                "message": "Thanks for telling me — I've recorded that 2019 episode.",
                "ops": [],
                "topics_covered": [],
                "intake_complete": False,
            }
        ]
    )
    app, _repo, _db, _calls = build_app(tmp_path, intake_agent_transport=intake_transport)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send",
        data={"text": "Back in 2019 I had crushing chest pain radiating to my left arm"},
    )

    assert response.status_code == 200
    body_lower = response.text.lower()
    assert "heads up" in body_lower  # the mandatory, code-inserted warning
    assert "cardiac chest pain" in body_lower  # naming the matched category
    # The turn still happened: the model's reply is there alongside the
    # warning (apostrophes are HTML-escaped in the rendered bubble).
    assert "recorded that 2019 episode" in body_lower


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
    app, repo, db, _calls = build_app(
        tmp_path, primary_transport=primary, challenger_transport=challenger
    )
    mark_intake_complete(repo)
    _seed_ana_titer_row(db)
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


def test_dosing_laden_composer_output_is_withheld_but_ledger_is_still_updated(
    tmp_path: Path,
) -> None:
    """S5 remediation: the Composer's output failing safety.treatment_gate
    raises a ContractViolation AFTER the diagnostic DAG's `apply` node has
    already committed the ledger diff (Ledger-Maintainer -> Challenger ->
    apply -> Composer). That must render as a warm, 200-status withheld
    message — never a bare 500/traceback — and the ledger update must
    stand: the case file was genuinely updated even though the reply text
    was blocked.
    """
    calls: list = []
    bad_reply = {
        "tiers_rendered": (
            "Most Likely: a lupus-like presentation. You should take 20 mg prednisone daily."
        ),
        "tests_to_request": [],
        "framing_ack": True,
    }
    primary = make_primary_transport([_PE_CANT_MISS_OP], bad_reply, calls)
    challenger = make_challenger_transport(counter_arguments=[], additional_ops=[], calls=calls)
    app, repo, _db, _calls = build_app(
        tmp_path, primary_transport=primary, challenger_transport=challenger
    )
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    ledger_before = load_ledger(repo.root / LEDGER_RELPATH)

    response = client.post(
        "/chat/send",
        data={"text": "My joints have been aching for weeks and I'm constantly exhausted."},
    )

    assert response.status_code == 200
    assert "traceback" not in response.text.lower()
    assert "internal server error" not in response.text.lower()
    body_lower = response.text.lower()
    assert "safety check" in body_lower or "withheld" in body_lower

    ledger_after = load_ledger(repo.root / LEDGER_RELPATH)
    assert ledger_after.version > ledger_before.version


_ACTIVE_NO_CANT_MISS_OP = {
    "op": "add_hypothesis",
    "hypothesis": {
        "id": "sle-01",
        "name": "Systemic lupus erythematosus",
        "tier": "expanded",
        "probability": "moderate",
        "status": "active",
        "origin": "model",
        "first_proposed": "2026-08-01",
    },
}


def test_ledger_invariant_violation_is_withheld_not_a_bare_500(tmp_path: Path) -> None:
    """S5 remediation: a LedgerInvariantError (here: the proposed diff would
    leave the cant-miss tier empty while a hypothesis remains active) must
    also render as a warm withheld message with a 200 status, never a bare
    500/traceback."""
    calls: list = []
    primary = make_primary_transport([_ACTIVE_NO_CANT_MISS_OP], {}, calls)
    challenger = make_challenger_transport(counter_arguments=[], additional_ops=[], calls=calls)
    app, repo, _db, _calls = build_app(
        tmp_path, primary_transport=primary, challenger_transport=challenger
    )
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send",
        data={"text": "My joints have been aching for weeks."},
    )

    assert response.status_code == 200
    assert "traceback" not in response.text.lower()
    assert "internal server error" not in response.text.lower()
    body_lower = response.text.lower()
    assert "safety guard" in body_lower or "consistency check" in body_lower


def test_chat_page_renders_without_error_when_transcript_is_empty(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/chat")

    assert response.status_code == 200


# --- one surface: onboarding merged into /chat (docs/adr/0012) -------------------------


def _intake_transport(queue: list[dict]):
    remaining = list(queue)

    def transport(request: TransportRequest) -> TransportResponse:
        assert request.schema is IntakeTurnResult
        if not remaining:
            raise AssertionError("no scripted intake_agent response left")
        return TransportResponse(
            text="", tool_input=remaining.pop(0), input_tokens=5, output_tokens=5
        )

    return transport


def test_fresh_chat_page_shows_the_deterministic_opener(tmp_path: Path) -> None:
    app, _repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/chat")

    assert response.status_code == 200
    # (Jinja HTML-escapes apostrophes, so check a clause that has none.)
    assert "This first conversation is how we build your case file together" in response.text
    assert calls == []  # a constant, never an LLM call


def test_chat_page_has_no_opener_once_intake_is_complete(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.get("/chat")

    assert response.status_code == 200
    assert INTAKE_OPENER_MESSAGE not in response.text


def test_no_progress_bar_or_section_list_while_intake_incomplete(tmp_path: Path) -> None:
    """Owner feedback: sections must never be visible UI, not even a
    progress percentage."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/chat")

    assert response.status_code == 200
    assert "Basics" not in response.text
    assert "of 10" not in response.text


def test_first_turn_while_intake_incomplete_routes_through_intake_agent(tmp_path: Path) -> None:
    intake_transport = _intake_transport(
        [
            {
                "message": "Got it — 41, female. What's your occupation?",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "basic-1",
                            "section": "basics",
                            "kind": "basic",
                            "statement": "Patient is 41, female.",
                            "fields": {"age": 41, "sex_at_birth": "female"},
                        },
                    }
                ],
                "topics_covered": [],
                "intake_complete": False,
            }
        ]
    )
    app, repo, _db, calls = build_app(tmp_path, intake_agent_transport=intake_transport)
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "I'm 41 and female."})

    assert response.status_code == 200
    assert "your occupation" in response.text
    # `calls` only accumulates primary/challenger transport invocations
    # (see web_support.build_fake_client) -- an empty list here confirms
    # the diagnostic pipeline was never touched for this turn.
    assert calls == []

    from adoc.intake.facts import IntakeFactsStore
    from adoc.web.casefile_helpers import read_recent_chat

    store = IntakeFactsStore(repo.root)
    assert len(store.active_facts()) == 1

    transcript = read_recent_chat(repo)
    # opener (assistant) -> patient -> assistant reply: one continuous
    # conversation, the opener written on the first patient turn.
    assert [entry["role"] for entry in transcript] == ["assistant", "patient", "assistant"]
    assert transcript[0]["text"] == INTAKE_OPENER_MESSAGE


def test_diagnostic_turns_unlock_only_once_wrap_up_is_accepted(tmp_path: Path) -> None:
    """Once the deterministic wrap-up gate accepts `intake_complete` on one
    turn, the VERY NEXT turn routes through the diagnostic pipeline instead
    of `run_intake_turn` — same `/chat/send` route, no separate surface,
    and every topic must already be covered with no active blockers for
    the accept to happen at all (`intake.agent.run_intake_turn`'s own unit
    tests cover the veto path in detail)."""
    from adoc.intake.coverage import CoverageState, TopicCoverage, save_coverage_state
    from adoc.intake.wizard import INTAKE_STATE_RELPATH

    intake_transport = _intake_transport(
        [
            {
                "message": "Thank you — I think I have a good picture. Feel free to send over "
                "any records, or ask me anything.",
                "ops": [],
                "topics_covered": [],
                "intake_complete": True,
            }
        ]
    )
    calls: list = []
    primary = make_primary_transport([_PE_CANT_MISS_OP], _PATIENT_REPLY, calls, route="diagnostic")
    challenger = make_challenger_transport(counter_arguments=[], additional_ops=[], calls=calls)
    app, repo, _db, _calls = build_app(
        tmp_path,
        intake_agent_transport=intake_transport,
        primary_transport=primary,
        challenger_transport=challenger,
    )
    # Every topic already covered (no active facts anywhere means no
    # blockers), so this turn's `intake_complete=True` proposal is
    # actually accepted rather than vetoed.
    from adoc.intake.sections import SECTIONS

    save_coverage_state(
        repo.root / INTAKE_STATE_RELPATH,
        CoverageState(topics={spec.key: TopicCoverage(covered=True) for spec in SECTIONS}),
    )
    repo.commit("chore: seed all-topics-covered state for test")
    client = TestClient(app)
    login(client)

    wrap_up = client.post("/chat/send", data={"text": "I think that's everything."})
    assert wrap_up.status_code == 200
    assert "good picture" in wrap_up.text

    response = client.post(
        "/chat/send",
        data={"text": "My joints have been aching for weeks and I'm constantly exhausted."},
    )

    assert response.status_code == 200
    assert "Most Likely" in response.text


def test_onboard_get_redirects_to_chat(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/onboard", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "/chat"


def test_onboard_send_post_redirects_to_chat(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/onboard/send", data={"text": "anything"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/chat"


# --- interval history: post-intake visit capture (docs/adr/0013) -----------------------


def _visit_capture_transport(ops: list, calls: list):
    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is VisitCaptureResult
        return TransportResponse(text="", tool_input={"ops": ops}, input_tokens=5, output_tokens=5)

    return transport


def test_successful_diagnostic_turn_triggers_visit_capture(tmp_path: Path) -> None:
    capture_calls: list = []
    capture_transport = _visit_capture_transport(
        [
            {
                "op": "add_fact",
                "fact": {
                    "id": "new-symptom",
                    "section": "symptoms",
                    "kind": "symptom",
                    "statement": "New knee swelling mentioned in this visit.",
                },
            }
        ],
        capture_calls,
    )
    calls: list = []
    primary = make_primary_transport([_PE_CANT_MISS_OP], _PATIENT_REPLY, calls)
    challenger = make_challenger_transport(counter_arguments=[], additional_ops=[], calls=calls)
    app, repo, _db, _calls = build_app(
        tmp_path,
        primary_transport=primary,
        challenger_transport=challenger,
        visit_capture_transport=capture_transport,
    )
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send",
        data={"text": "My joints have been aching for weeks and I'm constantly exhausted."},
    )

    assert response.status_code == 200
    assert len(capture_calls) == 1

    from adoc.intake.facts import IntakeFactsStore

    store = IntakeFactsStore(repo.root)
    fact = store.get("new-symptom")
    assert fact is not None
    assert fact.provenance.dag_node == "visit-capture"


def test_red_flag_turn_still_carries_the_warning_after_intake_is_complete(
    tmp_path: Path,
) -> None:
    """The warning is not intake-only: a flagged message in an ordinary
    post-onboarding visit is annotated the same way (ADR 0014)."""
    calls: list = []
    primary = make_primary_transport([_SLE_MOST_LIKELY_OP, _PE_CANT_MISS_OP], _PATIENT_REPLY, calls)
    challenger = make_challenger_transport(
        counter_arguments=[
            {"hypothesis_id": "sle-01", "argument": "Anti-dsDNA has not been checked yet."}
        ],
        additional_ops=[],
        calls=calls,
    )
    app, repo, db, _calls = build_app(
        tmp_path, primary_transport=primary, challenger_transport=challenger
    )
    mark_intake_complete(repo)
    _seed_ana_titer_row(db)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send", data={"text": "I have crushing chest pain radiating to my left arm"}
    )

    assert response.status_code == 200
    assert "heads up" in response.text.lower()
    assert "Most Likely" in response.text, "the diagnostic turn still runs"


def test_withheld_turn_never_triggers_visit_capture(tmp_path: Path) -> None:
    capture_calls: list = []
    bad_reply = {
        "tiers_rendered": (
            "Most Likely: a lupus-like presentation. You should take 20 mg prednisone daily."
        ),
        "tests_to_request": [],
        "framing_ack": True,
    }
    calls: list = []
    primary = make_primary_transport([_PE_CANT_MISS_OP], bad_reply, calls)
    challenger = make_challenger_transport(counter_arguments=[], additional_ops=[], calls=calls)
    app, repo, _db, _calls = build_app(
        tmp_path,
        primary_transport=primary,
        challenger_transport=challenger,
        visit_capture_transport=exploding_transport(capture_calls),
    )
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send",
        data={"text": "My joints have been aching for weeks and I'm constantly exhausted."},
    )

    assert response.status_code == 200
    assert capture_calls == []


def test_error_turn_never_triggers_visit_capture(tmp_path: Path) -> None:
    capture_calls: list = []

    def broken_route_transport(request: TransportRequest) -> TransportResponse:
        assert request.schema is not None
        if request.schema.__name__ == "TurnRoute":
            return TransportResponse(
                text="not structured", tool_input=None, input_tokens=1, output_tokens=1
            )
        raise AssertionError("must never reach past a failed route_turn call")

    app, repo, _db, _calls = build_app(
        tmp_path,
        primary_transport=broken_route_transport,
        visit_capture_transport=exploding_transport(capture_calls),
    )
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "My joints have been aching for weeks."})

    assert response.status_code == 200
    assert capture_calls == []

"""Chat surface tests: a diagnostic turn renders the three-tier
differential. There is no automated emergency screening anywhere in this
app (see `docs/adr/0021*.md`).

`docs/adr/0012-initial-visit-conversation.md` merged onboarding into this
same surface: while intake is incomplete every turn routes through
`intake.agent.run_intake_turn` instead of the diagnostic pipeline, so the
diagnostic-focused tests below seed `mark_intake_complete(repo)` first —
they are about what happens once the initial visit is done, not about
onboarding itself (that gets its own tests further down).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import (
    build_app,
    exploding_transport,
    login,
    make_challenger_transport,
    make_informational_transport,
    make_primary_transport,
    mark_intake_complete,
)

from adoc.casefile.ledger import load_ledger
from adoc.casefile.questions import (
    QUESTIONS_RELPATH,
    OpenQuestion,
    OpenQuestions,
    load_questions,
    question_id,
    save_questions,
)
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import Provenance
from adoc.intake.agent import INTAKE_OPENER_MESSAGE, IntakeTurnResult, VisitCaptureResult
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.client import TransportRequest, TransportResponse
from adoc.web.casefile_helpers import chat_log_path, read_recent_chat

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


def test_intake_message_recounting_history_records_the_turn_normally(
    tmp_path: Path,
) -> None:
    """No automated emergency screening anywhere in this app (see
    `docs/adr/0021*.md`): a patient recounting past history during intake
    just gets her turn recorded normally, with no warning banner."""
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
    assert "heads up" not in body_lower  # no warning banner of any kind
    # The turn happened normally: the model's reply is there.
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


def _seed_old_chat_entry(repo: DataRepo, *, days_ago: int) -> datetime:
    """Write a chat-transcript entry directly, dated `days_ago` in the past
    -- simulates "the last time we talked" for post-intake continuity
    (`docs/adr/0018-intake-clinical-progression-and-continuity.md`) without
    needing a real prior HTTP round trip."""
    when = datetime.now(UTC) - timedelta(days=days_ago)
    path = chat_log_path(repo, when.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": when.isoformat(),
        "role": "assistant",
        "kind": "informational",
        "text": "(a prior visit's reply)",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return when


def test_new_visit_greeting_carries_a_continuity_note(tmp_path: Path) -> None:
    """docs/adr/0018: the first reply of a new visit (gap since the last
    chat entry past the visit-gap threshold) opens with a short,
    deterministic continuity note -- here, naming the flagged follow-up."""
    from adoc.intake.coverage import INTAKE_STATE_RELPATH, CoverageState, save_coverage_state
    from adoc.intake.facts import AddFact, IntakeFactsStore, NewFact

    calls: list = []
    informational = make_informational_transport("Thanks for the update.", calls)
    app, repo, _db, _calls = build_app(tmp_path, primary_transport=informational)
    save_coverage_state(repo.root / INTAKE_STATE_RELPATH, CoverageState(intake_complete=True))
    repo.commit("chore: seed intake-complete state for test")

    store = IntakeFactsStore(repo.root)
    store.apply_ops(
        [
            AddFact(
                fact=NewFact(
                    id="rash-followup",
                    section="symptoms",
                    kind="symptom",
                    statement="Rash spreading on her forearm.",
                    follow_up=True,
                )
            )
        ],
        Provenance(
            app_version="0.0.0-test",
            prompt_template_version="1",
            model_id="fake",
            dag_node="intake-agent",
            timestamp=datetime.now(UTC),
        ),
    )
    store.save()
    repo.commit("chore: seed a flagged follow-up for test")

    _seed_old_chat_entry(repo, days_ago=21)
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "Hi, checking in again."})

    assert response.status_code == 200
    body_lower = response.text.lower()
    # Jinja autoescapes apostrophes ("it&#39;s been...") -- assert on the
    # apostrophe-free tail of the sentence instead.
    assert "since we last talked" in body_lower
    assert "rash spreading on her forearm" in body_lower
    assert "thanks for the update" in body_lower  # the real reply still followed


def test_same_sitting_reply_carries_no_continuity_note(tmp_path: Path) -> None:
    """A second turn moments after the first must NOT be mistaken for a new
    visit -- the gap is under `VISIT_GAP_THRESHOLD_HOURS`."""
    calls: list = []
    informational = make_informational_transport("Got it.", calls)
    app, repo, _db, _calls = build_app(tmp_path, primary_transport=informational)
    mark_intake_complete(repo)
    _seed_old_chat_entry(repo, days_ago=0)  # a few hours ago at most in wall-clock terms
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "Following up on that."})

    assert response.status_code == 200
    assert "since we last talked" not in response.text.lower()


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


def test_informational_dosing_reply_is_withheld_not_shown(tmp_path: Path) -> None:
    """Violation 1 regression: `reason.tools.informational_llm_result`
    used to return raw model text with no `treatment_gate` call at all,
    and this route rendered it straight to the patient — the only gated
    sibling (`tools.answer_informational`) was called from no production
    code. This posts real dosing language through the actual informational
    route (classifier routes to "informational", the informational LLM
    call answers with dosing language) and asserts the patient never sees
    it, end to end."""
    calls: list = []
    dosing_text = "You should take 20 mg prednisone daily for the inflammation."
    informational = make_informational_transport(dosing_text, calls)
    app, repo, _db, _calls = build_app(tmp_path, primary_transport=informational)
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/chat/send",
        data={"text": "What should I take for my joint inflammation?"},
    )

    assert response.status_code == 200
    assert "20 mg prednisone" not in response.text
    assert "withholding" in response.text.lower()


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


def test_diagnostic_turn_never_carries_an_emergency_warning(
    tmp_path: Path,
) -> None:
    """No automated emergency screening anywhere in this app (see
    `docs/adr/0021*.md`): an ordinary post-onboarding diagnostic turn, even
    one mentioning symptoms that would once have matched the removed
    screen, carries no warning banner."""
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
    assert "heads up" not in response.text.lower()
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


def test_chat_form_shows_a_pending_state_while_a_turn_is_in_flight(tmp_path: Path) -> None:
    """A diagnostic turn runs the whole DAG — minutes of sequential model
    calls. With no indicator and no disabled Send button the page did not
    move at all while that ran, and htmx queues a repeat submit behind the
    in-flight one, so pressing Send again did nothing either. It read as
    "the Send button stopped working after intake"."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    body = client.get("/chat").text

    assert 'hx-indicator="#chat-pending"' in body
    assert 'hx-disabled-elt="find button"' in body
    assert 'id="chat-pending"' in body
    # The wait is inherent, so the copy has to say so rather than imply
    # something is about to appear any second.
    assert "few minutes" in body


def test_an_oversized_message_is_refused_before_any_model_call(tmp_path: Path) -> None:
    """Enforced on the SERVER, not just by the textarea's maxlength — that
    attribute is a convenience, and a stale page or a non-browser client can
    exceed it.

    Refused before the transcript is appended or a model is called, so an
    oversized message costs nothing and leaves no half-recorded turn.
    """
    app, repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "x" * 5000})

    assert response.status_code == 200
    assert "2,000 at a time" in response.text
    # Nothing was sent to a model, and nothing was written to the transcript.
    assert calls == []
    assert read_recent_chat(repo) == []


def test_the_refusal_does_not_blame_the_patient_or_lose_her_text(tmp_path: Path) -> None:
    """She still has what she typed in the box, and the message says what to
    do rather than what she did wrong."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "y" * 3000})

    assert "Nothing is lost" in response.text
    assert "send it in a couple of parts" in response.text


def test_a_message_one_over_the_limit_is_refused(tmp_path: Path) -> None:
    """The boundary is inclusive, so 2,001 is the first refused length.

    Tested from the refusing side deliberately: a message AT the limit is
    accepted and goes on to call a model, which this harness forbids — that
    it proceeds at all is the behaviour being relied on here.
    """
    app, _repo, _db, calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "z" * 2001})

    assert "2,000 at a time" in response.text
    assert calls == []


def test_the_input_box_carries_the_limit(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/chat")

    assert 'maxlength="2000"' in response.text


def test_assistant_markdown_reaches_the_page_as_html(tmp_path: Path) -> None:
    """The bubble macro rendered `<p>{{ entry.text }}</p>`, so a reply written
    in markdown — which is what the model emits — reached the patient with
    literal `**` around every heading and every bulleted list collapsed into
    one unbroken block. Reported from production."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)

    when = datetime.now(UTC)
    path = chat_log_path(repo, when.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": when.isoformat(),
        "role": "assistant",
        "kind": "informational",
        "text": (
            "**What I am here for**\n\n"
            "I can help you *find* what is already on file.\n\n"
            "- **Look up your labs.**\n"
            "- **Search your documents.**\n"
        ),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    client = TestClient(app)
    login(client)
    body = client.get("/chat").text

    assert "<strong>What I am here for</strong>" in body
    assert "<em>find</em>" in body
    assert "<li>" in body
    # The literal markers must not survive to the patient.
    assert "**What I am here for**" not in body
    assert "- **Look up" not in body


def test_patient_text_is_never_rendered_as_markup(tmp_path: Path) -> None:
    """Only the assistant side goes through the markdown filter. Patient text
    is whatever she typed, and must stay escaped — a message containing HTML
    is a message, not markup."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)

    when = datetime.now(UTC)
    path = chat_log_path(repo, when.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": when.isoformat(),
        "role": "patient",
        "kind": "informational",
        "text": "<script>alert(1)</script> and **not bold**",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    client = TestClient(app)
    login(client)
    body = client.get("/chat").text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert "**not bold**" in body


def _seed_entries(repo: DataRepo, count: int) -> None:
    """`count` alternating patient/assistant entries, oldest first."""
    when = datetime.now(UTC)
    path = chat_log_path(repo, when.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for i in range(count):
            fh.write(
                json.dumps(
                    {
                        "timestamp": (when + timedelta(seconds=i)).isoformat(),
                        "role": "patient" if i % 2 == 0 else "assistant",
                        "kind": "informational",
                        "text": f"entry-{i:03d}",
                    }
                )
                + "\n"
            )


def test_transcript_reads_newest_first(tmp_path: Path) -> None:
    """The composer sits at the top of the page, so the newest exchange has to
    be the thing directly beneath it — not at the bottom of an ever-growing
    scroll."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    _seed_entries(repo, 4)

    client = TestClient(app)
    login(client)
    body = client.get("/chat").text

    assert body.index("entry-003") < body.index("entry-000")


def test_transcript_paginates_at_ten_entries(tmp_path: Path) -> None:
    """Five exchanges per page — a patient message and its reply are two
    entries."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    _seed_entries(repo, 25)

    client = TestClient(app)
    login(client)
    body = client.get("/chat").text

    assert "entry-024" in body
    assert "entry-015" in body
    # The eleventh-newest belongs to page 2.
    assert "entry-014" not in body
    assert "Page 1 of 3" in body


def test_older_pages_are_read_only(tmp_path: Path) -> None:
    """Sending only makes sense against the live end of the conversation. If
    the composer stayed on an older page, htmx would prepend the reply to a
    page it does not belong to."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    _seed_entries(repo, 25)

    client = TestClient(app)
    login(client)
    body = client.get("/chat?page=2").text

    assert "entry-014" in body
    assert 'name="text"' not in body
    assert "Return to the latest" in body


def test_page_beyond_the_end_clamps_to_the_last_page(tmp_path: Path) -> None:
    """A hand-typed or stale `?page=` must not render an empty transcript."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    _seed_entries(repo, 25)

    client = TestClient(app)
    login(client)
    body = client.get("/chat?page=99").text

    assert "Page 3 of 3" in body
    assert "entry-000" in body


def _capture_transport_with(payload: dict, calls: list):
    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        assert request.schema is VisitCaptureResult
        return TransportResponse(text="", tool_input=payload, input_tokens=5, output_tokens=5)

    return transport


def _seed_open_question(repo: DataRepo, panel: str) -> str:
    qid = question_id(panel)
    save_questions(
        repo.root / QUESTIONS_RELPATH,
        OpenQuestions(
            questions=[
                OpenQuestion(
                    id=qid,
                    panel=panel,
                    ask="List every supplement and its dose.",
                    audience="you",
                    first_asked_on=date(2026, 8, 1),
                    last_asked_on=date(2026, 8, 1),
                )
            ]
        ),
    )
    return qid


def test_answering_in_chat_closes_the_open_question(tmp_path: Path) -> None:
    """The defect this store exists to fix: she answers in chat, the answer is
    captured as a fact, and the next review asks again because nothing ever
    recorded that the question was closed."""
    capture_calls: list = []
    calls: list = []
    app, repo, _db, _calls = build_app(
        tmp_path,
        primary_transport=make_primary_transport([_PE_CANT_MISS_OP], _PATIENT_REPLY, calls),
        challenger_transport=make_challenger_transport(
            counter_arguments=[], additional_ops=[], calls=calls
        ),
        visit_capture_transport=_capture_transport_with(
            {"ops": [], "answered_question_ids": ["your-supplement-labels"]}, capture_calls
        ),
    )
    mark_intake_complete(repo)
    qid = _seed_open_question(repo, "Your supplement labels")
    client = TestClient(app)
    login(client)

    client.post("/chat/send", data={"text": "I take biotin 10mg and vitamin D 2000iu daily."})

    store = load_questions(repo.root / QUESTIONS_RELPATH)
    assert store.by_id(qid).status == "answered"
    assert store.open_questions() == []


def test_the_capture_pass_is_shown_the_question_ids(tmp_path: Path) -> None:
    """It cannot report an id it was never shown — ADR 0028's standing rule.
    The first version of a gloss backfill produced zero glosses for exactly
    this reason: the prompt asked the model to check a state the pack never
    showed it."""
    capture_calls: list = []
    calls: list = []
    app, repo, _db, _calls = build_app(
        tmp_path,
        primary_transport=make_primary_transport([_PE_CANT_MISS_OP], _PATIENT_REPLY, calls),
        challenger_transport=make_challenger_transport(
            counter_arguments=[], additional_ops=[], calls=calls
        ),
        visit_capture_transport=_capture_transport_with({"ops": []}, capture_calls),
    )
    mark_intake_complete(repo)
    _seed_open_question(repo, "Your supplement labels")
    client = TestClient(app)
    login(client)

    client.post("/chat/send", data={"text": "Nothing new today."})

    sent = capture_calls[0].messages[0].content
    assert "your-supplement-labels" in sent
    assert "answered_question_ids" in sent


def test_an_invented_question_id_does_not_break_the_turn(tmp_path: Path) -> None:
    """A bad id costs itself and nothing else; the facts from the same pass
    still land and the reply still reaches her."""
    capture_calls: list = []
    calls: list = []
    app, repo, _db, _calls = build_app(
        tmp_path,
        primary_transport=make_primary_transport([_PE_CANT_MISS_OP], _PATIENT_REPLY, calls),
        challenger_transport=make_challenger_transport(
            counter_arguments=[], additional_ops=[], calls=calls
        ),
        visit_capture_transport=_capture_transport_with(
            {"ops": [], "answered_question_ids": ["not-a-real-question"]}, capture_calls
        ),
    )
    mark_intake_complete(repo)
    qid = _seed_open_question(repo, "Your supplement labels")
    client = TestClient(app)
    login(client)

    response = client.post("/chat/send", data={"text": "Hello."})

    assert response.status_code == 200
    assert load_questions(repo.root / QUESTIONS_RELPATH).by_id(qid).status == "open"


def test_transcript_endpoint_returns_the_newest_bubbles(tmp_path: Path) -> None:
    """The page polls this while a turn is in flight. A turn is slow — measured
    in production, an informational turn took 63s and a diagnostic one over ten
    minutes, against an ALB idle timeout of 60s — so the connection carrying
    the reply may well die before the reply exists."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    _seed_entries(repo, 4)

    client = TestClient(app)
    login(client)
    body = client.get("/chat/transcript").text

    assert "entry-003" in body
    assert body.index("entry-003") < body.index("entry-000")
    # The fragment only — not a whole page.
    assert "<form" not in body


def test_a_reply_is_persisted_even_if_the_response_never_arrives(tmp_path: Path) -> None:
    """The reason polling works at all. `chat_send` appends the assistant entry
    BEFORE rendering, so a turn whose connection dropped has still recorded its
    answer — the patient's reply is late, never lost, and she must never be
    told to send it again and pay for the turn twice."""
    calls: list = []
    app, repo, _db, _calls = build_app(
        tmp_path,
        primary_transport=make_primary_transport([_PE_CANT_MISS_OP], _PATIENT_REPLY, calls),
        challenger_transport=make_challenger_transport(
            counter_arguments=[], additional_ops=[], calls=calls
        ),
    )
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    client.post("/chat/send", data={"text": "My joints ache and I am exhausted."})

    # Simulate the patient's browser having given up: fetch the transcript the
    # way the poller does, with no reference to the POST that produced it.
    polled = client.get("/chat/transcript").text
    assert "chat-bubble-assistant" in polled


def test_the_transcript_endpoint_needs_a_login(tmp_path: Path) -> None:
    """It renders case-file content, so it is not a public endpoint."""
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)

    response = TestClient(app).get("/chat/transcript", follow_redirects=False)

    assert response.status_code in (302, 303, 401, 403)

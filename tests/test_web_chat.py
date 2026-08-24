"""Chat surface tests: the red-flag screen runs before any model call, and
a diagnostic turn renders the three-tier differential.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login, make_challenger_transport, make_primary_transport

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH

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
    app, _repo, _db, _calls = build_app(
        tmp_path, primary_transport=primary, challenger_transport=challenger
    )
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

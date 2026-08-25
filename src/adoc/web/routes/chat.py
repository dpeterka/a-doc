"""Chat surface (PLAN.md "Session loops (b)"; onboarding merged in per
`docs/adr/0012-initial-visit-conversation.md`). There is no automated
emergency screening anywhere in this app (see `docs/adr/0021*.md` for why).

**One chat surface.** While the initial-visit conversation is incomplete
(`intake.agent.intake_is_complete` is false), every turn — including the
very first — is routed through `intake.agent.run_intake_turn` instead of
`route_turn`/the diagnostic pipeline; once the deterministic wrap-up gate
accepts `intake_complete`, turns flow through the normal diagnostic/
informational routing below, forever after. Both paths append into the
SAME transcript (`web.casefile_helpers.append_chat_entry`/`read_recent_chat`)
so the patient experiences one continuous conversation with no visible
seam between "onboarding" and "chat." The deterministic first-visit opener
(`intake.agent.INTAKE_OPENER_MESSAGE`, a constant — never an LLM call) is
rendered into a fresh `GET /chat` when the transcript is empty and intake
is incomplete, and is written into that transcript on the first patient
turn so later renders (and the intake engine's own context) see coherent
history.

**Interval history** (`docs/adr/0013-fact-corroboration.md`): once intake is
complete, every successful informational/diagnostic turn also runs
`intake.agent.run_visit_capture` — a silent pass that may add or update
`IntakeFact`s from the patient's message, never touching the DAG or its
contracts. Skipped entirely on a withheld or error outcome (only a turn
that actually reached the patient is worth capturing from); failures
inside `run_visit_capture` itself are swallowed there, so this route never
needs to handle them.

**Post-intake continuity** (`docs/adr/0018-intake-clinical-progression-and-
continuity.md`): the first successful informational/diagnostic reply of a
new "visit" (the gap since the previous chat-transcript entry, captured
BEFORE this turn's patient message is appended, exceeds `intake.agent.
VISIT_GAP_THRESHOLD_HOURS`) is prefixed with a short, deterministic,
code-composed continuity note (`intake.agent.render_continuity_note`) —
fixed text the model cannot suppress or soften, applied after the model
has already spoken.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from sse_starlette.sse import EventSourceResponse
from starlette.responses import HTMLResponse, Response

from adoc.casefile.ledger import LedgerInvariantError
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.intake.agent import (
    INTAKE_OPENER_MESSAGE,
    VISIT_GAP_THRESHOLD_HOURS,
    build_continuity_info,
    intake_is_complete,
    render_continuity_note,
    run_intake_turn,
    run_visit_capture,
)
from adoc.intake.facts import IntakeFactsStore
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, LlmError, LlmResult
from adoc.reason.dag import ContractViolation
from adoc.reason.stages import PatientReply, route_turn, run_diagnostic_turn, run_informational_turn
from adoc.web.casefile_helpers import append_chat_entry, last_chat_at, read_recent_chat
from adoc.web.deps import get_client, get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/chat")

logger = logging.getLogger(__name__)

# A `ContractViolation` (e.g. the Composer's output failing
# `safety.treatment_gate`) can only ever fire AFTER the diagnostic DAG's
# `apply` node has already committed a ledger diff (Ledger-Maintainer ->
# Challenger -> apply -> Composer, CLAUDE.md rule 3) — so by the time this
# message is shown, the case file was already durably updated; only the
# reply text itself was withheld. This is a safety gate doing its job, not
# an application error, so it must never render as a bare 500/traceback.
_CONTRACT_VIOLATION_MESSAGE = (
    "Your case file was updated with this turn's new information, but I withheld my reply "
    "because it failed one of a-doc's built-in safety checks before it could reach you (the "
    "same deterministic guard that blocks treatment/dosing language in every response). "
    "Nothing is wrong with your account. Please try rephrasing this turn, or bring it up at "
    "your next review if it keeps happening."
)

# A `LedgerInvariantError` from the same DAG run means one of the ledger's
# own consistency rules blocked the proposed update — unlike the case
# above, nothing was committed for this turn (the invariant check runs
# before the ledger is saved), so the message does not claim otherwise.
_LEDGER_INVARIANT_MESSAGE = (
    "This turn could not be safely recorded to your case file — one of its built-in "
    "consistency checks blocked the update, so nothing was changed and no reply was "
    "generated. This is a safety guard working as intended, not an error with your account. "
    "Please try again, or mention it at your next review if it keeps happening."
)


def _continuity_note_for_new_visit(
    repo: DataRepo, *, prior_chat_at: datetime | None, now: datetime
) -> str | None:
    """The post-intake continuity note (`docs/adr/0018-intake-clinical-
    progression-and-continuity.md`) for the FIRST reply of a new visit, or
    `None` when this turn isn't one: no prior chat on file yet, or the gap
    since `prior_chat_at` (captured BEFORE this turn's patient message was
    appended — see `chat_send`) is under `VISIT_GAP_THRESHOLD_HOURS`, so a
    same-sitting back-and-forth is never mistaken for a new visit."""
    if prior_chat_at is None:
        return None
    if now - prior_chat_at < timedelta(hours=VISIT_GAP_THRESHOLD_HOURS):
        return None
    facts_store = IntakeFactsStore(repo.root)
    info = build_continuity_info(repo, facts_store, last_visit_at=prior_chat_at, now=now)
    return render_continuity_note(info, now=now)


def _wants_sse(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept


def _handle_intake_turn(
    text: str, *, client: LlmClient, repo: DataRepo, db: LabsDb
) -> dict[str, Any]:
    """One turn of the initial-visit conversation, while intake is
    incomplete. `run_intake_turn` owns fact-op application and
    coverage/wrap-up gating; this just maps its outcome onto the same
    `{kind, text, tests_to_request}` shape `_handle_turn` returns, so the
    shared `_chat_turn.html` template and transcript persistence need no
    branching on which path produced a turn."""
    outcome = run_intake_turn(client, repo, db, text)
    return {"kind": outcome.kind, "text": outcome.text, "tests_to_request": []}


def _handle_turn(text: str, *, client: LlmClient, repo: DataRepo, db: LabsDb) -> dict[str, Any]:
    """Run one chat turn. Returns a dict of template context for the
    assistant's rendered turn: `kind` (informational/diagnostic/error/
    withheld), `text`, and `tests_to_request` (diagnostic only).

    `ContractViolation` and `LedgerInvariantError` are caught alongside
    `LlmError` around every DAG-running call: both are expected,
    safety-driven outcomes of the diagnostic DAG (CLAUDE.md rules 2/3/5) —
    never a reason to let a bare 500/traceback reach the patient.
    """
    try:
        route = route_turn(client, text)
    except LlmError as exc:
        return {"kind": "error", "text": str(exc), "tests_to_request": []}

    if route.route == "informational":
        outcome: LlmResult | PatientReply
        try:
            outcome = run_informational_turn(client, repo, db, text)
        except LlmError as exc:
            return {"kind": "error", "text": str(exc), "tests_to_request": []}
        except ContractViolation as exc:
            logger.warning(
                "informational chat turn hit a ContractViolation: node=%s contract=%s",
                exc.node,
                exc.contract_name,
            )
            return {"kind": "withheld", "text": _CONTRACT_VIOLATION_MESSAGE, "tests_to_request": []}
        except LedgerInvariantError:
            logger.warning("informational chat turn hit a LedgerInvariantError")
            return {"kind": "withheld", "text": _LEDGER_INVARIANT_MESSAGE, "tests_to_request": []}
        run_visit_capture(client, repo, db, text)
        return {"kind": "informational", "text": outcome.text, "tests_to_request": []}

    try:
        outcome = run_diagnostic_turn(client, repo, db, repo.root / LEDGER_RELPATH, text)
    except LlmError as exc:
        return {"kind": "error", "text": str(exc), "tests_to_request": []}
    except ContractViolation as exc:
        # E.g. the Composer's output failing safety.treatment_gate as a DAG
        # postcondition: `apply` (which commits the ledger diff) always
        # runs before `composer` in build_diagnostic_dag, so this turn's
        # case-file update already happened.
        #
        # Log hygiene: only the structured, non-content fields (`node`,
        # `contract_name`) go to the log — never `str(exc)`/`exc.message`,
        # which `stages.treatment_gate_contract` builds from the offending
        # span of the Composer's actual patient-facing reply text.
        logger.warning(
            "diagnostic chat turn hit a ContractViolation: node=%s contract=%s",
            exc.node,
            exc.contract_name,
        )
        return {"kind": "withheld", "text": _CONTRACT_VIOLATION_MESSAGE, "tests_to_request": []}
    except LedgerInvariantError:
        logger.warning("diagnostic chat turn hit a LedgerInvariantError")
        return {"kind": "withheld", "text": _LEDGER_INVARIANT_MESSAGE, "tests_to_request": []}
    run_visit_capture(client, repo, db, text)
    return {
        "kind": "diagnostic",
        "text": outcome.tiers_rendered,
        "tests_to_request": outcome.tests_to_request,
    }


@router.get("")
def chat_page(request: Request, repo: DataRepo = Depends(get_repo)) -> Response:
    transcript = read_recent_chat(repo)
    intake_incomplete = not intake_is_complete(repo)
    if not transcript and intake_incomplete:
        # Deterministic opener, rendered but not yet persisted — it is
        # written into the real transcript on the first patient turn (see
        # chat_send) so later renders show coherent history.
        transcript = [{"role": "assistant", "kind": "informational", "text": INTAKE_OPENER_MESSAGE}]
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"transcript": transcript, "intake_incomplete": intake_incomplete},
    )


@router.post("/send")
def chat_send(
    request: Request,
    text: str = Form(...),
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
    client: LlmClient = Depends(get_client),
) -> Response:
    stripped = text.strip()
    now = datetime.now(UTC)

    if not stripped:
        turn = {"kind": "error", "text": "Please type a message first.", "tests_to_request": []}
        html = templates.get_template("_chat_turn.html").render(
            request=request, user_text=text, turn=turn
        )
        return HTMLResponse(html)

    transcript_was_empty = not read_recent_chat(repo)
    intake_incomplete = not intake_is_complete(repo)
    # Captured BEFORE this turn's patient message is appended below, so it
    # reflects the PREVIOUS visit's last entry, not this one
    # (docs/adr/0018-intake-clinical-progression-and-continuity.md).
    prior_chat_at = last_chat_at(repo)
    if transcript_was_empty and intake_incomplete:
        append_chat_entry(
            repo,
            {
                "timestamp": now.isoformat(),
                "role": "assistant",
                "kind": "informational",
                "text": INTAKE_OPENER_MESSAGE,
                "tests_to_request": [],
            },
        )

    append_chat_entry(repo, {"timestamp": now.isoformat(), "role": "patient", "text": stripped})
    turn = (
        _handle_intake_turn(stripped, client=client, repo=repo, db=db)
        if intake_incomplete
        else _handle_turn(stripped, client=client, repo=repo, db=db)
    )
    if not intake_incomplete and turn["kind"] in ("informational", "diagnostic"):
        continuity_note = _continuity_note_for_new_visit(repo, prior_chat_at=prior_chat_at, now=now)
        if continuity_note:
            turn["text"] = f"{continuity_note}\n\n{turn['text']}"
    append_chat_entry(
        repo,
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "role": "assistant",
            "kind": turn["kind"],
            "text": turn["text"],
            "tests_to_request": turn.get("tests_to_request", []),
        },
    )

    html = templates.get_template("_chat_turn.html").render(
        request=request, user_text=stripped, turn=turn
    )

    if _wants_sse(request):

        async def _event_stream() -> Any:
            yield {"event": "message", "data": html}

        return EventSourceResponse(_event_stream())

    return HTMLResponse(html)

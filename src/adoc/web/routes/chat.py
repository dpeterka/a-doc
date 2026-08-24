"""Chat surface (PLAN.md "Session loops (b)"): the red-flag screen runs
server-side, before any model call, for every turn — this module calls
`safety.red_flag_screen` itself, first, rather than relying on it only
happening inside `run_diagnostic_turn`/`run_informational_turn`, because
`route_turn` (which decides which of those two to call) is itself a model
call. On a flagged turn, `route_turn` and every runner are skipped
entirely — zero client calls, matching the red-team contract in
`tests/test_stages.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from sse_starlette.sse import EventSourceResponse
from starlette.responses import HTMLResponse, Response

from adoc.casefile.ledger import LedgerInvariantError
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, LlmError, LlmResult
from adoc.reason.dag import ContractViolation
from adoc.reason.safety import RedFlagResult, red_flag_screen
from adoc.reason.stages import PatientReply, route_turn, run_diagnostic_turn, run_informational_turn
from adoc.web.casefile_helpers import append_chat_entry, read_recent_chat
from adoc.web.deps import get_client, get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/chat")

logger = logging.getLogger(__name__)

# S5 remediation: a `ContractViolation` (e.g. the Composer's output failing
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


def _wants_sse(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept


def _handle_turn(text: str, *, client: LlmClient, repo: DataRepo, db: LabsDb) -> dict[str, Any]:
    """Run one chat turn. Returns a dict of template context for the
    assistant's rendered turn: `kind`
    (urgent/informational/diagnostic/error/withheld), `text`, and
    `tests_to_request` (diagnostic only).

    `ContractViolation` and `LedgerInvariantError` are caught alongside
    `LlmError` around every DAG-running call (S5 remediation): both are
    expected, safety-driven outcomes of the diagnostic DAG (CLAUDE.md rules
    2/3/5) — never a reason to let a bare 500/traceback reach the patient.
    """
    screen = red_flag_screen(text)
    if screen.flagged:
        return {"kind": "urgent", "text": screen.message or "", "tests_to_request": []}

    try:
        route = route_turn(client, text)
    except LlmError as exc:
        return {"kind": "error", "text": str(exc), "tests_to_request": []}

    if route.route == "informational":
        outcome: LlmResult | RedFlagResult | PatientReply
        try:
            outcome = run_informational_turn(client, repo, db, text)
        except LlmError as exc:
            return {"kind": "error", "text": str(exc), "tests_to_request": []}
        except ContractViolation as exc:
            logger.warning("informational chat turn hit a ContractViolation: %s", exc)
            return {"kind": "withheld", "text": _CONTRACT_VIOLATION_MESSAGE, "tests_to_request": []}
        except LedgerInvariantError as exc:
            logger.warning("informational chat turn hit a LedgerInvariantError: %s", exc)
            return {"kind": "withheld", "text": _LEDGER_INVARIANT_MESSAGE, "tests_to_request": []}
        if isinstance(outcome, RedFlagResult):
            return {"kind": "urgent", "text": outcome.message or "", "tests_to_request": []}
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
        logger.warning("diagnostic chat turn hit a ContractViolation: %s", exc)
        return {"kind": "withheld", "text": _CONTRACT_VIOLATION_MESSAGE, "tests_to_request": []}
    except LedgerInvariantError as exc:
        logger.warning("diagnostic chat turn hit a LedgerInvariantError: %s", exc)
        return {"kind": "withheld", "text": _LEDGER_INVARIANT_MESSAGE, "tests_to_request": []}
    if isinstance(outcome, RedFlagResult):
        return {"kind": "urgent", "text": outcome.message or "", "tests_to_request": []}
    return {
        "kind": "diagnostic",
        "text": outcome.tiers_rendered,
        "tests_to_request": outcome.tests_to_request,
    }


@router.get("")
def chat_page(request: Request, repo: DataRepo = Depends(get_repo)) -> Response:
    transcript = read_recent_chat(repo)
    return templates.TemplateResponse(request, "chat.html", {"transcript": transcript})


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

    append_chat_entry(repo, {"timestamp": now.isoformat(), "role": "patient", "text": stripped})
    turn = _handle_turn(stripped, client=client, repo=repo, db=db)
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

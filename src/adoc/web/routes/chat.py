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

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from sse_starlette.sse import EventSourceResponse
from starlette.responses import HTMLResponse, Response

from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, LlmError, LlmResult
from adoc.reason.safety import RedFlagResult, red_flag_screen
from adoc.reason.stages import PatientReply, route_turn, run_diagnostic_turn, run_informational_turn
from adoc.web.casefile_helpers import append_chat_entry, read_recent_chat
from adoc.web.deps import get_client, get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/chat")


def _wants_sse(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept


def _handle_turn(text: str, *, client: LlmClient, repo: DataRepo, db: LabsDb) -> dict[str, Any]:
    """Run one chat turn. Returns a dict of template context for the
    assistant's rendered turn: `kind` (urgent/informational/diagnostic/error),
    `text`, and `tests_to_request` (diagnostic only)."""
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
        if isinstance(outcome, RedFlagResult):
            return {"kind": "urgent", "text": outcome.message or "", "tests_to_request": []}
        return {"kind": "informational", "text": outcome.text, "tests_to_request": []}

    try:
        outcome = run_diagnostic_turn(client, repo, db, repo.root / LEDGER_RELPATH, text)
    except LlmError as exc:
        return {"kind": "error", "text": str(exc), "tests_to_request": []}
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

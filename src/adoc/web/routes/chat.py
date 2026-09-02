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
from adoc.config import Settings
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
from adoc.reason.progress import TRACKER
from adoc.reason.stages import PatientReply, route_turn, run_diagnostic_turn, run_informational_turn
from adoc.web.casefile_helpers import append_chat_entry, last_chat_at, read_recent_chat
from adoc.web.deps import get_client, get_db, get_repo, get_settings
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


# Five exchanges — a patient message and its reply are two entries. The whole
# transcript used to render on one page, which grew without bound and put the
# newest reply furthest from the composer.
CHAT_PAGE_SIZE = 10

# The chat page paginates, so it must see more than the dashboard's "recent"
# window — otherwise page 2 would fall off the end of what was ever loaded.
_CHAT_HISTORY_FILES = 3650
_CHAT_HISTORY_TURNS = 100_000


@router.get("")
def chat_page(
    request: Request,
    page: int = 1,
    ask: str = "",
    repo: DataRepo = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> Response:
    """The chat page. `ask` PRE-FILLS the composer and never sends (ADR 0045).

    The "explain this" links on the review and the case file arrive here with
    a question already written. They stop at pre-filling on purpose: a
    diagnostic turn runs the whole DAG, `apply` commits the ledger diff
    before the composer speaks, and the wait is minutes. A link that did all
    that from one click would be a mutation disguised as navigation — and
    the question is what decides the answer, so she should see it and be able
    to change it.
    """
    transcript = read_recent_chat(
        repo, max_files=_CHAT_HISTORY_FILES, max_turns=_CHAT_HISTORY_TURNS
    )
    intake_incomplete = not intake_is_complete(repo)
    if not transcript and intake_incomplete:
        # Deterministic opener, rendered but not yet persisted — it is
        # written into the real transcript on the first patient turn (see
        # chat_send) so later renders show coherent history.
        transcript = [{"role": "assistant", "kind": "informational", "text": INTAKE_OPENER_MESSAGE}]

    # Newest first: the composer sits at the top of the page, so the reply she
    # just read should be the thing directly beneath it.
    newest_first = list(reversed(transcript))
    page_count = max(1, -(-len(newest_first) // CHAT_PAGE_SIZE))
    page = min(max(page, 1), page_count)
    start = (page - 1) * CHAT_PAGE_SIZE

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "transcript": newest_first[start : start + CHAT_PAGE_SIZE],
            "intake_incomplete": intake_incomplete,
            "max_message_chars": settings.max_message_chars,
            # Truncated to the same limit `chat_send` enforces, so a seeded
            # question can never arrive pre-rejected. A URL is user input
            # like any other.
            "seeded_ask": " ".join(ask.split())[: settings.max_message_chars],
            "page": page,
            "page_count": page_count,
            # Sending only makes sense against the live end of the
            # conversation; an older page is a read-only view of history.
            "is_latest_page": page == 1,
        },
    )


@router.get("/progress")
def chat_progress(request: Request) -> Response:
    """The in-flight turn's stage, as an HTML fragment (ADR 0046).

    Polled by the waiting page every two seconds while the composer is
    disabled. Returns the honest "a few minutes" line plus which of the four
    stages is running — `chat.html` already told her the wait was long; this
    tells her it is moving.

    Publishes stage LABELS only, never any part of the turn, so a poll can
    carry no patient content even if the response were cached or logged.
    """
    progress = TRACKER.read()
    html = templates.get_template("_chat_progress.html").render(request=request, progress=progress)
    return HTMLResponse(html)


@router.get("/transcript")
def chat_transcript(
    request: Request,
    repo: DataRepo = Depends(get_repo),
) -> Response:
    """The newest page of the transcript, on its own.

    The page polls this while a turn is in flight. A turn is slow -- measured
    in production, an informational turn took 63s and a diagnostic one over
    ten minutes -- and anything between the browser and the app may give up on
    a request held open that long. The ALB did exactly that at its 60s
    default: the work finished and `chat_send` had already appended the reply
    to the transcript, but the response had nowhere to go, so the patient saw
    nothing and reloaded to find out whether anything had happened.

    The reply is persisted before it is rendered, so it is always here even
    when the POST never came back. Polling this turns a dropped connection
    into a few seconds of delay instead of a lost answer.
    """
    transcript = read_recent_chat(
        repo, max_files=_CHAT_HISTORY_FILES, max_turns=_CHAT_HISTORY_TURNS
    )
    if not transcript and not intake_is_complete(repo):
        transcript = [{"role": "assistant", "kind": "informational", "text": INTAKE_OPENER_MESSAGE}]
    newest_first = list(reversed(transcript))
    return templates.TemplateResponse(
        request,
        "_chat_transcript.html",
        {"transcript": newest_first[:CHAT_PAGE_SIZE]},
    )


@router.post("/send")
def chat_send(
    request: Request,
    text: str = Form(...),
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
    client: LlmClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    stripped = text.strip()
    now = datetime.now(UTC)

    if not stripped:
        turn = {"kind": "error", "text": "Please type a message first.", "tests_to_request": []}
        html = templates.get_template("_chat_turn.html").render(
            request=request, user_text=text, turn=turn
        )
        return HTMLResponse(html)

    # Enforced here as well as by the textarea's `maxlength`: that attribute is
    # a convenience, not a control — a stale page, a paste, or anything that is
    # not the browser can exceed it. Rejected BEFORE the transcript is appended
    # or any model is called, so an oversized message costs nothing and leaves
    # no half-recorded turn.
    limit = settings.max_message_chars
    if len(stripped) > limit:
        turn = {
            "kind": "error",
            "text": (
                f"That message is {len(stripped):,} characters and I can take "
                f"{limit:,} at a time. Nothing is lost — send it in a couple of "
                "parts and I will record them just the same. Shorter messages "
                "also get filed more accurately."
            ),
            "tests_to_request": [],
        }
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

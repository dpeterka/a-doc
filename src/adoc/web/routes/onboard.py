"""Onboarding surface (`docs/adr/0011-conversational-agentic-onboarding.md`):
a chat surface driven entirely by `intake.agent.run_intake_turn` — this
module never re-implements fact extraction, completion-gating, or
persistence, only HTTP plumbing and rendering around the engine's existing
public API (mirroring `web.routes.chat`'s form-POST pattern; no SSE
needed).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.intake.agent import read_intake_transcript, run_intake_turn
from adoc.intake.facts import IntakeFactsStore, section_completion_blockers
from adoc.intake.sections import SECTIONS
from adoc.intake.wizard import INTAKE_STATE_RELPATH, SectionState, load_intake_state
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient
from adoc.web.deps import get_client, get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/onboard")


def _section_rows(
    repo: DataRepo, facts_store: IntakeFactsStore, current_key: str | None
) -> list[dict[str, Any]]:
    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    rows: list[dict[str, Any]] = []
    for spec in SECTIONS:
        section_state = state.sections.get(spec.key, SectionState())
        open_items = len(section_completion_blockers(facts_store.facts, spec.key))
        rows.append(
            {
                "key": spec.key,
                "title": spec.title,
                "status": section_state.status,
                "is_current": spec.key == current_key,
                "open_items": open_items,
            }
        )
    return rows


def _panel_context(repo: DataRepo, *, error: str | None = None) -> dict[str, Any]:
    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    facts_store = IntakeFactsStore(repo.root)
    completed = sum(
        1 for section_state in state.sections.values() if section_state.status == "complete"
    )
    return {
        "transcript": read_intake_transcript(repo),
        "sections": _section_rows(repo, facts_store, state.cursor),
        "current_key": state.cursor,
        "progress": (completed, len(SECTIONS)),
        "baseline_incomplete": state.cursor is not None,
        "error": error,
    }


@router.get("")
def onboard_page(request: Request, repo: DataRepo = Depends(get_repo)) -> Response:
    return templates.TemplateResponse(request, "onboard.html", _panel_context(repo))


@router.post("/send")
def onboard_send(
    request: Request,
    text: str = Form(...),
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
    client: LlmClient = Depends(get_client),
) -> Response:
    stripped = text.strip()
    error: str | None = None
    if not stripped:
        error = "Please write something before sending."
    else:
        run_intake_turn(client, repo, db, stripped)

    return templates.TemplateResponse(
        request, "_onboard_panel.html", _panel_context(repo, error=error)
    )


@router.get("/review")
def onboard_review(request: Request, repo: DataRepo = Depends(get_repo)) -> Response:
    facts_store = IntakeFactsStore(repo.root)
    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)

    sections: dict[str, dict[str, Any]] = {}
    for spec in SECTIONS:
        section_facts = [f for f in facts_store.facts if f.section == spec.key]
        sections[spec.key] = {
            "title": spec.title,
            "active": [f for f in section_facts if f.status == "active"],
            "retracted": [f for f in section_facts if f.status == "retracted"],
        }

    return templates.TemplateResponse(
        request,
        "onboard_review.html",
        {"sections": sections, "baseline_incomplete": state.cursor is not None},
    )

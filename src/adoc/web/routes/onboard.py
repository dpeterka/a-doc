"""Onboarding surface (PLAN.md "Onboarding & end-user experience"): a web
wizard driven entirely by `intake.wizard.IntakeWizard` — this module never
re-implements section logic, merge semantics, or persistence, only HTTP
plumbing and rendering around the wizard's existing public API.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.config import Settings
from adoc.intake.sections import SECTIONS
from adoc.intake.wizard import (
    INTAKE_STATE_RELPATH,
    IntakeError,
    IntakeWizard,
    SectionState,
    load_intake_state,
)
from adoc.reason.client import LlmClient, LlmError
from adoc.web.deps import get_client, get_repo, get_settings
from adoc.web.templating import templates

router = APIRouter(prefix="/onboard")


def _build_wizard(repo: DataRepo, client: LlmClient, settings: Settings) -> IntakeWizard:
    return IntakeWizard(repo, client, dropbox_folder=settings.dropbox_folder)


def _section_rows(repo: DataRepo, current_key: str | None) -> list[dict[str, Any]]:
    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    rows: list[dict[str, Any]] = []
    for spec in SECTIONS:
        section_state = state.sections.get(spec.key, SectionState())
        rows.append(
            {
                "key": spec.key,
                "title": spec.title,
                "status": section_state.status,
                "is_current": spec.key == current_key,
            }
        )
    return rows


def _panel_context(wizard: IntakeWizard, repo: DataRepo, *, error: str | None = None) -> dict:
    spec = wizard.current_section()
    return {
        "prompt_text": wizard.prompt_for_current(),
        "status": wizard.current_status(),
        "section_key": spec.key if spec is not None else None,
        "progress": wizard.progress(),
        "sections": _section_rows(repo, spec.key if spec is not None else None),
        "error": error,
    }


@router.get("")
def onboard_page(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    client: LlmClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    wizard = _build_wizard(repo, client, settings)
    return templates.TemplateResponse(request, "onboard.html", _panel_context(wizard, repo))


@router.post("/submit")
def onboard_submit(
    request: Request,
    text: str = Form(...),
    repo: DataRepo = Depends(get_repo),
    client: LlmClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    wizard = _build_wizard(repo, client, settings)
    error: str | None = None
    if not text.strip():
        error = "Please write something before submitting."
    else:
        try:
            wizard.submit(text)
        except (LlmError, IntakeError) as exc:
            error = f"Sorry, I couldn't process that: {exc}"

    return templates.TemplateResponse(
        request, "_onboard_panel.html", _panel_context(wizard, repo, error=error)
    )


@router.post("/confirm")
def onboard_confirm(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    client: LlmClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    wizard = _build_wizard(repo, client, settings)
    error: str | None = None
    try:
        wizard.confirm()
    except IntakeError as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request, "_onboard_panel.html", _panel_context(wizard, repo, error=error)
    )


@router.get("/section/{key}")
def onboard_reopen(
    request: Request,
    key: str,
    repo: DataRepo = Depends(get_repo),
    client: LlmClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    wizard = _build_wizard(repo, client, settings)
    error: str | None = None
    try:
        wizard.reopen(key)
    except KeyError:
        error = f"No such section: {key!r}"

    return templates.TemplateResponse(
        request, "onboard.html", _panel_context(wizard, repo, error=error)
    )

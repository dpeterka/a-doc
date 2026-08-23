"""Login/logout: the one unauthenticated surface (PLAN.md "UI" auth design)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from adoc.config import Settings
from adoc.web.deps import get_settings
from adoc.web.security import (
    check_passphrase,
    clear_session_cookie,
    passphrase_from_settings,
    set_session_cookie,
)
from adoc.web.templating import templates

router = APIRouter()


@router.get("/login")
def login_form(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    passphrase: str = Form(...),
    settings: Settings = Depends(get_settings),
) -> Response:
    expected = passphrase_from_settings(settings)
    ok = expected is not None and check_passphrase(passphrase, expected)
    if not ok:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That passphrase didn't match. Please try again."},
            status_code=401,
        )

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, request.app.state.session_secret)
    return response


@router.post("/logout")
def logout(request: Request) -> Response:  # noqa: ARG001 - request kept for symmetry/future use
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response

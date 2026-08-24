"""Login/logout + health check: the unauthenticated surfaces
(PLAN.md "UI" auth design; README "patient access").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from adoc.config import Settings
from adoc.web.deps import get_settings
from adoc.web.security import (
    clear_session_cookie,
    client_ip,
    resolve_secure_cookie_flag,
    set_session_cookie,
)
from adoc.web.templating import templates
from adoc.web.users import USERS_RELPATH, get_fingerprint, verify_user

router = APIRouter()

_LOCKOUT_MESSAGE = "Too many failed sign-in attempts. Please wait a few minutes and try again."
_INVALID_CREDENTIALS_MESSAGE = "Invalid username or password."


@router.get("/healthz")
def healthz() -> Response:
    """Unauthenticated target for the ALB's target-group health check
    (`deploy/cfn/alb.yaml`'s TargetGroup)."""
    return PlainTextResponse("ok")


@router.get("/login")
def login_form(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings),
) -> Response:
    limiter = request.app.state.login_rate_limiter
    ip = client_ip(request, trust_forwarded_for=settings.trust_forwarded_for)

    if limiter.is_locked(username=username, ip=ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": _LOCKOUT_MESSAGE},
            status_code=429,
        )

    users_path = settings.data_dir / USERS_RELPATH
    if not verify_user(users_path, username, password):
        limiter.record_failure(username=username, ip=ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": _INVALID_CREDENTIALS_MESSAGE},
            status_code=401,
        )

    limiter.clear(username=username, ip=ip)
    response = RedirectResponse(url="/", status_code=303)
    secure = resolve_secure_cookie_flag(request, trust_forwarded_for=settings.trust_forwarded_for)
    fingerprint = get_fingerprint(users_path, username)
    if fingerprint is None:  # pragma: no cover - verify_user just confirmed this record exists
        raise RuntimeError(f"user store has no record for {username!r} immediately after login")
    set_session_cookie(
        response,
        request.app.state.session_secret,
        username=username,
        fingerprint=fingerprint,
        secure=secure,
    )
    return response


@router.post("/logout")
def logout(request: Request) -> Response:  # noqa: ARG001 - request kept for symmetry/future use
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response

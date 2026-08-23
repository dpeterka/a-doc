"""Single-user session auth: signed cookie over a stdlib HMAC.

`itsdangerous` is deliberately not a dependency (task constraint) — a
session token is `<issued_at>.<hex hmac-sha256 signature>`, verified with
`hmac.compare_digest` (constant-time). The signing secret is 32 random
bytes persisted at `<data_dir>/work/session-secret` (created on first use,
never committed — `work/` is gitignored per `casefile.repo`).

The passphrase check itself (`check_passphrase`) is the other constant-time
comparison this module provides: the login form's submitted passphrase is
compared against `Settings.session_passphrase` via `hmac.compare_digest`,
never `==`, so response timing cannot leak how many leading characters
matched.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from adoc.casefile.repo import DataRepo
from adoc.config import Settings

SESSION_COOKIE_NAME = "adoc_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days
LOGIN_PATH = "/login"
_PUBLIC_PREFIXES = ("/static/",)
_SESSION_SECRET_RELPATH = Path("work") / "session-secret"


def load_or_create_session_secret(repo: DataRepo) -> bytes:
    """Load the persisted 32-byte session-signing secret, creating it on
    first use. Stored hex-encoded at `<data_dir>/work/session-secret`."""
    path = repo.root / _SESSION_SECRET_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return bytes.fromhex(existing)
    secret = secrets.token_bytes(32)
    path.write_text(secret.hex(), encoding="utf-8")
    return secret


def make_session_token(secret: bytes, *, issued_at: int | None = None) -> str:
    """Build a signed session token: `<issued_at>.<hmac-sha256 hex digest>`."""
    issued = issued_at if issued_at is not None else int(time.time())
    issued_str = str(issued)
    signature = hmac.new(secret, issued_str.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{issued_str}.{signature}"


def verify_session_token(
    secret: bytes, token: str, *, max_age_seconds: int = SESSION_MAX_AGE_SECONDS
) -> bool:
    """Verify a session token's signature (constant-time) and freshness."""
    issued_str, _, signature = token.partition(".")
    if not issued_str or not signature:
        return False
    expected = hmac.new(secret, issued_str.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        issued = int(issued_str)
    except ValueError:
        return False
    return 0 <= (time.time() - issued) < max_age_seconds


def check_passphrase(submitted: str, expected: str) -> bool:
    """Constant-time compare of a submitted login passphrase against the
    configured `Settings.session_passphrase`."""
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    secret: bytes = request.app.state.session_secret
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    return verify_session_token(secret, token)


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Redirects every unauthenticated request to `/login`, except the
    login route itself and static assets. HTMX requests (`HX-Request:
    true`) get an `HX-Redirect` header instead of a body redirect, so a
    partial-swap request still navigates the whole page to `/login`.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path == LOGIN_PATH or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        if is_authenticated(request):
            return await call_next(request)

        if request.headers.get("HX-Request") == "true":
            response = Response(status_code=200)
            response.headers["HX-Redirect"] = LOGIN_PATH
            return response
        return RedirectResponse(url=LOGIN_PATH, status_code=303)


def set_session_cookie(response: Response, secret: bytes) -> None:
    token = make_session_token(secret)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)


def passphrase_from_settings(settings: Settings) -> str | None:
    if settings.session_passphrase is None:
        return None
    return settings.session_passphrase.get_secret_value()

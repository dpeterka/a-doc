"""Single-user-per-session-cookie auth: signed cookie over a stdlib HMAC,
plus in-app login rate limiting.

`itsdangerous` is deliberately not a dependency (task constraint) — a
session token is `<issued_at>.<hex hmac-sha256 signature>`, verified with
`hmac.compare_digest` (constant-time). The signing secret is 32 random
bytes persisted at `<data_dir>/work/session-secret` (created on first use,
never committed — `work/` is gitignored per `casefile.repo`).

Login itself (username/password) is verified by `web.users.verify_user`.
This module owns everything *around* that check: the rate limiter that
guards it, the client-IP heuristic the limiter keys on, and the
session-cookie mechanics that follow a successful login.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from adoc.casefile.repo import DataRepo

SESSION_COOKIE_NAME = "adoc_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days
LOGIN_PATH = "/login"
HEALTHZ_PATH = "/healthz"
_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_PATHS = (LOGIN_PATH, HEALTHZ_PATH)
_SESSION_SECRET_RELPATH = Path("work") / "session-secret"

# Login lockout thresholds (PLAN.md/README "patient access"): a username
# under sustained attack is locked out sooner than a shared IP, since a
# single IP (e.g. a household NAT, or in production the ALB itself for
# malformed X-Forwarded-For cases) legitimately produces more noise.
USERNAME_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 20
LOCKOUT_WINDOW_SECONDS = 15 * 60


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


def is_authenticated(request: Request) -> bool:
    secret: bytes = request.app.state.session_secret
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    return verify_session_token(secret, token)


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Redirects every unauthenticated request to `/login`, except the
    login route, the unauthenticated `/healthz` (ALB health check target),
    and static assets. HTMX requests (`HX-Request: true`) get an
    `HX-Redirect` header instead of a body redirect, so a partial-swap
    request still navigates the whole page to `/login`.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        if is_authenticated(request):
            return await call_next(request)

        if request.headers.get("HX-Request") == "true":
            response = Response(status_code=200)
            response.headers["HX-Redirect"] = LOGIN_PATH
            return response
        return RedirectResponse(url=LOGIN_PATH, status_code=303)


def set_session_cookie(response: Response, secret: bytes, *, secure: bool) -> None:
    """`secure` should be true whenever the request arrived over HTTPS.
    Callers derive that from `X-Forwarded-Proto` (set by the ALB) rather
    than `request.url.scheme`, since uvicorn itself only ever sees plain
    HTTP behind the load balancer."""
    token = make_session_token(secret)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_MAX_AGE_SECONDS,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)


def client_ip(request: Request, *, trust_forwarded_for: bool) -> str:
    """Best-effort client IP for rate-limiting purposes.

    `trust_forwarded_for` must only be true when every request that can
    reach this process has passed through the ALB — true in the deployed
    app (`deploy/cfn/ecs.yaml`'s ServiceSecurityGroup admits inbound
    8080 from the ALB security group only, so nothing else can set this
    header), false by default (`Settings.trust_forwarded_for`) so a local
    `adoc serve` or a test run never trusts a client-supplied header.

    When trusted, the *last* hop of `X-Forwarded-For` is used — that's the
    address the ALB itself appended, which cannot be spoofed by the
    original client (earlier hops can be arbitrary attacker-supplied
    values).
    """
    if trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            last_hop = forwarded.split(",")[-1].strip()
            if last_hop:
                return last_hop
    client = request.client
    return client.host if client is not None else "unknown"


class LoginRateLimiter:
    """In-memory, per-process login failure tracker (PLAN.md/README
    "patient access"): a sliding 15-minute window per username and per
    client IP. Deliberately not persisted — a process restart resets every
    counter, which is an accepted tradeoff for a single-patient app with
    no multi-instance deployment (documented in README).

    Not thread-safe against true concurrent access, but uvicorn's default
    single-worker/async-event-loop model never calls this from two threads
    at once here.
    """

    def __init__(
        self,
        *,
        username_limit: int = USERNAME_FAILURE_LIMIT,
        ip_limit: int = IP_FAILURE_LIMIT,
        window_seconds: float = LOCKOUT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._username_limit = username_limit
        self._ip_limit = ip_limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._username_failures: dict[str, list[float]] = {}
        self._ip_failures: dict[str, list[float]] = {}

    def _now(self) -> float:
        return self._clock()

    def _prune(self, failures: list[float], now: float) -> list[float]:
        cutoff = now - self._window_seconds
        return [t for t in failures if t > cutoff]

    def is_locked(self, *, username: str, ip: str) -> bool:
        now = self._now()
        username_failures = self._prune(self._username_failures.get(username, []), now)
        ip_failures = self._prune(self._ip_failures.get(ip, []), now)
        self._username_failures[username] = username_failures
        self._ip_failures[ip] = ip_failures
        return len(username_failures) >= self._username_limit or len(ip_failures) >= self._ip_limit

    def record_failure(self, *, username: str, ip: str) -> None:
        now = self._now()
        self._username_failures.setdefault(username, []).append(now)
        self._ip_failures.setdefault(ip, []).append(now)

    def clear(self, *, username: str, ip: str) -> None:
        self._username_failures.pop(username, None)
        self._ip_failures.pop(ip, None)

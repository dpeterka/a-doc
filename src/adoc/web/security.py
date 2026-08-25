"""Single-user-per-session-cookie auth: signed cookie over a stdlib HMAC,
plus in-app login rate limiting.

`itsdangerous` is deliberately not a dependency (task constraint) — a
session token is `<issued_at>.<username>.<fingerprint>.<hex hmac-sha256
signature>`, verified with `hmac.compare_digest` (constant-time). The
signing secret is 32 random bytes persisted at
`<data_dir>/work/session-secret` (created on first use, never committed —
`work/` is gitignored per `casefile.repo`).

Session binding (identity, not just time): the token carries the username
and a fingerprint of that user's *current* credential record at issuance
(`web.users.get_fingerprint` — `sha256(salt || hash)[:16]`), and the HMAC
signs all three fields together so neither can be forged without the
secret. On every request, `is_authenticated` re-derives the *current*
fingerprint from the user store (`work/users.yaml`) and rejects the
session if the user no longer exists or the fingerprint has changed (a
password reset rewrites salt+hash). This closes the gap where a 30-day
cookie signed only over `issued_at` would keep working after the user was
removed or their password changed — sessions of a removed/changed user die
on their very next request, not after 30 days. The user store is tiny
(single-digit users), so re-reading it per request is cheap; `_UserStoreCache`
still avoids re-parsing the YAML when the file's mtime hasn't changed.

Login itself (username/password) is verified by `web.users.verify_user`.
This module owns everything *around* that check: the rate limiter that
guards it, the client-IP heuristic the limiter keys on, and the
session-cookie mechanics that follow a successful login.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from adoc.casefile.repo import DataRepo
from adoc.web import users as users_module
from adoc.web.users import USERS_RELPATH

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


def _signing_payload(issued_str: str, username: str, fingerprint: str) -> bytes:
    return f"{issued_str}.{username}.{fingerprint}".encode()


def make_session_token(
    secret: bytes, *, username: str, fingerprint: str, issued_at: int | None = None
) -> str:
    """Build a signed session token:
    `<issued_at>.<username>.<fingerprint>.<hmac-sha256 hex digest>`.

    The HMAC covers `issued_at`, `username`, and `fingerprint` together, so
    none of the three can be altered without invalidating the signature.
    """
    issued = issued_at if issued_at is not None else int(time.time())
    issued_str = str(issued)
    signature = hmac.new(
        secret, _signing_payload(issued_str, username, fingerprint), hashlib.sha256
    ).hexdigest()
    return f"{issued_str}.{username}.{fingerprint}.{signature}"


def _parse_session_token(token: str) -> tuple[str, str, str, str] | None:
    """Split a token into `(issued_str, username, fingerprint, signature)`.

    Usernames are operator-chosen (`adoc user add`) and may themselves
    contain `.`, so parsing anchors on the fixed-position fields instead of
    a plain `split(".")`: `issued_at` is always first, the hmac signature
    and the 16-hex-char fingerprint are always last, and everything in
    between is the username.
    """
    parts = token.split(".")
    if len(parts) < 4:
        return None
    issued_str = parts[0]
    signature = parts[-1]
    fingerprint = parts[-2]
    username = ".".join(parts[1:-2])
    if not issued_str or not username or not fingerprint or not signature:
        return None
    return issued_str, username, fingerprint, signature


def verify_session_token(
    secret: bytes,
    token: str,
    *,
    fingerprint_lookup: Callable[[str], str | None],
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> bool:
    """Verify a session token's signature (constant-time), freshness, and
    that its embedded fingerprint still matches `fingerprint_lookup`'s
    *current* answer for that username — the identity-binding check (see
    module docstring). `fingerprint_lookup` returns `None` for a user who
    no longer exists.
    """
    parsed = _parse_session_token(token)
    if parsed is None:
        return False
    issued_str, username, fingerprint, signature = parsed

    expected_signature = hmac.new(
        secret, _signing_payload(issued_str, username, fingerprint), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return False

    try:
        issued = int(issued_str)
    except ValueError:
        return False
    if not (0 <= (time.time() - issued) < max_age_seconds):
        return False

    current_fingerprint = fingerprint_lookup(username)
    if current_fingerprint is None:
        return False
    return hmac.compare_digest(fingerprint, current_fingerprint)


class _UserStoreCache:
    """Per-process, mtime-gated cache of `work/users.yaml`'s fingerprints.

    The store is small (single-digit users) so re-parsing it on every
    request would already be fine; this just avoids doing that when
    nothing has changed. Never serves stale data across a real edit: a
    write from `adoc user add/remove` rewrites the file, which bumps its
    mtime and forces a reload on the next lookup.

    `is_authenticated` runs on every request and is driven from FastAPI's
    sync-route thread pool, so `self._cache` (a plain dict) is genuinely
    read and written from multiple threads at once - `self._lock` (a
    `threading.Lock`, non-reentrant is fine: no method here calls another)
    guards every read/write of it so a stat-then-read/then-write cache
    fill can never race another thread's fill of the same entry. This is
    a narrower race than `web.users`'s shared-`YAML()` bug (that one could
    corrupt ruamel's parser state and raise `DuplicateKeyError`; this one
    would "only" mean redundant reloads or a torn cache entry), but the
    fix is the same shape: hold a lock around the shared mutable state.
    The `users_module.load_fingerprints(path)` call itself is deliberately
    OUTSIDE the lock - it does its own file I/O and YAML parsing (each
    call gets its own `YAML()` instance, see `web.users._new_yaml`), so
    there's no need to serialize that part, only the cache dict itself.
    """

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def fingerprints(self, path: Path) -> dict[str, str]:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            with self._lock:
                self._cache.pop(path, None)
            return {}
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        fingerprints = users_module.load_fingerprints(path)
        with self._lock:
            self._cache[path] = (mtime, fingerprints)
        return fingerprints


_user_store_cache = _UserStoreCache()


def is_authenticated(request: Request) -> bool:
    secret: bytes = request.app.state.session_secret
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    users_path = request.app.state.settings.data_dir / USERS_RELPATH
    return verify_session_token(
        secret, token, fingerprint_lookup=_user_store_cache.fingerprints(users_path).get
    )


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


def set_session_cookie(
    response: Response, secret: bytes, *, username: str, fingerprint: str, secure: bool
) -> None:
    """`secure` should be true whenever the request arrived over HTTPS.
    Callers derive that from `resolve_secure_cookie_flag` (`X-Forwarded-Proto`,
    set by the ALB) rather than `request.url.scheme` directly, since
    uvicorn itself only ever sees plain HTTP behind the load balancer."""
    token = make_session_token(secret, username=username, fingerprint=fingerprint)
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


def resolve_secure_cookie_flag(request: Request, *, trust_forwarded_for: bool) -> bool:
    """Whether the login session cookie should carry the `Secure` flag.

    `X-Forwarded-Proto` is attacker-controllable on any request that
    doesn't actually pass through the ALB, so it is trusted only under the
    same condition `client_ip` trusts `X-Forwarded-For` under:
    `trust_forwarded_for` is true only when every request that can reach
    this process has already passed through the ALB (see that flag's
    docstring below). When untrusted (local dev, tests), fall back to the
    request's own scheme — correct locally, and never lets a spoofed
    header force a `Secure` cookie onto a plain-HTTP connection.
    """
    if trust_forwarded_for:
        return request.headers.get("x-forwarded-proto") == "https"
    return request.url.scheme == "https"


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

    Thread-safe against true concurrent access via `self._lock` (a plain
    `threading.Lock` — no method here calls another, so no reentrancy is
    needed). A previous version of this docstring claimed "uvicorn's
    default single-worker/async-event-loop model never calls this from two
    threads at once" — that was FALSE: `web.routes.auth`'s `login_submit`
    is a sync `def` route, so Starlette runs it in its sync-route thread
    pool, not on the event loop directly, and concurrent login attempts
    (a credential-stuffing burst is exactly the scenario this class exists
    to defend against) really do call `is_locked`/`record_failure` from
    multiple threads at once. An unlocked check-then-act read-modify-write
    over `_username_failures`/`_ip_failures` can under-count concurrent
    failures — e.g. two threads both read the same pre-append list before
    either appends, so one failure is silently lost from the count —
    weakening the only brute-force control on a public login surface with
    no WAF, no VPN, and no TOTP (ADR 0007). The lockout policy itself
    (limits, window) is unchanged; only the counting is now correct under
    concurrency.
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
        self._lock = threading.Lock()

    def _now(self) -> float:
        return self._clock()

    def _prune(self, failures: list[float], now: float) -> list[float]:
        cutoff = now - self._window_seconds
        return [t for t in failures if t > cutoff]

    def is_locked(self, *, username: str, ip: str) -> bool:
        with self._lock:
            now = self._now()
            username_failures = self._prune(self._username_failures.get(username, []), now)
            ip_failures = self._prune(self._ip_failures.get(ip, []), now)
            self._username_failures[username] = username_failures
            self._ip_failures[ip] = ip_failures
            return (
                len(username_failures) >= self._username_limit or len(ip_failures) >= self._ip_limit
            )

    def record_failure(self, *, username: str, ip: str) -> None:
        with self._lock:
            now = self._now()
            self._username_failures.setdefault(username, []).append(now)
            self._ip_failures.setdefault(ip, []).append(now)

    def clear(self, *, username: str, ip: str) -> None:
        with self._lock:
            self._username_failures.pop(username, None)
            self._ip_failures.pop(ip, None)

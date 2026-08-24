"""Auth surface tests: login required everywhere, wrong credentials
rejected, constant-time compare used for the password check, login rate
limiting (per-username and per-IP lockout), the unauthenticated /healthz
target, and the secure-cookie flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_support import DEFAULT_PASSWORD, DEFAULT_USERNAME, build_app, login

import adoc.web.users as users_module
from adoc.web.security import LoginRateLimiter
from adoc.web.users import USERS_RELPATH, add_user, remove_user

_PROTECTED_PATHS = [
    "/",
    "/chat",
    "/upload",
    "/confirm",
    "/failed",
    "/labs",
    "/ledger",
    "/reviews",
    "/onboard",
]


@pytest.mark.parametrize("path", _PROTECTED_PATHS)
def test_protected_routes_redirect_to_login_when_unauthenticated(tmp_path: Path, path: str) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_htmx_request_gets_hx_redirect_header_instead_of_a_body_redirect(
    tmp_path: Path,
) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/login"


def test_login_form_itself_is_public(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/login")

    assert response.status_code == 200
    assert "username" in response.text.lower()
    assert "password" in response.text.lower()


def test_healthz_is_public_and_returns_ok(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.text == "ok"


def test_wrong_password_is_rejected(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": "definitely-not-it"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "adoc_session" not in response.cookies
    assert "invalid username or password" in response.text.lower()


def test_unknown_username_is_rejected(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"username": "no-such-user", "password": "whatever"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "adoc_session" not in response.cookies


def test_correct_credentials_log_in_and_grant_access(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    login(client)

    response = client.get("/")
    assert response.status_code == 200


def test_logout_clears_the_session(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)
    assert client.get("/").status_code == 200

    logout_response = client.post("/logout", follow_redirects=False)
    assert logout_response.status_code == 303

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_uses_constant_time_compare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = users_module.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return bool(real_compare_digest(a, b))

    monkeypatch.setattr(users_module.hmac, "compare_digest", spy)

    response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": "wrong-one"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    # The password check must go through hmac.compare_digest, never `==`.
    assert len(calls) == 1


def test_unknown_username_pays_the_same_scrypt_cost_as_a_known_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_user must not short-circuit on an unknown username - that
    would let response timing enumerate valid usernames."""
    app, repo, _db, _calls = build_app(tmp_path)

    scrypt_calls: list[str] = []
    real_scrypt = users_module.hashlib.scrypt

    def spy_scrypt(password: bytes, **kwargs: object) -> bytes:
        scrypt_calls.append(password.decode("utf-8"))
        return real_scrypt(password, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(users_module.hashlib, "scrypt", spy_scrypt)

    users_path = repo.root / USERS_RELPATH
    assert users_module.verify_user(users_path, "no-such-user", "whatever") is False
    assert scrypt_calls == ["whatever"]

    scrypt_calls.clear()
    assert users_module.verify_user(users_path, DEFAULT_USERNAME, "wrong-password") is False
    assert scrypt_calls == ["wrong-password"]


def test_secure_cookie_flag_set_when_forwarded_proto_is_https_and_trusted(tmp_path: Path) -> None:
    """`trust_forwarded_for=True` (the deployed ECS setting - every request
    has actually passed through the ALB) is what makes `X-Forwarded-Proto`
    trustworthy at all - see `test_secure_cookie_flag_ignores_forwarded_proto_when_not_trusted`
    for the untrusted case (W3)."""
    app, _repo, _db, _calls = build_app(tmp_path, trust_forwarded_for=True)
    client = TestClient(app)

    plain_response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        follow_redirects=False,
    )
    assert "Secure" not in plain_response.headers.get("set-cookie", "")

    https_response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert "Secure" in https_response.headers["set-cookie"]


def test_secure_cookie_flag_ignores_forwarded_proto_when_not_trusted(tmp_path: Path) -> None:
    """W3: a client-supplied `X-Forwarded-Proto: https` must NOT force a
    `Secure` cookie onto what (without `trust_forwarded_for`) is only ever
    a plain-HTTP test/local-dev connection - the header is untrustworthy
    unless the deployment guarantees every request passed through the ALB
    first (`Settings.trust_forwarded_for`, same flag `client_ip` gates
    `X-Forwarded-For` on)."""
    app, _repo, _db, _calls = build_app(tmp_path, trust_forwarded_for=False)
    client = TestClient(app)

    spoofed_response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert "Secure" not in spoofed_response.headers.get("set-cookie", "")


def test_lockout_after_five_username_failures_returns_429(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    for _ in range(5):
        response = client.post(
            "/login",
            data={"username": DEFAULT_USERNAME, "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 401

    locked_response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        follow_redirects=False,
    )

    assert locked_response.status_code == 429
    assert "too many failed sign-in attempts" in locked_response.text.lower()
    # Even the correct password is rejected while locked out.
    assert "adoc_session" not in locked_response.cookies


def test_lockout_after_twenty_ip_failures_across_different_usernames(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    for i in range(20):
        response = client.post(
            "/login",
            data={"username": f"nonexistent-{i}", "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 401

    locked_response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        follow_redirects=False,
    )

    assert locked_response.status_code == 429


def test_lockout_clears_once_the_15_minute_window_expires(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    now = [1000.0]
    app.state.login_rate_limiter = LoginRateLimiter(clock=lambda: now[0])

    for _ in range(5):
        response = client.post(
            "/login",
            data={"username": DEFAULT_USERNAME, "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 401

    locked_response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        follow_redirects=False,
    )
    assert locked_response.status_code == 429

    # Fast-forward past the 15-minute window: the oldest (and only)
    # failures age out and the lockout clears.
    now[0] += 15 * 60 + 1

    unlocked_response = client.post(
        "/login",
        data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        follow_redirects=False,
    )
    assert unlocked_response.status_code == 303
    assert "adoc_session" in unlocked_response.cookies


def test_successful_login_clears_the_username_failure_count(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    for _ in range(4):
        response = client.post(
            "/login",
            data={"username": DEFAULT_USERNAME, "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 401

    login(client)  # a 5th, correct attempt - must not be treated as the 5th failure

    # Failures should have been cleared on success: three more failures
    # (not yet enough to relock on their own) are still just rejected, not
    # locked out.
    for _ in range(3):
        response = client.post(
            "/login",
            data={"username": DEFAULT_USERNAME, "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 401


def test_client_ip_uses_last_hop_of_x_forwarded_for_only_when_trusted() -> None:
    from starlette.requests import Request

    from adoc.web.security import client_ip

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
        "client": ("9.9.9.9", 12345),
    }
    request = Request(scope)

    assert client_ip(request, trust_forwarded_for=True) == "5.6.7.8"
    assert client_ip(request, trust_forwarded_for=False) == "9.9.9.9"


def test_client_ip_falls_back_to_socket_peer_without_the_header() -> None:
    from starlette.requests import Request

    from adoc.web.security import client_ip

    scope = {"type": "http", "headers": [], "client": ("9.9.9.9", 12345)}
    request = Request(scope)

    assert client_ip(request, trust_forwarded_for=True) == "9.9.9.9"


def test_session_dies_immediately_when_the_user_is_removed(tmp_path: Path) -> None:
    """W1: a 30-day session cookie must not keep working after the user it
    was issued for no longer exists - removing a user must revoke their
    outstanding sessions on their very next request, not after 30 days."""
    app, repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)
    assert client.get("/").status_code == 200

    removed = remove_user(repo.root / USERS_RELPATH, DEFAULT_USERNAME)
    assert removed is True

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_session_dies_immediately_when_the_password_changes(tmp_path: Path) -> None:
    """W1: a password reset (which rewrites salt+hash, rotating the
    fingerprint) must invalidate sessions issued under the old password
    immediately, not just block future logins with the old password."""
    app, repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)
    assert client.get("/").status_code == 200

    add_user(repo.root / USERS_RELPATH, DEFAULT_USERNAME, "a-brand-new-password")

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_session_still_valid_for_an_unrelated_user_after_another_users_password_changes(
    tmp_path: Path,
) -> None:
    """The identity binding is per-user - rotating one user's credential
    record must not revoke a different, still-valid session."""
    app, repo, _db, _calls = build_app(tmp_path)
    add_user(repo.root / USERS_RELPATH, "second-user", "second-password")
    client = TestClient(app)
    login(client)
    assert client.get("/").status_code == 200

    add_user(repo.root / USERS_RELPATH, "second-user", "rotated-password")

    assert client.get("/").status_code == 200

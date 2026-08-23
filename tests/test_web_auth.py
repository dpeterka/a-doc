"""Auth surface tests: login required everywhere, wrong passphrase
rejected, constant-time compare used for the passphrase check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_support import DEFAULT_PASSPHRASE, build_app, login

import adoc.web.security as security

_PROTECTED_PATHS = ["/", "/chat", "/upload", "/confirm", "/labs", "/ledger", "/reviews", "/onboard"]


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
    assert "passphrase" in response.text.lower()


def test_wrong_passphrase_is_rejected(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/login", data={"passphrase": "definitely-not-it"}, follow_redirects=False
    )

    assert response.status_code == 401
    assert "adoc_session" not in response.cookies
    assert "try again" in response.text.lower()


def test_correct_passphrase_logs_in_and_grants_access(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    login(client, DEFAULT_PASSPHRASE)

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
    real_compare_digest = security.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return bool(real_compare_digest(a, b))

    monkeypatch.setattr(security.hmac, "compare_digest", spy)

    response = client.post("/login", data={"passphrase": "wrong-one"}, follow_redirects=False)

    assert response.status_code == 401
    # The passphrase check must go through hmac.compare_digest, never `==`.
    assert any(a == b"wrong-one" for a, _b in calls)

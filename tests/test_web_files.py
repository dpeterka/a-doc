"""Authenticated page-image file route: requires a session, and refuses
any path-traversal attempt in `sha`/`filename`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

SHA = "e" * 64


def _seed_page_image(repo) -> None:
    page_dir = repo.root / "sources" / "pages" / SHA
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "p-1.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")


def test_page_image_requires_auth(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_page_image(repo)
    client = TestClient(app)

    response = client.get(f"/files/pages/{SHA}/p-1.png", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_page_image_serves_when_authenticated(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_page_image(repo)
    client = TestClient(app)
    login(client)

    response = client.get(f"/files/pages/{SHA}/p-1.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_page_image_refuses_path_traversal_in_filename(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_page_image(repo)
    # Put a real secret file one level above the page-images directory to
    # make sure a traversal attempt (if it worked) would actually read it.
    secret = repo.root / "sources" / "pages" / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    client = TestClient(app)
    login(client)

    response = client.get(f"/files/pages/{SHA}/..%2Fsecret.txt", follow_redirects=False)

    assert response.status_code in (400, 404)
    assert "top secret" not in response.text


def test_page_image_refuses_an_invalid_sha(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_page_image(repo)
    client = TestClient(app)
    login(client)

    response = client.get("/files/pages/not-a-sha/p-1.png")

    assert response.status_code == 404


def test_page_image_404s_for_an_unknown_filename(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_page_image(repo)
    client = TestClient(app)
    login(client)

    response = client.get(f"/files/pages/{SHA}/p-99.png")

    assert response.status_code == 404

"""Weekly review surface tests: list + render of `case/reviews/*.md`."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login


def test_reviews_index_lists_review_files(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    reviews_dir = repo.root / "case" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "2026-06-01-weekly.md").write_text(
        "# Weekly review\n\n- churn: low\n", encoding="utf-8"
    )
    client = TestClient(app)
    login(client)

    response = client.get("/reviews")

    assert response.status_code == 200
    assert "2026-06-01-weekly.md" in response.text


def test_reviews_detail_renders_markdown(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    reviews_dir = repo.root / "case" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "2026-06-01-weekly.md").write_text(
        "# Weekly review\n\n- churn: low\n", encoding="utf-8"
    )
    client = TestClient(app)
    login(client)

    response = client.get("/reviews/2026-06-01-weekly.md")

    assert response.status_code == 200
    assert "<h1>Weekly review</h1>" in response.text
    assert "<li>churn: low</li>" in response.text


def test_reviews_detail_refuses_path_traversal(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    (repo.root / "case" / "secret.md").write_text("top secret", encoding="utf-8")
    client = TestClient(app)
    login(client)

    response = client.get("/reviews/..%2Fsecret.md")

    assert response.status_code == 404
    assert "top secret" not in response.text

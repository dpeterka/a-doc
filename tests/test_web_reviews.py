"""Weekly review surface tests: list + render of `case/reviews/*.md`, and
(when none exist yet) an explanation of what a weekly review is, when the
next one runs, and that the first one needs a diagnostic conversation on
file first.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.web.routes.reviews import REVIEW_SCHEDULE_PHRASE


def test_reviews_index_empty_state_explains_the_mechanism_and_schedule(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/reviews")

    assert response.status_code == 200
    body = response.text
    assert "No reviews yet." not in body
    assert "blind re-differential panel" in body
    assert "without" in body.lower()
    assert REVIEW_SCHEDULE_PHRASE in body
    assert "at least one diagnostic conversation" in body
    assert 'href="/chat"' in body


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


def test_reviews_detail_redacts_dosing_language_in_persisted_markdown(tmp_path: Path) -> None:
    """Violation 2 regression: `reviews_detail` used to render
    `case/reviews/*.md` verbatim, with no gate on either the write path
    (`reason.review.render_review_markdown`, model-written adjudication
    rationale/test-chooser text) or the read path — so an already-persisted
    review containing dosing language rendered straight to the patient."""
    app, repo, _db, _calls = build_app(tmp_path)
    reviews_dir = repo.root / "case" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "2026-06-01-weekly.md").write_text(
        "# Weekly review\n\n- Take 20 mg prednisone daily as discussed at your last visit.\n",
        encoding="utf-8",
    )
    client = TestClient(app)
    login(client)

    response = client.get("/reviews/2026-06-01-weekly.md")

    assert response.status_code == 200
    assert "20 mg prednisone" not in response.text
    assert "withheld" in response.text.lower()
    assert "last visit" in response.text


def test_reviews_detail_refuses_path_traversal(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    (repo.root / "case" / "secret.md").write_text("top secret", encoding="utf-8")
    client = TestClient(app)
    login(client)

    response = client.get("/reviews/..%2Fsecret.md")

    assert response.status_code == 404
    assert "top secret" not in response.text

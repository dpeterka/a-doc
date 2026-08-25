"""Deep-review surface tests: `/reviews` redirects to the merged `/ledger`
page (docs/adr/0019-event-triggered-review.md "UI merge"); `/reviews/
{filename}` — the permalink each review is actually reached by — is
unchanged. `tests/test_web_ledger.py` covers the merged page itself.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login


def test_reviews_index_redirects_to_ledger(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/reviews", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "/ledger"


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
    assert 'href="/ledger"' in response.text


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

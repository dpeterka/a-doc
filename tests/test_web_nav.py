"""Top-nav tests: "Add a document" / "Review queue" / "Failed uploads" are
consolidated into one "Documents" dropdown (Add / Review / Consumed /
Failed) — pure CSS/HTML (`<details>`/`<summary>`), no new JS. Home / Chat /
Labs / Full picture are untouched; "Weekly reviews" was removed as its own
nav entry (docs/adr/0019-event-triggered-review.md "UI merge") — the
review index is now part of "Full picture" (`/ledger`), so a separate nav
entry for it would point at the same place twice.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login


def test_nav_has_a_documents_dropdown_with_all_four_links(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "<details" in body
    assert ">Documents<" in body
    assert 'href="/upload"' in body
    assert 'href="/confirm"' in body
    assert 'href="/documents/consumed"' in body
    assert 'href="/failed"' in body


def test_nav_drops_the_old_top_level_document_entries(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    body = response.text
    nav_start = body.index('<nav class="app-nav">')
    nav_end = body.index("</nav>", nav_start)
    nav_html = body[nav_start:nav_end]

    assert "Add a document" not in nav_html
    assert "Review queue" not in nav_html
    assert "Failed uploads" not in nav_html


def test_nav_keeps_home_chat_labs_and_ledger(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    body = response.text
    nav_start = body.index('<nav class="app-nav">')
    nav_end = body.index("</nav>", nav_start)
    nav_html = body[nav_start:nav_end]

    assert 'href="/"' in nav_html
    assert 'href="/chat"' in nav_html
    assert 'href="/labs"' in nav_html
    assert 'href="/ledger"' in nav_html


def test_nav_no_longer_has_a_separate_reviews_entry(tmp_path: Path) -> None:
    """docs/adr/0019-event-triggered-review.md "UI merge": one nav entry
    where there used to be two — `/reviews` is still reachable (as a
    redirect to `/ledger`, `tests/test_web_ledger.py`), just not linked
    separately from the nav."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    body = response.text
    nav_start = body.index('<nav class="app-nav">')
    nav_end = body.index("</nav>", nav_start)
    nav_html = body[nav_start:nav_end]

    assert 'href="/reviews"' not in nav_html

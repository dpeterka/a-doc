"""Tests for the "Ask about this" bridge — ADR 0045 (PAT-05).

The bridge pre-fills the chat composer from a link and never sends. Every
test here exists because the alternative reading of PAT-05 — post the
excerpt straight to `/chat/send` — would fire the whole diagnostic DAG,
commit a ledger diff, and spend minutes of frontier calls from one click.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from fastapi.testclient import TestClient
from web_support import build_app, exploding_transport, login

from adoc.web.markdown_lite import render_markdown_lite


def test_a_section_heading_gains_an_ask_link() -> None:
    html = render_markdown_lite("## What changed this week\n\nSomething.", ask_sections=True)

    assert "Ask about this" in html
    assert "/chat?ask=" in html
    assert quote_plus("What changed this week") in html


def test_only_second_level_headings_get_a_link() -> None:
    """A link on every `###` inside a criteria set would bury the report in
    them — ADR 0039 just finished cutting its size."""
    html = render_markdown_lite(
        "# Title\n\n## Section\n\n### Subsection\n\n#### Deeper", ask_sections=True
    )

    assert html.count("Ask about this") == 1


def test_the_renderer_adds_nothing_unless_asked() -> None:
    """A chat reply offering to explain itself in chat would be a loop, so
    the links are a separate filter rather than the default."""
    html = render_markdown_lite("## Section\n\nText.")

    assert "Ask about this" not in html
    assert "/chat?ask=" not in html


def test_a_heading_with_markup_and_quotes_is_escaped_into_the_link() -> None:
    """Headings are model-adjacent text and land in an href."""
    html = render_markdown_lite('## A "quoted" & <odd> heading', ask_sections=True)

    assert "<odd>" not in html
    assert "&lt;odd&gt;" in html or "%3Codd%3E" in html


def test_the_chat_page_prefills_the_composer(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    body = client.get("/chat", params={"ask": "Can you explain my complement results?"}).text

    assert "Can you explain my complement results?" in body
    assert "This question came from a link" in body


def test_prefilling_sends_nothing_and_calls_no_model(tmp_path: Path) -> None:
    """The whole point. `build_app`'s default transports fail the test if
    invoked, so this proves the click costs nothing."""
    calls: list = []
    app, _repo, _db, _ = build_app(
        tmp_path,
        primary_transport=exploding_transport(calls),
        challenger_transport=exploding_transport(calls),
    )
    client = TestClient(app)
    login(client)

    response = client.get("/chat", params={"ask": "Explain this please"})

    assert response.status_code == 200
    assert calls == []


def test_a_seeded_question_is_escaped_not_executed(tmp_path: Path) -> None:
    """A seeded question arrives in a URL, so it is user input like any
    other. It renders as the textarea's content, where Jinja escapes it."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    body = client.get("/chat", params={"ask": "</textarea><script>alert(1)</script>"}).text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_an_oversized_seeded_question_is_truncated_not_pre_rejected(tmp_path: Path) -> None:
    """`chat_send` rejects anything over the limit. A link that arrived
    already-rejected would be a dead end she could not diagnose."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    limit = 4000
    body = client.get("/chat", params={"ask": "x" * (limit * 3)}).text

    assert "x" * 200 in body
    # Nowhere near the raw length: the field is capped to the send limit.
    assert body.count("x") <= limit + 50


def test_the_review_sections_carry_links_on_the_case_file_page(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    reviews = repo.root / "case" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / "2026-09-02-weekly.md").write_text(
        "# Weekly review\n\n## What changed this week\n\nNothing much.\n"
    )
    client = TestClient(app)
    login(client)

    body = client.get("/ledger").text

    # The SECTION link specifically. "Ask about this" alone would also match
    # the hypothesis card's "Ask about this lead".
    assert quote_plus("What changed this week") in body
    assert ">Ask about this</a>" in body

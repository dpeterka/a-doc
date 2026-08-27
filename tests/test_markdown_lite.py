"""Tests for adoc.web.markdown_lite — the dependency-free markdown subset.

The subset exists because no markdown library is among the allowed runtime
deps. It renders the patient's own case files, so every rule here is also a
safety boundary: input is HTML-escaped first, and links are restricted to
internal paths.
"""

from __future__ import annotations

from adoc.web.markdown_lite import render_markdown_lite


def test_headings_bold_and_lists_still_render() -> None:
    """The original subset, pinned so the additions did not regress it."""
    html = render_markdown_lite("# Title\n\n- **bold item**\n\nA paragraph.")

    assert "<h1>Title</h1>" in html
    assert "<strong>bold item</strong>" in html
    assert "<p>A paragraph.</p>" in html


def test_input_is_escaped_before_rendering() -> None:
    html = render_markdown_lite("<script>alert(1)</script>")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_italic_renders_but_identifiers_survive() -> None:
    """`_text_` becomes emphasis, but snake_case identifiers and source refs
    must pass through untouched — they are full of underscores."""
    assert "<em>Relevant to:</em>" in render_markdown_lite("_Relevant to:_")
    out = render_markdown_lite("a labs:some_analyte ref and snake_case_name here")
    assert "<em>" not in out
    assert "some_analyte" in out


def test_internal_links_render_and_external_ones_do_not() -> None:
    """The source of this markdown is model-authored, so an unrestricted href
    would let a reasoning stage put an arbitrary destination in front of the
    patient. Only internal absolute paths are linkified."""
    assert '<a href="/ledger#sle-01">SLE</a>' in render_markdown_lite("[SLE](/ledger#sle-01)")
    for hostile in ("[x](https://evil.example/steal)", "[x](javascript:alert(1))"):
        assert "<a" not in render_markdown_lite(hostile)


def test_indented_lines_stay_inside_their_bullet() -> None:
    """The next-appointment page puts the ask, the related hypotheses and the
    rationale under one panel bullet. Before continuation support they broke
    out of the <li> and collapsed into a single detached paragraph."""
    html = render_markdown_lite(
        "- **Celiac screen**\n  Ask for a coeliac blood screen.\n  _Relevant to:_\n"
    )

    assert html.count("<li>") == 1
    assert "Ask for a coeliac blood screen." in html
    # Everything belongs to the item, so no stray paragraph escaped the list.
    assert "<p>" not in html

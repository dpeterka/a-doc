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


def test_asterisk_italics_render() -> None:
    """Models emit `*text*`; only the `_text_` form was handled, so a chat
    reply reading "help you *find what's already in it*" reached the patient
    with the asterisks still in it."""
    out = render_markdown_lite("help you *find what is already in it* — not to diagnose")

    assert "<em>find what is already in it</em>" in out
    assert "*find" not in out


def test_asterisk_italics_leave_arithmetic_and_intra_word_stars_alone() -> None:
    """The guards that keep the new rule from eating text that is not
    emphasis. Space-hugged stars are arithmetic; word-hugged stars are not a
    delimiter."""
    assert "2 * 3 * 4" in render_markdown_lite("2 * 3 * 4")
    assert "a*b*c" in render_markdown_lite("a*b*c")


def test_bold_still_wins_over_the_new_italic_rule() -> None:
    """`**bold**` must not be re-matched as two italics by the star rule,
    which is why it runs after the bold substitution."""
    out = render_markdown_lite("**bold**")

    assert "<strong>bold</strong>" in out
    assert "<em>" not in out


def test_pipe_tables_render_as_tables() -> None:
    """The criteria scorers emit their per-item breakdown as a markdown
    table. With no rule for it every row fell through to the paragraph branch
    and was joined with spaces, so the whole table reached the patient as one
    unbroken line of pipes and dashes."""
    out = render_markdown_lite(
        "| | Criterion | Points |\n|---|---|---|\n| ? | Neuropsychiatric — Seizure | 5 |\n"
    )

    assert "<table>" in out
    assert "<th>Criterion</th>" in out
    assert "<td>Neuropsychiatric — Seizure</td>" in out
    assert "|---|" not in out


def test_a_paragraph_containing_pipes_is_not_a_table() -> None:
    """A row only starts a table when the NEXT line is a divider. Without
    that pairing, prose mentioning a pipe would be swallowed into a table."""
    out = render_markdown_lite("The value was recorded as | separated | text.")

    assert "<table>" not in out


def test_a_short_row_is_padded_to_the_header_width() -> None:
    """A malformed row must not skew the rest of the table."""
    out = render_markdown_lite("| A | B | C |\n|---|---|---|\n| only-one |\n")

    assert out.count("<td>") == 3


def test_table_cells_are_still_escaped_and_inlined() -> None:
    out = render_markdown_lite("| A |\n|---|\n| **bold** <script> |\n")

    assert "<strong>bold</strong>" in out
    assert "&lt;script&gt;" in out


def test_code_spans_render() -> None:
    """The review report and the theories file cite sources as
    `encounter:...` / `labs:...`. With no rule for them the backticks reached
    the patient literally — six of them on the live case file."""
    out = render_markdown_lite("See `encounter:2026-08-04--scan.md` for detail.")

    assert "<code>encounter:2026-08-04--scan.md</code>" in out
    assert "`" not in out


def test_code_span_contents_are_not_touched_by_emphasis_rules() -> None:
    """Spans are lifted out before the emphasis rules run and restored after,
    so a ref containing underscores or stars survives intact."""
    out = render_markdown_lite("`labs:free_t4` and `a *b* c` and *real italics*")

    assert "<code>labs:free_t4</code>" in out
    assert "<code>a *b* c</code>" in out
    assert "<em>real italics</em>" in out


def test_code_spans_are_still_escaped() -> None:
    out = render_markdown_lite("`<script>alert(1)</script>`")

    assert "&lt;script&gt;" in out
    assert "<script>" not in out

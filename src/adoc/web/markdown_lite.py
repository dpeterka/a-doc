"""A tiny, dependency-free markdown-ish renderer.

No markdown library is among the task's two allowed new runtime deps
(`python-multipart`, `sse-starlette`), so `case/questions-open.md` and
`case/reviews/*.md` are rendered with this narrow, regex-based converter
instead: headings (`#`..`######`), unordered list items (`-`/`*`), bold
(`**text**`), italic (`_text_`), internal links (`[text](/path)`),
continuation lines belonging to a list item, and paragraphs. Everything else
passes through as an escaped paragraph. Input is HTML-escaped first, so this
is safe to render even though the source is always the patient's own data.

The italic, link and continuation-line support exist because the
next-appointment page needs them. Its renderer emits a bulleted panel with an
indented ask, the hypotheses the panel bears on, and a folded rationale — and
without these three rules that rendered as literal underscores and detached
paragraphs, which was worse than the wall of text it replaced.
"""

from __future__ import annotations

import html
import re

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LIST_ITEM_RE = re.compile(r"^[-*]\s+(.*)$")

# `_text_` only when the underscores hug the text and sit on a word boundary,
# so snake_case identifiers and `labs:some_analyte` refs are left alone.
_ITALIC_RE = re.compile(r"(?<![\w`])_(?!\s)([^_`]+?)(?<!\s)_(?![\w`])")

# Links are restricted to INTERNAL absolute paths. The source of this markdown
# is model-authored text, so an unrestricted href would let a reasoning stage
# put an arbitrary destination — or a `javascript:` URL — in front of the
# patient. A relative path can only ever point back into this app.
_LINK_RE = re.compile(r"\[([^\]]+)\]\((/[^)\s\"\']*)\)")

# A continuation line is indented under a list item and belongs to it. HTML
# escaping runs first, so this operates on already-escaped text.
_CONTINUATION_INDENT = 2


def _inline(text: str) -> str:
    out = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    return _LINK_RE.sub(r'<a href="\2">\1</a>', out)


def render_markdown_lite(text: str) -> str:
    """Render a small markdown subset to HTML. Input is escaped first."""
    escaped = html.escape(text, quote=False)
    lines = escaped.splitlines()

    html_parts: list[str] = []
    paragraph: list[str] = []
    list_open = False
    item_parts: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            html_parts.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_item() -> None:
        """Close the open `<li>`, folding in any continuation lines.

        Continuations become `<div class="li-line">` rather than being joined
        into one run of text, so a panel's ask, its related hypotheses and its
        rationale stay on separate lines inside the same bullet.
        """
        if not item_parts:
            return
        head = _inline(item_parts[0])
        rest = "".join(f'<div class="li-line">{_inline(part)}</div>' for part in item_parts[1:])
        html_parts.append(f"<li>{head}{rest}</li>")
        item_parts.clear()

    def close_list() -> None:
        nonlocal list_open
        flush_item()
        if list_open:
            html_parts.append("</ul>")
            list_open = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_list()
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            html_parts.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        item = _LIST_ITEM_RE.match(stripped)
        if item:
            flush_paragraph()
            flush_item()
            if not list_open:
                html_parts.append("<ul>")
                list_open = True
            item_parts.append(item.group(1))
            continue

        # An indented line while a list item is open continues that item
        # rather than starting a paragraph outside the list.
        indent = len(line) - len(line.lstrip())
        if item_parts and indent >= _CONTINUATION_INDENT:
            item_parts.append(stripped)
            continue

        close_list()
        paragraph.append(stripped)

    flush_paragraph()
    close_list()
    return "\n".join(html_parts)

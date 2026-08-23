"""A tiny, dependency-free markdown-ish renderer.

No markdown library is among the task's two allowed new runtime deps
(`python-multipart`, `sse-starlette`), so `case/questions-open.md` and
`case/reviews/*.md` are rendered with this narrow, regex-based converter
instead: headings (`#`..`######`), unordered list items (`-`/`*`), bold
(`**text**`), and paragraphs. Everything else passes through as an escaped
paragraph. Input is HTML-escaped first, so this is safe to render even
though the source is always the patient's own data.
"""

from __future__ import annotations

import html
import re

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LIST_ITEM_RE = re.compile(r"^[-*]\s+(.*)$")


def _inline(text: str) -> str:
    return _BOLD_RE.sub(r"<strong>\1</strong>", text)


def render_markdown_lite(text: str) -> str:
    """Render a small markdown subset to HTML. Input is escaped first."""
    escaped = html.escape(text, quote=False)
    lines = escaped.splitlines()

    html_parts: list[str] = []
    list_open = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            html_parts.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_open
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
            if not list_open:
                html_parts.append("<ul>")
                list_open = True
            html_parts.append(f"<li>{_inline(item.group(1))}</li>")
            continue

        close_list()
        paragraph.append(stripped)

    flush_paragraph()
    close_list()
    return "\n".join(html_parts)

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
from urllib.parse import quote_plus

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LIST_ITEM_RE = re.compile(r"^[-*]\s+(.*)$")

# `_text_` only when the underscores hug the text and sit on a word boundary,
# so snake_case identifiers and `labs:some_analyte` refs are left alone.
_ITALIC_RE = re.compile(r"(?<![\w`])_(?!\s)([^_`]+?)(?<!\s)_(?![\w`])")

# `*text*` — the form models actually emit. Only the underscore form was
# handled, so a chat reply reading "help you *find what's already in it*"
# reached the patient with the asterisks still in it. Applied AFTER
# `_BOLD_RE`, so `**bold**` is already consumed and cannot be re-matched.
# The space guards keep arithmetic ("2 * 3") and the word-boundary guards
# keep intra-word asterisks from becoming emphasis.
_ITALIC_STAR_RE = re.compile(r"(?<![\w*`])\*(?!\s)([^*`]+?)(?<!\s)\*(?![\w*`])")

# Links are restricted to INTERNAL absolute paths. The source of this markdown
# is model-authored text, so an unrestricted href would let a reasoning stage
# put an arbitrary destination — or a `javascript:` URL — in front of the
# patient. A relative path can only ever point back into this app.
_LINK_RE = re.compile(r"\[([^\]]+)\]\((/[^)\s\"\']*)\)")

# Code spans. The review report and the theories file cite sources as
# `encounter:...` / `labs:...`; with no rule for them the backticks reached
# the patient literally. Measured on the live case file: 6 of them.
_CODE_RE = re.compile(r"`([^`\n]+)`")

# A continuation line is indented under a list item and belongs to it. HTML
# escaping runs first, so this operates on already-escaped text.
_CONTINUATION_INDENT = 2

# Pipe tables. The criteria scorers render their per-item breakdown as a
# markdown table; with no rule for it every row fell through to the paragraph
# branch and `flush_paragraph` joined them with spaces, so the whole table
# reached the patient as one unbroken line of pipes and dashes.
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")
_TABLE_DIVIDER_RE = re.compile(r"^\|[\s:|-]+\|$")


def _table_cells(row: str) -> list[str]:
    """Split one `| a | b |` row into its cells.

    The outer pipes are stripped by `_TABLE_ROW_RE`; splitting the remainder
    keeps interior empties, which matters because the criteria tables lead
    with an empty state column.
    """
    return [cell.strip() for cell in row.split("|")]


def _inline(text: str) -> str:
    # Code spans are lifted out FIRST and restored last, so their contents are
    # never touched by the emphasis rules. A citation ref like
    # `labs:free_t4` is exactly the kind of text that would otherwise be
    # chewed on by an underscore rule.
    spans: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    out = _CODE_RE.sub(_stash, text)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    out = _ITALIC_STAR_RE.sub(r"<em>\1</em>", out)
    out = _LINK_RE.sub(r'<a href="\2">\1</a>', out)
    for index, span in enumerate(spans):
        out = out.replace(f"\x00{index}\x00", f"<code>{span}</code>")
    return out


ASK_PROMPT_TEMPLATE = 'Can you explain the "{section}" part of my review in plain terms?'
"""What the section link seeds into the chat box (ADR 0045).

Phrased as the patient's own question because that is what she will send. A
"payload" written in the system's voice reads to the model as an instruction
and to her as something she did not say."""

ASK_SECTION_LABEL = "Ask about this"


def render_markdown_lite(text: str, *, ask_sections: bool = False) -> str:
    """Render a small markdown subset to HTML. Input is escaped first.

    With `ask_sections`, every `##` heading gains a link that PRE-FILLS the
    chat box with a question about that section (ADR 0045). It pre-fills and
    does not send: a diagnostic turn runs the whole DAG, writes the ledger
    before the composer ever speaks, and costs minutes — a single click must
    not do that silently, and the question is the thing that decides the
    answer, so she should see it first.
    """
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

    def flush_table(rows: list[str]) -> None:
        """Render collected rows as a table. The first row is the header and
        the divider has already been dropped."""
        if not rows:
            return
        head = _table_cells(rows[0])
        body = [_table_cells(r) for r in rows[1:]]
        out = ["<table>", "<thead><tr>"]
        out += [f"<th>{_inline(c)}</th>" for c in head]
        out.append("</tr></thead>")
        if body:
            out.append("<tbody>")
            for cells in body:
                out.append("<tr>")
                # Pad or trim to the header width so a malformed row cannot
                # skew the whole table.
                cells = (cells + [""] * len(head))[: len(head)]
                out += [f"<td>{_inline(c)}</td>" for c in cells]
                out.append("</tr>")
            out.append("</tbody>")
        out.append("</table>")
        html_parts.append("".join(out))

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        # A table is a row whose NEXT line is a divider — that pairing is what
        # separates a real table from a paragraph that happens to contain pipes.
        row = _TABLE_ROW_RE.match(stripped)
        if row and index + 1 < len(lines) and _TABLE_DIVIDER_RE.match(lines[index + 1].strip()):
            flush_paragraph()
            close_list()
            collected = [row.group(1)]
            index += 2  # skip the divider
            while index < len(lines):
                nxt = _TABLE_ROW_RE.match(lines[index].strip())
                if not nxt:
                    break
                collected.append(nxt.group(1))
                index += 1
            flush_table(collected)
            continue

        index += 1
        if not stripped:
            flush_paragraph()
            close_list()
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            title = heading.group(2)
            rendered = f"<h{level}>{_inline(title)}</h{level}>"
            if ask_sections and level == 2:
                # `title` is already HTML-escaped by the module-level
                # `html.escape`; `quote_plus` handles the URL layer.
                seeded = ASK_PROMPT_TEMPLATE.format(section=html.unescape(title).strip())
                href = f"/chat?ask={quote_plus(seeded)}"
                rendered += f'<p class="section-ask"><a href="{href}">{ASK_SECTION_LABEL}</a></p>'
            html_parts.append(rendered)
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

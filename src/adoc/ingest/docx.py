"""Deterministic `.docx` text extraction - no LLM, no LibreOffice/PDF
conversion (PLAN.md docx ingestion design decision: "docx = TEXT
documents").

Real Dropbox drops contain `.docx` narrative documents (clinical history,
supplement plans) that the old `%PDF-` magic gate rejected outright. Rather
than convert them to PDF/images (a new system dependency, and a lossy
detour for something that is already clean text), a-doc reads them
directly with `python-docx` - a pure-Python runtime dependency - and treats
the result as the document's canonical text: paragraphs in reading order,
with any tables rendered as pipe-delimited rows so their structure survives
as plain text for a downstream LLM pass (`ingest.extract.double_pass_extract_text`)
or, for a narrative document, for a human reading the encounter file
directly.

This module does no interpretation of the text - it is pure, deterministic
transcription, exactly like the OCR/vision extractors are supposed to be:
`extract_docx_text` never talks to a model and never fabricates content
that isn't in the document.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


class DocxExtractionError(Exception):
    """Raised when `python-docx` cannot parse an archived `.docx` file."""


def _paragraph_text(paragraph: Paragraph) -> str:
    """The paragraph's text, skipping empty/whitespace-only runs.

    Falls back to `paragraph.text` (python-docx's own run-independent
    accumulation) if run-by-run text somehow came back empty - e.g. text
    exposed only through non-`w:r` content python-docx still surfaces via
    `.text`."""
    parts = [run.text for run in paragraph.runs if run.text and run.text.strip()]
    if parts:
        return "".join(parts).strip()
    return paragraph.text.strip()


def _table_to_rows(table: Table) -> str:
    """Render one table as pipe-delimited rows (`"| a | b | c |"`), one line
    per table row, skipping rows where every cell is blank."""
    lines: list[str] = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def extract_docx_text(path: Path) -> str:
    """Extract `path`'s full text: paragraphs and tables, in document
    order, blank paragraphs/rows skipped. Deterministic - the same file
    always yields the same text; never calls a model.
    """
    try:
        document = Document(str(path))
    except Exception as exc:  # noqa: BLE001 - python-docx raises varied package errors
        raise DocxExtractionError(f"{path.name}: could not read .docx file: {exc}") from exc

    blocks: list[str] = []
    for element in document.element.body.iterchildren():
        if element.tag.endswith("}p"):
            text = _paragraph_text(Paragraph(element, document))
            if text:
                blocks.append(text)
        elif element.tag.endswith("}tbl"):
            rendered = _table_to_rows(Table(element, document))
            if rendered:
                blocks.append(rendered)

    return "\n\n".join(blocks)


__all__ = ["DocxExtractionError", "extract_docx_text"]

"""The document-TEXT layer: deterministic full-text extraction + storage for
every ingested NON-GENOMIC document (docs/adr/0015-document-text-corpus.md).

This mirrors `ingest.docx`'s "no LLM, no interpretation" transcription
principle: `pdftotext` (poppler-utils, invoked as a subprocess — no new
Python runtime dependency), `python-docx` (via `ingest.docx.extract_docx_text`,
already a runtime dependency), and plain-text reads are pure, deterministic
code, never a model call.

**Genomic files are structurally excluded** (CRITICAL DESIGN RULE, ADR 0010,
unchanged by this feature): `extract_text_for_kind`'s `kind` parameter is
typed `ingest.archive.DocKind`, a `Literal["pdf", "docx", "text"]` with no
`"genomic"` member at all — there is no code path from a genomic file's
bytes into this module. `backfill_document_text` additionally skips every
`documents` row whose `doc_type == GENOMIC_DOC_TYPE` before it ever looks at
that document's archived bytes, so the exclusion holds even for a document
already on file before this feature existed.

Storage: full text is written verbatim to a committed file in the data repo,
`doc-text/<sha256>.txt` — human-diffable, git-tracked (unlike
`sources/genomics/`, never gitignored), and the single source of truth
`labs.sqlite`'s `document_text`/`document_text_fts` tables are derived from
and can always be rebuilt from (`rebuild_document_text_from_files` — the
document-text analogue of `LabsDb.rebuild_from_jsonl`).

Pagination: `pdftotext`'s default output separates pages with a form-feed
character (`\\f`). `_split_pages` keys off that character alone — present,
split into 1-indexed pages; absent (a docx/plain-text document, or a
single-page PDF, which `pdftotext` emits with no form feed at all), store as
one page-less (`page=None`) row. This is a deliberate simplification: a
one-page PDF's citation renders as document-level (`doc:<filename>`) rather
than `doc:<filename>#p1` — the priority is never asserting a page number
`pdftotext` didn't actually signal, over perfect page attribution for the
single-page case. Because both the initial write and a later rebuild derive
pagination from this exact same rule applied to the exact same stored text,
the two can never disagree — "the sqlite side stays derived and rebuildable
from the committed files" holds by construction, not by convention.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from adoc.casefile.repo import DataRepo
from adoc.ingest.archive import DocKind
from adoc.ingest.docx import DocxExtractionError, extract_docx_text
from adoc.ingest.filetypes import detect_intake_kind
from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.labs.db import DocumentTextPage, LabsDb

logger = logging.getLogger(__name__)

DOC_TEXT_RELDIR = "doc-text"

PdfTextExtractor = Callable[[Path], "str | None"]
"""`(pdf_path) -> extracted text, or None on failure/unavailability`. An
injection seam — tests fake this so extraction tests never depend on
`pdftotext` actually being installed (mirrors `ingest.archive.PageRenderer`)."""


def pdftotext_extractor(path: Path) -> str | None:
    """Default `PdfTextExtractor`: shell out to `pdftotext -layout` (part of
    poppler-utils, already required for `ingest.archive.pdftoppm_renderer`).

    Never raises: a missing binary or a failed subprocess is logged and
    returns `None` — text extraction must NEVER fail an ingest (see
    `ingest.pipeline`'s module docstring: "lab-row extraction is the primary
    job").
    """
    if shutil.which("pdftotext") is None:
        logger.warning("pdftotext is not installed; skipping text extraction for %s", path.name)
        return None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("pdftotext failed for %s: %s", path.name, exc)
        return None
    return result.stdout.decode("utf-8", errors="replace")


def extract_text_for_kind(
    kind: DocKind, path: Path, *, pdf_extractor: PdfTextExtractor = pdftotext_extractor
) -> str | None:
    """Extract `path`'s full text for its already-known `kind` (`"pdf"`,
    `"docx"`, or `"text"` — never `"genomic"`, which isn't a `DocKind`
    member at all; see module docstring). Returns `None` (never raises) on
    any extraction failure, so every caller can log-and-continue rather
    than fail an ingest.
    """
    if kind == "pdf":
        return pdf_extractor(path)
    if kind == "docx":
        try:
            return extract_docx_text(path)
        except DocxExtractionError as exc:
            logger.warning("docx text extraction failed for %s: %s", path.name, exc)
            return None
    # kind == "text"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read text file %s: %s", path.name, exc)
        return None


def document_text_path(repo_root: Path, sha256: str) -> Path:
    return repo_root / DOC_TEXT_RELDIR / f"{sha256}.txt"


def _split_pages(text: str) -> list[DocumentTextPage]:
    """`text` split on `pdftotext`'s form-feed page separator (module
    docstring). Absent any form feed, this is one `page=None` row."""
    if "\f" not in text:
        return [DocumentTextPage(page=None, text=text)]
    parts = text.split("\f")
    return [DocumentTextPage(page=index + 1, text=part) for index, part in enumerate(parts)]


def store_document_text(
    repo: DataRepo, db: LabsDb, sha256: str, text: str, *, at: datetime | None = None
) -> Path:
    """Write `text` verbatim to `doc-text/<sha256>.txt` (committed,
    human-diffable) and (re)populate `labs.sqlite`'s `document_text`/
    `document_text_fts` from it. Idempotent — re-storing the same sha
    replaces both the file and the sqlite rows (`LabsDb.replace_document_text`
    is itself delete-then-insert). Returns the written file path.
    """
    when = at or datetime.now(UTC)
    path = document_text_path(repo.root, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    db.replace_document_text(sha256, _split_pages(text), extracted_at=when)
    return path


def rebuild_document_text_from_files(db: LabsDb, repo_root: Path) -> int:
    """Repopulate `document_text`/`document_text_fts` from the committed
    `doc-text/*.txt` files on disk — the document-text analogue of
    `LabsDb.rebuild_from_jsonl`, used after a fresh checkout/restore where
    `labs.sqlite` itself is gitignored and rebuilt from committed sources.
    Returns how many documents were (re)populated. A stray text file whose
    sha isn't a known `documents` row is skipped (never raises) — `labs`'s
    `document_text.source_doc` foreign key requires the `documents` row to
    already exist, so `documents` must already be populated (e.g. via
    `rebuild_from_jsonl`) before this runs.
    """
    doc_text_dir = repo_root / DOC_TEXT_RELDIR
    if not doc_text_dir.is_dir():
        return 0
    count = 0
    for path in sorted(doc_text_dir.glob("*.txt")):
        sha256 = path.stem
        if db.get_document(sha256) is None:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        db.replace_document_text(sha256, _split_pages(text), extracted_at=datetime.now(UTC))
        count += 1
    return count


def _find_archived_original(repo_root: Path, sha256: str) -> Path | None:
    """The immutable archived original for `sha256` under `sources/`
    (`<sha>__<origname>`, `ingest.archive`'s naming convention) — `None` if
    it isn't there or isn't uniquely identifiable. Never looks under
    `sources/genomics/` (a different, gitignored subtree — callers only
    reach here for a non-genomic `documents` row in the first place)."""
    sources_dir = repo_root / "sources"
    if not sources_dir.is_dir():
        return None
    prefix = f"{sha256}__"
    matches = [
        entry
        for entry in sources_dir.iterdir()
        if entry.is_file() and entry.name.startswith(prefix)
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class BackfillDocTextReport:
    """What `backfill_document_text` (`adoc backfill-doc-text`) did."""

    total_non_genomic: int
    already_covered: int
    extracted: int
    skipped_no_source: int
    skipped_genomic: int


def backfill_document_text(
    repo: DataRepo, db: LabsDb, *, pdf_extractor: PdfTextExtractor = pdftotext_extractor
) -> BackfillDocTextReport:
    """`adoc backfill-doc-text`: extract and store text for every
    already-ingested, non-genomic document that doesn't have a
    `document_text` row yet. Idempotent — a document already covered
    (`LabsDb.document_text_shas()`), regardless of whether its stored text
    is empty, is never reprocessed; a document whose extraction fails this
    run (missing `pdftotext`, an unreadable file) is simply left uncovered
    for the next run rather than raising.

    Genomic documents (`doc_type == GENOMIC_DOC_TYPE`) are skipped before
    their archived bytes are ever touched — the same structural exclusion
    `ingest.pipeline` observes for a live ingest (module docstring).
    """
    already = db.document_text_shas()
    total_non_genomic = 0
    already_covered = 0
    extracted = 0
    skipped_no_source = 0
    skipped_genomic = 0

    for doc in db.list_documents():
        if doc.doc_type == GENOMIC_DOC_TYPE:
            skipped_genomic += 1
            continue
        total_non_genomic += 1
        if doc.sha256 in already:
            already_covered += 1
            continue

        original = _find_archived_original(repo.root, doc.sha256)
        kind = detect_intake_kind(original) if original is not None else None
        if original is None or kind not in ("pdf", "docx", "text"):
            skipped_no_source += 1
            continue

        text = extract_text_for_kind(kind, original, pdf_extractor=pdf_extractor)
        if text is None:
            continue
        store_document_text(repo, db, doc.sha256, text)
        extracted += 1

    return BackfillDocTextReport(
        total_non_genomic=total_non_genomic,
        already_covered=already_covered,
        extracted=extracted,
        skipped_no_source=skipped_no_source,
        skipped_genomic=skipped_genomic,
    )


__all__ = [
    "DOC_TEXT_RELDIR",
    "BackfillDocTextReport",
    "PdfTextExtractor",
    "backfill_document_text",
    "document_text_path",
    "extract_text_for_kind",
    "pdftotext_extractor",
    "rebuild_document_text_from_files",
    "store_document_text",
]

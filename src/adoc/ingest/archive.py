"""Immutable source-document archival (PLAN.md session loop (a)).

`archive_document` detects the document's kind by content
(`ingest.filetypes.detect_intake_kind`), computes a sha256, and copies the
original byte-for-byte into `sources/<sha>__<origname>` (never mutated
afterwards - PLAN.md "State": "immutable `sources/`"). For a PDF it also
renders one PNG per page into `sources/pages/<sha>/`; a `.docx` or plain
`.txt`/`.md` has no page images (`ArchivedDoc.page_paths == []` - see the
module docstring of `ingest.docx` for why: it is ingested as TEXT, not
converted to PDF/images). Either way, `LabsDb.documents` is checked so the
pipeline can skip re-extraction of a document it has already ingested.

This module only ever archives the three kinds it can turn into
`ArchivedDoc`s that flow through the normal document pipeline - `"pdf"`,
`"docx"`, `"text"`. Genomic files and zip archives are detected by the same
`detect_intake_kind` gate but are routed by `ingest.pipeline` to their own
handling *before* `archive_document` is ever called on them (genomic:
`ingest.genomics.archive_genomic_file`, a different destination directory
and no `LabsDb.documents` row shaped like a document's; zip: expanded into
its members, never archived itself) - passing either kind here is a
programming error and raises `ArchiveError` immediately.

A `"text"` document is capped at `text_max_bytes` (default 1 MiB,
`DEFAULT_TEXT_MAX_BYTES`) - oversized non-genomic text is rejected with a
clear `ArchiveError` reason rather than silently truncated (genomic text
exports, which run to tens of megabytes, are never subject to this cap -
they are detected as `"genomic"`, not `"text"`, and never reach this check).

Page rendering defaults to shelling out to `pdftoppm` (poppler-utils) - an
*optional system binary*, never a new Python runtime dependency
(CLAUDE.md / task constraints). Tests inject a fake `PageRenderer` so CI
never depends on poppler being installed.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from adoc.ingest.filetypes import detect_intake_kind
from adoc.labs.db import LabsDb

DocKind = Literal["pdf", "docx", "text"]
"""The kinds `archive_document` actually archives - a subset of
`ingest.filetypes.IntakeKind` (which also has `"genomic"`/`"zip"`, handled
elsewhere - see the module docstring)."""

DEFAULT_TEXT_MAX_BYTES = 1024 * 1024
"""Size cap for a `"text"`-kind document (item 1 of the genomics/filetypes
task spec): "larger non-genomic text is rejected with a clear reason
rather than silently truncated"."""

PageRenderer = Callable[[Path, Path], list[Path]]
"""`(pdf_path, out_dir) -> sorted page PNG paths, one per page, 1-indexed."""


class ArchiveError(Exception):
    """Raised when archival cannot proceed (renderer missing, IO failure)."""


@dataclass(frozen=True)
class ArchivedDoc:
    sha256: str
    original_path: Path
    """The archived immutable copy: `sources/<sha>__<origname>`."""
    page_paths: list[Path]
    """Sorted page PNG paths: `sources/pages/<sha>/p-*.png`. Always `[]` for
    a `.docx` or `"text"` document - neither is ever rendered to images
    (TEXT ingestion)."""
    already_ingested: bool
    """True if `sha256` already had a `documents` row before this call."""
    kind: DocKind = "pdf"
    """`"pdf"`, `"docx"`, or `"text"` - see `ingest.filetypes.detect_intake_kind`."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdftoppm_renderer(pdf_path: Path, out_dir: Path) -> list[Path]:
    """Default `PageRenderer`: shell out to `pdftoppm -png` (poppler-utils).

    Raises `ArchiveError` with a clear message if `pdftoppm` is not on
    `PATH`, instead of letting a cryptic `FileNotFoundError` surface -
    callers that can't install poppler should inject a `PageRenderer`.
    """
    if shutil.which("pdftoppm") is None:
        raise ArchiveError(
            "pdftoppm is not installed (poppler-utils) - it is required to render "
            "page PNGs for the extractor_pass_b vision pass; install poppler-utils "
            "or pass an injectable `renderer=` for tests/CI"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "p"
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", str(pdf_path), str(prefix)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise ArchiveError(f"pdftoppm failed on {pdf_path}: {stderr}") from exc
    return sorted(out_dir.glob("p-*.png"))


def archive_document(
    repo_root: Path,
    path: Path,
    *,
    db: LabsDb,
    renderer: PageRenderer = pdftoppm_renderer,
    text_max_bytes: int = DEFAULT_TEXT_MAX_BYTES,
) -> ArchivedDoc:
    """Archive `path` into `repo_root/sources/` and render its page PNGs.

    Idempotent: re-archiving identical bytes is a no-op copy/render (the
    immutable original and its already-rendered pages are reused rather
    than recreated). `already_ingested` reflects whether `LabsDb.documents`
    already had a row for this sha *before* this call - the pipeline uses
    it to skip re-classification/re-extraction of a duplicate document.

    A `"text"` document larger than `text_max_bytes` is rejected with a
    clear `ArchiveError` *before* anything is copied into `sources/` -
    never silently truncated. `"genomic"`/`"zip"` kinds are never valid
    here (see the module docstring); passing one raises immediately.
    """
    kind = detect_intake_kind(path)
    if kind is None:
        raise ArchiveError(
            f"{path.name}: not a PDF (unsupported type); convert to PDF and re-upload"
        )
    if kind == "genomic" or kind == "zip":
        raise ArchiveError(
            f"{path.name}: {kind} files are ingested via a dedicated path, not archive_document"
        )
    if kind == "text":
        size = path.stat().st_size
        if size > text_max_bytes:
            raise ArchiveError(
                f"{path.name}: text file is {size:,} bytes, larger than the "
                f"{text_max_bytes:,}-byte limit for text documents; split it up "
                "or convert it to PDF and re-upload"
            )

    sha = sha256_file(path)
    already_ingested = db.get_document(sha) is not None

    sources_dir = repo_root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    archived_path = sources_dir / f"{sha}__{path.name}"
    if not archived_path.exists():
        shutil.copy2(path, archived_path)

    if kind in ("docx", "text"):
        # No page rendering for docx/text - both are ingested as TEXT (see
        # the module docstring); `page_paths` stays empty by design.
        page_paths: list[Path] = []
    else:
        pages_dir = repo_root / "sources" / "pages" / sha
        if pages_dir.is_dir() and any(pages_dir.iterdir()):
            page_paths = sorted(pages_dir.iterdir())
        else:
            page_paths = renderer(archived_path, pages_dir)

    return ArchivedDoc(
        sha256=sha,
        original_path=archived_path,
        page_paths=page_paths,
        already_ingested=already_ingested,
        kind=kind,
    )

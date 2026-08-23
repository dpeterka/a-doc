"""Immutable source-document archival (PLAN.md session loop (a)).

`archive_document` computes a sha256, copies the original byte-for-byte
into `sources/<sha>__<origname>` (never mutated afterwards - PLAN.md
"State": "immutable `sources/`"), renders one PNG per page into
`sources/pages/<sha>/`, and checks `LabsDb.documents` so the pipeline can
skip re-extraction of a document it has already ingested.

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

from adoc.labs.db import LabsDb

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
    """Sorted page PNG paths: `sources/pages/<sha>/p-*.png`."""
    already_ingested: bool
    """True if `sha256` already had a `documents` row before this call."""


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
) -> ArchivedDoc:
    """Archive `path` into `repo_root/sources/` and render its page PNGs.

    Idempotent: re-archiving identical bytes is a no-op copy/render (the
    immutable original and its already-rendered pages are reused rather
    than recreated). `already_ingested` reflects whether `LabsDb.documents`
    already had a row for this sha *before* this call - the pipeline uses
    it to skip re-classification/re-extraction of a duplicate document.
    """
    sha = sha256_file(path)
    already_ingested = db.get_document(sha) is not None

    sources_dir = repo_root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    archived_path = sources_dir / f"{sha}__{path.name}"
    if not archived_path.exists():
        shutil.copy2(path, archived_path)

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
    )

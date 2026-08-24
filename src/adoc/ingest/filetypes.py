"""Document type detection by content, never by filename alone (PLAN.md
docx ingestion: "real Dropbox drops contain .docx narrative documents").

`detect_doc_kind` is the single gate `ingest.archive.archive_document` uses
to decide whether a dropped file is a document a-doc can ingest at all:

- **pdf**: the file starts with the `%PDF-` magic bytes.
- **docx**: the file starts with the zip local-file-header magic (`PK\\x03\\x04`),
  has a `.docx` suffix, AND its zip central directory actually contains
  `[Content_Types].xml` - the cheapest reliable signal that this is a real
  OOXML package and not some other zip-based format a file was merely
  renamed to look like (see `test_archive_rejects_non_pdf_files`'s fake
  `PK...` header, which must still be rejected).
- anything else: `None` (unsupported) - callers raise `ArchiveError`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Literal

DocKind = Literal["pdf", "docx"]

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_CONTENT_TYPES_ENTRY = "[Content_Types].xml"


def _looks_like_docx_package(path: Path) -> bool:
    """True only if `path` is a real zip archive containing the OOXML
    `[Content_Types].xml` manifest - never raises on a malformed/fake zip,
    it just returns `False` (unsupported), matching `detect_doc_kind`'s
    "anything else -> None" contract."""
    try:
        with zipfile.ZipFile(path) as archive:
            return _DOCX_CONTENT_TYPES_ENTRY in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def detect_doc_kind(path: Path) -> DocKind | None:
    """Classify `path` by content, returning `None` for anything a-doc
    cannot ingest. Never raises - even an unreadable path just yields `None`
    so callers can produce one clear `ArchiveError` message."""
    try:
        with path.open("rb") as fh:
            header = fh.read(8)
    except OSError:
        return None

    if header.startswith(_PDF_MAGIC):
        return "pdf"

    if header.startswith(_ZIP_MAGIC) and path.suffix.lower() == ".docx":
        if _looks_like_docx_package(path):
            return "docx"
        return None

    return None


__all__ = ["DocKind", "detect_doc_kind"]

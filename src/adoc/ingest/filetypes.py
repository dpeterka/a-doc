"""Intake type detection by content, never by filename alone (PLAN.md
docx ingestion: "real Dropbox drops contain .docx narrative documents";
this slice adds real-world genomic uploads and zip archives).

`detect_intake_kind` is the single gate `ingest.archive.archive_document`
(for document-shaped files) and `ingest.pipeline` (for everything,
including the types `archive_document` never sees) use to decide what a
dropped file actually is:

- **pdf**: the file starts with the `%PDF-` magic bytes.
- **docx**: the file starts with the zip local-file-header magic (`PK\\x03\\x04`),
  has a `.docx` suffix, AND its zip central directory actually contains
  `[Content_Types].xml` - the cheapest reliable signal that this is a real
  OOXML package and not some other zip-based format a file was merely
  renamed to look like (see `test_archive_rejects_non_pdf_files`'s fake
  `PK...` header, which must still be rejected). Checked BEFORE `zip` so a
  `.docx` is never misdetected as a plain zip archive to expand.
- **genomic**: a file a-doc archives byte-for-byte and NEVER reads for
  document content (CRITICAL DESIGN RULE - see `ingest.genomics`): a
  23andMe-style raw text export (a leading `# `-commented block followed
  by a `rsid\\t`/`# rsid` header line, sniffed from the first ~2KB only), a
  BGZF/gzip-compressed file with a `.bcf`/`.vcf.gz`/`.bam`/`.fastq.gz`/
  `.fq.gz` suffix, a plain `.vcf` starting with the `##fileformat=VCF`
  header, or a plain FASTQ (`.fastq`/`.fq`, starting with `@`).
- **zip**: the zip local-file-header magic with a `.zip` suffix (a `.docx`
  is excluded first, above).
- **text**: a `.txt`/`.md` file that is not genomic. Not size-capped here -
  detection is a pure classification step; `ingest.archive.archive_document`
  is where an oversized text file gets rejected with a clear reason (never
  silently truncated).
- anything else: `None` (unsupported) - callers raise a clear error.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Literal

IntakeKind = Literal["pdf", "docx", "text", "genomic", "zip"]

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"
_VCF_HEADER = b"##fileformat=VCF"
_DOCX_CONTENT_TYPES_ENTRY = "[Content_Types].xml"

_GENOMIC_TEXT_SNIFF_BYTES = 2048
_RSID_HEADER_RE = re.compile(r"^#?\s*rsid\t", re.IGNORECASE)

# BGZF (a block-gzip'd BAM/BCF/VCF.gz) is still plain gzip-magic at the
# start of the stream - the suffix is what tells a-doc it's genomic rather
# than some other gzip-compressed drop.
_GENOMIC_GZ_SUFFIXES = (".bcf", ".vcf.gz", ".bam", ".fastq.gz", ".fq.gz")
_GENOMIC_PLAIN_FASTQ_SUFFIXES = (".fastq", ".fq")


def _looks_like_docx_package(path: Path) -> bool:
    """True only if `path` is a real zip archive containing the OOXML
    `[Content_Types].xml` manifest - never raises on a malformed/fake zip,
    it just returns `False` (unsupported), matching `detect_intake_kind`'s
    "anything else -> None" contract."""
    try:
        with zipfile.ZipFile(path) as archive:
            return _DOCX_CONTENT_TYPES_ENTRY in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _looks_like_plain_vcf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(len(_VCF_HEADER))
    except OSError:
        return False
    return head == _VCF_HEADER


def _looks_like_23andme_text(path: Path) -> bool:
    """Sniff only the first ~2KB (real exports run to ~17MB - never read
    the whole file just to classify it): a leading block of `#`-commented
    lines, followed by a header line matching `rsid\\t...` or
    `# rsid\\t...`. A header line reached with no prior comment line, or
    any other non-comment content before the header is found, is not this
    format."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_GENOMIC_TEXT_SNIFF_BYTES)
    except OSError:
        return False
    text = head.decode("utf-8", errors="replace")

    saw_comment = False
    for raw_line in text.splitlines():
        line = raw_line.strip("\r")
        if not line.strip():
            continue
        if _RSID_HEADER_RE.match(line):
            return saw_comment
        if line.startswith("#"):
            saw_comment = True
            continue
        return False
    return False


def _has_genomic_gz_suffix(name_lower: str) -> bool:
    return name_lower.endswith(_GENOMIC_GZ_SUFFIXES)


def detect_intake_kind(path: Path) -> IntakeKind | None:
    """Classify `path` by content, returning `None` for anything a-doc
    cannot ingest at all. Never raises - even an unreadable path just
    yields `None` so callers can produce one clear error message."""
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError:
        return None

    suffix = path.suffix.lower()
    name_lower = path.name.lower()

    if header.startswith(_PDF_MAGIC):
        return "pdf"

    if header.startswith(_ZIP_MAGIC) and suffix == ".docx":
        return "docx" if _looks_like_docx_package(path) else None

    # --- genomic (CRITICAL DESIGN RULE: never enters the LLM document
    # pipeline - checked before the generic zip/text branches below so a
    # 23andMe .txt export or a .vcf.gz never falls through to "text"/"zip"). ---
    if header[:2] == _GZIP_MAGIC and _has_genomic_gz_suffix(name_lower):
        return "genomic"
    if suffix == ".vcf" and _looks_like_plain_vcf(path):
        return "genomic"
    if suffix in _GENOMIC_PLAIN_FASTQ_SUFFIXES and header[:1] == b"@":
        return "genomic"
    if suffix == ".txt" and _looks_like_23andme_text(path):
        return "genomic"

    if header.startswith(_ZIP_MAGIC) and suffix == ".zip":
        return "zip"

    if suffix in (".txt", ".md"):
        return "text"

    return None


__all__ = ["IntakeKind", "detect_intake_kind"]

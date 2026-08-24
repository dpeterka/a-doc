"""Genomic data intake (CRITICAL DESIGN RULE, genomics/filetypes task
spec): genomic data NEVER enters the LLM document pipeline - no vision or
text extraction call is ever made for a file `ingest.filetypes.detect_intake_kind`
classifies as `"genomic"`.

Real-world shape this exists for: a 23andMe v5 raw text export (~17MB), and
one BGZF-compressed `.bcf` file per chromosome from an imputation service
(up to ~34MB each - up to ~400MB across a full set plus the export, often
dropped as a single `.zip`). These are archived byte-for-byte into a
dedicated `sources/genomics/<sha>__<origname>` subtree that
`casefile.repo.DataRepo`'s `.gitignore` excludes from the data repo's git
history entirely (see `_ensure_gitignore_excludes_genomics` below) - a
patient's raw genotype files must never bloat the data repo's git bundle,
which is otherwise a handful of small markdown/YAML files and PDFs.

`sources/genomics/` is NOT excluded from `adoc backup`'s S3 sync
(`backup.py`'s `_sync_sources` walks the whole `sources/` tree on disk, not
git-tracked paths) - the bytes are still backed up to S3, just never
committed to git. See `backup.py`'s module docstring / this task's report
for the explicit confirmation.

No per-file encounter is created for a genomic file - 25 imputed `.bcf`
files would otherwise become 25 junk encounters with nothing for a human
or the reasoner to read. Instead, every ingested genomic file is folded
into ONE regenerated summary artifact the reasoner can see,
`case/genomics-inventory.md` (`regenerate_inventory`) - a table of every
archived genomic file plus a fixed paragraph on what genotype data on file
enables (and does not, yet).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from adoc.ingest.archive import sha256_file
from adoc.labs.db import LabsDb

GENOMIC_DOC_TYPE = "genomic_data"
"""`documents.doc_type` value for an ingested genomic file. Note: unlike
`labs.extraction_status`/`documents.status`, `documents.doc_type` has no
CHECK constraint in the schema (`labs/db.py`'s DDL) - any string is a valid
value already, so introducing this one needed no schema migration (see the
task report's "deviations" section for why)."""

GENOMICS_SOURCES_RELDIR = "sources/genomics"
GENOMICS_INVENTORY_RELPATH = "case/genomics-inventory.md"

_GENOMICS_GITIGNORE_LINE = "sources/genomics/"

_INVENTORY_EXPLAINER = (
    "Genotype data is on file for this patient. This enables genomic "
    "analysis (e.g. checking specific variants against phenotype-driven "
    "hypotheses) as a later phase of work - it is archived here, "
    "untouched, so nothing is lost in the meantime. These files are never "
    "read as documents: no vision or text extraction call is ever made "
    "against them, and none of their content reaches any model.\n"
)

_CHR_RE = re.compile(r"chr(?:omosome)?[_.\-]?(\d{1,2}|x|y|m|mt)\b", re.IGNORECASE)


@dataclass(frozen=True)
class GenomicArchiveResult:
    sha256: str
    archived_path: Path
    """The archived immutable copy: `sources/genomics/<sha>__<origname>`."""
    already_ingested: bool
    """True if `sha256` already had a `documents` row before this call."""


def _ensure_gitignore_excludes_genomics(repo_root: Path) -> None:
    """Lazily append `sources/genomics/` to `.gitignore` if it isn't there
    yet - covers a data repo initialized by an older `DataRepo.init_at`
    (before this slice added the line to its template) that is now
    ingesting its first genomic file. Idempotent; a no-op once the line is
    present."""
    repo_root.mkdir(parents=True, exist_ok=True)
    gitignore_path = repo_root / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    if _GENOMICS_GITIGNORE_LINE in existing.splitlines():
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    gitignore_path.write_text(existing + _GENOMICS_GITIGNORE_LINE + "\n", encoding="utf-8")


def archive_genomic_file(repo_root: Path, path: Path, *, db: LabsDb) -> GenomicArchiveResult:
    """Archive `path` byte-for-byte into `repo_root/sources/genomics/` -
    the genomic analogue of `ingest.archive.archive_document`, but never
    calling it: no page rendering, no LLM classification, a different
    (gitignored) destination directory. Idempotent/deduping the same way:
    re-archiving identical bytes is a no-op copy, and `already_ingested`
    reflects whether `LabsDb.documents` already had a row for this sha
    *before* this call.
    """
    _ensure_gitignore_excludes_genomics(repo_root)

    sha = sha256_file(path)
    already_ingested = db.get_document(sha) is not None

    genomics_dir = repo_root / GENOMICS_SOURCES_RELDIR
    genomics_dir.mkdir(parents=True, exist_ok=True)
    archived_path = genomics_dir / f"{sha}__{path.name}"
    if not archived_path.exists():
        shutil.copy2(path, archived_path)

    return GenomicArchiveResult(
        sha256=sha, archived_path=archived_path, already_ingested=already_ingested
    )


def guess_genomic_kind(filename: str) -> str:
    """A short human-readable guess at what kind of genomic file this is,
    for the inventory table - never authoritative, just a filename-based
    label (no file content is parsed beyond what `detect_intake_kind`
    already sniffed to call this genomic in the first place)."""
    lower = filename.lower()
    if lower.endswith(".bcf"):
        chrom = _guess_chromosome(filename)
        return f"imputed BCF chr{chrom}" if chrom else "imputed BCF"
    if lower.endswith(".vcf.gz") or lower.endswith(".vcf"):
        return "VCF"
    if lower.endswith(".bam"):
        return "aligned reads (BAM)"
    if lower.endswith((".fastq.gz", ".fq.gz", ".fastq", ".fq")):
        return "raw sequencing reads (FASTQ)"
    if lower.endswith(".txt"):
        return "23andMe raw export"
    return "genomic data file"


def _guess_chromosome(filename: str) -> str | None:
    match = _CHR_RE.search(filename)
    return match.group(1).upper() if match else None


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - defensive, unreachable given the loop above


def regenerate_inventory(repo_root: Path, db: LabsDb) -> Path:
    """Rewrite `case/genomics-inventory.md` from scratch from every
    `documents` row with `doc_type == GENOMIC_DOC_TYPE`, sorted by
    filename for a deterministic diff. Called after every genomic file is
    archived (whole-file regeneration, not an append - the task spec's
    "ONE summary artifact ... regenerated each ingest"). Returns the
    written path.
    """
    docs = sorted(
        (doc for doc in db.list_documents() if doc.doc_type == GENOMIC_DOC_TYPE),
        key=lambda doc: doc.filename,
    )

    lines = [
        "# Genomic Data Inventory\n",
        "\n",
        "| File | Kind | Size | SHA (short) |\n",
        "|---|---|---|---|\n",
    ]
    for doc in docs:
        archived_path = repo_root / GENOMICS_SOURCES_RELDIR / f"{doc.sha256}__{doc.filename}"
        size = archived_path.stat().st_size if archived_path.is_file() else 0
        lines.append(
            f"| {doc.filename} | {guess_genomic_kind(doc.filename)} | "
            f"{_format_size(size)} | {doc.sha256[:8]} |\n"
        )
    lines.append("\n")
    lines.append(_INVENTORY_EXPLAINER)

    inventory_path = repo_root / GENOMICS_INVENTORY_RELPATH
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text("".join(lines), encoding="utf-8")
    return inventory_path


__all__ = [
    "GENOMIC_DOC_TYPE",
    "GENOMICS_INVENTORY_RELPATH",
    "GENOMICS_SOURCES_RELDIR",
    "GenomicArchiveResult",
    "archive_genomic_file",
    "guess_genomic_kind",
    "regenerate_inventory",
]

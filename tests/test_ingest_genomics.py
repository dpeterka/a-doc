"""Tests for adoc.ingest.genomics: byte-for-byte archival into
`sources/genomics/` (gitignored), and the regenerated
`case/genomics-inventory.md` summary artifact (genomics/filetypes task
item 2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from git import Repo

from adoc.casefile.repo import DataRepo
from adoc.ingest.genomics import (
    GENOMIC_DOC_TYPE,
    GENOMICS_INVENTORY_RELPATH,
    GENOMICS_SOURCES_RELDIR,
    archive_genomic_file,
    guess_genomic_kind,
    regenerate_inventory,
)
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, LabDocument

BGZF_MAGIC = bytes.fromhex("1f8b0804")


def test_archive_genomic_file_copies_bytes_into_sources_genomics(tmp_path: Path) -> None:
    repo_root = tmp_path / "data-repo"
    db = LabsDb(tmp_path / "labs.sqlite")
    src = tmp_path / "chr1.imputed.bcf"
    src.write_bytes(BGZF_MAGIC + b"\x00" * 64)

    archived = archive_genomic_file(repo_root, src, db=db)

    assert archived.archived_path.exists()
    assert archived.archived_path.read_bytes() == src.read_bytes()
    assert archived.archived_path.parent == repo_root / GENOMICS_SOURCES_RELDIR
    assert archived.archived_path.name == f"{archived.sha256}__chr1.imputed.bcf"
    assert archived.already_ingested is False


def test_archive_genomic_file_is_idempotent_and_dedupes(tmp_path: Path) -> None:
    repo_root = tmp_path / "data-repo"
    db = LabsDb(tmp_path / "labs.sqlite")
    src = tmp_path / "chr1.imputed.bcf"
    src.write_bytes(BGZF_MAGIC + b"\x00" * 64)

    first = archive_genomic_file(repo_root, src, db=db)
    db.upsert_document(
        LabDocument(
            sha256=first.sha256,
            filename="chr1.imputed.bcf",
            doc_type=GENOMIC_DOC_TYPE,
            page_count=1,
            ingested_at=datetime(2026, 8, 1, tzinfo=UTC),
            status=DocumentStatus.COMPLETE,
        )
    )
    second = archive_genomic_file(repo_root, src, db=db)

    assert first.sha256 == second.sha256
    assert second.already_ingested is True


def test_archive_genomic_file_never_enters_git_history(tmp_path: Path) -> None:
    """`sources/genomics/` must be excluded from the data repo's git
    history (438MB of genotypes must never enter a git bundle) - a fresh
    `DataRepo.init_at` already gitignores it; committing "everything" after
    archiving a genomic file must leave it untracked."""
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    src = tmp_path / "chr1.imputed.bcf"
    src.write_bytes(BGZF_MAGIC + b"\x00" * 64)

    archive_genomic_file(repo.root, src, db=db)
    repo.commit("chore: test commit", paths=None)  # stage everything, like `git add -A`

    git_repo = Repo(repo.root)
    status = git_repo.git.status("--porcelain", "--ignored")
    assert "sources/genomics/" in (repo.root / ".gitignore").read_text(encoding="utf-8")
    # The whole `sources/genomics/` directory is reported ignored by git
    # itself (not merely never-`git add`ed), and the archived file is
    # never a tracked blob.
    assert "!! sources/genomics/" in status
    assert "chr1.imputed.bcf" not in git_repo.git.ls_files()

    archived_files = list((repo.root / "sources" / "genomics").glob("*__chr1.imputed.bcf"))
    assert len(archived_files) == 1
    assert archived_files[0].exists()


def test_archive_genomic_file_lazily_appends_gitignore_line_for_an_older_repo(
    tmp_path: Path,
) -> None:
    """A data repo initialized before this slice existed has no
    `sources/genomics/` line in its `.gitignore` - the first genomic
    ingest against it must add the line rather than silently risking a
    later `git add -A` sweeping the file in."""
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    # Simulate an older repo: strip the genomics line back out.
    gitignore_path = repo.root / ".gitignore"
    gitignore_path.write_text(
        gitignore_path.read_text(encoding="utf-8").replace("sources/genomics/\n", ""),
        encoding="utf-8",
    )
    assert "sources/genomics/" not in gitignore_path.read_text(encoding="utf-8")

    src = tmp_path / "chr1.imputed.bcf"
    src.write_bytes(BGZF_MAGIC + b"\x00" * 64)
    archive_genomic_file(repo.root, src, db=db)

    assert "sources/genomics/" in gitignore_path.read_text(encoding="utf-8").splitlines()


def _upsert_genomic_doc(db: LabsDb, *, sha: str, filename: str) -> None:
    db.upsert_document(
        LabDocument(
            sha256=sha,
            filename=filename,
            doc_type=GENOMIC_DOC_TYPE,
            page_count=1,
            ingested_at=datetime(2026, 8, 1, tzinfo=UTC),
            status=DocumentStatus.COMPLETE,
        )
    )


def test_regenerate_inventory_lists_every_genomic_document_sorted_by_filename(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "data-repo"
    db = LabsDb(tmp_path / "labs.sqlite")

    for filename in ("chr2.imputed.bcf", "chr1.imputed.bcf"):
        src = tmp_path / filename
        # Distinct content per file - two identical byte-strings would
        # share a sha256 and dedupe as the "same" genomic file.
        src.write_bytes(BGZF_MAGIC + filename.encode() + b"\x00" * 64)
        archived = archive_genomic_file(repo_root, src, db=db)
        _upsert_genomic_doc(db, sha=archived.sha256, filename=filename)

    path = regenerate_inventory(repo_root, db)

    assert path == repo_root / GENOMICS_INVENTORY_RELPATH
    content = path.read_text(encoding="utf-8")
    assert content.index("chr1.imputed.bcf") < content.index("chr2.imputed.bcf")
    assert "imputed BCF chr1" in content
    assert "imputed BCF chr2" in content
    assert "genomic analysis" in content
    assert "never read as documents" in content or "no vision or text extraction" in content


def test_regenerate_inventory_is_a_full_rewrite_not_an_append(tmp_path: Path) -> None:
    """Two ingests must produce ONE inventory listing BOTH files - not two
    separate entries appended, and not overwritten to show only the
    latest."""
    repo_root = tmp_path / "data-repo"
    db = LabsDb(tmp_path / "labs.sqlite")

    src1 = tmp_path / "genome_First_Last.txt"
    src1.write_text("# 23andMe\n# rsid\tchromosome\tposition\tgenotype\nrs1\t1\t1\tAA\n")
    archived1 = archive_genomic_file(repo_root, src1, db=db)
    _upsert_genomic_doc(db, sha=archived1.sha256, filename="genome_First_Last.txt")
    regenerate_inventory(repo_root, db)

    src2 = tmp_path / "chr1.imputed.bcf"
    src2.write_bytes(BGZF_MAGIC + b"\x00" * 64)
    archived2 = archive_genomic_file(repo_root, src2, db=db)
    _upsert_genomic_doc(db, sha=archived2.sha256, filename="chr1.imputed.bcf")
    content = regenerate_inventory(repo_root, db).read_text(encoding="utf-8")

    assert "genome_First_Last.txt" in content
    assert "chr1.imputed.bcf" in content
    # Exactly one occurrence each - not accumulated duplicate rows.
    assert content.count("genome_First_Last.txt") == 1
    assert content.count("chr1.imputed.bcf") == 1


def test_guess_genomic_kind_labels() -> None:
    assert guess_genomic_kind("chr1.imputed.bcf") == "imputed BCF chr1"
    assert guess_genomic_kind("sample.vcf.gz") == "VCF"
    assert guess_genomic_kind("genome_First_Last.txt") == "23andMe raw export"
    assert guess_genomic_kind("reads.fastq") == "raw sequencing reads (FASTQ)"
    assert guess_genomic_kind("aligned.bam") == "aligned reads (BAM)"

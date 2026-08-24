"""Document ingestion pipeline (PLAN.md "Ingestion", session loop (a)).

`ingest_file`/`ingest_inbox` wire together: dedupe -> archive -> classify
(`classifier` role, first page) -> lab_report: double-pass extract ->
reconcile -> `LabsDb.insert_results` + `labs-export.jsonl` export;
non-lab document: an encounter stub (no LLM summary in this slice - a
later slice wires the incremental reasoning pass) -> exactly one
`DataRepo.commit` per document.

This module deliberately does NOT touch `casefile.ledger` or `reason.dag`:
per the task scope, incremental reasoning over newly-ingested rows is a
later slice.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from adoc.casefile.encounters import (
    Encounter,
    EncounterFrontmatter,
    EncounterType,
    write_encounter,
)
from adoc.casefile.repo import DataRepo
from adoc.ingest.archive import ArchiveError, PageRenderer, archive_document, pdftoppm_renderer
from adoc.ingest.extract import double_pass_extract
from adoc.ingest.reconcile import ReconciledRow, parse_flag, parse_ref_range, reconcile
from adoc.ingest.schema import ClassifyResult, DocType, DocumentExtraction
from adoc.ingest.vision import ImagePart, VisionClient, VisionError
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult

CLASSIFY_PROMPT_VERSION = "classifier-v1"
CLASSIFY_PROMPT = f"""[{CLASSIFY_PROMPT_VERSION}]
Look at this single page image - the first page of a medical document -
and classify the document as one of: lab_report (a lab/blood-work results
report), clinical_note (a doctor/specialist visit note or letter),
imaging_report (a radiology/imaging report), or other. Also give your best
guess at the document's date (the report date or visit date shown on the
page) as doc_date, or omit it if no date is visible on this page.
"""

_ENCOUNTER_TYPE_BY_DOC_TYPE: dict[str, EncounterType] = {
    "clinical_note": "specialist-visit",
    "imaging_report": "imaging",
    "other": "specialist-visit",
}

FileOutcomeKind = Literal["ingested", "duplicate", "error"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FileOutcome(BaseModel):
    """The result of ingesting one document."""

    path: str
    sha256: str | None = None
    outcome: FileOutcomeKind
    doc_type: DocType | None = None
    rows_auto: int = 0
    rows_pending: int = 0
    commit_sha: str | None = None
    issues: list[str] = Field(default_factory=list)


class IngestReport(BaseModel):
    """One or more `FileOutcome`s from an `ingest_file`/`ingest_inbox` run."""

    files: list[FileOutcome] = Field(default_factory=list)

    @property
    def total_auto(self) -> int:
        return sum(f.rows_auto for f in self.files)

    @property
    def total_pending(self) -> int:
        return sum(f.rows_pending for f in self.files)


def _to_lab_result(row: ReconciledRow, *, source_doc: str) -> LabResult:
    ref_low, ref_high = parse_ref_range(row.ref_range_raw)
    status = ExtractionStatus.AUTO if row.status == "auto" else ExtractionStatus.PENDING
    return LabResult(
        date=row.date,
        name=row.canonical_name or row.name_raw,
        name_raw=row.name_raw,
        value=row.value,
        value_text=row.value_text,
        ucum_unit=row.unit_raw,
        ref_low=ref_low,
        ref_high=ref_high,
        ref_text=row.ref_range_raw,
        flag=parse_flag(row.flag_raw),
        source_doc=source_doc,
        source_page=row.source_page,
        extraction_status=status,
        raw_json=row.raw_json,
    )


def _commit_message(*, label: str, doc_date: date | None, rows_auto: int, rows_pending: int) -> str:
    date_part = doc_date.isoformat() if doc_date else "undated"
    return f"ingest: {label} {date_part} ({rows_auto} rows, {rows_pending} queued)"


def _ingest_lab_report(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    clock: Callable[[], datetime],
    doc_date: date | None,
    pass_a: DocumentExtraction,
    pass_b: DocumentExtraction,
    sha256: str,
    page_count: int,
) -> FileOutcome:
    reconciled = reconcile(pass_a, pass_b, db)

    lab_rows: list[LabResult] = []
    issues: list[str] = []
    for row in reconciled:
        try:
            lab_rows.append(_to_lab_result(row, source_doc=sha256))
        except ValueError as exc:
            issues.append(f"{row.name_raw}: could not build a lab row: {exc}")

    # `labs.source_doc` is a foreign key into `documents.sha256` - the
    # document row must exist before any lab row referencing it is inserted.
    db.upsert_document(
        LabDocument(
            sha256=sha256,
            filename=path.name,
            doc_type="lab_report",
            doc_date=doc_date,
            page_count=page_count,
            ingested_at=clock(),
            status=DocumentStatus.COMPLETE,
        )
    )
    db.insert_results(lab_rows)
    db.export_jsonl(repo.root / "labs-export.jsonl")

    rows_auto = sum(1 for row in reconciled if row.status == "auto")
    rows_pending = sum(1 for row in reconciled if row.status == "pending")
    issues.extend(reason for row in reconciled if row.status == "pending" for reason in row.reasons)

    facility = pass_a.facility or pass_b.facility or path.name
    message = _commit_message(
        label=facility, doc_date=doc_date, rows_auto=rows_auto, rows_pending=rows_pending
    )
    commit_sha = repo.commit(message, paths=["sources", "labs-export.jsonl"])

    return FileOutcome(
        path=str(path),
        sha256=sha256,
        outcome="ingested",
        doc_type="lab_report",
        rows_auto=rows_auto,
        rows_pending=rows_pending,
        commit_sha=commit_sha,
        issues=issues,
    )


def _ingest_non_lab(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    clock: Callable[[], datetime],
    doc_date: date,
    doc_type: DocType,
    sha256: str,
    archived_name: str,
    page_count: int,
) -> FileOutcome:
    encounter = Encounter(
        frontmatter=EncounterFrontmatter(
            date=doc_date,
            type=_ENCOUNTER_TYPE_BY_DOC_TYPE.get(doc_type, "specialist-visit"),
            sources=[archived_name],
        ),
        summary="(pending review)",
    )
    write_encounter(repo.root / "case" / "encounters", encounter, slug=path.stem)

    db.upsert_document(
        LabDocument(
            sha256=sha256,
            filename=path.name,
            doc_type=doc_type,
            doc_date=doc_date,
            page_count=page_count,
            ingested_at=clock(),
            status=DocumentStatus.NEEDS_REVIEW,
        )
    )

    message = _commit_message(label=path.name, doc_date=doc_date, rows_auto=0, rows_pending=0)
    commit_sha = repo.commit(message, paths=["sources", "case/encounters"])

    return FileOutcome(
        path=str(path),
        sha256=sha256,
        outcome="ingested",
        doc_type=doc_type,
        commit_sha=commit_sha,
    )


def _ingest_one(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime],
    renderer: PageRenderer,
) -> FileOutcome:
    try:
        archived = archive_document(repo.root, path, db=db, renderer=renderer)
    except ArchiveError as exc:
        return FileOutcome(path=str(path), outcome="error", issues=[str(exc)])

    if archived.already_ingested:
        return FileOutcome(path=str(path), sha256=archived.sha256, outcome="duplicate")

    try:
        classify = vision.extract(
            "classifier",
            system=CLASSIFY_PROMPT,
            parts=[ImagePart(data=archived.page_paths[0].read_bytes(), page=1)],
            schema=ClassifyResult,
        )
    except VisionError as exc:
        return FileOutcome(
            path=str(path), sha256=archived.sha256, outcome="error", issues=[str(exc)]
        )

    doc_date = classify.doc_date or clock().date()
    page_count = len(archived.page_paths)

    if classify.doc_type == "lab_report":
        try:
            pass_a, pass_b = double_pass_extract(vision, archived)
        except VisionError as exc:
            return FileOutcome(
                path=str(path), sha256=archived.sha256, outcome="error", issues=[str(exc)]
            )
        return _ingest_lab_report(
            path,
            repo=repo,
            db=db,
            clock=clock,
            doc_date=doc_date,
            pass_a=pass_a,
            pass_b=pass_b,
            sha256=archived.sha256,
            page_count=page_count,
        )

    return _ingest_non_lab(
        path,
        repo=repo,
        db=db,
        clock=clock,
        doc_date=doc_date,
        doc_type=classify.doc_type,
        sha256=archived.sha256,
        archived_name=archived.original_path.name,
        page_count=page_count,
    )


def ingest_file(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime] = _utcnow,
    renderer: PageRenderer = pdftoppm_renderer,
) -> IngestReport:
    """Ingest a single document. Always one `DataRepo.commit` unless the
    document is a duplicate (nothing changed) or archival/extraction failed.

    `renderer` is forwarded to `archive.archive_document` - tests inject a
    fake `PageRenderer` so this never depends on `pdftoppm` being installed.
    """
    return IngestReport(
        files=[_ingest_one(path, repo=repo, db=db, vision=vision, clock=clock, renderer=renderer)]
    )


def _scan_files(root: Path) -> list[Path]:
    """All regular files under `root`, recursive, name-ordered.

    Recursive because Dropbox uploads (and `rclone move`) preserve
    subfolder structure inside the inbox. Hidden files and Windows
    sync artifacts are skipped.
    """
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.name.lower() != "desktop.ini"
    )


def ingest_inbox(
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime] = _utcnow,
    renderer: PageRenderer = pdftoppm_renderer,
) -> IngestReport:
    """Scan `repo.root/inbox/` and ingest every file found there, in name order."""
    inbox = repo.root / "inbox"
    if not inbox.is_dir():
        return IngestReport(files=[])
    files = _scan_files(inbox)
    return IngestReport(
        files=[
            _ingest_one(p, repo=repo, db=db, vision=vision, clock=clock, renderer=renderer)
            for p in files
        ]
    )


def ingest_directory(
    directory: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime] = _utcnow,
    renderer: PageRenderer = pdftoppm_renderer,
) -> IngestReport:
    """Ingest every file directly under `directory`, in name order (`adoc backfill`)."""
    files = _scan_files(directory)
    return IngestReport(
        files=[
            _ingest_one(p, repo=repo, db=db, vision=vision, clock=clock, renderer=renderer)
            for p in files
        ]
    )


__all__ = [
    "FileOutcome",
    "IngestReport",
    "ingest_directory",
    "ingest_file",
    "ingest_inbox",
]

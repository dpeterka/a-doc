"""Document ingestion pipeline (PLAN.md "Ingestion", session loop (a)).

`ingest_file`/`ingest_inbox` wire together: dedupe -> archive -> route on
`ArchivedDoc.kind`.

**PDF** (unchanged): classify (`classifier` role, first page image) ->
lab_report: vision double-pass extract -> reconcile -> `LabsDb.insert_results`
+ `labs-export.jsonl` export; non-lab document: an encounter stub.

**docx** (PLAN.md docx ingestion design decision: docx = TEXT documents):
`ingest.docx.extract_docx_text` -> classify over the text (`classifier`
role, `LlmClient.complete`, no vision) -> lab_report: TEXT double-pass
(`extract.double_pass_extract_text`) -> the SAME reconcile/insert/export
path as a PDF lab report; else (narrative/other/imaging): an encounter
whose body carries the FULL extracted text under a `## Extracted text`
section - PLAN.md's context pack needs the full narrative, not a summary.

Either way, exactly one `DataRepo.commit` per document. This module
deliberately does NOT touch `casefile.ledger` or `reason.dag`: per the task
scope, incremental reasoning over newly-ingested rows is a later slice.

**Post-ingest inbox hygiene** applies ONLY to files a caller identifies as
owned by the data repo's `inbox/`, via `inbox_root=` (see `_ingest_one`):
once a file's `FileOutcome` is known, an `ingested`/`duplicate` outcome
deletes the inbox copy (the immutable `sources/` archive - or, for a
duplicate, the archive from the *first* ingest - is authoritative; nothing
is lost), and an `error` outcome moves the file to `work/failed/` and
records it in `work/failed/failures.jsonl` (see `ingest.failures`) so the
web `/failed` page can surface it. `ingest_inbox` passes its own `inbox/`
as `inbox_root`; `ingest_file` and `ingest_directory` default to
`inbox_root=None` (no hygiene) unless a caller opts in.

**Invariant**: `ingest_directory` (the engine behind `adoc backfill
<external dir>`) never passes `inbox_root` and so NEVER deletes or moves a
file a caller passed it, error or not - backfilling a patient's own photo
library or Dropbox export must never touch their files. Only
`ingest_inbox`, and any `ingest_file` caller that explicitly opts in (the
web upload route, ingesting a file it just wrote into `inbox/`), get
hygiene. See `test_ingest_pipeline.py`'s
`test_backfill_directory_never_deletes_or_moves_user_files_even_on_error`.
"""

from __future__ import annotations

import shutil
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
from adoc.ingest.archive import (
    ArchivedDoc,
    ArchiveError,
    PageRenderer,
    archive_document,
    pdftoppm_renderer,
)
from adoc.ingest.docx import DocxExtractionError, extract_docx_text
from adoc.ingest.extract import double_pass_extract, double_pass_extract_text
from adoc.ingest.failures import FAILED_DIR_RELPATH, FailureRecord, append_failure, flatten_relpath
from adoc.ingest.reconcile import ReconciledRow, parse_flag, parse_ref_range, reconcile
from adoc.ingest.schema import ClassifyResult, DocType, DocumentExtraction
from adoc.ingest.vision import ImagePart, VisionClient, VisionError
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.reason.client import LlmClient, LlmError, Message

CLASSIFY_PROMPT_VERSION = "classifier-v1"
CLASSIFY_PROMPT = f"""[{CLASSIFY_PROMPT_VERSION}]
Look at this single page image - the first page of a medical document -
and classify the document as one of: lab_report (a lab/blood-work results
report), clinical_note (a doctor/specialist visit note or letter),
imaging_report (a radiology/imaging report), or other. Also give your best
guess at the document's date (the report date or visit date shown on the
page) as doc_date, or omit it if no date is visible on this page.
"""

DOCX_CLASSIFY_PROMPT_VERSION = "classifier-docx-v1"
DOCX_CLASSIFY_PROMPT = f"""[{DOCX_CLASSIFY_PROMPT_VERSION}]
Read this document's full extracted text (paragraphs in reading order,
with any tables rendered as pipe-delimited rows) and classify the document
as one of: lab_report (a lab/blood-work results report), clinical_note (a
doctor/specialist visit note, or a narrative clinical history the patient
wrote themselves), imaging_report (a radiology/imaging report), or other
(e.g. a supplement plan or other narrative document). Also give your best
guess at the document's date (a report date or visit date mentioned in the
text) as doc_date, or omit it if no date is mentioned.
"""

_ENCOUNTER_TYPE_BY_DOC_TYPE: dict[str, EncounterType] = {
    "clinical_note": "specialist-visit",
    "imaging_report": "imaging",
    "other": "specialist-visit",
}

# docx narrative documents have no clinician letterhead/scan behind them -
# they are text the patient wrote or assembled themselves (clinical
# history, supplement plans), so PLAN.md's "same door as doctor notes,
# labeled" `patient-report` type fits better than `specialist-visit` for
# anything that isn't clearly imaging.
_DOCX_ENCOUNTER_TYPE_BY_DOC_TYPE: dict[str, EncounterType] = {
    "imaging_report": "imaging",
    "clinical_note": "patient-report",
    "other": "patient-report",
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
    encounter_type_map: dict[str, EncounterType],
    default_encounter_type: EncounterType,
    extracted_text: str = "",
) -> FileOutcome:
    encounter = Encounter(
        frontmatter=EncounterFrontmatter(
            date=doc_date,
            type=encounter_type_map.get(doc_type, default_encounter_type),
            sources=[archived_name],
        ),
        summary="(pending review)",
        extracted_text=extracted_text,
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


def _ingest_pdf(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime],
    archived: ArchivedDoc,
) -> FileOutcome:
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
        encounter_type_map=_ENCOUNTER_TYPE_BY_DOC_TYPE,
        default_encounter_type="specialist-visit",
    )


def _ingest_docx(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    client: LlmClient,
    clock: Callable[[], datetime],
    archived: ArchivedDoc,
) -> FileOutcome:
    """docx flow (PLAN.md docx ingestion): TEXT extraction -> text-based
    classify -> lab_report: TEXT double-pass into the same reconcile/
    insert/export gates a PDF lab report uses; else: a full-text encounter.
    """
    try:
        text = extract_docx_text(archived.original_path)
    except DocxExtractionError as exc:
        return FileOutcome(
            path=str(path), sha256=archived.sha256, outcome="error", issues=[str(exc)]
        )

    try:
        classify_result = client.complete(
            "classifier",
            system=DOCX_CLASSIFY_PROMPT,
            messages=[Message(role="user", content=text)],
            schema=ClassifyResult,
        )
    except LlmError as exc:
        return FileOutcome(
            path=str(path), sha256=archived.sha256, outcome="error", issues=[str(exc)]
        )
    classify = classify_result.parsed
    assert isinstance(classify, ClassifyResult)  # schema= guarantees this

    doc_date = classify.doc_date or clock().date()
    # A docx has no page structure - `LabDocument.page_count` still
    # requires >=1, so it's recorded as one logical unit.
    page_count = 1

    if classify.doc_type == "lab_report":
        try:
            pass_a, pass_b = double_pass_extract_text(client, text)
        except LlmError as exc:
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
        encounter_type_map=_DOCX_ENCOUNTER_TYPE_BY_DOC_TYPE,
        default_encounter_type="patient-report",
        extracted_text=text,
    )


def _apply_inbox_hygiene(
    path: Path,
    outcome: FileOutcome,
    *,
    repo: DataRepo,
    inbox_root: Path | None,
    clock: Callable[[], datetime],
) -> None:
    """Post-ingest inbox hygiene (module docstring): only fires when
    `inbox_root` is given AND `path` actually falls under it - `ingest_file`
    and `ingest_directory` default `inbox_root=None`, so nothing happens
    for them unless a caller opts in, and `ingest_directory` never opts in
    (the `adoc backfill <external dir>` invariant)."""
    if inbox_root is None:
        return
    try:
        rel = path.relative_to(inbox_root)
    except ValueError:
        # Defensive: `path` wasn't actually under `inbox_root` - never
        # guess at hygiene for a file we can't place relative to it.
        return

    if outcome.outcome in ("ingested", "duplicate"):
        path.unlink(missing_ok=True)
        return

    # outcome.outcome == "error"
    failed_dir = repo.root / FAILED_DIR_RELPATH
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / flatten_relpath(rel)
    shutil.move(str(path), str(dest))
    append_failure(
        repo,
        FailureRecord(
            filename=path.name,
            failed_at=clock(),
            reason=outcome.issues[0] if outcome.issues else "unknown error",
            original_inbox_path=str(rel),
        ),
    )


def _ingest_one(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime],
    renderer: PageRenderer,
    inbox_root: Path | None = None,
) -> FileOutcome:
    try:
        archived = archive_document(repo.root, path, db=db, renderer=renderer)
    except ArchiveError as exc:
        outcome = FileOutcome(path=str(path), outcome="error", issues=[str(exc)])
        _apply_inbox_hygiene(path, outcome, repo=repo, inbox_root=inbox_root, clock=clock)
        return outcome

    if archived.already_ingested:
        outcome = FileOutcome(path=str(path), sha256=archived.sha256, outcome="duplicate")
        _apply_inbox_hygiene(path, outcome, repo=repo, inbox_root=inbox_root, clock=clock)
        return outcome

    if archived.kind == "docx":
        outcome = _ingest_docx(
            path, repo=repo, db=db, client=vision.client, clock=clock, archived=archived
        )
    else:
        outcome = _ingest_pdf(path, repo=repo, db=db, vision=vision, clock=clock, archived=archived)

    _apply_inbox_hygiene(path, outcome, repo=repo, inbox_root=inbox_root, clock=clock)
    return outcome


def ingest_file(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime] = _utcnow,
    renderer: PageRenderer = pdftoppm_renderer,
    inbox_root: Path | None = None,
) -> IngestReport:
    """Ingest a single document. Always one `DataRepo.commit` unless the
    document is a duplicate (nothing changed) or archival/extraction failed.

    `renderer` is forwarded to `archive.archive_document` - tests inject a
    fake `PageRenderer` so this never depends on `pdftoppm` being installed.

    `inbox_root`, if given, opts `path` into post-ingest inbox hygiene (see
    the module docstring) - the web upload route passes the `inbox/` dir it
    just wrote `path` into; callers ingesting a file that isn't the
    patient's own inbox copy (e.g. a test fixture, or a retry that already
    moved the file back into `inbox/` itself) pass it deliberately, and
    every other caller leaves it `None` so nothing is ever deleted or moved
    by surprise.
    """
    return IngestReport(
        files=[
            _ingest_one(
                path,
                repo=repo,
                db=db,
                vision=vision,
                clock=clock,
                renderer=renderer,
                inbox_root=inbox_root,
            )
        ]
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
    """Scan `repo.root/inbox/` and ingest every file found there, in name
    order. Every file scanned here IS the patient's own inbox copy, so each
    one is opted into post-ingest inbox hygiene (module docstring):
    ingested/duplicate deletes it, error moves it to `work/failed/`.
    """
    inbox = repo.root / "inbox"
    if not inbox.is_dir():
        return IngestReport(files=[])
    files = _scan_files(inbox)
    return IngestReport(
        files=[
            _ingest_one(
                p, repo=repo, db=db, vision=vision, clock=clock, renderer=renderer, inbox_root=inbox
            )
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
    """Ingest every file directly under `directory`, in name order (`adoc
    backfill <directory>`). Deliberately never passes `inbox_root` to
    `_ingest_one` - see the module docstring's invariant: a backfilled
    directory is the patient's own external file store, and this must
    never delete or move a file in it, whatever the outcome.
    """
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

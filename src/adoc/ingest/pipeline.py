"""Document ingestion pipeline (PLAN.md "Ingestion", session loop (a)).

`ingest_file`/`ingest_inbox` wire together: dedupe -> archive -> route on
kind (`ingest.filetypes.detect_intake_kind`, checked *before*
`archive_document` so `"genomic"`/`"zip"` never reach it - see `_ingest_one`).

**PDF** (unchanged): classify (`classifier` role, first page image) ->
lab_report: vision double-pass extract -> reconcile -> `LabsDb.insert_results`
+ `labs-export.jsonl` export; non-lab document: an encounter stub.

**docx / text** (PLAN.md docx ingestion design decision: docx = TEXT
documents; plain `.txt`/`.md` documents follow the identical path -
`_ingest_text_like` is shared by both): get the document's full text
(`ingest.docx.extract_docx_text` for docx; `path.read_text(errors=
"replace")` for plain text - see `_ingest_text`) -> classify over the text
(`classifier` role, `LlmClient.complete`, no vision) -> lab_report: TEXT
double-pass (`extract.double_pass_extract_text`) -> the SAME reconcile/
insert/export path as a PDF lab report; else (narrative/other/imaging): an
encounter whose body carries the FULL extracted text under a `##
Extracted text` section - PLAN.md's context pack needs the full narrative,
not a summary.

**genomic** (CRITICAL DESIGN RULE: never enters the LLM document pipeline
- no vision/text extraction call, ever): routed to `ingest.genomics`
instead of `archive_document` - archived into `sources/genomics/`
(gitignored), registered as one `documents` row (`doc_type=
"genomic_data"`), and folded into the single regenerated
`case/genomics-inventory.md` rather than a per-file encounter. See
`_ingest_genomic` and `ingest.genomics`'s module docstring.

**zip**: expanded, not archived (the zip itself is a container, never a
document - see `_ingest_zip`); each member is routed through
`detect_intake_kind` and ingested exactly as if it were its own inbox
file (same hygiene/dedupe, one flattened `FileOutcome` per member in the
report). Depth 1 only - a member that is itself a zip is rejected, not
recursed into. Capped at `MAX_ZIP_MEMBERS` members and
`MAX_ZIP_UNCOMPRESSED_BYTES` total uncompressed size; every member path is
checked for traversal (absolute / `..`) before extraction. A hard failure
partway through a zip leaves already-processed members' commits standing
and reports the zip itself as `"error"` (routed to `work/failed/` by the
usual inbox hygiene, same as any other failed file).

**Document text** (docs/adr/0015-document-text-corpus.md): for a *new*
(non-duplicate) document, `_ingest_pdf`/`_ingest_text_like` extract the full
plain text (`_best_effort_extract_text` for pdf, via
`ingest.doctext.extract_text_for_kind` -> `pdftotext`; docx/text already have
it in hand from classification) and thread it down as `doc_text` into
`_ingest_lab_report`/`_ingest_non_lab`, which store it
(`_store_text_best_effort` -> `ingest.doctext.store_document_text`) right
after the document's `documents` row is inserted and before that same
function's `repo.commit()` - so it rides along in the SAME commit as the
rest of the document's ingest (`doc-text/<sha>.txt`, committed, plus
`labs.sqlite`'s `document_text`/`document_text_fts`). This NEVER fails the
ingest - a missing `pdftotext` binary or any other extraction/storage
failure is logged and the pipeline proceeds exactly as if the call had
never been made; lab-row extraction remains the primary job. Genomic files
never reach this call at all (see "genomic" below).

Either way, exactly one `DataRepo.commit` per document (genomic: per
archived file, committing only the regenerated inventory - see
`ingest.genomics`). This module deliberately does NOT touch
`casefile.ledger` or `reason.dag`: per the task scope, incremental
reasoning over newly-ingested rows is a later slice.

**Post-ingest inbox hygiene** applies ONLY to files a caller identifies as
owned by the data repo's `inbox/`, via `inbox_root=` (see `_ingest_one`):
once a file's `FileOutcome` is known, an `ingested`/`duplicate` outcome
deletes the inbox copy (the immutable `sources/` archive - or, for a
duplicate, the archive from the *first* ingest - is authoritative; nothing
is lost), and an `error` outcome moves the file to `work/failed/` and
records it in `work/failed/failures.jsonl` (see `ingest.failures`) so the
web `/failed` page can surface it. `ingest_inbox` passes its own `inbox/`
as `inbox_root`; `ingest_file` and `ingest_directory` default to
`inbox_root=None` (no hygiene) unless a caller opts in. A zip's *members*
are never individually opted into inbox hygiene (they live in a scratch
temp directory, not under `inbox/`) - only the zip file itself is.

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

import logging
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
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
    DocKind,
    PageRenderer,
    archive_document,
    pdftoppm_renderer,
)
from adoc.ingest.doctext import DOC_TEXT_RELDIR, extract_text_for_kind, store_document_text
from adoc.ingest.docx import DocxExtractionError, extract_docx_text
from adoc.ingest.extract import double_pass_extract, double_pass_extract_text
from adoc.ingest.failures import FAILED_DIR_RELPATH, FailureRecord, append_failure, flatten_relpath
from adoc.ingest.filetypes import detect_intake_kind
from adoc.ingest.genomics import (
    GENOMIC_DOC_TYPE,
    GENOMICS_INVENTORY_RELPATH,
    archive_genomic_file,
    regenerate_inventory,
)
from adoc.ingest.reconcile import ReconciledRow, parse_flag, parse_ref_range, reconcile
from adoc.ingest.schema import ClassifyResult, DocType, DocumentExtraction
from adoc.ingest.vision import ImagePart, VisionClient, VisionError
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.reason.client import LlmClient, LlmError, Message

logger = logging.getLogger(__name__)

MAX_ZIP_MEMBERS = 200
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024**3  # 2 GiB

CLASSIFY_PROMPT_VERSION = "classifier-v1"
CLASSIFY_PROMPT = f"""[{CLASSIFY_PROMPT_VERSION}]
Look at this single page image - the first page of a medical document -
and classify the document as one of: lab_report (a lab/blood-work results
report), clinical_note (a doctor/specialist visit note or letter),
imaging_report (a radiology/imaging report), or other. Also give your best
guess at the document's date (the report date or visit date shown on the
page) as doc_date, or omit it if no date is visible on this page.
"""

# Shared by both TEXT-kind intake paths - a real .docx narrative/lab
# export AND a plain .txt/.md drop (`_ingest_text_like`, used by both
# `_ingest_docx` and `_ingest_text`) - the prompt itself is generic over
# "full extracted text", so this is reused rather than duplicated for the
# new plain-text path (kept under its original name; nothing docx-specific
# in the prompt text itself).
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
    # `str`, not `DocType` - a genomic file's outcome carries
    # `GENOMIC_DOC_TYPE` ("genomic_data"), which is not one of `DocType`'s
    # LLM-classifier values (a genomic file is never classified).
    doc_type: str | None = None
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
        specimen=row.specimen,
        source_doc=source_doc,
        source_page=row.source_page,
        extraction_status=status,
        raw_json=row.raw_json,
    )


def _commit_message(*, label: str, doc_date: date | None, rows_auto: int, rows_pending: int) -> str:
    date_part = doc_date.isoformat() if doc_date else "undated"
    return f"ingest: {label} {date_part} ({rows_auto} rows, {rows_pending} queued)"


def _commit_paths_with_doc_text(base: list[str], repo: DataRepo) -> list[str]:
    """`base` plus `"doc-text"` when that directory actually has something
    to stage. `doc-text/` may not exist at all yet (extraction never
    succeeded even once, e.g. `pdftotext` isn't installed) — passing a
    nonexistent path to `DataRepo.commit`'s `git add` would raise, so this
    only adds it when there is something there to add.
    """
    if (repo.root / DOC_TEXT_RELDIR).is_dir():
        return [*base, DOC_TEXT_RELDIR]
    return base


def _best_effort_extract_text(kind: DocKind, path: Path) -> str | None:
    """Best-effort document-text extraction for the pdf archived kind
    (docs/adr/0015-document-text-corpus.md) — docx/text kinds never call
    this, since `_ingest_text_like` already has their full text in hand
    (see that function). NEVER raises: lab-row extraction is the primary
    job of this pipeline (module docstring), so a text-layer failure
    (missing `pdftotext`, a corrupt file, anything) is logged and the
    ingest proceeds exactly as if this call had never been made.

    Never reached for a genomic file — `_ingest_one` routes `"genomic"`-kind
    files to `_ingest_genomic` before `archive_document` is ever called
    (CRITICAL DESIGN RULE, module docstring); `kind` here is `DocKind`,
    which has no `"genomic"` member at all.
    """
    try:
        return extract_text_for_kind(kind, path)
    except Exception as exc:  # noqa: BLE001 - text extraction must never fail an ingest
        logger.warning("doc-text extraction failed for %s: %s", path.name, exc)
        return None


def _store_text_best_effort(repo: DataRepo, db: LabsDb, sha256: str, text: str) -> None:
    """Best-effort document-text storage (docs/adr/0015) — called with text
    already in hand (either just-extracted for a pdf, or already-extracted
    for docx/text). NEVER raises, same rationale as
    `_best_effort_extract_text`: storage (disk write + sqlite insert)
    failing must never fail the surrounding ingest.
    """
    try:
        store_document_text(repo, db, sha256, text)
    except Exception as exc:  # noqa: BLE001 - text storage must never fail an ingest
        logger.warning("doc-text storage failed for %s: %s", sha256, exc)


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
    doc_text: str | None = None,
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
    # document row must exist before any lab row (or document_text row,
    # docs/adr/0015) referencing it is inserted.
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
    if doc_text is not None:
        _store_text_best_effort(repo, db, sha256, doc_text)
    db.insert_results(lab_rows)
    db.export_jsonl(repo.root / "labs-export.jsonl")

    rows_auto = sum(1 for row in reconciled if row.status == "auto")
    rows_pending = sum(1 for row in reconciled if row.status == "pending")
    issues.extend(reason for row in reconciled if row.status == "pending" for reason in row.reasons)

    facility = pass_a.facility or pass_b.facility or path.name
    message = _commit_message(
        label=facility, doc_date=doc_date, rows_auto=rows_auto, rows_pending=rows_pending
    )
    commit_sha = repo.commit(
        message, paths=_commit_paths_with_doc_text(["sources", "labs-export.jsonl"], repo)
    )

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
    doc_text: str | None = None,
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
    # docs/adr/0015: `doc_text` is independent of `extracted_text` above -
    # `extracted_text` only controls the encounter's rendered `## Extracted
    # text` markdown section (docx/text only, per ADR 0008); `doc_text` is
    # what gets stored for the document-TEXT layer (pdf too).
    if doc_text is not None:
        _store_text_best_effort(repo, db, sha256, doc_text)

    message = _commit_message(label=path.name, doc_date=doc_date, rows_auto=0, rows_pending=0)
    commit_sha = repo.commit(
        message, paths=_commit_paths_with_doc_text(["sources", "case/encounters"], repo)
    )

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
    # docs/adr/0015: extracted once here (pdftotext), then threaded into
    # whichever branch below actually stores/commits it - never re-derived.
    doc_text = _best_effort_extract_text("pdf", archived.original_path)

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
            doc_text=doc_text,
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
        doc_text=doc_text,
    )


def _ingest_text_like(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    client: LlmClient,
    clock: Callable[[], datetime],
    archived: ArchivedDoc,
    text: str,
) -> FileOutcome:
    """Shared TEXT-document flow (PLAN.md docx ingestion design decision,
    now shared by both `_ingest_docx` and `_ingest_text` - item 3 of the
    genomics/filetypes task): text-based classify -> lab_report: TEXT
    double-pass into the same reconcile/insert/export gates a PDF lab
    report uses; else: a full-text encounter.
    """
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
    # Neither a docx nor a plain text file has page structure -
    # `LabDocument.page_count` still requires >=1, so it's recorded as one
    # logical unit.
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
            doc_text=text,
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
        doc_text=text,
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
    """docx flow (PLAN.md docx ingestion): deterministic TEXT extraction,
    then the shared `_ingest_text_like` flow."""
    try:
        text = extract_docx_text(archived.original_path)
    except DocxExtractionError as exc:
        return FileOutcome(
            path=str(path), sha256=archived.sha256, outcome="error", issues=[str(exc)]
        )
    return _ingest_text_like(
        path, repo=repo, db=db, client=client, clock=clock, archived=archived, text=text
    )


def _ingest_text(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    client: LlmClient,
    clock: Callable[[], datetime],
    archived: ArchivedDoc,
) -> FileOutcome:
    """Plain `.txt`/`.md` document flow (genomics/filetypes task item 3):
    read the archived file's text with `errors="replace"` (never raises on
    bad encoding - a plain-text drop is not expected to always be clean
    UTF-8), then the SAME shared `_ingest_text_like` flow docx uses -
    reusing the docx prompts rather than duplicating them.
    """
    text = archived.original_path.read_text(encoding="utf-8", errors="replace")
    return _ingest_text_like(
        path, repo=repo, db=db, client=client, clock=clock, archived=archived, text=text
    )


def _ingest_genomic(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    clock: Callable[[], datetime],
) -> FileOutcome:
    """Genomic flow (CRITICAL DESIGN RULE - module docstring): archive
    byte-for-byte into `sources/genomics/` (never `archive_document`, never
    a vision/text extraction call), register one `documents` row
    (`doc_type=GENOMIC_DOC_TYPE`, `status=COMPLETE`, `page_count=1` - no
    page structure), and regenerate the single `case/genomics-inventory.md`
    summary rather than writing a per-file encounter. Only the inventory
    file - never the genomic bytes themselves - is committed to git (see
    `ingest.genomics`'s module docstring on why `sources/genomics/` is
    gitignored).
    """
    archived = archive_genomic_file(repo.root, path, db=db)
    if archived.already_ingested:
        return FileOutcome(path=str(path), sha256=archived.sha256, outcome="duplicate")

    db.upsert_document(
        LabDocument(
            sha256=archived.sha256,
            filename=path.name,
            doc_type=GENOMIC_DOC_TYPE,
            doc_date=None,
            page_count=1,
            ingested_at=clock(),
            status=DocumentStatus.COMPLETE,
        )
    )
    regenerate_inventory(repo.root, db)

    commit_sha = repo.commit(
        f"ingest: genomic data file {path.name}", paths=[GENOMICS_INVENTORY_RELPATH]
    )
    return FileOutcome(
        path=str(path),
        sha256=archived.sha256,
        outcome="ingested",
        doc_type=GENOMIC_DOC_TYPE,
        commit_sha=commit_sha,
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


def _safe_member_relpath(name: str) -> Path | None:
    """The zip-slip guard (genomics/filetypes task item 4): `None` if
    `name` is an absolute path, escapes via a `..` component, or looks
    like a Windows drive-absolute path - anything that could extract
    outside the scratch directory it's given. A directory entry (trailing
    `/`) also returns `None` - only file members are ingested."""
    if not name or name.endswith("/") or name.endswith("\\"):
        return None
    if name.startswith("/") or name.startswith("\\"):
        return None
    if len(name) >= 2 and name[1] == ":":  # e.g. "C:\\Windows\\..."
        return None
    posix = PurePosixPath(name)
    if posix.is_absolute() or ".." in posix.parts or "" in posix.parts:
        return None
    return Path(*posix.parts)


def _validate_zip_caps(zf: zipfile.ZipFile, zip_name: str) -> list[zipfile.ZipInfo]:
    """Raises `ArchiveError` if `zf` exceeds either cap (genomics/filetypes
    task item 4) - checked from metadata alone, before extracting a single
    byte. Returns the non-directory members to process otherwise."""
    infos = [info for info in zf.infolist() if not info.is_dir()]
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ArchiveError(
            f"{zip_name}: {len(infos)} members exceeds the {MAX_ZIP_MEMBERS}-member cap"
        )
    total = sum(info.file_size for info in infos)
    if total > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ArchiveError(
            f"{zip_name}: {total:,} bytes uncompressed exceeds the "
            f"{MAX_ZIP_UNCOMPRESSED_BYTES:,}-byte cap"
        )
    return infos


def _ingest_zip_member(
    info: zipfile.ZipInfo,
    zf: zipfile.ZipFile,
    tmp_dir: Path,
    zip_name: str,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime],
    renderer: PageRenderer,
) -> FileOutcome:
    """Extract one zip member to `tmp_dir` and ingest it exactly as if it
    were its own inbox file (module docstring: "same hygiene, same
    dedupe") - reusing `_ingest_one` for the pdf/docx/text/genomic
    dispatch, with `inbox_root=None` since the extracted copy lives in a
    scratch temp directory, not the real `inbox/` (the zip's OWN inbox
    hygiene, applied by the caller, covers the container)."""
    member_label = f"{zip_name}:{info.filename}"
    relpath = _safe_member_relpath(info.filename)
    if relpath is None:
        return FileOutcome(
            path=member_label,
            outcome="error",
            issues=["unsafe member path (absolute or contains '..')"],
        )

    extracted_path = tmp_dir / relpath
    extracted_path.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as src, extracted_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)

    # Depth 1 only (module docstring): a member that is itself a zip is
    # rejected outright, never recursed into.
    if detect_intake_kind(extracted_path) == "zip":
        return FileOutcome(
            path=member_label, outcome="error", issues=["nested zip archives are not supported"]
        )

    outcomes = _ingest_one(
        extracted_path,
        repo=repo,
        db=db,
        vision=vision,
        clock=clock,
        renderer=renderer,
        inbox_root=None,
    )
    outcome = outcomes[0]
    return outcome.model_copy(update={"path": member_label})


def _ingest_zip(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime],
    renderer: PageRenderer,
) -> tuple[list[FileOutcome], str | None]:
    """Expand `path` and ingest each member (module docstring: "the zip
    itself is not archived; its members are"). Returns
    `(member_outcomes, zip_level_error)` - `zip_level_error` is set only
    when the *whole zip* is rejected (bad zip file, over a cap) or a hard
    failure stops processing partway through (genomics/filetypes task item
    4: "already-processed members stand; the zip moves to work/failed/
    with reason") - a single bad/unsafe/unsupported MEMBER does not stop
    the rest of the zip; it is just one more `"error"` `FileOutcome`.
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        return [], f"{path.name}: not a valid zip archive: {exc}"

    with zf:
        try:
            infos = _validate_zip_caps(zf, path.name)
        except ArchiveError as exc:
            return [], str(exc)

        outcomes: list[FileOutcome] = []
        with tempfile.TemporaryDirectory(prefix="adoc-zip-") as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            try:
                for info in infos:
                    outcomes.append(
                        _ingest_zip_member(
                            info,
                            zf,
                            tmp_dir,
                            path.name,
                            repo=repo,
                            db=db,
                            vision=vision,
                            clock=clock,
                            renderer=renderer,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - a hard mid-zip failure: keep
                # what's already committed, stop, and report the zip itself as
                # failed (already-appended member outcomes are untouched).
                return outcomes, f"{path.name}: zip processing stopped part-way through: {exc}"

        return outcomes, None


def _ingest_one(
    path: Path,
    *,
    repo: DataRepo,
    db: LabsDb,
    vision: VisionClient,
    clock: Callable[[], datetime],
    renderer: PageRenderer,
    inbox_root: Path | None = None,
) -> list[FileOutcome]:
    kind = detect_intake_kind(path)

    if kind == "zip":
        member_outcomes, zip_error = _ingest_zip(
            path, repo=repo, db=db, vision=vision, clock=clock, renderer=renderer
        )
        zip_outcome = FileOutcome(
            path=str(path),
            outcome="error" if zip_error else "ingested",
            issues=[zip_error] if zip_error else [],
        )
        _apply_inbox_hygiene(path, zip_outcome, repo=repo, inbox_root=inbox_root, clock=clock)
        # The zip container itself is never a "document" in the report
        # (module docstring) - its own outcome only needs surfacing when
        # it failed; a clean expansion just reports its members.
        return [*member_outcomes, zip_outcome] if zip_error else member_outcomes

    if kind == "genomic":
        outcome = _ingest_genomic(path, repo=repo, db=db, clock=clock)
        _apply_inbox_hygiene(path, outcome, repo=repo, inbox_root=inbox_root, clock=clock)
        return [outcome]

    try:
        archived = archive_document(repo.root, path, db=db, renderer=renderer)
    except ArchiveError as exc:
        outcome = FileOutcome(path=str(path), outcome="error", issues=[str(exc)])
        _apply_inbox_hygiene(path, outcome, repo=repo, inbox_root=inbox_root, clock=clock)
        return [outcome]

    if archived.already_ingested:
        outcome = FileOutcome(path=str(path), sha256=archived.sha256, outcome="duplicate")
        _apply_inbox_hygiene(path, outcome, repo=repo, inbox_root=inbox_root, clock=clock)
        return [outcome]

    # Document-TEXT layer (docs/adr/0015): each of these three branches
    # extracts (pdf: `_best_effort_extract_text`; docx/text: already have
    # the full text in hand) and stores it (`_store_text_best_effort`, via
    # `_ingest_lab_report`/`_ingest_non_lab`'s `doc_text` parameter) AFTER
    # `db.upsert_document` inserts this document's row but BEFORE that same
    # function's `repo.commit()` — one commit per document is preserved,
    # and `document_text.source_doc`'s foreign key is always satisfied by
    # the time it's written. Only reached for a genuinely NEW document — a
    # duplicate already has its text stored from the first ingest (see the
    # `already_ingested` return above).
    if archived.kind == "docx":
        outcome = _ingest_docx(
            path, repo=repo, db=db, client=vision.client, clock=clock, archived=archived
        )
    elif archived.kind == "text":
        outcome = _ingest_text(
            path, repo=repo, db=db, client=vision.client, clock=clock, archived=archived
        )
    else:
        outcome = _ingest_pdf(path, repo=repo, db=db, vision=vision, clock=clock, archived=archived)

    _apply_inbox_hygiene(path, outcome, repo=repo, inbox_root=inbox_root, clock=clock)
    return [outcome]


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
        files=_ingest_one(
            path,
            repo=repo,
            db=db,
            vision=vision,
            clock=clock,
            renderer=renderer,
            inbox_root=inbox_root,
        )
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
    outcomes: list[FileOutcome] = []
    for p in files:
        outcomes.extend(
            _ingest_one(
                p, repo=repo, db=db, vision=vision, clock=clock, renderer=renderer, inbox_root=inbox
            )
        )
    return IngestReport(files=outcomes)


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
    outcomes: list[FileOutcome] = []
    for p in files:
        outcomes.extend(
            _ingest_one(p, repo=repo, db=db, vision=vision, clock=clock, renderer=renderer)
        )
    return IngestReport(files=outcomes)


__all__ = [
    "FileOutcome",
    "IngestReport",
    "ingest_directory",
    "ingest_file",
    "ingest_inbox",
]

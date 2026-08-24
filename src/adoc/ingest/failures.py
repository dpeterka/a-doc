"""Failed-ingestion tracking (PLAN.md "Ingestion" post-ingest inbox hygiene).

When `ingest.pipeline`'s hygiene moves a file that failed to ingest out of
`inbox/`, it lands under `<data_dir>/work/failed/` (see `flatten_relpath` —
a nested inbox-relative path like `Labs/LabCorp/b.pdf` is flattened to
`Labs__LabCorp__b.pdf` so every failed file is exactly one path segment,
which keeps the web `/failed` routes' URLs simple) and one line is appended
to `work/failed/failures.jsonl`: `{filename, failed_at, reason,
original_inbox_path}`.

This module owns reading/writing that log so `ingest.pipeline` (which
writes it after a failure) and `web.routes.failed` (which lists it, and
lets the patient retry or remove an entry) share one implementation of the
record shape and the flatten/lookup convention.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from adoc.casefile.repo import DataRepo

FAILED_DIR_RELPATH = "work/failed"
FAILURES_LOG_RELPATH = "work/failed/failures.jsonl"


class FailureRecord(BaseModel):
    """One `work/failed/failures.jsonl` line: a document that failed to
    ingest, moved out of `inbox/` into `work/failed/`."""

    filename: str
    failed_at: datetime
    reason: str
    original_inbox_path: str


def flatten_relpath(rel: Path) -> str:
    """Flatten a nested inbox-relative path into one path segment
    (`Labs/LabCorp/b.pdf` -> `Labs__LabCorp__b.pdf`) so every failed file
    lives directly under `work/failed/` and every `/failed` route needs
    only a single filename segment, never a multi-segment path."""
    return "__".join(rel.parts)


def failed_file_path(repo: DataRepo, record: FailureRecord) -> Path:
    """Where `record`'s file lives under `work/failed/`."""
    return repo.root / FAILED_DIR_RELPATH / flatten_relpath(Path(record.original_inbox_path))


def read_failures(repo: DataRepo) -> list[FailureRecord]:
    log_path = repo.root / FAILURES_LOG_RELPATH
    if not log_path.exists():
        return []
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(FailureRecord.model_validate_json(line))
    return records


def _write_failures(repo: DataRepo, records: list[FailureRecord]) -> None:
    log_path = repo.root / FAILURES_LOG_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "".join(record.model_dump_json() + "\n" for record in records), encoding="utf-8"
    )


def append_failure(repo: DataRepo, record: FailureRecord) -> None:
    """Append `record`, first dropping any existing record for the same
    `original_inbox_path` — a file that fails again (e.g. after a failed
    retry) replaces its own stale record rather than accumulating
    duplicates."""
    records = [
        r for r in read_failures(repo) if r.original_inbox_path != record.original_inbox_path
    ]
    records.append(record)
    _write_failures(repo, records)


def remove_failure(repo: DataRepo, original_inbox_path: str) -> None:
    """Drop the record for `original_inbox_path`, if any (a no-op
    otherwise) — used once a retry succeeds or the patient removes a
    failed file for good."""
    records = [r for r in read_failures(repo) if r.original_inbox_path != original_inbox_path]
    _write_failures(repo, records)


def find_failure(repo: DataRepo, flat_name: str) -> FailureRecord | None:
    """Find the record whose flattened path matches `flat_name` (the
    on-disk filename under `work/failed/`, and the identifier the `/failed`
    web routes use in their URLs)."""
    for record in read_failures(repo):
        if flatten_relpath(Path(record.original_inbox_path)) == flat_name:
            return record
    return None


def restore_to_inbox(repo: DataRepo, record: FailureRecord, inbox_dir: Path) -> Path:
    """Move `record`'s failed file back into `inbox/` at its original
    relative location, for a retry. Returns the destination path; a no-op
    move (still returning the destination) if the failed file is already
    gone (removed out-of-band)."""
    source = failed_file_path(repo, record)
    dest = inbox_dir / record.original_inbox_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.move(str(source), str(dest))
    return dest


__all__ = [
    "FAILED_DIR_RELPATH",
    "FAILURES_LOG_RELPATH",
    "FailureRecord",
    "append_failure",
    "failed_file_path",
    "find_failure",
    "flatten_relpath",
    "read_failures",
    "remove_failure",
    "restore_to_inbox",
]

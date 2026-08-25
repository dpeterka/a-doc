"""Small persistence helpers the web routes need that don't belong to any
one foundation module: the "what's new since your last visit" bookmark,
the chat transcript log, and page-image lookup for the confirm queue /
ledger source-ref links.

All paths written here live under the data repo's gitignored `work/` or
`logs/` top-level dirs (see `casefile.repo._TOP_LEVEL_DIRS`) — never
committed, and never PHI-scrubbed on the way in (this is local disk, not a
model call).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from adoc.casefile.repo import HISTORY_RELPATH, DataRepo
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument

_LAST_SEEN_RELPATH = Path("work") / "last-seen-ledger.txt"
_CHAT_LOG_DIR = Path("logs") / "chat"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# --- "what's new since your last visit" ------------------------------------------------


def read_last_seen(repo: DataRepo) -> datetime | None:
    path = repo.root / _LAST_SEEN_RELPATH
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def write_last_seen(repo: DataRepo, when: datetime) -> None:
    path = repo.root / _LAST_SEEN_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(when.isoformat(), encoding="utf-8")


def ledger_history_since(repo: DataRepo, since: datetime | None) -> list[dict[str, Any]]:
    """Ledger-history entries strictly newer than `since`.

    `since=None` (no prior visit recorded yet) yields an empty list — the
    home page's current three-tier differential already reflects
    everything; "what's new" is only meaningful once there is a prior
    visit to compare against.
    """
    if since is None:
        return []
    path = repo.root / HISTORY_RELPATH
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            resulting_updated = datetime.fromisoformat(record["resulting_updated"])
            if resulting_updated > since:
                entries.append(record)
    return entries


# --- chat transcript ---------------------------------------------------------------------


def chat_log_path(repo: DataRepo, day: date) -> Path:
    return repo.root / _CHAT_LOG_DIR / f"{day.isoformat()}.jsonl"


def append_chat_entry(repo: DataRepo, entry: dict[str, Any]) -> None:
    path = chat_log_path(repo, datetime.now(UTC).date())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str))
        fh.write("\n")


def read_recent_chat(repo: DataRepo, *, max_files: int = 3, max_turns: int = 100) -> list[dict]:
    """The most recent chat transcript entries, oldest first."""
    log_dir = repo.root / _CHAT_LOG_DIR
    if not log_dir.is_dir():
        return []
    files = sorted((p for p in log_dir.iterdir() if p.suffix == ".jsonl"), reverse=True)[:max_files]
    entries: list[dict[str, Any]] = []
    for path in reversed(files):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    return entries[-max_turns:]


def last_chat_date(repo: DataRepo) -> date | None:
    """The date of the most recent chat-transcript entry (patient or
    assistant turn, whichever landed last), for the home dashboard's "last
    conversation" line — `None` if no chat has happened yet. Only the
    single newest entry is needed, so `max_turns=1` regardless of
    `max_files`'s default (the newest entry always lives in the newest
    day-file)."""
    at = last_chat_at(repo)
    return at.date() if at is not None else None


def last_chat_at(repo: DataRepo) -> datetime | None:
    """Like `last_chat_date` but the full timestamp — post-intake continuity
    (`docs/adr/0018-intake-clinical-progression-and-continuity.md`) needs an
    hours-scale "how long has it been" gap, not just a date, to decide
    whether a turn is starting a new visit
    (`intake.agent.VISIT_GAP_THRESHOLD_HOURS`) and to render "it's been
    about 3 hours"/"yesterday"/etc. `None` if no chat has happened yet."""
    entries = read_recent_chat(repo, max_turns=1)
    if not entries:
        return None
    timestamp = entries[-1].get("timestamp")
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        return None


# --- "what's already on file" strip (home dashboard, empty-state fix) --------------------


@dataclass(frozen=True)
class OnFileSummary:
    """Server-computed "what's already on file" counts for the home
    dashboard. Owner-observed feedback: a fresh install with documents and
    labs already ingested (a seeded/restored deployment, or a local repo
    that ran a backfill) but no diagnostic conversation yet must not render
    as if nothing exists — this is what lets the home page say so.
    `doc_count == 0` is the signal the template uses to show an "add
    documents" pointer instead of the strip."""

    doc_count: int
    lab_row_count: int
    analyte_count: int
    date_span: tuple[date, date] | None
    encounter_count: int


def on_file_summary(repo: DataRepo, db: LabsDb) -> OnFileSummary:
    """Compute `OnFileSummary` from the labs DB + data repo — read-only
    queries only, no schema changes, safe to call on every home-page
    request."""
    doc_count = len(db.documents_overview())

    rows = db.all_non_rejected_rows()
    lab_row_count = len(rows)
    analyte_count = len({row.name for row in rows})
    date_span = (min(row.date for row in rows), max(row.date for row in rows)) if rows else None

    encounters_dir = repo.root / "case" / "encounters"
    encounter_count = (
        sum(1 for p in encounters_dir.iterdir() if p.suffix == ".md")
        if encounters_dir.is_dir()
        else 0
    )

    return OnFileSummary(
        doc_count=doc_count,
        lab_row_count=lab_row_count,
        analyte_count=analyte_count,
        date_span=date_span,
        encounter_count=encounter_count,
    )


# --- page images (confirm queue, ledger doc: refs) ---------------------------------------


def _is_safe_sha(sha: str) -> bool:
    return bool(_SHA_RE.match(sha))


def _is_safe_filename(filename: str) -> bool:
    """No path separators, no `..`, no leading dot — refuses traversal."""
    return bool(_SAFE_FILENAME_RE.match(filename)) and filename not in {".", ".."}


def page_images_dir(repo: DataRepo, sha: str) -> Path:
    return repo.root / "sources" / "pages" / sha


def list_page_images(
    repo: DataRepo, sha: str, *, cache: dict[str, list[Path]] | None = None
) -> list[Path]:
    """`cache`, when given, memoizes this directory listing per `sha` -
    a confirm-queue page or ledger view commonly calls this once per row/
    evidence-ref, and several of those often share one document's `sha`.
    Without a cache each call re-lists the same directory (a filesystem
    `iterdir()`/stat) on every one of those calls; on the deployed app's
    EFS/NFS-backed data repo that round trip costs real milliseconds, same
    as a `labs.sqlite` query. Defaults to `None`, which lists fresh exactly
    as before.
    """
    if cache is not None and sha in cache:
        return cache[sha]
    if not _is_safe_sha(sha):
        result: list[Path] = []
    else:
        directory = page_images_dir(repo, sha)
        result = sorted(p for p in directory.iterdir() if p.is_file()) if directory.is_dir() else []
    if cache is not None:
        cache[sha] = result
    return result


def page_image_url(
    repo: DataRepo, sha: str, page: int | None, *, cache: dict[str, list[Path]] | None = None
) -> str | None:
    """The `/files/pages/<sha>/<filename>` URL for `page` (1-indexed), or
    `None` if the document has no rendered page images / the page is out
    of range. `cache` is forwarded to `list_page_images` unchanged - see
    its docstring."""
    if page is None or page < 1:
        return None
    images = list_page_images(repo, sha, cache=cache)
    if page > len(images):
        return None
    filename = images[page - 1].name
    return f"/files/pages/{sha}/{filename}"


def resolve_page_image_path(repo: DataRepo, sha: str, filename: str) -> Path | None:
    """Resolve a requested `(sha, filename)` to a real file strictly inside
    that document's page-image directory, or `None` if either component is
    unsafe, the file doesn't exist, or (defense in depth) the resolved path
    escapes the expected directory."""
    if not _is_safe_sha(sha) or not _is_safe_filename(filename):
        return None
    directory = page_images_dir(repo, sha)
    candidate = directory / filename
    try:
        resolved = candidate.resolve()
        resolved_dir = directory.resolve()
    except OSError:
        return None
    if resolved_dir not in resolved.parents and resolved != resolved_dir:
        return None
    if resolved.parent != resolved_dir:
        return None
    if not resolved.is_file():
        return None
    return resolved


def resolve_original_document_path(repo: DataRepo, sha: str) -> Path | None:
    """Resolve `sha` to its immutable archived original under `sources/`
    (filenames there are `<sha>__<origname>`, see `ingest.archive`), or
    `None` if `sha` isn't a safe bare sha256, no archived original
    exists, or (defense in depth) the match resolves outside `sources/`.

    Same traversal-defense shape as `resolve_page_image_path`: the only
    untrusted input is `sha`, checked against `_is_safe_sha` before it
    ever touches the filesystem, and the resolved path is re-checked
    against the expected parent directory afterwards.
    """
    if not _is_safe_sha(sha):
        return None
    sources_dir = repo.root / "sources"
    if not sources_dir.is_dir():
        return None
    try:
        resolved_dir = sources_dir.resolve()
    except OSError:
        return None
    prefix = f"{sha}__"
    matches = [
        entry
        for entry in sources_dir.iterdir()
        if entry.is_file() and entry.name.startswith(prefix)
    ]
    if len(matches) != 1:
        return None
    try:
        resolved = matches[0].resolve()
    except OSError:
        return None
    if resolved.parent != resolved_dir:
        return None
    return resolved


def find_document_by_filename(
    db: LabsDb, filename: str, *, documents: list[LabDocument] | None = None
) -> LabDocument | None:
    """`documents`, when given, is searched instead of calling
    `db.list_documents()` - the ledger view calls this once per `doc:`
    evidence ref, and without a pre-fetched list each call re-runs the
    same full-table `labs.sqlite` query. Defaults to `None`, which queries
    fresh exactly as before."""
    for doc in documents if documents is not None else db.list_documents():
        if doc.filename == filename:
            return doc
    return None

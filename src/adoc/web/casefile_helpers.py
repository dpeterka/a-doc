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


# --- page images (confirm queue, ledger doc: refs) ---------------------------------------


def _is_safe_sha(sha: str) -> bool:
    return bool(_SHA_RE.match(sha))


def _is_safe_filename(filename: str) -> bool:
    """No path separators, no `..`, no leading dot — refuses traversal."""
    return bool(_SAFE_FILENAME_RE.match(filename)) and filename not in {".", ".."}


def page_images_dir(repo: DataRepo, sha: str) -> Path:
    return repo.root / "sources" / "pages" / sha


def list_page_images(repo: DataRepo, sha: str) -> list[Path]:
    if not _is_safe_sha(sha):
        return []
    directory = page_images_dir(repo, sha)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file())


def page_image_url(repo: DataRepo, sha: str, page: int | None) -> str | None:
    """The `/files/pages/<sha>/<filename>` URL for `page` (1-indexed), or
    `None` if the document has no rendered page images / the page is out
    of range."""
    if page is None or page < 1:
        return None
    images = list_page_images(repo, sha)
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


def find_document_by_filename(db: LabsDb, filename: str) -> LabDocument | None:
    for doc in db.list_documents():
        if doc.filename == filename:
            return doc
    return None

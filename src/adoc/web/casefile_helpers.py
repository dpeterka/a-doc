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
from collections.abc import Sequence
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


# Reading order for a differential, strongest first. A 24-hypothesis ledger
# rendered in file order is a list of disease names with no signal about
# which matter — for a patient reading her own case file that is not merely
# untidy, it reads as "you might have 24 things".
_TIER_RANK = {"most-likely": 0, "cant-miss": 1, "expanded": 2}
_PROBABILITY_RANK = {"high": 0, "moderate": 1, "low": 2, "minimal": 3}

# Below this, a lead is real but not something to read on the way in. Kept,
# never hidden — rendered behind a disclosure so the page leads with what
# actually matters.
SECONDARY_PROBABILITIES = frozenset({"low", "minimal"})


def sort_hypotheses(hypotheses: Sequence[Any]) -> list[Any]:
    """Tier first (most-likely, then can't-miss, then expanded), probability
    within tier, then name so the order is stable between renders."""
    return sorted(
        hypotheses,
        key=lambda h: (
            _TIER_RANK.get(h.tier, 99),
            _PROBABILITY_RANK.get(h.probability, 99),
            h.name.lower(),
        ),
    )


def is_unsubstantiated(hypothesis: Any) -> bool:
    """A lead with nothing behind it yet: no supporting evidence, and not
    thought likely.

    The can't-miss tier is a safety net, so the challenger is expected to
    raise entries there speculatively — "if this were true, missing it would
    be catastrophic" — before anything supports them. That is the tier
    working as intended. What is NOT intended is such an entry appearing at
    the top of the page with the same weight as a lead the labs actually
    point at.

    A can't-miss placeholder is kept and never hidden; it is simply not what
    to read first.
    """
    return not getattr(hypothesis, "evidence_for", None) and (
        hypothesis.probability in SECONDARY_PROBABILITIES
    )


def group_hypotheses(hypotheses: Sequence[Any]) -> dict[str, list[Any]]:
    """Split a sorted differential into what to lead with and what to fold
    away: `leading` (can't-miss at any probability, plus anything high or
    moderate) and `secondary` (the low/minimal tail).

    Within `leading`, substantiated leads come first. A can't-miss entry with
    no cited evidence and low or minimal probability is a placeholder the
    challenger raised as a safety net — it belongs on the page, but reading
    it above a lead the labs actually support tells the patient the wrong
    thing about her own case.
    """
    ordered = sort_hypotheses(hypotheses)
    leading = [
        h for h in ordered if h.tier == "cant-miss" or h.probability not in SECONDARY_PROBABILITIES
    ]
    # Stable: `sort_hypotheses` order is preserved inside each half.
    leading.sort(key=is_unsubstantiated)
    secondary = [h for h in ordered if h not in leading]
    return {"leading": leading, "secondary": secondary}


def summarize_diff_ops(diff: Any) -> dict[str, Any]:
    """What a ledger diff actually DID, derived from its typed ops.

    The home page used to render `diff.rationale` verbatim — the model's
    full adjudication prose for every divergence, concatenated. On a real
    review that was thousands of words of clinical argument in a single
    unbroken paragraph, which is unreadable and buries the one thing the
    page is for: what changed.

    The ops are typed, so the change set is derivable rather than narrated.

    `ledger-history.jsonl` is read back as plain JSON, so a diff arrives here
    as a `dict`, not a `LedgerDiff`. Both shapes are handled: an
    attribute-only reader silently returned empty for every real entry,
    which is the failure mode this whole change exists to remove.
    """

    def field(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    added: list[str] = []
    changed: list[str] = []
    challenged = 0
    evidence = 0
    for op in field(diff, "ops", []) or []:
        kind = field(op, "op", "")
        if kind == "add_hypothesis":
            hypothesis = field(op, "hypothesis", {}) or {}
            name = field(hypothesis, "name", "") or ""
            if name:
                added.append(name)
        elif kind == "update_hypothesis":
            # A gloss-only update is not a change worth announcing. The
            # challenge sweep backfills `plain_language` for every hypothesis
            # that lacks one, so without this the first review after that
            # field shipped would report all 26 hypotheses as "changed" on the
            # home page — the precise noise this summary exists to avoid.
            substantive = any(
                field(op, name) is not None
                for name in ("tier", "probability", "status", "discriminators")
            )
            if substantive:
                changed.append(str(field(op, "id", "") or ""))
        elif kind == "record_challenge":
            challenged += 1
        elif kind == "add_evidence":
            evidence += 1
    return {
        "added": added,
        "changed": changed,
        "challenged": challenged,
        "evidence": evidence,
        "rationale": field(diff, "rationale", "") or "",
    }


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


# --- rendering LLM prose so a person can read it --------------------------------------


_CHALLENGE_NOTE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Added from weekly blind-panel divergence adjudication:", "Added by review"),
    ("Weekly blind panel did not independently surface this hypothesis:", "Not surfaced by panel"),
    ("Reviewed again:", "Reviewed"),
    ("Challenge:", "Challenge"),
)
"""Sentence stems the review path writes at the head of a note, mapped to a
short label. The stem is redundant once the note is shown under that label,
and it is the longest, least informative part of every entry."""


def split_challenge_notes(notes: str | None) -> list[dict[str, str]]:
    """One `challenger_notes` blob split back into the entries it was built
    from, each labelled.

    `apply_diff` appends every `RecordChallenge` with `"\\n"`, so the field
    accumulates one entry per review — and the card rendered the whole
    accumulation into a single `<p>`. Three challenges from three different
    weeks arrived as one 200-word paragraph with no boundary between them,
    each opening with the same 60-character bureaucratic stem. That is the
    "barely readable blobs of text" the patient sees.

    Splitting is on the newline the append uses. Where an entry opens with a
    known stem, the stem becomes a label and is stripped from the body, which
    removes the repetition and gives the reader a way to skim.
    """
    if not notes or not notes.strip():
        return []
    entries: list[dict[str, str]] = []
    for raw in notes.splitlines():
        text = raw.strip()
        if not text:
            continue
        label = "Note"
        for prefix, prefix_label in _CHALLENGE_NOTE_PREFIXES:
            if text.startswith(prefix):
                label = prefix_label
                text = text[len(prefix) :].strip()
                break
        if text:
            entries.append({"label": label, "text": text})
    return entries


def humanize_source_ref(source: str) -> str:
    """A source ref as a person would read it.

    The card printed the machine ref verbatim —
    `(labs:lumbar-spine-percent-change-vs-2024:2026-08-04)` — beside every
    claim. It is the citation's identity, not its presentation: the patient
    and her doctors need to know WHICH row is being cited, and the slug's
    hyphens and prefix are noise in the way of that.
    """
    if source.startswith("labs:"):
        _, _, rest = source.partition(":")
        slug, _, when = rest.rpartition(":")
        name = (slug or rest).replace("-", " ").strip()
        return f"{name} · {when}" if when else name
    if source.startswith("doc:"):
        body = source[len("doc:") :]
        path, sep, page = body.partition("#p")
        return f"{path} · p{page}" if sep else path
    if source.startswith("encounter:"):
        return source[len("encounter:") :]
    if source.startswith("patient-report:"):
        return f"you reported this · {source[len('patient-report:') :]}"
    if source.startswith("pmid:"):
        return f"PubMed {source[len('pmid:') :]}"
    return source

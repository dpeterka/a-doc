"""Labs SQLite store: DDL, FTS5, migrations, and JSONL export/rebuild.

Stdlib `sqlite3` only (no ORM), per CLAUDE.md's no-new-runtime-deps
constraint. Schema is versioned via `PRAGMA user_version` plus an ordered
`_MIGRATIONS` list of SQL scripts so future schema changes are additive and
auditable (PLAN.md "Provenance & re-evaluation policy: Schema changes").

Per PLAN.md "State", `labs.sqlite` is a *derived* artifact: the data repo
commits `labs-export.jsonl` (append-friendly, human-diffable) and the sqlite
file is gitignored, rebuilt on demand via `rebuild_from_jsonl`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import wraps
from pathlib import Path
from types import TracebackType
from typing import Any, Concatenate, Literal

from adoc.labs.models import ExtractionStatus, LabDocument, LabFlag, LabResult, Specimen
from adoc.labs.validate import canonical_rename_target


@dataclass(frozen=True)
class DocumentOverview:
    """One ingested document alongside its lab-row status counts.

    Returned by `LabsDb.documents_overview()` for the "Documents > Consumed"
    page: `accepted_count` is every row past human/auto review (`auto`,
    `confirmed`, `corrected` - the same trio `resolved_rows_for_document`
    treats as "resolved"), `awaiting_review_count` is rows still `pending`.
    A `rejected` row counts toward neither - it was a duplicate/mistake,
    not a result the patient has or is waiting on. A genomic-data document
    never has labs rows at all, so both counts are simply 0 for it - the
    web route recognizes `doc_type == GENOMIC_DOC_TYPE` and shows its own
    "stored for later genomic analysis" wording instead.
    """

    document: LabDocument
    accepted_count: int
    awaiting_review_count: int


@dataclass(frozen=True)
class DocumentTextPage:
    """One page (or, for a non-paginated docx/text document, the whole
    document) of extracted text, as written to `document_text` by
    `LabsDb.replace_document_text()`. `page` is `None` for a document with
    no page structure (docx/plain-text) — see `ingest.doctext`'s module
    docstring for the form-feed-driven pagination rule."""

    page: int | None
    text: str


@dataclass(frozen=True)
class DocumentTextHit:
    """One ranked FTS5 match from `LabsDb.search_document_text()`.

    `source_ref` is PLAN.md's source-ref grammar rendering:
    `doc:<filename>#p<page>` when `page` is known, `doc:<filename>`
    otherwise — verbatim (never paraphrased) so a model can cite it and a
    later verifier can check it against the source.
    """

    source_doc: str
    filename: str
    page: int | None
    snippet: str
    source_ref: str


@dataclass(frozen=True)
class PendingRow:
    """One PENDING lab row joined with its source document.

    Returned by `LabsDb.pending_grouped()` for the confirm-queue UI, which
    needs to group PENDING rows by document (filename/date/type/page
    count) without a second round trip per row (PLAN.md "Ingestion"
    triage: "models agreed" vs. "models disagreed" buckets, each grouped
    by document).
    """

    row: LabResult
    doc_filename: str
    doc_date: date | None
    doc_type: str
    doc_page_count: int


# --------------------------------------------------------------------------
# Schema (versioned migrations)
# --------------------------------------------------------------------------

_MIGRATIONS: list[str] = [
    """
    CREATE TABLE documents (
        sha256 TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        doc_date TEXT,
        page_count INTEGER NOT NULL,
        ingested_at TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK(status IN ('processing', 'complete', 'needs-review', 'failed'))
    );

    CREATE TABLE labs (
        id INTEGER PRIMARY KEY,
        date TEXT NOT NULL,
        loinc_code TEXT,
        name TEXT NOT NULL,
        name_raw TEXT NOT NULL,
        value REAL,
        value_text TEXT,
        ucum_unit TEXT,
        ref_low REAL,
        ref_high REAL,
        ref_text TEXT,
        flag TEXT CHECK(flag IN ('H', 'L', 'HH', 'LL', 'A') OR flag IS NULL),
        source_doc TEXT NOT NULL REFERENCES documents(sha256),
        source_page INTEGER,
        extraction_status TEXT NOT NULL DEFAULT 'auto'
            CHECK(extraction_status IN ('auto', 'confirmed', 'corrected', 'pending', 'rejected')),
        raw_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(date, name, source_doc)
    );

    CREATE INDEX labs_by_analyte ON labs(name, date);

    CREATE VIRTUAL TABLE labs_fts USING fts5(
        name, name_raw, content='labs', content_rowid='id'
    );

    CREATE TRIGGER labs_ai AFTER INSERT ON labs BEGIN
        INSERT INTO labs_fts(rowid, name, name_raw) VALUES (new.id, new.name, new.name_raw);
    END;

    CREATE TRIGGER labs_ad AFTER DELETE ON labs BEGIN
        INSERT INTO labs_fts(labs_fts, rowid, name, name_raw)
        VALUES('delete', old.id, old.name, old.name_raw);
    END;

    CREATE TRIGGER labs_au AFTER UPDATE ON labs BEGIN
        INSERT INTO labs_fts(labs_fts, rowid, name, name_raw)
        VALUES('delete', old.id, old.name, old.name_raw);
        INSERT INTO labs_fts(rowid, name, name_raw) VALUES (new.id, new.name, new.name_raw);
    END;
    """,
    # Migration 2: add the `specimen` dimension (real finding: urinalysis
    # GLUCOSE "NEGATIVE" and a serum glucose mg/dL reading canonicalized to
    # the same `name` and so shared one trend series). SQLite can't
    # ALTER a UNIQUE constraint or add a CHECK'd column with a plain
    # `ALTER TABLE ADD COLUMN`, so this follows sqlite.org's 12-step
    # table-rebuild recipe: build the new table, copy rows across
    # (defaulting every existing row's `specimen` to 'unknown' - it is
    # unknown, not a guess), drop the old table (which also drops its
    # triggers and `labs_by_analyte` index), rename the new table into
    # place, then recreate the index, the FTS5 external-content table, and
    # its sync triggers - the FTS table's `content_rowid` ties it to
    # `labs.id`, which is preserved by the copy (`INSERT ... SELECT id,
    # ...`), so search behavior over the same rows is unaffected once
    # repopulated.
    """
    CREATE TABLE labs_new (
        id INTEGER PRIMARY KEY,
        date TEXT NOT NULL,
        loinc_code TEXT,
        name TEXT NOT NULL,
        name_raw TEXT NOT NULL,
        value REAL,
        value_text TEXT,
        ucum_unit TEXT,
        ref_low REAL,
        ref_high REAL,
        ref_text TEXT,
        flag TEXT CHECK(flag IN ('H', 'L', 'HH', 'LL', 'A') OR flag IS NULL),
        specimen TEXT NOT NULL DEFAULT 'unknown'
            CHECK(specimen IN ('serum', 'plasma', 'whole_blood', 'urine', 'stool', 'csf',
                                'saliva', 'other', 'unknown')),
        source_doc TEXT NOT NULL REFERENCES documents(sha256),
        source_page INTEGER,
        extraction_status TEXT NOT NULL DEFAULT 'auto'
            CHECK(extraction_status IN ('auto', 'confirmed', 'corrected', 'pending', 'rejected')),
        raw_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(date, name, specimen, source_doc)
    );

    INSERT INTO labs_new (
        id, date, loinc_code, name, name_raw, value, value_text, ucum_unit,
        ref_low, ref_high, ref_text, flag, specimen, source_doc, source_page,
        extraction_status, raw_json, created_at
    )
    SELECT id, date, loinc_code, name, name_raw, value, value_text, ucum_unit,
           ref_low, ref_high, ref_text, flag, 'unknown', source_doc, source_page,
           extraction_status, raw_json, created_at
    FROM labs;

    DROP TABLE labs_fts;
    DROP TABLE labs;
    ALTER TABLE labs_new RENAME TO labs;

    CREATE INDEX labs_by_analyte ON labs(name, date);

    CREATE VIRTUAL TABLE labs_fts USING fts5(
        name, name_raw, content='labs', content_rowid='id'
    );
    INSERT INTO labs_fts(rowid, name, name_raw) SELECT id, name, name_raw FROM labs;

    CREATE TRIGGER labs_ai AFTER INSERT ON labs BEGIN
        INSERT INTO labs_fts(rowid, name, name_raw) VALUES (new.id, new.name, new.name_raw);
    END;

    CREATE TRIGGER labs_ad AFTER DELETE ON labs BEGIN
        INSERT INTO labs_fts(labs_fts, rowid, name, name_raw)
        VALUES('delete', old.id, old.name, old.name_raw);
    END;

    CREATE TRIGGER labs_au AFTER UPDATE ON labs BEGIN
        INSERT INTO labs_fts(labs_fts, rowid, name, name_raw)
        VALUES('delete', old.id, old.name, old.name_raw);
        INSERT INTO labs_fts(rowid, name, name_raw) VALUES (new.id, new.name, new.name_raw);
    END;
    """,
    # Migration 3: the document-TEXT layer (docs/adr/0015-document-text-corpus.md).
    # One row per (source_doc, page) - `page` is NULL for a docx/plain-text
    # document (no page structure); an `id` column (not `source_doc`) backs
    # the FTS5 external-content table because FTS5's `content_rowid` must be
    # an INTEGER PRIMARY KEY-backed rowid, and `source_doc` (a sha256 TEXT)
    # can't serve as one - mirrors `labs`/`labs_fts`'s own `id`/`content_rowid`
    # pairing above.
    """
    CREATE TABLE document_text (
        id INTEGER PRIMARY KEY,
        source_doc TEXT NOT NULL REFERENCES documents(sha256),
        page INTEGER,
        text TEXT NOT NULL,
        extracted_at TEXT NOT NULL
    );

    CREATE INDEX document_text_by_source ON document_text(source_doc);

    CREATE VIRTUAL TABLE document_text_fts USING fts5(
        text, content='document_text', content_rowid='id'
    );

    CREATE TRIGGER document_text_ai AFTER INSERT ON document_text BEGIN
        INSERT INTO document_text_fts(rowid, text) VALUES (new.id, new.text);
    END;

    CREATE TRIGGER document_text_ad AFTER DELETE ON document_text BEGIN
        INSERT INTO document_text_fts(document_text_fts, rowid, text)
        VALUES('delete', old.id, old.text);
    END;

    CREATE TRIGGER document_text_au AFTER UPDATE ON document_text BEGIN
        INSERT INTO document_text_fts(document_text_fts, rowid, text)
        VALUES('delete', old.id, old.text);
        INSERT INTO document_text_fts(rowid, text) VALUES (new.id, new.text);
    END;
    """,
    # ADR 0025: a result reported as a BOUND ("<20", ">150") is still a
    # number. 183 real rows kept theirs in `value_text`, invisible to every
    # numeric consumer. Purely additive - an existing row keeps
    # `comparator IS NULL`, which reads as "point measurement", so nothing
    # already stored changes meaning. `adoc labs-revalidate` is what
    # backfills the rows whose number is still trapped in text.
    """
    ALTER TABLE labs ADD COLUMN comparator TEXT
        CHECK(comparator IN ('<', '<=', '>', '>=') OR comparator IS NULL);
    """,
]

_CORRECTABLE_FIELDS = {
    "date",
    "loinc_code",
    "name",
    "name_raw",
    "value",
    "comparator",
    "value_text",
    "ucum_unit",
    "ref_low",
    "ref_high",
    "ref_text",
    "flag",
    "specimen",
    "source_page",
}


def _row_to_document(row: sqlite3.Row) -> LabDocument:
    return LabDocument.model_validate(dict(row))


def _row_to_lab(row: sqlite3.Row) -> LabResult:
    return LabResult.model_validate(dict(row))


def _document_params(doc: LabDocument) -> tuple[Any, ...]:
    return (
        doc.sha256,
        doc.filename,
        doc.doc_type,
        doc.doc_date.isoformat() if doc.doc_date else None,
        doc.page_count,
        doc.ingested_at.isoformat(),
        doc.status.value,
    )


def _lab_params(row: LabResult) -> tuple[Any, ...]:
    return (
        row.date.isoformat(),
        row.loinc_code,
        row.name,
        row.name_raw,
        row.value,
        row.comparator,
        row.value_text,
        row.ucum_unit,
        row.ref_low,
        row.ref_high,
        row.ref_text,
        row.flag.value if row.flag else None,
        row.specimen,
        row.source_doc,
        row.source_page,
        row.extraction_status.value,
        row.raw_json,
        row.created_at.isoformat(),
    )


def _fts_query(text: str) -> str:
    """Build a safe FTS5 MATCH query: AND of phrase-quoted tokens.

    Quoting each token as an FTS5 string literal avoids MATCH syntax errors
    on punctuation in free-text search input. Appropriate for a short,
    deliberate search term (an analyte name, a case-file grep phrase) —
    every token must match, which is the right default when the caller
    picked every word on purpose.
    """
    tokens = re.findall(r"\w+", text)
    if not tokens:
        return '""'
    return " AND ".join(f'"{t}"' for t in tokens)


def _fts_query_any(text: str) -> str:
    """Build a safe FTS5 MATCH query: OR of phrase-quoted tokens, ranked by
    FTS5's own bm25 relevance (`ORDER BY rank`).

    Used by `search_document_text` (docs/adr/0015-document-text-corpus.md),
    whose `query` is typically a whole, unedited conversational message
    (a chat turn, a patient's intake reply) rather than a short deliberate
    search phrase — requiring EVERY word to match (`_fts_query`'s AND
    semantics) would make a full sentence match almost nothing in
    practice. OR lets a passage sharing even one distinctive word rank and
    surface, with bm25 naturally favoring a passage that matches more of
    the query's terms over one that matches only one.
    """
    tokens = re.findall(r"\w+", text)
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


# Deliberately private copies of `ingest.reconcile`'s `parse_ref_range`/
# `parse_flag`, used by `resolve_with_pass` below - `labs` is a lower layer
# than `ingest`, so it never imports from it (that
# would invert the dependency direction even though no runtime import
# cycle would actually result); these two are tiny and stable enough that
# duplicating them here is simpler than restructuring either module.
_REF_RANGE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*[-–]\s*([0-9]*\.?[0-9]+)\s*$")


def _parse_ref_range(ref_range_raw: str | None) -> tuple[float | None, float | None]:
    if not ref_range_raw:
        return None, None
    match = _REF_RANGE_RE.match(ref_range_raw)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def _parse_flag(flag_raw: str | None) -> LabFlag | None:
    if not flag_raw:
        return None
    try:
        return LabFlag(flag_raw.strip().upper())
    except ValueError:
        return None


def _readings_identical(existing: LabResult, new: LabResult) -> bool:
    """True iff `existing` and `new` report the same reading
    (`value`/`value_text`/`ucum_unit`) - `insert_results`'s case (c)/(d)
    split for a row colliding with one already occupying its UNIQUE key."""
    return (
        existing.value == new.value
        and existing.value_text == new.value_text
        and existing.ucum_unit == new.ucum_unit
    )


def _reading_display(row: LabResult) -> str:
    """A short human-readable rendering of `row`'s reading, for a
    `re_extraction_conflict` reason message."""
    if row.value is not None:
        return str(row.value)
    return str(row.value_text)


class ResolutionConvergedError(Exception):
    """`resolve_with_pass` found the chosen pass's reading already exists as
    another row of the same document - the queue item was rejected as that
    row's duplicate instead of being updated (see resolve_with_pass)."""

    def __init__(self, *, row_id: int, existing_id: int) -> None:
        self.row_id = row_id
        self.existing_id = existing_id
        super().__init__(
            f"row {row_id} converged onto existing row {existing_id}; rejected as duplicate"
        )


def _synchronized[**P, T](
    method: Callable[Concatenate[LabsDb, P], T],
) -> Callable[Concatenate[LabsDb, P], T]:
    """Serialize one `LabsDb` method call on the instance's `self._lock`.

    Applied explicitly to every `LabsDb` method (public and private) that
    touches `self._conn` in any way - `execute`/`executemany`/`executescript`,
    `commit`, or a fresh `cursor()` - including ones that only touch it
    indirectly by calling another such method. The `with self._lock:` spans
    the WHOLE method body, so `execute(...)` and the `fetchall()`/
    `fetchone()`/`commit()` that consumes its result always run as one
    atomic unit from another thread's point of view; a lock that were
    released between `execute` and `fetch` would not prevent the
    interleaving that causes `sqlite3.InterfaceError: bad parameter or
    other API misuse` (see `LabsDb.__init__`). `self._lock` is a
    `threading.RLock`, not a plain `Lock`, because several of these methods
    call other synchronized methods on `self` (e.g. `insert_results` calls
    `_find_at_key`/`_insert_new_row`, `resolve_with_pass` calls `get_row`) -
    a plain lock would deadlock a thread against itself on that re-entry.
    A decorator (rather than a metaclass or `__getattribute__` override) so
    the next reader can see exactly which methods are guarded just by
    reading their definitions. Typed with `ParamSpec`/`Concatenate` (not
    `Callable[..., _T]`) so each decorated method keeps its own precise
    parameter types under mypy instead of degrading every call site to
    `Any` args.
    """

    @wraps(method)
    def wrapper(self: LabsDb, *args: P.args, **kwargs: P.kwargs) -> T:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class LabsDb:
    """Sqlite-backed store for the `documents`/`labs` tables (stdlib sqlite3)."""

    def __init__(self, path: str | Path, *, journal_mode: str = "WAL") -> None:
        """
        `journal_mode` (default `"WAL"`) is deliberately a constructor
        parameter, not a hardcoded pragma: WAL relies on a shared-memory
        index file (`-wal`/`-shm`) coordinated via `mmap` + POSIX advisory
        (`fcntl`) locks between writers, and both NFS and EFS (its AWS
        equivalent) have historically unreliable/non-atomic advisory-lock
        and mmap write-back semantics across clients — SQLite's own docs
        warn WAL must not be used on a network filesystem, where it can
        silently corrupt the database. WAL is safe and fast for local/dev/
        test runs (the default here), but the ECS Fargate deployment mounts
        the data directory from EFS (see `deploy/cfn/ecs.yaml`) and sets
        `ADOC_SQLITE_JOURNAL_MODE=TRUNCATE` (via `config.Settings.
        sqlite_journal_mode`) so the deployed app never uses WAL against
        EFS. TRUNCATE keeps ordinary rollback-journal semantics (a single
        journal file, no shared-memory index) and is safe on NFS/EFS,
        provided there is still only ever one writer — see the single-writer
        discipline note in `deploy/cfn/ecs.yaml`'s web service (desired
        count 1, `MaximumPercent: 100`/`MinimumHealthyPercent: 0` deployment
        so old and new tasks never run concurrently) and ADR 0006.
        """
        self._path = path
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # `self._lock` is created before anything else touches `self._conn`
        # (including `_migrate()` below) - every `@_synchronized` method
        # assumes `self._lock` already exists, and `_migrate()` is one of
        # them.
        self._lock = threading.RLock()
        with self._lock:
            # check_same_thread=False: a single LabsDb instance is shared
            # across an ASGI server's worker threads (FastAPI runs sync
            # routes in a thread pool) and across a test client's per-call
            # portal thread. This comment used to claim the shared
            # connection was "never touched concurrently from two threads
            # at once ... only sequentially from different ones" - that was
            # FALSE: a browser issuing multiple requests (the user clicking
            # between pages, or a slow page leaving a wide window) lands
            # concurrently in the threadpool, and two threads driving one
            # sqlite3 connection at the same time is exactly what produced
            # the production crash this class now guards against
            # (`sqlite3.InterfaceError: bad parameter or other API misuse`,
            # raised from `LabsDb.series` while `/upload`, `/reviews`, and
            # `/confirm` were being served in the same second). Sharing one
            # connection across threads is only safe because every method
            # below holds `self._lock` (a `threading.RLock`, see
            # `_synchronized`) for the full duration of its sqlite3 calls;
            # `check_same_thread=False` merely disables sqlite3's own
            # (weaker, same-thread-only) guard so that sharing is allowed
            # to happen at all - the RLock is what actually makes it safe.
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA journal_mode={journal_mode}")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @_synchronized
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LabsDb:
        return self

    @_synchronized
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @_synchronized
    def _migrate(self) -> None:
        current: int = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for version, script in enumerate(_MIGRATIONS, start=1):
            if version <= current:
                continue
            self._conn.executescript(script)
            self._conn.execute(f"PRAGMA user_version = {version}")
        self._conn.commit()

    # ----------------------------------------------------------------
    # Documents
    # ----------------------------------------------------------------

    @_synchronized
    def upsert_document(self, doc: LabDocument) -> None:
        self._conn.execute(
            """
            INSERT INTO documents (sha256, filename, doc_type, doc_date, page_count,
                                    ingested_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                filename = excluded.filename,
                doc_type = excluded.doc_type,
                doc_date = excluded.doc_date,
                page_count = excluded.page_count,
                ingested_at = excluded.ingested_at,
                status = excluded.status
            """,
            _document_params(doc),
        )
        self._conn.commit()

    @_synchronized
    def get_document(self, sha256: str) -> LabDocument | None:
        row = self._conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
        return _row_to_document(row) if row else None

    @_synchronized
    def list_documents(self) -> list[LabDocument]:
        rows = self._conn.execute("SELECT * FROM documents ORDER BY ingested_at DESC").fetchall()
        return [_row_to_document(row) for row in rows]

    @_synchronized
    def documents_overview(self) -> list[DocumentOverview]:
        """Every ingested document, newest first, alongside its
        accepted/awaiting-review lab-row counts — the "Documents >
        Consumed" page's per-document summary (see `DocumentOverview`)."""
        rows = self._conn.execute(
            "SELECT source_doc, extraction_status, COUNT(*) AS n FROM labs "
            "GROUP BY source_doc, extraction_status"
        ).fetchall()
        counts_by_doc: dict[str, dict[str, int]] = defaultdict(dict)
        for row in rows:
            counts_by_doc[row["source_doc"]][row["extraction_status"]] = row["n"]

        accepted_statuses = (
            ExtractionStatus.AUTO.value,
            ExtractionStatus.CONFIRMED.value,
            ExtractionStatus.CORRECTED.value,
        )
        overviews: list[DocumentOverview] = []
        for doc in self.list_documents():
            by_status = counts_by_doc.get(doc.sha256, {})
            accepted = sum(by_status.get(status, 0) for status in accepted_statuses)
            awaiting = by_status.get(ExtractionStatus.PENDING.value, 0)
            overviews.append(
                DocumentOverview(
                    document=doc, accepted_count=accepted, awaiting_review_count=awaiting
                )
            )
        return overviews

    # ----------------------------------------------------------------
    # Labs: insert + confirm queue
    # ----------------------------------------------------------------

    @_synchronized
    def insert_results(self, results: Sequence[LabResult]) -> list[int | None]:
        """Insert lab rows, one `id | None` per input row (in order).

        A row's key is `(date, name, specimen, source_doc)` (the UNIQUE
        constraint). Before inserting, the key is looked up so a re-
        extraction of the same document (e.g. after a prompt/model change,
        `adoc backfill --re-extract`) is never a silent no-op against a row
        a human already resolved one way or another (D1):

          (a) no existing row at the key -> insert normally.
          (b) existing row is REJECTED -> a human rejected THAT reading;
              this is a fresh extraction at the same key, so it REVIVES the
              row instead of leaving the correction stranded behind a dead
              rejected row: overwrite its fields with the new reading, flip
              it back to PENDING for a fresh human look, and merge raw_json
              (the old payload survives under `"superseded_rejection"`,
              plus a `"re_extraction_after_rejection"` reason).
          (c) existing row is anything else (auto/confirmed/corrected/
              pending) and the new reading's value/value_text/ucum_unit are
              IDENTICAL to the existing row's -> ordinary dedupe, `None`
              (unchanged behavior).
          (d) same as (c) but the new reading DIFFERS -> never silently
              drop the correction: flip the existing row to PENDING and
              merge the new reading into raw_json under
              `"re_extraction_conflict"`, with a reason naming both
              readings, so a human resolves the disagreement instead of it
              vanishing.
        """
        ids: list[int | None] = []
        for row in results:
            existing = self._find_at_key(row)
            if existing is None:
                ids.append(self._insert_new_row(row))
            elif existing.extraction_status == ExtractionStatus.REJECTED:
                ids.append(self._revive_rejected_row(existing, row))
            elif _readings_identical(existing, row):
                ids.append(None)
            else:
                ids.append(self._flip_to_pending_with_conflict(existing, row))
        self._conn.commit()
        return ids

    @_synchronized
    def _find_at_key(self, row: LabResult) -> LabResult | None:
        """The row (any `extraction_status`) already occupying `row`'s
        `(date, name, specimen, source_doc)` UNIQUE key, if any."""
        found = self._conn.execute(
            "SELECT * FROM labs WHERE date = ? AND name = ? AND specimen = ? AND source_doc = ?",
            (row.date.isoformat(), row.name, row.specimen, row.source_doc),
        ).fetchone()
        return _row_to_lab(found) if found else None

    @_synchronized
    def _insert_new_row(self, row: LabResult) -> int | None:
        cur = self._conn.execute(
            """
            INSERT INTO labs (
                date, loinc_code, name, name_raw, value, comparator, value_text, ucum_unit,
                ref_low, ref_high, ref_text, flag, specimen, source_doc, source_page,
                extraction_status, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, name, specimen, source_doc) DO NOTHING
            """,
            _lab_params(row),
        )
        return cur.lastrowid if cur.rowcount else None

    @_synchronized
    def _revive_rejected_row(self, existing: LabResult, new: LabResult) -> int:
        """Case (b): a prior human rejection at this key, now superseded by
        a fresh extraction - overwrite the row's fields with the new
        reading and requeue it PENDING rather than leaving the correction
        stranded behind the dead rejected row."""
        assert existing.id is not None
        payload = new.raw_payload()
        payload["superseded_rejection"] = existing.raw_payload()
        reasons = list(payload.get("reasons", []))
        if "re_extraction_after_rejection" not in reasons:
            reasons.insert(0, "re_extraction_after_rejection")
        payload["reasons"] = reasons
        self._conn.execute(
            """
            UPDATE labs SET
                date = ?, loinc_code = ?, name = ?, name_raw = ?, value = ?, comparator = ?,
                value_text = ?, ucum_unit = ?, ref_low = ?, ref_high = ?, ref_text = ?,
                flag = ?, specimen = ?, source_page = ?, extraction_status = ?, raw_json = ?
            WHERE id = ?
            """,
            (
                new.date.isoformat(),
                new.loinc_code,
                new.name,
                new.name_raw,
                new.value,
                new.comparator,
                new.value_text,
                new.ucum_unit,
                new.ref_low,
                new.ref_high,
                new.ref_text,
                new.flag.value if new.flag else None,
                new.specimen,
                new.source_page,
                ExtractionStatus.PENDING.value,
                json.dumps(payload),
                existing.id,
            ),
        )
        return existing.id

    @_synchronized
    def _flip_to_pending_with_conflict(self, existing: LabResult, new: LabResult) -> int:
        """Case (d): the existing (already-resolved-one-way-or-another) row
        at this key disagrees with a fresh extraction's reading. The
        existing row's fields are left untouched (a confirmed/corrected
        human decision is never silently overwritten by an unconfirmed
        re-extraction) - only its status flips to PENDING and the new
        reading is merged into raw_json for a human to resolve."""
        assert existing.id is not None
        payload = existing.raw_payload()
        payload["re_extraction_conflict"] = new.raw_payload()
        reasons = list(payload.get("reasons", []))
        reason = (
            f"re_extraction_conflict: existing={_reading_display(existing)!r} "
            f"vs new={_reading_display(new)!r}"
        )
        if reason not in reasons:
            reasons.insert(0, reason)
        payload["reasons"] = reasons
        self._conn.execute(
            "UPDATE labs SET extraction_status = ?, raw_json = ? WHERE id = ?",
            (ExtractionStatus.PENDING.value, json.dumps(payload), existing.id),
        )
        return existing.id

    @_synchronized
    def pending(self) -> list[LabResult]:
        """Rows awaiting human confirmation (the confirm queue)."""
        rows = self._conn.execute(
            "SELECT * FROM labs WHERE extraction_status = ? ORDER BY date, id",
            (ExtractionStatus.PENDING.value,),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def pending_grouped(self) -> list[PendingRow]:
        """PENDING rows joined with their document, ordered by document
        date descending (then by the row's own date/id).

        The confirm queue's "models disagreed" bucket wants newest
        documents surfaced first (PLAN.md "Ingestion" triage); grouping
        the flat list by `PendingRow.row.source_doc` is left to the
        caller so it can further split rows into buckets first (see
        `web.routes.confirm`). SQLite orders `NULL` dates last in a
        `DESC` sort, so documents with no resolved date sink to the
        bottom rather than jumping to the front.
        """
        rows = self._conn.execute(
            """
            SELECT labs.*,
                   documents.filename AS doc_filename,
                   documents.doc_date AS doc_doc_date,
                   documents.doc_type AS doc_doc_type,
                   documents.page_count AS doc_page_count
            FROM labs
            JOIN documents ON documents.sha256 = labs.source_doc
            WHERE labs.extraction_status = ?
            ORDER BY documents.doc_date DESC, labs.date, labs.id
            """,
            (ExtractionStatus.PENDING.value,),
        ).fetchall()
        result: list[PendingRow] = []
        for row in rows:
            doc_date_raw = row["doc_doc_date"]
            result.append(
                PendingRow(
                    row=_row_to_lab(row),
                    doc_filename=row["doc_filename"],
                    doc_date=date.fromisoformat(doc_date_raw) if doc_date_raw else None,
                    doc_type=row["doc_doc_type"],
                    doc_page_count=row["doc_page_count"],
                )
            )
        return result

    @_synchronized
    def lab_counts_by_document(self) -> dict[str, int]:
        """Total lab-row count (any `extraction_status`) per document
        `sha256`. The confirm queue's per-document header derives its
        "done" count as `total - pending` from this."""
        rows = self._conn.execute(
            "SELECT source_doc, COUNT(*) AS n FROM labs GROUP BY source_doc"
        ).fetchall()
        return {row["source_doc"]: row["n"] for row in rows}

    @_synchronized
    def get_row(self, row_id: int) -> LabResult | None:
        row = self._conn.execute("SELECT * FROM labs WHERE id = ?", (row_id,)).fetchone()
        return _row_to_lab(row) if row else None

    @_synchronized
    def confirm_row(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE labs SET extraction_status = ? WHERE id = ?",
            (ExtractionStatus.CONFIRMED.value, row_id),
        )
        self._conn.commit()

    @_synchronized
    def bulk_confirm(self, ids: Sequence[int]) -> int:
        """Confirm every currently-PENDING row in `ids`; returns how many
        rows were actually updated.

        Rows in `ids` that are unknown or already resolved are silently
        skipped rather than erroring - mirrors `confirm_row`'s per-row
        idempotence, and lets a caller safely re-derive `ids` from a
        possibly-stale read (e.g. a bulk-confirm request racing a
        concurrent single-row action) without double-counting.
        """
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cur = self._conn.execute(
            f"UPDATE labs SET extraction_status = ? "
            f"WHERE id IN ({placeholders}) AND extraction_status = ?",
            (ExtractionStatus.CONFIRMED.value, *ids, ExtractionStatus.PENDING.value),
        )
        self._conn.commit()
        return cur.rowcount

    @_synchronized
    def correct_row(self, row_id: int, **fields: Any) -> None:
        """Apply a human correction to `row_id` and mark it `corrected`.

        `fields` must be a subset of `_CORRECTABLE_FIELDS`; the original
        extraction is left intact in `raw_json` for audit.
        """
        if not fields:
            raise ValueError("correct_row requires at least one field to correct")
        unknown = set(fields) - _CORRECTABLE_FIELDS
        if unknown:
            raise ValueError(f"correct_row: not a correctable field: {sorted(unknown)}")

        set_clauses: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key == "date" and isinstance(value, date):
                value = value.isoformat()
            elif key == "flag" and isinstance(value, LabFlag):
                value = value.value
            set_clauses.append(f"{key} = ?")
            params.append(value)
        set_clauses.append("extraction_status = ?")
        params.append(ExtractionStatus.CORRECTED.value)
        params.append(row_id)

        self._conn.execute(
            f"UPDATE labs SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        self._conn.commit()

    @_synchronized
    def resolve_with_pass(self, row_id: int, which: Literal["a", "b"]) -> None:
        """Apply pass A's or pass B's reading wholesale to a disagreement
        row and mark it `corrected` (the confirm queue's "Use reading A"/
        "Use reading B" actions).

        Reads the chosen pass's payload straight out of `row_id`'s own
        `raw_json` (`ingest.reconcile.reconcile` serializes both passes'
        raw `ExtractedResult`s there - see that module's docstring), and
        overwrites `name`, `name_raw`, `value`, `value_text`, `ucum_unit`,
        `ref_low`/`ref_high`/`ref_text`, `flag`, and `specimen` with that
        pass's fields. The original extraction (both passes) is left
        intact in `raw_json`; only a `resolved_with: "pass_a"|"pass_b"`
        audit marker is added alongside it.
        """
        if which not in ("a", "b"):
            raise ValueError(f"resolve_with_pass: which must be 'a' or 'b', got {which!r}")
        row = self.get_row(row_id)
        if row is None:
            raise ValueError(f"resolve_with_pass: no such row {row_id}")

        payload = row.raw_payload()
        pass_key = f"pass_{which}"
        pass_data: dict[str, Any] | None = payload.get(pass_key)
        if pass_data is None:
            raise ValueError(f"resolve_with_pass: row {row_id} has no {pass_key!r} data to apply")

        name_raw = pass_data.get("name_raw") or row.name_raw
        # Exact alias only - `resolve_with_pass` WRITES a stored name, so
        # the permissive `canonicalize` (suffix-strip/score-suffix rules)
        # must not name the row (labs.validate, "Matching vs. renaming").
        canonical = canonical_rename_target(name_raw, name_raw)
        ref_low, ref_high = _parse_ref_range(pass_data.get("ref_range_raw"))
        flag = _parse_flag(pass_data.get("flag_raw"))

        # Applying this pass's name/specimen may converge onto a row that
        # ALREADY exists for the same (date, name, specimen, source_doc) -
        # the unpaired other-pass twin of this very reading. That is a
        # resolution, not an error: reject this queue item as the twin's
        # duplicate instead of violating the UNIQUE constraint.
        new_name = canonical or name_raw
        new_specimen = pass_data.get("specimen") or row.specimen
        collision = self._conn.execute(
            """
            SELECT id FROM labs
            WHERE date = ? AND name = ? AND specimen = ? AND source_doc = ? AND id != ?
            """,
            (row.date.isoformat(), new_name, new_specimen, row.source_doc, row_id),
        ).fetchone()
        if collision is not None:
            self.reject_row_as_twin(row_id, twin_of=int(collision[0]), method="rule")
            raise ResolutionConvergedError(row_id=row_id, existing_id=int(collision[0]))

        payload["resolved_with"] = f"pass_{which}"
        new_raw_json = json.dumps(payload)

        self._conn.execute(
            """
            UPDATE labs SET
                name = ?, name_raw = ?, value = ?, value_text = ?, ucum_unit = ?,
                ref_low = ?, ref_high = ?, ref_text = ?, flag = ?, specimen = ?,
                extraction_status = ?, raw_json = ?
            WHERE id = ?
            """,
            (
                canonical or name_raw,
                name_raw,
                pass_data.get("value"),
                pass_data.get("value_text"),
                pass_data.get("unit_raw"),
                ref_low,
                ref_high,
                pass_data.get("ref_range_raw"),
                flag.value if flag else None,
                pass_data.get("specimen") or "unknown",
                ExtractionStatus.CORRECTED.value,
                new_raw_json,
                row_id,
            ),
        )
        self._conn.commit()

    @_synchronized
    def reject_row(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE labs SET extraction_status = ? WHERE id = ?",
            (ExtractionStatus.REJECTED.value, row_id),
        )
        self._conn.commit()

    @_synchronized
    def mark_single_pass_as_name_variant(self, row_id: int, *, other_name: str) -> None:
        """Upgrade a `single_pass` PENDING row that the twin sweep paired
        with its opposite-pass twin: the `single_pass` reason becomes
        `name_variant` (the agreed bucket) and the twin's differently-worded
        name is recorded for audit. Status stays PENDING - a human still
        OKs it, just via the bulk "models agreed" path instead of the
        disagreement path.
        """
        row = self.get_row(row_id)
        if row is None:
            raise ValueError(f"mark_single_pass_as_name_variant: no such row {row_id}")
        payload = row.raw_payload()
        reasons = [r for r in payload.get("reasons", []) if r != "single_pass"]
        if "name_variant" not in reasons:
            reasons.insert(0, "name_variant")
        payload["reasons"] = reasons
        payload["name_variant_of"] = other_name
        self._conn.execute(
            "UPDATE labs SET raw_json = ? WHERE id = ?",
            (json.dumps(payload), row_id),
        )
        self._conn.commit()

    @_synchronized
    def reclassify_row(self, row_id: int, *, reasons: list[str], auto: bool, at: datetime) -> None:
        """Apply `labs.reclassify.reclassify_pending`'s recomputed reason
        list to a still-PENDING row: `auto=True` flips `extraction_status`
        to `AUTO` (every reason the old literal comparators manufactured
        turned out to be a false positive); `auto=False` leaves it PENDING
        but rewrites `raw_json["reasons"]` so the confirm queue's agreed-
        vs-disagreed bucketing (`row_is_agreed`) reflects the current
        comparators. The original reason list is preserved as
        `previous_reasons` for audit, alongside a `reclassified_at`
        timestamp - every other key in `raw_json` (both passes' full
        payloads) is left untouched.
        """
        row = self.get_row(row_id)
        if row is None:
            raise ValueError(f"reclassify_row: no such row {row_id}")
        payload = row.raw_payload()
        payload["previous_reasons"] = payload.get("reasons", [])
        payload["reasons"] = reasons
        payload["reclassified_at"] = at.isoformat()
        status = ExtractionStatus.AUTO.value if auto else ExtractionStatus.PENDING.value
        self._conn.execute(
            "UPDATE labs SET extraction_status = ?, raw_json = ? WHERE id = ?",
            (status, json.dumps(payload), row_id),
        )
        self._conn.commit()

    @_synchronized
    def reject_row_as_twin(
        self,
        row_id: int,
        *,
        twin_of: int,
        method: Literal["rule", "llm"],
        model_id: str | None = None,
        prompt_template_version: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Reject `row_id` as a duplicate ("twin") of `twin_of` - the
        legacy-row LLM twin sweep, `labs/twins.py` / `adoc
        labs-dedupe-twins`. Same status change as `reject_row`, plus an
        audit note (`auto_rejected_twin_of`, `method`) merged into
        `row_id`'s own `raw_json` so the rejection's provenance survives
        alongside the original extraction.

        CONFIRMED bug fix (CLAUDE.md "Every persisted LLM-derived artifact
        carries provenance"): a `method="llm"` rejection used to persist
        only `method` - no `model_id`, no `prompt_template_version`, no
        timestamp - so PLAN.md's staleness/re-evaluation policy had
        nothing to key on: rebinding the `classifier` role or bumping
        `labs.twins.TWIN_CLASSIFY_PROMPT_VERSION` couldn't tell which
        already-rejected rows were decided under the old binding/prompt.
        `model_id`/`prompt_template_version` are optional here (`None` for
        `method="rule"`, where no LLM was ever called - see
        `resolve_with_pass`'s internal `method="rule"` call above, and
        `labs/twins.py`'s deterministic `names_equivalent_by_rule` path -
        stamping a model/prompt version on a decision no model made would
        fabricate provenance, not record it) but REQUIRED in practice for
        every `method="llm"` call site (`labs/twins.py`'s
        `sweep_twins`/`_retro_pair_pending_twins`, which now thread
        `LlmClient.complete`'s returned `LlmResult.model_id` and
        `TWIN_CLASSIFY_PROMPT_VERSION` through). `at` defaults to
        `datetime.now(UTC)` - a caller may pass an explicit value for
        deterministic tests.
        """
        row = self.get_row(row_id)
        if row is None:
            raise ValueError(f"reject_row_as_twin: no such row {row_id}")
        payload = row.raw_payload()
        payload["auto_rejected_twin_of"] = twin_of
        payload["method"] = method
        if model_id is not None:
            payload["model_id"] = model_id
        if prompt_template_version is not None:
            payload["prompt_template_version"] = prompt_template_version
        payload["rejected_at"] = (at or datetime.now(UTC)).isoformat()
        self._conn.execute(
            "UPDATE labs SET extraction_status = ?, raw_json = ? WHERE id = ?",
            (ExtractionStatus.REJECTED.value, json.dumps(payload), row_id),
        )
        self._conn.commit()

    @_synchronized
    def resolved_rows_for_document(self, source_doc: str) -> list[LabResult]:
        """Rows in `source_doc` already past human/auto review (`auto`,
        `confirmed`, `corrected`) - the candidate pool for `labs/twins.py`'s
        deterministic twin gate. A still-PENDING row is never compared
        against another still-PENDING row - only against ones already
        resolved one way or another."""
        statuses = (
            ExtractionStatus.AUTO.value,
            ExtractionStatus.CONFIRMED.value,
            ExtractionStatus.CORRECTED.value,
        )
        placeholders = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM labs WHERE source_doc = ? AND extraction_status IN ({placeholders}) "
            "ORDER BY id",
            (source_doc, *statuses),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    # ----------------------------------------------------------------
    # Read side
    # ----------------------------------------------------------------

    @_synchronized
    def series(
        self, name: str, specimen: Specimen | None = None, *, include_rejected: bool = False
    ) -> list[LabResult]:
        """Time-ordered trend series for one canonical analyte name.

        `specimen=None` (the default) means "all specimens" — back-compat
        with callers that don't yet care about the dimension (and with
        pre-migration data, which is all `"unknown"`). Passing a specimen
        scopes the series to just that one, so e.g. a serum glucose trend
        never includes a urinalysis GLUCOSE "NEGATIVE" reading that happens
        to canonicalize to the same `name` (see `labs/models.py`'s
        `Specimen` docstring for the finding that motivated this).
        """
        clauses = ["name = ?"]
        params: list[Any] = [name]
        if specimen is not None:
            clauses.append("specimen = ?")
            params.append(specimen)
        if not include_rejected:
            clauses.append("extraction_status != ?")
            params.append(ExtractionStatus.REJECTED.value)
        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM labs WHERE {where} ORDER BY date, id", params
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def series_by_key(
        self, *, include_rejected: bool = False
    ) -> dict[tuple[str, str], list[LabResult]]:
        """Every analyte's trend series at once, keyed by `(name, specimen)`.

        Same rows, same order (`date, id`), and same rejected-row filtering
        as calling `series(name, specimen)` once per analyte — but in ONE
        query instead of one per analyte.

        This exists because the labs index page renders a sparkline for
        every analyte: on local disk the per-analyte version was invisible
        (~0.02 ms/query), but the deployed app reads `labs.sqlite` over
        EFS/NFS, where each query costs milliseconds of round-trip. At ~450
        analytes that turned into seconds of pure latency on one page load.
        """
        clauses = []
        params: list[Any] = []
        if not include_rejected:
            clauses.append("extraction_status != ?")
            params.append(ExtractionStatus.REJECTED.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM labs {where} ORDER BY date, id", params
        ).fetchall()
        grouped: dict[tuple[str, str], list[LabResult]] = defaultdict(list)
        for row in rows:
            result = _row_to_lab(row)
            grouped[(result.name, result.specimen)].append(result)
        return dict(grouped)

    @_synchronized
    def latest_panel(self) -> list[LabResult]:
        """Most recent non-rejected row per distinct (analyte name, specimen).

        Grouping includes `specimen` (not just `name`) so that, e.g., a
        serum glucose reading and a urinalysis GLUCOSE reading — both
        canonicalized to `name = "glucose"` but different specimens — each
        surface as their own "latest" row instead of one silently hiding
        the other's.
        """
        rows = self._conn.execute(
            """
            SELECT l.* FROM labs l
            JOIN (
                SELECT name, specimen, MAX(date) AS max_date FROM labs
                WHERE extraction_status != ?
                GROUP BY name, specimen
            ) latest ON latest.name = l.name AND latest.specimen = l.specimen
                AND latest.max_date = l.date
            WHERE l.extraction_status != ?
            ORDER BY l.name, l.specimen
            """,
            (ExtractionStatus.REJECTED.value, ExtractionStatus.REJECTED.value),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def abnormal_since(self, since: date) -> list[LabResult]:
        """Flagged rows on/after `since`, most recent first.

        `specimen` is carried through for free: this already selects full
        rows (`SELECT *`), so each row's `specimen` is populated exactly as
        stored — nothing to filter here, `abnormal_summary` callers that
        care about specimen read it straight off each returned `LabResult`.
        """
        rows = self._conn.execute(
            "SELECT * FROM labs WHERE date >= ? AND flag IS NOT NULL "
            "AND extraction_status != ? ORDER BY date DESC, id DESC",
            (since.isoformat(), ExtractionStatus.REJECTED.value),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def all_non_rejected_rows(self) -> list[LabResult]:
        """Every lab row (any date/analyte) whose `extraction_status` isn't
        `rejected`, ordered by id - the working set for `adoc
        labs-recanonicalize` (`labs/recanonicalize.py`)."""
        rows = self._conn.execute(
            "SELECT * FROM labs WHERE extraction_status != ? ORDER BY id",
            (ExtractionStatus.REJECTED.value,),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def distinct_analyte_names(self) -> list[str]:
        """Every analyte name with at least one non-rejected row, ordered.

        For `reason.context`'s trajectory section, which needs to ask "what
        is moving?" across the whole corpus rather than per-analyte.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT name FROM labs WHERE extraction_status != ? ORDER BY name",
            (ExtractionStatus.REJECTED.value,),
        ).fetchall()
        return [row[0] for row in rows]

    @_synchronized
    def all_rows(self) -> list[LabResult]:
        """Every lab row, REJECTED included, ordered by id.

        `all_non_rejected_rows` is the working set for recanonicalization;
        this is for `labs/review.py` (ADR 0026), where a rejection is itself
        a human decision that a rebuild must carry forward — dropping it
        would re-present a row a person already said was wrong as a fresh
        one to review.
        """
        rows = self._conn.execute("SELECT * FROM labs ORDER BY id").fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def rejected_row_keys(self) -> set[tuple[str, str, str, str]]:
        """The `(date, name, specimen, source_doc)` keys currently held by
        REJECTED rows. The table's UNIQUE constraint spans rejected rows
        too, so `adoc labs-recanonicalize`'s planner must treat these
        tombstones as key-occupants: a rename targeting one of these keys
        would raise IntegrityError even though no live row sits there."""
        rows = self._conn.execute(
            "SELECT date, name, specimen, source_doc FROM labs WHERE extraction_status = ?",
            (ExtractionStatus.REJECTED.value,),
        ).fetchall()
        return {(r[0], r[1], r[2], r[3]) for r in rows}

    @_synchronized
    def rename_for_recanonicalization(self, row_id: int, new_name: str) -> None:
        """Set `row_id`'s `name` to `new_name` directly, without touching
        `extraction_status` (mirrors `update_specimen` - a deterministic
        maintenance rewrite, not a human correction) - `adoc
        labs-recanonicalize`'s plain-rename case (no collision at the new
        key)."""
        self._conn.execute("UPDATE labs SET name = ? WHERE id = ?", (new_name, row_id))
        self._conn.commit()

    @_synchronized
    def reject_row_as_recanonicalization_duplicate(self, row_id: int, *, kept_id: int) -> None:
        """Reject `row_id` (status -> REJECTED) as an exact-reading
        duplicate of `kept_id`, discovered when both rows would
        canonicalize to the same `(date, name, specimen, source_doc)` key
        (`adoc labs-recanonicalize`). Same status change as `reject_row`,
        plus an audit note merged into `row_id`'s own `raw_json` recording
        which row survived - `row_id`'s own fields/reading are left
        intact for audit, only `extraction_status`/`raw_json` change."""
        row = self.get_row(row_id)
        if row is None:
            raise ValueError(f"reject_row_as_recanonicalization_duplicate: no such row {row_id}")
        payload = row.raw_payload()
        payload["recanonicalization_duplicate_of"] = kept_id
        self._conn.execute(
            "UPDATE labs SET extraction_status = ?, raw_json = ? WHERE id = ?",
            (ExtractionStatus.REJECTED.value, json.dumps(payload), row_id),
        )
        self._conn.commit()

    @_synchronized
    def flip_to_pending_for_recanonicalization_conflict(
        self, row_id: int, *, conflicting: LabResult
    ) -> None:
        """Flip `row_id` (the row already occupying the target canonical
        key) to PENDING and merge `conflicting`'s full reading into its
        `raw_json` under `"recanonicalize_conflict"` - `adoc
        labs-recanonicalize`'s differing-reading collision case (module
        docstring). `row_id`'s own fields are left untouched (mirrors
        `_flip_to_pending_with_conflict`'s re-extraction-conflict case);
        only its status and `raw_json` change, so a human resolves the
        disagreement via the confirm queue."""
        row = self.get_row(row_id)
        if row is None:
            raise ValueError(
                f"flip_to_pending_for_recanonicalization_conflict: no such row {row_id}"
            )
        payload = row.raw_payload()
        payload["recanonicalize_conflict"] = conflicting.raw_payload()
        reasons = list(payload.get("reasons", []))
        reason = (
            f"recanonicalize_conflict: existing={_reading_display(row)!r} "
            f"vs other={_reading_display(conflicting)!r}"
        )
        if reason not in reasons:
            reasons.insert(0, reason)
        payload["reasons"] = reasons
        self._conn.execute(
            "UPDATE labs SET extraction_status = ?, raw_json = ? WHERE id = ?",
            (ExtractionStatus.PENDING.value, json.dumps(payload), row_id),
        )
        self._conn.commit()

    @_synchronized
    def reject_row_as_superseded_by_recanonicalize_conflict(
        self, row_id: int, *, survivor_id: int
    ) -> None:
        """Reject `row_id` (status -> REJECTED) whose DIFFERING reading was
        just merged into `survivor_id`'s `raw_json` by
        `flip_to_pending_for_recanonicalization_conflict` - `row_id` is
        never renamed (that would collide with `survivor_id`'s key), so it
        would otherwise dangle forever under its old, un-canonical name;
        its full reading survives in `survivor_id`'s payload for a human
        to resolve via the confirm queue."""
        row = self.get_row(row_id)
        if row is None:
            raise ValueError(
                f"reject_row_as_superseded_by_recanonicalize_conflict: no such row {row_id}"
            )
        payload = row.raw_payload()
        payload["superseded_by_recanonicalize_conflict"] = survivor_id
        self._conn.execute(
            "UPDATE labs SET extraction_status = ?, raw_json = ? WHERE id = ?",
            (ExtractionStatus.REJECTED.value, json.dumps(payload), row_id),
        )
        self._conn.commit()

    @_synchronized
    def rows_with_unknown_specimen(self) -> list[LabResult]:
        """Every row (any `extraction_status`) whose `specimen` is still
        `"unknown"` — the working set for `adoc labs-infer-specimen`
        (`labs/specimen.py`)."""
        rows = self._conn.execute(
            "SELECT * FROM labs WHERE specimen = 'unknown' ORDER BY id"
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def update_specimen(self, row_id: int, specimen: Specimen) -> None:
        """Set `row_id`'s `specimen` directly, without touching
        `extraction_status` — used by `adoc labs-infer-specimen`'s
        deterministic maintenance pass, as opposed to `correct_row` (a
        human review action that also marks the row `corrected`)."""
        self._conn.execute("UPDATE labs SET specimen = ? WHERE id = ?", (specimen, row_id))
        self._conn.commit()

    @_synchronized
    def search(self, text: str) -> list[LabResult]:
        """Full-text search over `name`/`name_raw` via the FTS5 index."""
        rows = self._conn.execute(
            """
            SELECT labs.* FROM labs_fts
            JOIN labs ON labs.id = labs_fts.rowid
            WHERE labs_fts MATCH ?
            ORDER BY rank
            """,
            (_fts_query(text),),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    # ----------------------------------------------------------------
    # Document text (docs/adr/0015-document-text-corpus.md)
    # ----------------------------------------------------------------

    @_synchronized
    def replace_document_text(
        self, source_doc: str, pages: Sequence[DocumentTextPage], *, extracted_at: datetime
    ) -> None:
        """Replace every stored page of `source_doc`'s extracted text with
        `pages` — delete-then-insert, so this is idempotent and also the
        primitive both a fresh ingest and a from-scratch rebuild
        (`ingest.doctext.rebuild_document_text_from_files`) share. `pages`
        empty is a valid call (nothing to store — a caller that got no
        pages back from extraction should simply not call this at all;
        kept permissive here rather than raising, since "no rows" is a
        harmless no-op for a delete-then-insert).
        """
        self._conn.execute("DELETE FROM document_text WHERE source_doc = ?", (source_doc,))
        for page in pages:
            self._conn.execute(
                "INSERT INTO document_text (source_doc, page, text, extracted_at) "
                "VALUES (?, ?, ?, ?)",
                (source_doc, page.page, page.text, extracted_at.isoformat()),
            )
        self._conn.commit()

    @_synchronized
    def document_text_shas(self) -> set[str]:
        """Every `source_doc` sha256 with at least one stored text row —
        "already has text extracted" regardless of whether that text is
        empty (a scanned-image PDF that `pdftotext` returned nothing for is
        still covered, so `adoc backfill-doc-text` doesn't retry it forever).
        """
        rows = self._conn.execute("SELECT DISTINCT source_doc FROM document_text").fetchall()
        return {row[0] for row in rows}

    @_synchronized
    def get_document_text(self, source_doc: str) -> str | None:
        """`source_doc`'s full text, pages rejoined with the same form-feed
        (`\\f`) separator `ingest.doctext` splits on — `None` if no text has
        ever been stored for this document (never extracted, or extraction
        failed every time it was attempted)."""
        rows = self._conn.execute(
            "SELECT text FROM document_text WHERE source_doc = ? ORDER BY (page IS NOT NULL), page",
            (source_doc,),
        ).fetchall()
        if not rows:
            return None
        return "\f".join(row[0] for row in rows)

    @_synchronized
    def get_document_page_text(self, source_doc: str, page: int) -> str | None:
        """One page's stored text, or `None` when this document's text was
        stored whole (no per-page split) or that page has none.

        Used by `reason.verify`'s source resolution so a `doc:<file>#p<n>`
        evidence ref is entailment-checked against the page it actually
        cites rather than the whole document — a claim is much easier to
        judge against one page than against thirty."""
        row = self._conn.execute(
            "SELECT text FROM document_text WHERE source_doc = ? AND page = ?",
            (source_doc, page),
        ).fetchone()
        return str(row[0]) if row is not None else None

    @_synchronized
    def search_document_text(self, query: str, *, limit: int = 5) -> list[DocumentTextHit]:
        """Ranked FTS5 snippet search over every document's extracted text
        (`reason.tools.search_documents`/`reason.context`'s document-excerpts
        section). Each hit's `snippet` comes from sqlite's own `snippet()`
        function (bracketed match highlighting, ~12 tokens of surrounding
        context) — never hand-rolled truncation.
        """
        rows = self._conn.execute(
            """
            SELECT document_text.source_doc AS source_doc,
                   document_text.page AS page,
                   documents.filename AS filename,
                   snippet(document_text_fts, 0, '[', ']', ' ... ', 12) AS snip
            FROM document_text_fts
            JOIN document_text ON document_text.id = document_text_fts.rowid
            JOIN documents ON documents.sha256 = document_text.source_doc
            WHERE document_text_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (_fts_query_any(query), limit),
        ).fetchall()
        hits: list[DocumentTextHit] = []
        for row in rows:
            page = row["page"]
            ref = f"doc:{row['filename']}#p{page}" if page is not None else f"doc:{row['filename']}"
            hits.append(
                DocumentTextHit(
                    source_doc=row["source_doc"],
                    filename=row["filename"],
                    page=page,
                    snippet=row["snip"],
                    source_ref=ref,
                )
            )
        return hits

    # ----------------------------------------------------------------
    # JSONL export / rebuild (PLAN.md "State": sqlite is derived)
    # ----------------------------------------------------------------

    @_synchronized
    def _all_lab_rows(self) -> list[LabResult]:
        rows = self._conn.execute("SELECT * FROM labs ORDER BY id").fetchall()
        return [_row_to_lab(row) for row in rows]

    @_synchronized
    def export_jsonl(self, path: str | Path) -> None:
        """Write a deterministic full snapshot to `path`, one JSON object per line.

        Documents (sorted by sha256) come first, then labs rows (sorted by
        id). The snapshot is a full-row dump, not a diff, so it is what the
        data repo commits: git then shows a human-readable diff of exactly
        what changed. Because the ordering and content are deterministic,
        `export -> rebuild -> export` produces byte-identical output.
        """
        documents = sorted(self.list_documents(), key=lambda d: d.sha256)
        rows = self._all_lab_rows()
        with Path(path).open("w", encoding="utf-8") as fh:
            for doc in documents:
                fh.write(json.dumps({"table": "document", "row": doc.model_dump(mode="json")}))
                fh.write("\n")
            for row in rows:
                fh.write(json.dumps({"table": "lab", "row": row.model_dump(mode="json")}))
                fh.write("\n")

    @_synchronized
    def rebuild_from_jsonl(self, path: str | Path) -> None:
        """Replace this db's documents/labs content with a JSONL export's content.

        Used to derive `labs.sqlite` from the committed `labs-export.jsonl`
        after a fresh checkout (the sqlite file itself is gitignored).
        """
        self._conn.execute("DELETE FROM labs")
        self._conn.execute("DELETE FROM labs_fts")
        self._conn.execute("DELETE FROM documents")
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if payload["table"] == "document":
                    self._insert_document_raw(LabDocument.model_validate(payload["row"]))
                elif payload["table"] == "lab":
                    self._insert_lab_raw(LabResult.model_validate(payload["row"]))
                else:
                    raise ValueError(f"rebuild_from_jsonl: unknown table {payload['table']!r}")
        self._conn.commit()

    @_synchronized
    def _insert_document_raw(self, doc: LabDocument) -> None:
        self._conn.execute(
            """
            INSERT INTO documents (sha256, filename, doc_type, doc_date, page_count,
                                    ingested_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            _document_params(doc),
        )

    @_synchronized
    def _insert_lab_raw(self, row: LabResult) -> None:
        self._conn.execute(
            """
            INSERT INTO labs (
                id, date, loinc_code, name, name_raw, value, comparator, value_text, ucum_unit,
                ref_low, ref_high, ref_text, flag, specimen, source_doc, source_page,
                extraction_status, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row.id, *_lab_params(row)),
        )

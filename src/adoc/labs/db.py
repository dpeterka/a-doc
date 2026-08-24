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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any

from adoc.labs.models import ExtractionStatus, LabDocument, LabFlag, LabResult


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
]

_CORRECTABLE_FIELDS = {
    "date",
    "loinc_code",
    "name",
    "name_raw",
    "value",
    "value_text",
    "ucum_unit",
    "ref_low",
    "ref_high",
    "ref_text",
    "flag",
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
        row.value_text,
        row.ucum_unit,
        row.ref_low,
        row.ref_high,
        row.ref_text,
        row.flag.value if row.flag else None,
        row.source_doc,
        row.source_page,
        row.extraction_status.value,
        row.raw_json,
        row.created_at.isoformat(),
    )


def _fts_query(text: str) -> str:
    """Build a safe FTS5 MATCH query: AND of phrase-quoted tokens.

    Quoting each token as an FTS5 string literal avoids MATCH syntax errors
    on punctuation in free-text search input.
    """
    tokens = re.findall(r"\w+", text)
    if not tokens:
        return '""'
    return " AND ".join(f'"{t}"' for t in tokens)


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
        # check_same_thread=False: a single LabsDb is shared across an ASGI
        # server's worker threads (FastAPI's sync-route thread pool, or a
        # test client's per-call portal thread) - the connection itself is
        # never touched concurrently from two threads at once (this app is
        # single-user/single-request-at-a-time), only sequentially from
        # different ones, which sqlite3's default same-thread check would
        # otherwise reject.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA journal_mode={journal_mode}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LabsDb:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

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

    def get_document(self, sha256: str) -> LabDocument | None:
        row = self._conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
        return _row_to_document(row) if row else None

    def list_documents(self) -> list[LabDocument]:
        rows = self._conn.execute("SELECT * FROM documents ORDER BY ingested_at DESC").fetchall()
        return [_row_to_document(row) for row in rows]

    # ----------------------------------------------------------------
    # Labs: insert + confirm queue
    # ----------------------------------------------------------------

    def insert_results(self, results: Sequence[LabResult]) -> list[int | None]:
        """Insert lab rows, one `id | None` per input row (in order).

        Rows that collide with an existing `(date, name, source_doc)` row
        (the UNIQUE constraint) are silently skipped (`None` in the returned
        list) rather than raising — dedupe is expected during re-ingestion
        of the same document.
        """
        ids: list[int | None] = []
        cur = self._conn.cursor()
        for row in results:
            cur.execute(
                """
                INSERT INTO labs (
                    date, loinc_code, name, name_raw, value, value_text, ucum_unit,
                    ref_low, ref_high, ref_text, flag, source_doc, source_page,
                    extraction_status, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, name, source_doc) DO NOTHING
                """,
                _lab_params(row),
            )
            ids.append(cur.lastrowid if cur.rowcount else None)
        self._conn.commit()
        return ids

    def pending(self) -> list[LabResult]:
        """Rows awaiting human confirmation (the confirm queue)."""
        rows = self._conn.execute(
            "SELECT * FROM labs WHERE extraction_status = ? ORDER BY date, id",
            (ExtractionStatus.PENDING.value,),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

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

    def lab_counts_by_document(self) -> dict[str, int]:
        """Total lab-row count (any `extraction_status`) per document
        `sha256`. The confirm queue's per-document header derives its
        "done" count as `total - pending` from this."""
        rows = self._conn.execute(
            "SELECT source_doc, COUNT(*) AS n FROM labs GROUP BY source_doc"
        ).fetchall()
        return {row["source_doc"]: row["n"] for row in rows}

    def get_row(self, row_id: int) -> LabResult | None:
        row = self._conn.execute("SELECT * FROM labs WHERE id = ?", (row_id,)).fetchone()
        return _row_to_lab(row) if row else None

    def confirm_row(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE labs SET extraction_status = ? WHERE id = ?",
            (ExtractionStatus.CONFIRMED.value, row_id),
        )
        self._conn.commit()

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

    def reject_row(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE labs SET extraction_status = ? WHERE id = ?",
            (ExtractionStatus.REJECTED.value, row_id),
        )
        self._conn.commit()

    # ----------------------------------------------------------------
    # Read side
    # ----------------------------------------------------------------

    def series(self, name: str, *, include_rejected: bool = False) -> list[LabResult]:
        """Time-ordered trend series for one canonical analyte name."""
        if include_rejected:
            rows = self._conn.execute(
                "SELECT * FROM labs WHERE name = ? ORDER BY date, id", (name,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM labs WHERE name = ? AND extraction_status != ? ORDER BY date, id",
                (name, ExtractionStatus.REJECTED.value),
            ).fetchall()
        return [_row_to_lab(row) for row in rows]

    def latest_panel(self) -> list[LabResult]:
        """Most recent non-rejected row per distinct analyte name."""
        rows = self._conn.execute(
            """
            SELECT l.* FROM labs l
            JOIN (
                SELECT name, MAX(date) AS max_date FROM labs
                WHERE extraction_status != ?
                GROUP BY name
            ) latest ON latest.name = l.name AND latest.max_date = l.date
            WHERE l.extraction_status != ?
            ORDER BY l.name
            """,
            (ExtractionStatus.REJECTED.value, ExtractionStatus.REJECTED.value),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

    def abnormal_since(self, since: date) -> list[LabResult]:
        rows = self._conn.execute(
            "SELECT * FROM labs WHERE date >= ? AND flag IS NOT NULL "
            "AND extraction_status != ? ORDER BY date DESC, id DESC",
            (since.isoformat(), ExtractionStatus.REJECTED.value),
        ).fetchall()
        return [_row_to_lab(row) for row in rows]

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
    # JSONL export / rebuild (PLAN.md "State": sqlite is derived)
    # ----------------------------------------------------------------

    def _all_lab_rows(self) -> list[LabResult]:
        rows = self._conn.execute("SELECT * FROM labs ORDER BY id").fetchall()
        return [_row_to_lab(row) for row in rows]

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

    def _insert_document_raw(self, doc: LabDocument) -> None:
        self._conn.execute(
            """
            INSERT INTO documents (sha256, filename, doc_type, doc_date, page_count,
                                    ingested_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            _document_params(doc),
        )

    def _insert_lab_raw(self, row: LabResult) -> None:
        self._conn.execute(
            """
            INSERT INTO labs (
                id, date, loinc_code, name, name_raw, value, value_text, ucum_unit,
                ref_low, ref_high, ref_text, flag, source_doc, source_page,
                extraction_status, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row.id, *_lab_params(row)),
        )

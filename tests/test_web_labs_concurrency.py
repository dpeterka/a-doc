"""Web-level concurrency regression test for the production `/labs` 500.

Reproduces the actual failure shape from the traceback: concurrent HTTP
requests hitting different pages that all read through the same shared
`LabsDb` instance (FastAPI serves sync routes from a thread pool, so
concurrent requests really do run their route functions on different OS
threads against the one `sqlite3` connection `LabsDb` owns). All data here
is synthetic (CLAUDE.md PHI boundary rule 1).
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.labs.models import DocumentStatus, LabDocument, LabResult

SHA = "c" * 64


def _seed(db) -> None:
    db.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="doc.pdf",
            doc_type="lab_report",
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 1),
                name="crp",
                name_raw="CRP",
                value=6.0,
                ucum_unit="mg/L",
                ref_low=0.0,
                ref_high=10.0,
                source_doc=SHA,
                raw_json=json.dumps({}),
            ),
            LabResult(
                date=date(2026, 6, 1),
                name="crp",
                name_raw="CRP",
                value=12.0,
                ucum_unit="mg/L",
                ref_low=0.0,
                ref_high=10.0,
                source_doc=SHA,
                raw_json=json.dumps({}),
            ),
        ]
    )


def test_concurrent_requests_to_labs_and_documents_all_succeed(tmp_path: Path) -> None:
    """~30 concurrent requests split across `/labs` and `/documents/consumed`
    (both routed through the same shared `LabsDb`) - the same request
    pattern the production traceback showed (`/upload`, `/reviews`,
    `/confirm`, and `/labs` all served in the same second). Asserts every
    request comes back 200, with no exception escaping the route handler.
    """
    app, _repo, db, _calls = build_app(tmp_path)
    _seed(db)
    client = TestClient(app)
    login(client)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def hit(path: str) -> None:
        try:
            response = client.get(path)
            with statuses_lock:
                statuses.append(response.status_code)
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    paths = (["/labs"] * 15) + (["/documents/consumed"] * 15)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(hit, p) for p in paths]
        for future in futures:
            future.result()

    assert errors == [], f"concurrent web requests raised {len(errors)} exception(s): {errors[0]!r}"
    assert len(statuses) == len(paths)
    assert all(status == 200 for status in statuses), statuses

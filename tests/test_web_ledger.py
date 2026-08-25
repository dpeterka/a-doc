"""Ledger surface tests: the read-only full ledger view renders tier/
status/origin chips (including an `origin: patient` chip) and links
evidence source-refs back to their documents where resolvable — and, when
no hypotheses exist yet, an explanation state instead of the "complete,
unfiltered record" framing (which is only true once there's a record).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.casefile.ledger import save_ledger
from adoc.casefile.repo import LEDGER_RELPATH
from adoc.casefile.schema import Evidence, Hypothesis, Ledger
from adoc.labs.models import LabDocument


def test_ledger_view_empty_state_explains_instead_of_claiming_a_record(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    body = response.text
    assert "Nothing recorded yet." not in body
    assert "read-only." not in body
    assert "diagnostic conversation" in body
    assert 'href="/chat"' in body


def test_ledger_view_shows_origin_patient_chip(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="mcas-01",
        name="Mast cell activation syndrome",
        tier="expanded",
        probability="low",
        status="patient-proposed",
        origin="patient",
        first_proposed=date(2026, 1, 1),
        evidence_for=[
            Evidence(
                claim="Flushing after meals",
                source="patient-report:2026-01-01",
                strength="weak",
            )
        ],
    )
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp])
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    assert 'data-origin="patient"' in response.text
    assert "origin: patient" in response.text


def test_ledger_view_links_a_labs_source_ref(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="sle-01",
        name="Systemic lupus erythematosus",
        tier="most-likely",
        probability="moderate",
        status="active",
        origin="model",
        first_proposed=date(2026, 1, 1),
        evidence_for=[
            Evidence(claim="ANA elevated", source="labs:ana-titer:2026-05-02", strength="strong")
        ],
    )
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp])
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    assert 'href="/labs/ana-titer"' in response.text


# --------------------------------------------------------------------------
# Perf regression guard: resolving `doc:` evidence refs is O(1) in
# `db.list_documents()` queries and page-image directory listings, not
# O(evidence refs).
# --------------------------------------------------------------------------


def test_ledger_view_resolves_many_doc_refs_without_requerying_per_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_source_ref_href` used to call `find_document_by_filename` (a full
    `db.list_documents()` query) and `page_image_url` (a filesystem
    `iterdir()`) fresh for EVERY `doc:` evidence ref — a ledger with many
    hypotheses citing the same document re-ran both once per ref instead
    of once per page render. Both `labs.sqlite` and the data repo live on
    EFS/NFS in the deployed app, where each round trip costs milliseconds.
    """
    app, repo, db, _calls = build_app(tmp_path)
    sha = "c" * 64
    db.upsert_document(
        LabDocument(sha256=sha, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    page_dir = repo.root / "sources" / "pages" / sha
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "p-1.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")

    hypotheses = [
        Hypothesis(
            id=f"hyp-{i}",
            name=f"Hypothesis {i}",
            tier="expanded",
            probability="low",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
            evidence_for=[Evidence(claim=f"Finding {i}", source="doc:doc.pdf#p1", strength="weak")],
        )
        for i in range(15)
    ]
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=hypotheses)
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    document_selects: list[str] = []
    db._conn.set_trace_callback(
        lambda stmt: document_selects.append(stmt) if "FROM documents" in stmt else None
    )
    listings: list[Path] = []
    original_iterdir = Path.iterdir

    def counting_iterdir(self: Path) -> Any:
        if self == page_dir:
            listings.append(self)
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    try:
        client = TestClient(app)
        login(client)
        response = client.get("/ledger")
    finally:
        db._conn.set_trace_callback(None)

    assert response.status_code == 200
    assert response.text.count('href="/files/pages/') == 15
    # 15 evidence refs all citing the same document: a per-ref
    # implementation issues 15 `list_documents()` queries and 15 directory
    # listings. The memoized implementation issues exactly one of each.
    assert len(document_selects) == 1, f"expected 1 documents query, got {len(document_selects)}"
    assert len(listings) == 1, f"expected 1 page-image directory listing, got {len(listings)}"

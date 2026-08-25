"""Ledger surface tests: the read-only full ledger view renders tier/
status/origin chips (including an `origin: patient` chip) and links
evidence source-refs back to their documents where resolvable — and, when
no hypotheses exist yet, an explanation state instead of the "complete,
unfiltered record" framing (which is only true once there's a record).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.casefile.ledger import save_ledger
from adoc.casefile.repo import LEDGER_RELPATH
from adoc.casefile.schema import Evidence, Hypothesis, Ledger


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

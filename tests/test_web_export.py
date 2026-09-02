"""Tests for `GET /export/agenda` — the printable appointment agenda (ADR 0041)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.casefile.ledger import apply_and_save
from adoc.casefile.regimen import REGIMEN_RELPATH, Regimen, RegimenEntry, save_regimen
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import (
    AddHypothesis,
    Evidence,
    Hypothesis,
    LedgerDiff,
    Provenance,
    RuleOutCheck,
)
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult


def _seed_lead(repo: DataRepo) -> None:
    apply_and_save(
        repo.root / LEDGER_RELPATH,
        repo.root / HISTORY_RELPATH,
        LedgerDiff(
            provenance=Provenance(
                app_version="test",
                prompt_template_version="t@v1",
                model_id="m",
                dag_node="seed",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            rationale="seed",
            ops=[
                AddHypothesis(
                    hypothesis=Hypothesis(
                        id="sle",
                        name="Systemic lupus erythematosus",
                        tier="most-likely",
                        probability="high",
                        status="active",
                        origin="model",
                        first_proposed=date(2026, 8, 1),
                        last_challenged_version=1,
                        evidence_for=[
                            Evidence(
                                claim="ANA was 1:640 on 2026-05-02",
                                source="labs:ana:2026-05-02",
                                strength="strong",
                            )
                        ],
                        rule_out_check=RuleOutCheck(analyte="complement C3", operator="normal"),
                    )
                ),
                # The ledger's invariants require a non-empty can't-miss tier
                # while any hypothesis is active. This one is a placeholder
                # with no citation — which is also what ADR 0037 says must
                # not lead a page, so the agenda tests get that for free.
                AddHypothesis(
                    hypothesis=Hypothesis(
                        id="hae",
                        name="Hereditary angio-oedema",
                        tier="cant-miss",
                        probability="minimal",
                        status="active",
                        origin="challenger",
                        first_proposed=date(2026, 8, 1),
                        last_challenged_version=1,
                    )
                ),
            ],
        ),
    )


def _seed_abnormal(db: LabsDb) -> None:
    sha = "b" * 64
    db.upsert_document(
        LabDocument(sha256=sha, filename="labs.pdf", doc_type="lab_report", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="CRP",
                name_raw="CRP",
                value=8.5,
                ucum_unit="mg/L",
                flag="H",
                source_doc=sha,
                raw_json=json.dumps({"name_raw": "CRP"}),
            )
        ]
    )


def test_the_agenda_renders_one_printable_page(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_lead(repo)
    _seed_abnormal(db)
    client = TestClient(app)
    login(client)

    response = client.get("/export/agenda")

    assert response.status_code == 200
    body = response.text
    assert "Appointment agenda" in body
    assert "Systemic lupus erythematosus" in body
    assert "ANA was 1:640" in body
    assert "CRP" in body
    # The print stylesheet is the other half of the one-page bound.
    assert "@page" in body and "letter" in body


def test_the_agenda_makes_no_model_call(tmp_path: Path) -> None:
    """`build_app`'s default transports explode if invoked. The agenda is
    deterministic — every field is copied from the ledger, the labs database
    or `regimen.yaml`."""
    app, repo, db, calls = build_app(tmp_path)
    _seed_lead(repo)
    _seed_abnormal(db)
    client = TestClient(app)
    login(client)

    assert client.get("/export/agenda").status_code == 200
    assert calls == []


def test_the_medication_table_reaches_the_printed_page(tmp_path: Path) -> None:
    """The section that only exists because of the `recording_only` scribe
    exemption — and the whole reason `regimen.py` was written."""
    app, repo, db, _calls = build_app(tmp_path)
    _seed_lead(repo)
    save_regimen(
        repo.root / Path(REGIMEN_RELPATH),
        Regimen(
            entries=[
                RegimenEntry(
                    name="Biotin", dose="10000 mcg", frequency="daily", started=date(2026, 6, 1)
                )
            ]
        ),
    )
    client = TestClient(app)
    login(client)

    body = client.get("/export/agenda").text

    assert "Biotin" in body
    assert "10000 mcg" in body
    assert "patient-reported" in body


def test_the_markdown_export_is_the_same_page(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_lead(repo)
    _seed_abnormal(db)
    client = TestClient(app)
    login(client)

    response = client.get("/export/agenda.md")

    assert response.status_code == 200
    assert response.text.startswith("# Appointment agenda")
    assert "Systemic lupus erythematosus" in response.text


def test_the_agenda_requires_a_login(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app, follow_redirects=False)

    for path in ("/export/agenda", "/export/agenda.md"):
        response = client.get(path)
        assert response.status_code in (302, 303, 307), path
        assert "/login" in response.headers["location"], path


def test_an_empty_case_file_still_renders_a_page(tmp_path: Path) -> None:
    """A patient with an appointment and nothing on file yet gets a page that
    says so, not a 500."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/export/agenda")

    assert response.status_code == 200
    assert "No lead currently carries cited evidence" in response.text


def test_the_page_is_reachable_from_the_case_file(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_lead(repo)
    client = TestClient(app)
    login(client)

    body = client.get("/ledger").text

    assert "/export/agenda" in body
    assert "one-page agenda" in body


def test_a_gate_failure_refuses_the_page_rather_than_printing_holes(tmp_path: Path) -> None:
    """It is about to be handed to a clinician; there is no version of this
    artifact where partial is better than absent. Evidence claims have no
    gate on their WRITE path (`apply_diff` never runs `treatment_gate`), so
    an ungated claim can be at rest in the ledger."""
    app, repo, _db, _calls = build_app(tmp_path)
    apply_and_save(
        repo.root / LEDGER_RELPATH,
        repo.root / HISTORY_RELPATH,
        LedgerDiff(
            provenance=Provenance(
                app_version="test",
                prompt_template_version="t@v1",
                model_id="m",
                dag_node="seed",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            rationale="seed",
            ops=[
                AddHypothesis(
                    hypothesis=Hypothesis(
                        id="h",
                        name="Something",
                        tier="expanded",
                        probability="low",
                        status="active",
                        origin="model",
                        first_proposed=date(2026, 8, 1),
                        last_challenged_version=1,
                        evidence_for=[
                            Evidence(
                                claim="You should start taking prednisone for this",
                                source="labs:crp:2026-05-02",
                                strength="weak",
                            )
                        ],
                    )
                ),
                AddHypothesis(
                    hypothesis=Hypothesis(
                        id="hae",
                        name="Hereditary angio-oedema",
                        tier="cant-miss",
                        probability="minimal",
                        status="active",
                        origin="challenger",
                        first_proposed=date(2026, 8, 1),
                        last_challenged_version=1,
                    )
                ),
            ],
        ),
    )
    client = TestClient(app)
    login(client)

    body = client.get("/export/agenda").text

    # Jinja escapes the apostrophe in "a-doc's".
    assert "treatment/dosing safety check" in body
    assert "nothing was rendered rather than printing a page with gaps" in body
    assert "start taking prednisone" not in body
    assert client.get("/export/agenda.md").text.startswith("This page could not be prepared")


def test_an_uncited_cant_miss_placeholder_does_not_reach_the_printed_page(tmp_path: Path) -> None:
    """ADR 0037 kept it off the patient's page. On a page handed to a doctor
    it matters more: a hereditary syndrome listed with nothing behind it is
    what makes a clinician stop reading."""
    app, repo, db, _calls = build_app(tmp_path)
    _seed_lead(repo)
    _seed_abnormal(db)
    client = TestClient(app)
    login(client)

    body = client.get("/export/agenda").text

    assert "Systemic lupus erythematosus" in body
    assert "Hereditary angio-oedema" not in body
    assert "no citation yet" in body

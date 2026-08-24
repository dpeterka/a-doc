"""Labs surface tests: the analyte list, the per-analyte JSON data feed,
and the detail page (checked only for a `<script>` tag — Plotly itself is
never executed in tests).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.labs.models import LabDocument, LabResult

SHA = "f" * 64


def _seed_series(db) -> None:
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
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


def test_labs_index_lists_analytes(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)
    client = TestClient(app)
    login(client)

    response = client.get("/labs")

    assert response.status_code == 200
    assert "crp" in response.text
    assert "/labs/crp" in response.text


def test_labs_data_endpoint_returns_the_expected_json_shape(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)
    client = TestClient(app)
    login(client)

    response = client.get("/labs/crp/data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "crp"
    assert payload["dates"] == ["2026-05-01", "2026-06-01"]
    assert payload["values"] == [6.0, 12.0]
    assert payload["ref_low"] == [0.0, 0.0]
    assert payload["ref_high"] == [10.0, 10.0]
    assert payload["flags"] == [None, None]


def test_labs_detail_page_contains_a_script_tag_but_never_executes_plotly(
    tmp_path: Path,
) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)
    client = TestClient(app)
    login(client)

    response = client.get("/labs/crp")

    assert response.status_code == 200
    assert "<script" in response.text
    assert "plotly-basic.min.js" in response.text
    assert "Plotly.newPlot" in response.text


def _seed_glucose_two_specimens(db) -> None:
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 1),
                name="glucose",
                name_raw="GLUCOSE",
                value=None,
                value_text="NEGATIVE",
                source_doc=SHA,
                raw_json=json.dumps({}),
                specimen="urine",
            ),
            LabResult(
                date=date(2026, 5, 1),
                name="glucose",
                name_raw="Glucose",
                value=92.0,
                ucum_unit="mg/dL",
                source_doc=SHA,
                raw_json=json.dumps({}),
                specimen="serum",
            ),
        ]
    )


def test_labs_index_shows_a_specimen_chip_when_not_unknown(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_glucose_two_specimens(db)
    client = TestClient(app)
    login(client)

    response = client.get("/labs")

    assert response.status_code == 200
    assert 'class="chip chip-specimen"' in response.text
    assert ">urine<" in response.text
    assert ">serum<" in response.text


def test_labs_index_does_not_show_a_chip_for_unknown_specimen(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)  # default specimen "unknown"
    client = TestClient(app)
    login(client)

    response = client.get("/labs")

    assert response.status_code == 200
    assert "chip-specimen" not in response.text


def test_labs_detail_page_renders_one_chart_per_specimen(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_glucose_two_specimens(db)
    client = TestClient(app)
    login(client)

    response = client.get("/labs/glucose")

    assert response.status_code == 200
    assert 'id="labs-chart-1"' in response.text
    assert 'id="labs-chart-2"' in response.text
    assert "glucose (urine)" in response.text
    assert "glucose (serum)" in response.text


def test_labs_data_endpoint_filters_by_specimen(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_glucose_two_specimens(db)
    client = TestClient(app)
    login(client)

    urine = client.get("/labs/glucose/data?specimen=urine").json()
    assert urine["values"] == [None]
    assert urine["value_text"] == ["NEGATIVE"]

    serum = client.get("/labs/glucose/data?specimen=serum").json()
    assert serum["values"] == [92.0]

    combined = client.get("/labs/glucose/data").json()
    assert len(combined["dates"]) == 2


def test_labs_detail_page_handles_an_analyte_with_no_data(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/labs/unknown-analyte")

    assert response.status_code == 200
    assert "No results recorded" in response.text

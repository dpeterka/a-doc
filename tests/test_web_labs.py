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


def test_labs_detail_page_handles_an_analyte_with_no_data(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/labs/unknown-analyte")

    assert response.status_code == 200
    assert "No results recorded" in response.text

"""Labs surface tests: the analyte list, the per-analyte JSON data feed,
and the detail page (checked only for a `<script>` tag — Plotly itself is
never executed in tests).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.labs.models import LabDocument, LabResult
from adoc.web.routes.labs import encode_analyte_id

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
    # The index links to the stable base64 id, not the raw analyte name
    # (see `web.routes.labs.encode_analyte_id` - a plain-name path segment
    # 404s for names containing "/", so the index never emits one).
    assert f"/labs/{encode_analyte_id('crp')}" in response.text


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


def test_labs_detail_handles_a_legacy_path_outside_the_id_alphabet(tmp_path: Path) -> None:
    """A pre-fix bookmark could carry any character a raw analyte name did
    - not just the urlsafe-base64 alphabet `decode_analyte_id` otherwise
    tries first. This one (spaces, a dot, parens) fails that check
    immediately and falls back to a literal-name lookup, same as any other
    legacy bookmark that doesn't resolve to data."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/labs/B. MIYAMOTOI AB (IGG)")

    assert response.status_code == 200
    assert "No results recorded" in response.text


# --- routing fix: real analyte names carry "/", ".", "(", ")", "%", spaces -------------


def _seed_single_reading(
    db,
    *,
    name: str,
    value: float | None = None,
    value_text: str | None = None,
    ucum_unit: str | None = None,
) -> None:
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 1),
                name=name,
                name_raw=name,
                value=value,
                value_text=value_text,
                ucum_unit=ucum_unit,
                source_doc=SHA,
                raw_json=json.dumps({}),
            )
        ]
    )


def test_labs_detail_no_longer_404s_for_a_slash_in_the_analyte_name(tmp_path: Path) -> None:
    """Root-cause regression test: a plain `{name}` path parameter can't
    carry a literal "/" - even percent-encoded, the ASGI server decodes
    "%2F" before Starlette's router ever sees it, splitting "A/G Ratio"
    into two path segments and 404ing a single-segment route. The
    base64-id scheme (`encode_analyte_id`) never emits "/", so the fixed
    URL must succeed where the historical `quote()`-encoded one 404s.
    """
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_single_reading(db, name="A/G Ratio", value=1.6)
    client = TestClient(app)
    login(client)

    broken_url = f"/labs/{quote('A/G Ratio', safe='')}"
    assert client.get(broken_url).status_code == 404

    fixed_url = f"/labs/{encode_analyte_id('A/G Ratio')}"
    response = client.get(fixed_url)
    assert response.status_code == 200
    assert "A/G Ratio" in response.text


@pytest.mark.parametrize(
    "awkward_name",
    [
        "A/G Ratio",
        "B. MIYAMOTOI AB (IGG)",
        "FRAX 10-year probability of hip fracture",
        "50% Complete",
    ],
)
def test_labs_routing_round_trips_awkward_analyte_names(tmp_path: Path, awkward_name: str) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_single_reading(db, name=awkward_name, value=1.0, ucum_unit="%")
    client = TestClient(app)
    login(client)

    encoded = encode_analyte_id(awkward_name)

    index_response = client.get("/labs")
    assert index_response.status_code == 200
    assert f"/labs/{encoded}" in index_response.text

    detail_response = client.get(f"/labs/{encoded}")
    assert detail_response.status_code == 200
    assert awkward_name in detail_response.text

    data_response = client.get(f"/labs/{encoded}/data")
    assert data_response.status_code == 200
    assert data_response.json()["name"] == awkward_name


def test_labs_detail_redirects_a_legacy_literal_bookmark_to_the_canonical_id(
    tmp_path: Path,
) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)
    client = TestClient(app)
    login(client)

    response = client.get("/labs/crp", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == f"/labs/{encode_analyte_id('crp')}"


def test_labs_detail_page_includes_reference_band_shading_code_when_ranges_exist(
    tmp_path: Path,
) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)  # ref_low=0.0, ref_high=10.0 on both rows

    client = TestClient(app)
    login(client)

    response = client.get(f"/labs/{encode_analyte_id('crp')}")

    assert response.status_code == 200
    assert "tonexty" in response.text


def test_labs_detail_lists_qualitative_readings_in_the_table(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_glucose_two_specimens(db)
    client = TestClient(app)
    login(client)

    response = client.get(f"/labs/{encode_analyte_id('glucose')}")

    assert response.status_code == 200
    assert "NEGATIVE" in response.text
    assert "92.0" in response.text
    assert f"/files/original/{SHA}" in response.text


def test_labs_detail_labels_a_score_kind_analyte(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_single_reading(
        db, name="FRAX 10-year probability of hip fracture", value=8.5, ucum_unit="%"
    )
    client = TestClient(app)
    login(client)

    response = client.get(f"/labs/{encode_analyte_id('FRAX 10-year probability of hip fracture')}")

    assert response.status_code == 200
    assert "calculated score" in response.text.lower()


def test_labs_detail_does_not_label_an_ordinary_analyte_a_calculated_score(
    tmp_path: Path,
) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)
    client = TestClient(app)
    login(client)

    response = client.get(f"/labs/{encode_analyte_id('crp')}")

    assert response.status_code == 200
    assert "calculated score" not in response.text.lower()


def test_labs_index_renders_a_dot_for_a_single_reading_analyte(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_single_reading(db, name="solo-marker", value=42.0)
    client = TestClient(app)
    login(client)

    response = client.get("/labs")

    assert response.status_code == 200
    assert "<circle" in response.text


def test_labs_index_groups_analytes_under_panel_headings_in_curated_order(
    tmp_path: Path,
) -> None:
    """CBC comes before Comprehensive Metabolic Panel in `PANEL_ORDER`, and
    an analyte with no curated panel ("Other") always sorts last -
    deterministic, not incidental to insertion order."""
    app, _repo, db, _calls = build_app(tmp_path)
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 1),
                name="an unmapped one-off marker",
                name_raw="an unmapped one-off marker",
                value=1.0,
                source_doc=SHA,
                raw_json=json.dumps({}),
            ),
            LabResult(
                date=date(2026, 5, 1),
                name="sodium",
                name_raw="Sodium",
                value=140.0,
                ucum_unit="mmol/L",
                source_doc=SHA,
                raw_json=json.dumps({}),
            ),
            LabResult(
                date=date(2026, 5, 1),
                name="WBC",
                name_raw="WBC",
                value=6.0,
                ucum_unit="K/uL",
                source_doc=SHA,
                raw_json=json.dumps({}),
            ),
        ]
    )
    client = TestClient(app)
    login(client)

    response = client.get("/labs")

    assert response.status_code == 200
    text = response.text
    cbc_pos = text.index(">CBC<")
    cmp_pos = text.index(">Comprehensive Metabolic Panel<")
    other_pos = text.index(">Other<")
    assert cbc_pos < cmp_pos < other_pos


def test_labs_detail_shows_the_panel_and_derived_from_note(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_single_reading(db, name="TSAT", value=25.0, ucum_unit="%")
    client = TestClient(app)
    login(client)

    response = client.get(f"/labs/{encode_analyte_id('TSAT')}")

    assert response.status_code == 200
    assert "Iron Studies" in response.text
    assert "calculated from Iron and TIBC" in response.text


def test_labs_detail_handles_a_single_reading_analyte_gracefully(tmp_path: Path) -> None:
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_single_reading(db, name="solo-marker", value=42.0, ucum_unit="mg/dL")
    client = TestClient(app)
    login(client)

    response = client.get(f"/labs/{encode_analyte_id('solo-marker')}")

    assert response.status_code == 200
    assert "42.0" in response.text


def test_labs_index_does_not_issue_a_query_per_analyte(tmp_path: Path) -> None:
    """Perf regression guard: the index used to call `trend_series` once per
    analyte. Locally that was invisible (~0.02 ms/query against page cache),
    but the deployed app reads `labs.sqlite` over EFS/NFS where each query
    costs milliseconds of round-trip - ~450 analytes made the page take
    ~11 s in production. The whole page must now stay O(1) in queries, not
    O(analytes)."""
    app, _repo, db, _calls = build_app(tmp_path)
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    rows = []
    for i in range(40):
        for day in (1, 2):
            rows.append(
                LabResult(
                    date=date(2026, 5, day),
                    name=f"analyte-{i}",
                    name_raw=f"Analyte {i}",
                    value=float(i + day),
                    source_doc=SHA,
                    raw_json=json.dumps({}),
                )
            )
    db.insert_results(rows)

    selects: list[str] = []
    db._conn.set_trace_callback(lambda stmt: selects.append(stmt))
    try:
        client = TestClient(app)
        login(client)
        response = client.get("/labs")
    finally:
        db._conn.set_trace_callback(None)

    assert response.status_code == 200
    labs_selects = [s for s in selects if "FROM labs" in s]
    # 40 analytes: a per-analyte implementation issues 40+ of these. The
    # bulk implementation needs only a handful regardless of analyte count.
    assert len(labs_selects) < 10, f"expected O(1) labs queries, got {len(labs_selects)}"


def test_series_by_key_matches_per_analyte_series(tmp_path: Path) -> None:
    """`series_by_key` must be a drop-in for calling `series()` per analyte:
    same rows, same order, same rejected-row filtering."""
    app, _repo, db, _calls = build_app(tmp_path)
    _seed_series(db)
    db.insert_results(
        [
            LabResult(
                date=date(2026, 6, 2),
                name="glucose",
                name_raw="GLUCOSE",
                value_text="NEGATIVE",
                specimen="urine",
                source_doc=SHA,
                raw_json=json.dumps({}),
            )
        ]
    )

    bulk = db.series_by_key()
    for latest in db.latest_panel():
        key = (latest.name, latest.specimen)
        expected = db.series(latest.name, latest.specimen)
        assert [r.id for r in bulk[key]] == [r.id for r in expected]

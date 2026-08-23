"""Static assets (vendored htmx/plotly + the stylesheet) are public — no
session cookie needed — and are actually the files vendored in `static/`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app


def test_static_assets_are_public_and_served(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)

    htmx = client.get("/static/vendor/htmx.min.js")
    plotly = client.get("/static/vendor/plotly-basic.min.js")
    css = client.get("/static/styles.css")

    assert htmx.status_code == 200
    assert "htmx" in htmx.text.lower()
    assert plotly.status_code == 200
    assert "plotly" in plotly.text.lower()
    assert css.status_code == 200

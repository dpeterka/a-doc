"""Upload surface tests: a saved file is run through the real ingestion
pipeline and its `IngestReport` (rows added / queued) is shown.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from conftest import TINY_PDF_BYTES, fake_page_renderer
from fastapi.testclient import TestClient
from pydantic import BaseModel
from web_support import build_app, login

from adoc.ingest.schema import ClassifyResult, DocumentExtraction
from adoc.ingest.vision import Part

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "extractions"


def _load_fixture(name: str) -> tuple[DocumentExtraction, DocumentExtraction]:
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return (
        DocumentExtraction.model_validate(payload["pass_a"]),
        DocumentExtraction.model_validate(payload["pass_b"]),
    )


class FakeVisionClient:
    """Duck-types `VisionClient.extract` over a fixture pair — no network."""

    def __init__(self, fixture_name: str) -> None:
        self.pass_a, self.pass_b = _load_fixture(fixture_name)

    def extract(
        self,
        role: str,
        *,
        system: str,
        parts: Sequence[Part],
        schema: type[BaseModel],
        binding_index: int = 0,
        max_tokens: int = 4096,
    ) -> Any:
        if role == "classifier":
            doc_date = self.pass_a.collection_date or self.pass_a.report_date
            return ClassifyResult(doc_type=self.pass_a.doc_type, doc_date=doc_date)
        if role == "extractor_pass_a":
            return self.pass_a
        if role == "extractor_pass_b":
            return self.pass_b
        raise AssertionError(f"unexpected role: {role}")


def test_upload_page_loads(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/upload")

    assert response.status_code == 200
    assert "Add a document" in response.text


def test_upload_ingests_and_shows_the_report(tmp_path: Path) -> None:
    vision = FakeVisionClient("clean_agreement.json")
    app, repo, db, _calls = build_app(tmp_path, vision=vision, renderer=fake_page_renderer(1))  # type: ignore[arg-type]
    client = TestClient(app)
    login(client)

    response = client.post(
        "/upload",
        files={"file": ("quest-2026-05-02.pdf", TINY_PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 200
    assert "<strong>2</strong> row" in response.text  # rows_auto from the fixture
    assert (repo.root / "inbox" / "quest-2026-05-02.pdf").exists()
    assert len(db.list_documents()) == 1

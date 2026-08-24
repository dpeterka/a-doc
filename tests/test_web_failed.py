"""Failed-uploads surface tests: `/failed` lists `work/failed/failures.jsonl`
entries, Retry moves a file back into `inbox/` and re-ingests it (clearing
the record on success), and Remove deletes the file and its record.
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


def _seed_failure(
    repo, *, filename: str, contents: bytes, reason: str = "some earlier problem"
) -> None:
    failed_dir = repo.root / "work" / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    (failed_dir / filename).write_bytes(contents)
    record = {
        "filename": filename,
        "failed_at": "2026-05-01T00:00:00+00:00",
        "reason": reason,
        "original_inbox_path": filename,
    }
    log_path = failed_dir / "failures.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def test_failed_page_lists_entries(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)
    _seed_failure(
        repo,
        filename="junk.pdf",
        contents=b"junk",
        reason="pdftoppm failed on junk.pdf: some stderr",
    )

    response = client.get("/failed")

    assert response.status_code == 200
    assert "junk.pdf" in response.text
    assert "2026-05-01" in response.text
    # A plain-language reason, not the raw technical string.
    assert "pdftoppm failed on junk.pdf" not in response.text
    assert "a-doc" in response.text.lower()


def test_failed_page_empty_state(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/failed")

    assert response.status_code == 200
    assert "nothing here" in response.text.lower()


def test_retry_moves_file_back_reingests_and_clears_record_on_success(tmp_path: Path) -> None:
    vision = FakeVisionClient("clean_agreement.json")
    app, repo, db, _calls = build_app(tmp_path, vision=vision, renderer=fake_page_renderer(1))  # type: ignore[arg-type]
    client = TestClient(app)
    login(client)
    _seed_failure(repo, filename="quest.pdf", contents=TINY_PDF_BYTES)

    response = client.post("/failed/quest.pdf/retry")

    assert response.status_code == 200
    assert "ingested" in response.text.lower()

    failed_dir = repo.root / "work" / "failed"
    assert not (failed_dir / "quest.pdf").exists()
    assert not (repo.root / "inbox" / "quest.pdf").exists()
    assert (failed_dir / "failures.jsonl").read_text(encoding="utf-8").strip() == ""
    assert len(db.list_documents()) == 1
    # No longer listed on the page.
    assert "quest.pdf" not in client.get("/failed").text


def test_retry_on_a_file_that_still_fails_keeps_it_listed(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)  # no vision override -> classify explodes
    client = TestClient(app)
    login(client)
    # Not a real PDF/docx - archival itself fails, so the retry fails again.
    _seed_failure(repo, filename="bad.pdf", contents=b"still not a pdf")

    response = client.post("/failed/bad.pdf/retry")

    assert response.status_code == 200
    # Jinja auto-escapes the apostrophe ("couldn&#39;t") - check around it.
    body = response.text.lower()
    assert "still could" in body and "processed" in body
    failed_dir = repo.root / "work" / "failed"
    assert (failed_dir / "bad.pdf").exists()
    lines = (failed_dir / "failures.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_remove_deletes_file_and_record(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)
    _seed_failure(repo, filename="junk.pdf", contents=b"junk")

    response = client.post("/failed/junk.pdf/remove")

    assert response.status_code == 200
    assert "removed" in response.text.lower()
    failed_dir = repo.root / "work" / "failed"
    assert not (failed_dir / "junk.pdf").exists()
    assert (failed_dir / "failures.jsonl").read_text(encoding="utf-8").strip() == ""

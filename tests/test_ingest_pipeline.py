"""Tests for adoc.ingest.pipeline: end-to-end wiring over fixture extractions.

Fake `VisionClient`s return fixture `DocumentExtraction`/`ClassifyResult`
payloads (`tests/fixtures/extractions/*.json`) instead of calling any real
provider - no network in tests. A fake page renderer stands in for
`pdftoppm` (no poppler in CI).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from conftest import TINY_PDF_BYTES, fake_page_renderer
from git import Repo
from pydantic import BaseModel

from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.ingest.pipeline import ingest_file, ingest_inbox
from adoc.ingest.schema import ClassifyResult, DocumentExtraction
from adoc.ingest.vision import Part
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "extractions"


def _load_fixture(name: str) -> tuple[DocumentExtraction, DocumentExtraction]:
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return (
        DocumentExtraction.model_validate(payload["pass_a"]),
        DocumentExtraction.model_validate(payload["pass_b"]),
    )


class FakeVisionClient:
    """Stands in for `VisionClient`: routes by role to fixture payloads."""

    def __init__(self, fixture_name: str) -> None:
        self.pass_a, self.pass_b = _load_fixture(fixture_name)
        self.calls: list[str] = []

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
        self.calls.append(role)
        if role == "classifier":
            doc_date = self.pass_a.collection_date or self.pass_a.report_date
            return ClassifyResult(doc_type=self.pass_a.doc_type, doc_date=doc_date)
        if role == "extractor_pass_a":
            return self.pass_a
        if role == "extractor_pass_b":
            return self.pass_b
        raise AssertionError(f"unexpected role: {role}")


def _fixed_clock() -> datetime:
    return datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


def _ingest(path: Path, *, repo: DataRepo, db: LabsDb, vision: FakeVisionClient):  # type: ignore[no-untyped-def]
    return ingest_file(
        path,
        repo=repo,
        db=db,
        vision=vision,  # type: ignore[arg-type]
        clock=_fixed_clock,
        renderer=fake_page_renderer(1),
    )


def test_lab_report_full_agreement_is_all_auto_and_exports_jsonl(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "quest-2026-05-02.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("clean_agreement.json")

    report = ingest_file(
        doc_path,
        repo=repo,
        db=db,
        vision=vision,  # type: ignore[arg-type]
        clock=_fixed_clock,
        renderer=fake_page_renderer(1),
    )

    assert len(report.files) == 1
    outcome = report.files[0]
    assert outcome.outcome == "ingested"
    assert outcome.doc_type == "lab_report"
    assert outcome.rows_auto == 2
    assert outcome.rows_pending == 0
    assert outcome.issues == []
    assert outcome.commit_sha is not None

    export_path = repo.root / "labs-export.jsonl"
    assert export_path.exists()
    lines = export_path.read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(line)["table"] == "lab" for line in lines)

    documents = db.list_documents()
    assert len(documents) == 1
    assert documents[0].status == DocumentStatus.COMPLETE
    assert documents[0].doc_date == date(2026, 5, 2)


def test_commit_message_has_expected_shape(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "quest.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("clean_agreement.json")

    _ingest(doc_path, repo=repo, db=db, vision=vision)

    git_repo = Repo(repo.root)
    message = git_repo.head.commit.message
    assert message.startswith("ingest: Quest Diagnostics 2026-05-02")
    assert "(2 rows, 0 queued)" in message


def test_ingesting_the_same_document_twice_dedupes_and_makes_no_second_commit(
    tmp_path: Path,
) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "quest.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("clean_agreement.json")

    first = _ingest(doc_path, repo=repo, db=db, vision=vision)
    commit_count_after_first = len(list(Repo(repo.root).iter_commits()))

    second = _ingest(doc_path, repo=repo, db=db, vision=vision)
    commit_count_after_second = len(list(Repo(repo.root).iter_commits()))

    assert first.files[0].outcome == "ingested"
    assert second.files[0].outcome == "duplicate"
    assert second.files[0].commit_sha is None
    assert commit_count_after_second == commit_count_after_first
    # the classifier/extractor roles must not even be called on the duplicate
    assert vision.calls.count("classifier") == 1


def test_value_disagreement_queues_the_row_pending(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "quest.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("value_disagreement.json")

    report = _ingest(doc_path, repo=repo, db=db, vision=vision)

    outcome = report.files[0]
    assert outcome.rows_auto == 0
    assert outcome.rows_pending == 1
    assert any("value_mismatch" in issue for issue in outcome.issues)

    pending_rows = db.pending()
    assert len(pending_rows) == 1
    assert pending_rows[0].name == "potassium"


def test_non_lab_document_creates_an_encounter_stub_not_a_labs_row(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    doc_path = tmp_path / "rheum-visit.pdf"
    doc_path.write_bytes(TINY_PDF_BYTES)
    vision = FakeVisionClient("non_lab_clinical_note.json")

    report = _ingest(doc_path, repo=repo, db=db, vision=vision)

    outcome = report.files[0]
    assert outcome.outcome == "ingested"
    assert outcome.doc_type == "clinical_note"
    assert outcome.rows_auto == 0
    assert outcome.rows_pending == 0
    # extractor roles are never called for a non-lab document
    assert "extractor_pass_a" not in vision.calls
    assert "extractor_pass_b" not in vision.calls
    assert db.pending() == []

    encounters_dir = repo.root / "case" / "encounters"
    encounter_files = list(encounters_dir.glob("2026-06-10--*.md"))
    assert len(encounter_files) == 1
    encounter = read_encounter(encounter_files[0])
    assert encounter.frontmatter.type == "specialist-visit"
    assert encounter.summary == "(pending review)"
    assert encounter.frontmatter.sources


def test_ingest_inbox_scans_and_ingests_every_file(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data-repo")
    db = LabsDb(tmp_path / "labs.sqlite")
    (repo.root / "inbox" / "a.pdf").write_bytes(TINY_PDF_BYTES)
    (repo.root / "inbox" / "b.pdf").write_bytes(TINY_PDF_BYTES + b"\x00")
    vision = FakeVisionClient("clean_agreement.json")

    report = ingest_inbox(
        repo=repo,
        db=db,
        vision=vision,
        clock=_fixed_clock,
        renderer=fake_page_renderer(1),  # type: ignore[arg-type]
    )

    assert len(report.files) == 2
    assert all(f.outcome == "ingested" for f in report.files)
    assert report.total_auto == 4
    assert report.total_pending == 0


def test_scan_files_is_recursive_and_skips_sync_artifacts(tmp_path: Path) -> None:
    """Dropbox/rclone preserve subfolders in the inbox; nested files must be found."""
    from adoc.ingest.pipeline import _scan_files

    (tmp_path / "Labs" / "LabCorp").mkdir(parents=True)
    (tmp_path / "Labs" / "LabCorp" / "b.pdf").write_bytes(b"x")
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "desktop.ini").write_bytes(b"x")
    (tmp_path / ".hidden").write_bytes(b"x")

    names = [p.name for p in _scan_files(tmp_path)]

    # Deterministic full-path order: "Labs/LabCorp/b.pdf" sorts before "a.pdf".
    assert names == ["b.pdf", "a.pdf"]

"""Tests for the encounter full-text corpus.

ADR 0015 gave documents a searchable corpus; encounters never got one, so an
encounter body reached a reasoner as one summary line. On the real case file
a 110-line encounter contributed 107 characters and 3,446 reached no model at
all — and that applies to every patient-report encounter written from chat,
not just the regimen.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from adoc.casefile.encounter_text import sync_encounter_text
from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.repo import DataRepo
from adoc.labs.db import LabsDb


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    return LabsDb(tmp_path / "labs.sqlite")


def _write(repo: DataRepo, slug: str, summary: str, when: date = date(2026, 8, 1)) -> str:
    path = write_encounter(
        repo.root / "case" / "encounters",
        Encounter(
            frontmatter=EncounterFrontmatter(date=when, type="patient-report"),
            summary=summary,
        ),
        slug,
    )
    return path.name


def test_an_encounter_body_becomes_searchable(repo: DataRepo, db: LabsDb) -> None:
    """The whole point: the body, not just the title."""
    name = _write(repo, "regimen", "Takes magnesium glycinate each morning.")

    report = sync_encounter_text(repo, db)
    hits = db.search_encounter_text("magnesium")

    assert report.indexed == 1
    assert [h.filename for h in hits] == [name]
    assert "magnesium" in hits[0].snippet.lower()


def test_a_hit_cites_the_encounter_grammar_not_the_document_one(repo: DataRepo, db: LabsDb) -> None:
    """An encounter is cited `encounter:<filename>`. Emitting a `doc:` ref
    would be uncheckable — there is no document with that name."""
    name = _write(repo, "visit", "Reports new joint pain in both hands.")
    sync_encounter_text(repo, db)

    hit = db.search_encounter_text("joint pain")[0]

    assert hit.source_ref == f"encounter:{name}"
    assert hit.page is None


def test_syncing_twice_does_not_duplicate(repo: DataRepo, db: LabsDb) -> None:
    _write(repo, "visit", "Reports fatigue after meals.")

    sync_encounter_text(repo, db)
    second = sync_encounter_text(repo, db)

    assert second.indexed == 1
    assert len(db.search_encounter_text("fatigue")) == 1


def test_an_encounter_removed_from_the_repo_stops_answering(repo: DataRepo, db: LabsDb) -> None:
    """The repo is the source of truth and this index is derived, so a
    deleted encounter must not keep matching searches."""
    name = _write(repo, "visit", "Reports flushing after meals.")
    sync_encounter_text(repo, db)
    assert db.search_encounter_text("flushing")

    (repo.root / "case" / "encounters" / name).unlink()
    report = sync_encounter_text(repo, db)

    assert report.pruned == 1
    assert db.search_encounter_text("flushing") == []


def test_an_edited_encounter_reindexes(repo: DataRepo, db: LabsDb) -> None:
    name = _write(repo, "visit", "Reports mild headache.")
    sync_encounter_text(repo, db)

    path = repo.root / "case" / "encounters" / name
    path.write_text(path.read_text().replace("mild headache", "severe migraine"))
    sync_encounter_text(repo, db)

    assert db.search_encounter_text("migraine")
    assert db.search_encounter_text("headache") == []


def test_a_malformed_encounter_costs_itself_not_the_sweep(repo: DataRepo, db: LabsDb) -> None:
    """One bad file must not cost the index every other one."""
    _write(repo, "good", "Reports improved sleep.")
    (repo.root / "case" / "encounters" / "2026-08-02--broken.md").write_text("not an encounter")

    report = sync_encounter_text(repo, db)

    assert report.indexed == 1
    assert report.failed == ["2026-08-02--broken.md"]
    assert db.search_encounter_text("sleep")

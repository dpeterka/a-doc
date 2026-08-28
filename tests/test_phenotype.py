"""Tests for the phenotype profile and its backfill."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.phenotype import (
    PHENOTYPE_RELPATH,
    PhenotypeProfile,
    PhenotypeTerm,
    load_phenotype,
    merge_terms,
    save_phenotype,
    select_for_engine,
)
from adoc.casefile.phenotype_backfill import backfill_phenotype
from adoc.casefile.repo import DataRepo
from adoc.knowledge.hpo import HpoIndex

TERMS = {"HP:0002829": "Arthralgia", "HP:0012378": "Fatigue", "HP:0001945": "Fever"}
LOOKUP = {"joint pain": "HP:0002829", "fatigue": "HP:0012378", "fever": "HP:0001945"}


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


@pytest.fixture
def index() -> HpoIndex:
    return HpoIndex(TERMS, LOOKUP)


def _encounter(repo: DataRepo, slug: str, summary: str, when: date) -> None:
    write_encounter(
        repo.root / "case" / "encounters",
        Encounter(
            frontmatter=EncounterFrontmatter(date=when, type="patient-report"), summary=summary
        ),
        slug,
    )


def test_terms_are_dated_from_the_encounter_they_came_from(repo: DataRepo, index: HpoIndex) -> None:
    """A symptom recorded in 2024 must not be presented as current."""
    _encounter(repo, "old", "Reports joint pain.", date(2024, 3, 1))

    profile, _ = backfill_phenotype(repo, index, PhenotypeProfile())
    term = profile.by_id("HP:0002829")

    assert term is not None
    assert term.first_seen == date(2024, 3, 1)
    assert term.sources == ["encounter:2024-03-01--old.md"]


def test_present_and_excluded_terms_are_both_recorded(repo: DataRepo, index: HpoIndex) -> None:
    """LIRICAL takes an excluded phenotype as evidence in its own right."""
    _encounter(repo, "visit", "Reports fatigue. Denies fever.", date(2026, 8, 1))

    profile, report = backfill_phenotype(repo, index, PhenotypeProfile())

    assert profile.present_terms() == ["HP:0012378"]
    assert profile.excluded_terms() == ["HP:0001945"]
    assert report.present == 1 and report.excluded == 1


def test_the_same_term_across_encounters_widens_its_dates(repo: DataRepo, index: HpoIndex) -> None:
    _encounter(repo, "first", "Reports joint pain.", date(2024, 3, 1))
    _encounter(repo, "second", "Still has joint pain.", date(2026, 8, 1))

    profile, _ = backfill_phenotype(repo, index, PhenotypeProfile())
    term = profile.by_id("HP:0002829")

    assert term is not None
    assert term.first_seen == date(2024, 3, 1)
    assert term.last_seen == date(2026, 8, 1)
    assert len(term.sources) == 2


def test_a_conflict_resolves_to_present(index: HpoIndex) -> None:
    """A patient who reported a symptom once and denied it later genuinely
    had it. Dropping it would erase a real finding; keeping it costs at most
    one term a clinician can dismiss."""
    profile = merge_terms(
        PhenotypeProfile(),
        [
            PhenotypeTerm(term_id="HP:0001945", label="Fever", present=False),
            PhenotypeTerm(term_id="HP:0001945", label="Fever", present=True),
        ],
    )

    assert profile.present_terms() == ["HP:0001945"]


def test_a_malformed_encounter_costs_itself_not_the_sweep(repo: DataRepo, index: HpoIndex) -> None:
    _encounter(repo, "good", "Reports fatigue.", date(2026, 8, 1))
    (repo.root / "case" / "encounters" / "2026-08-02--broken.md").write_text("not an encounter")

    profile, report = backfill_phenotype(repo, index, PhenotypeProfile())

    assert report.skipped == ["2026-08-02--broken.md"]
    assert profile.present_terms() == ["HP:0012378"]


def test_round_trips_through_yaml(repo: DataRepo) -> None:
    path = repo.root / Path(PHENOTYPE_RELPATH)
    profile = PhenotypeProfile(
        entries=[
            PhenotypeTerm(
                term_id="HP:0002829",
                label="Arthralgia",
                first_seen=date(2024, 3, 1),
                sources=["encounter:x.md"],
                matched_text=["reports joint pain most mornings"],
            )
        ]
    )

    save_phenotype(path, profile)
    first = path.read_bytes()
    reloaded = load_phenotype(path)
    save_phenotype(path, reloaded)

    assert path.read_bytes() == first
    assert reloaded.entries[0].matched_text == ["reports joint pain most mornings"]


def test_engine_selection_caps_and_prefers_current_corroborated_terms(
    repo: DataRepo, index: HpoIndex
) -> None:
    """The full profile is the RECORD; the engine input is a QUERY.

    Conflating them produced an unusable ranking: at 82 terms LIRICAL's top
    composite LR was -25.97, against +4.82 at eight terms. Terms no single
    disease explains subtract without bound.
    """
    _encounter(repo, "old", "Reports joint pain.", date(2019, 1, 1))
    _encounter(repo, "recent-a", "Reports fatigue.", date(2026, 8, 1))
    _encounter(repo, "recent-b", "Still fatigued, and a fever.", date(2026, 8, 20))

    profile, _ = backfill_phenotype(repo, index, PhenotypeProfile())
    observed, excluded = select_for_engine(profile, today=date(2026, 8, 28), limit=2)

    assert len(observed) == 2
    # Fatigue is both current and twice-corroborated; the 2019 joint pain is
    # neither, and a differential about today should not be asked to explain
    # an episode that never recurred.
    assert "HP:0012378" in observed
    assert "HP:0002829" not in observed
    assert excluded == []


def test_excluded_terms_are_not_capped_with_the_observed_ones(repo: DataRepo) -> None:
    """LIRICAL takes a negated phenotype as evidence in its own right, there
    are typically few of them, and each is a deliberate clinical statement
    rather than an incidental mention."""
    profile = PhenotypeProfile(
        entries=[
            PhenotypeTerm(term_id=f"HP:000000{n}", label=f"t{n}", present=False)
            for n in range(1, 6)
        ]
    )

    observed, excluded = select_for_engine(profile, today=date(2026, 8, 28), limit=2)

    assert observed == []
    assert len(excluded) == 5

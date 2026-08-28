"""Tests for adoc.casefile.regimen_chat — chat turns updating the regimen.

The model proposes; deterministic code decides. These tests are about the
deciding.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from adoc.casefile.regimen import REGIMEN_RELPATH, Regimen, RegimenEntry, load_regimen, save_regimen
from adoc.casefile.regimen_chat import RegimenChange, apply_regimen_changes, to_entries
from adoc.casefile.repo import DataRepo

TODAY = date(2026, 8, 28)


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


def _entries(changes: list[RegimenChange], message: str) -> list[RegimenEntry]:
    entries, _ = to_entries(changes, message=message, today=TODAY, source_ref="patient-report:x")
    return entries


def test_a_name_the_patient_never_said_is_dropped() -> None:
    """A model that invents a substance writes a fiction into a medical
    record, and no prompt wording makes that impossible. A substring check
    does — the same principle as citation checking."""
    entries, dropped = to_entries(
        [RegimenChange(name="Magnesium", action="taking")],
        message="I've been really tired lately.",
        today=TODAY,
        source_ref="patient-report:x",
    )

    assert entries == []
    assert dropped == ["Magnesium"]


def test_grounding_tolerates_casing_and_punctuation() -> None:
    """What she typed and what the model returned will differ in shape."""
    entries = _entries(
        [RegimenChange(name="Vitamin D3", action="taking")],
        "i take vitamin-d3 every morning",
    )

    assert [e.name for e in entries] == ["Vitamin D3"]


def test_a_stop_with_a_vague_date_keeps_its_precision() -> None:
    """ "Last month" is a month, not a day. Recording a bare day would place a
    substance outside a lab draw it may actually have overlapped (ADR 0027)."""
    entries = _entries(
        [RegimenChange(name="selenium", action="stopped", when_text="last month")],
        "I stopped the selenium last month.",
    )

    assert entries[0].stopped is not None
    assert entries[0].stopped_precision in {"month", "approximate", "day"}
    assert entries[0].started is None


def test_a_start_with_no_stated_date_does_not_claim_today() -> None:
    """Recording today as the start would claim she began it during this
    conversation. Attesting today says only what is true: she is on it now."""
    entries = _entries(
        [RegimenChange(name="zinc", action="started")],
        "I started taking zinc.",
    )

    assert entries[0].started is None
    assert entries[0].attested_on == [TODAY]
    assert entries[0].overlaps(TODAY) == "active"


def test_a_plain_statement_attests_today_and_claims_nothing_else() -> None:
    entries = _entries([RegimenChange(name="iodine")], "I take iodine.")

    assert entries[0].started is None and entries[0].stopped is None
    assert entries[0].attested_on == [TODAY]
    assert entries[0].overlaps(date(2026, 1, 1)) == "unknown"


def test_a_stop_reported_in_chat_closes_the_interval_on_disk(repo: DataRepo) -> None:
    """The end-to-end behaviour the record exists for: she says she stopped
    something, and the open interval closes rather than a duplicate
    appearing."""
    path = repo.root / Path(REGIMEN_RELPATH)
    save_regimen(
        path,
        Regimen(entries=[RegimenEntry(name="Selenium", started=date(2026, 1, 1))]),
    )

    report = apply_regimen_changes(
        repo,
        [RegimenChange(name="selenium", action="stopped", when_text="last month")],
        message="I stopped the selenium last month.",
        today=TODAY,
    )

    after = load_regimen(path)
    assert report.applied == ["selenium"]
    assert len(after.entries) == 1
    assert after.entries[0].started == date(2026, 1, 1)
    assert after.entries[0].stopped is not None
    # ...and the entry now carries where the claim came from.
    assert any(s.startswith("patient-report:") for s in after.entries[0].sources)


def test_a_substance_not_already_on_file_is_recorded_from_chat(repo: DataRepo) -> None:
    """Biotin and selenium are absent from the backfilled document. A chat
    statement is how they get on the record at all."""
    report = apply_regimen_changes(
        repo,
        [RegimenChange(name="biotin", action="taking", dose="10000 mcg")],
        message="I take biotin 10000 mcg for my hair.",
        today=TODAY,
    )

    entries = load_regimen(repo.root / Path(REGIMEN_RELPATH)).entries
    assert report.applied == ["biotin"]
    assert entries[0].dose == "10000 mcg"
    assert entries[0].attested_on == [TODAY]


def test_a_turn_with_nothing_to_record_touches_no_disk(repo: DataRepo) -> None:
    """Most turns say nothing about the regimen; those must not write."""
    path = repo.root / Path(REGIMEN_RELPATH)

    report = apply_regimen_changes(repo, [], message="How are my labs?", today=TODAY)

    assert report.applied == []
    assert not path.exists()


def test_every_proposal_ungrounded_writes_nothing(repo: DataRepo) -> None:
    path = repo.root / Path(REGIMEN_RELPATH)

    report = apply_regimen_changes(
        repo,
        [RegimenChange(name="Magnesium")],
        message="I'm feeling better this week.",
        today=TODAY,
    )

    assert report.applied == []
    assert report.dropped_ungrounded == ["Magnesium"]
    assert not path.exists()

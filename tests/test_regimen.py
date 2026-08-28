"""Tests for adoc.casefile.regimen — what the patient takes, and when.

The whole point of this record is answering "was she taking X when this
specimen was drawn", so every test here is about time.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from adoc.casefile.regimen import (
    Regimen,
    RegimenEntry,
    load_regimen,
    merge_entries,
    save_regimen,
)


def test_a_boolean_cannot_answer_the_question_an_interval_can() -> None:
    """`still_taking` was the entire previous temporal model. This is the
    query it could never serve: high-dose biotin distorts immunoassays, so
    whether a hormone result is real depends on an interval overlapping a
    specimen date."""
    regimen = Regimen(
        entries=[
            RegimenEntry(
                name="Biotin",
                dose="10000 mcg",
                started=date(2026, 6, 1),
                started_precision="day",
                stopped=date(2026, 8, 1),
                stopped_precision="day",
            )
        ]
    )

    assert [e.name for e in regimen.active_on(date(2026, 7, 15))] == ["Biotin"]
    assert regimen.active_on(date(2026, 5, 1)) == []
    assert regimen.active_on(date(2026, 8, 20)) == []


def test_an_undated_entry_is_unknown_not_absent() -> None:
    """Treating an undated supplement as absent on a specimen date would give
    a confident wrong answer to exactly the question this record exists to
    settle."""
    entry = RegimenEntry(name="Selenium")

    assert entry.overlaps(date(2026, 7, 15)) == "unknown"
    # ...and it must not silently appear in an "active" answer.
    assert Regimen(entries=[entry]).active_on(date(2026, 7, 15)) == []
    assert Regimen(entries=[entry]).undated() == [entry]


def test_a_stop_date_without_a_start_still_places_the_entry() -> None:
    """A patient often knows when she stopped something far better than when
    she began, and that is enough to answer some questions."""
    entry = RegimenEntry(name="Iodine", stopped=date(2026, 3, 1))

    assert entry.overlaps(date(2026, 1, 1)) == "active"
    assert entry.overlaps(date(2026, 6, 1)) == "stopped"


def test_a_restart_is_a_new_interval_not_a_widened_one() -> None:
    """ "Took it in 2024, stopped, restarted in 2026" is clinically different
    from "took it continuously since 2024". Merging them would fabricate
    exposure across the gap — which may be exactly what a lab result
    reflects."""
    regimen = Regimen(
        entries=[RegimenEntry(name="Biotin", started=date(2024, 1, 1), stopped=date(2024, 6, 1))]
    )

    merged = merge_entries(regimen, [RegimenEntry(name="Biotin", started=date(2026, 6, 1))])

    assert len(merged.entries) == 2
    # The gap is preserved: nothing was active in between.
    assert merged.active_on(date(2025, 1, 1)) == []
    assert [e.name for e in merged.active_on(date(2026, 7, 1))] == ["Biotin"]


def test_a_stop_reported_later_closes_the_open_interval() -> None:
    """The update path that matters: the patient says in chat that she has
    stopped something. That closes the open interval rather than appending a
    second one."""
    regimen = Regimen(entries=[RegimenEntry(name="Selenium", started=date(2026, 1, 1))])

    merged = merge_entries(
        regimen,
        [RegimenEntry(name="selenium", stopped=date(2026, 8, 1), stopped_precision="day")],
    )

    assert len(merged.entries) == 1
    assert merged.entries[0].started == date(2026, 1, 1)
    assert merged.entries[0].stopped == date(2026, 8, 1)


def test_merging_fills_gaps_but_never_overwrites_a_known_value() -> None:
    """A later, vaguer mention must not erase a dose already on file."""
    regimen = Regimen(
        entries=[RegimenEntry(name="Vitamin D", dose="5000 IU", started=date(2026, 1, 1))]
    )

    merged = merge_entries(regimen, [RegimenEntry(name="Vitamin D", frequency="daily")])

    assert merged.entries[0].dose == "5000 IU"
    assert merged.entries[0].frequency == "daily"


def test_by_name_returns_every_interval_oldest_first() -> None:
    regimen = Regimen(
        entries=[
            RegimenEntry(name="Biotin", started=date(2026, 6, 1)),
            RegimenEntry(name="Biotin", started=date(2024, 1, 1), stopped=date(2024, 6, 1)),
        ]
    )

    assert [e.started for e in regimen.by_name("biotin")] == [
        date(2024, 1, 1),
        date(2026, 6, 1),
    ]


def test_round_trips_through_yaml(tmp_path: Path) -> None:
    """Saving identical content twice must produce byte-identical files, so
    git diffs stay meaningful — the same convention `save_ledger` follows."""
    path = tmp_path / "case" / "regimen.yaml"
    regimen = Regimen(
        entries=[
            RegimenEntry(
                name="Biotin",
                kind="supplement",
                dose="10000 mcg",
                started=date(2026, 6, 1),
                started_precision="month",
                attribution="self-started",
                sources=["encounter:2026-08-01--supplementregimenaugust2026.md"],
            )
        ]
    )

    save_regimen(path, regimen)
    first = path.read_bytes()
    reloaded = load_regimen(path)
    save_regimen(path, reloaded)

    assert path.read_bytes() == first
    assert reloaded.entries[0].started_precision == "month"
    assert reloaded.entries[0].sources == ["encounter:2026-08-01--supplementregimenaugust2026.md"]


def test_a_missing_file_is_an_empty_regimen(tmp_path: Path) -> None:
    assert load_regimen(tmp_path / "nope.yaml").entries == []

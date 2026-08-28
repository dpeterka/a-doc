"""Tests for adoc.casefile.regimen_backfill.

Synthetic fixture only — the real regimen list never appears in this repo
(CLAUDE.md: tests and CI use synthetic fixtures under `tests/fixtures/`).
The shapes below mirror the real document's structure: `Name — dose/frequency`
bullets with instruction lines mixed among them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from adoc.casefile.regimen import Regimen, merge_entries
from adoc.casefile.regimen_backfill import parse_regimen_bullets

SOURCE = "encounter:2026-08-01--regimen.md"

BODY = """---
date: '2026-08-01'
type: patient-report
---

## Summary

Daily regimen.

- Magnesium glycinate — once per breakfast
- CoQ10 — 600 mg
- Vitamin D3 with K2 — once per breakfast; take with a fatty meal
- B Complex — once per breakfast
- Take iron on an empty stomach.
- Space calcium and iron four hours apart.
- Magnesium glycinate — once per evening
"""


def test_parses_name_dose_and_frequency() -> None:
    entries = parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)
    by_name = {e.name: e for e in entries}

    assert by_name["CoQ10"].dose == "600 mg"
    assert by_name["CoQ10"].frequency is None
    assert by_name["Magnesium glycinate"].dose is None
    assert by_name["Magnesium glycinate"].frequency == "once per breakfast"
    assert by_name["Vitamin D3 with K2"].notes == "take with a fatty meal"


def test_instruction_lines_are_skipped_not_invented_as_substances() -> None:
    """The source mixes instructions among the items. Turning "Take iron on an
    empty stomach" into a supplement named "Take iron on an empty stomach"
    would put a fiction into the case file."""
    entries = parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)
    names = {e.name for e in entries}

    assert not any(name.lower().startswith("take ") for name in names)
    assert not any(name.lower().startswith("space ") for name in names)


def test_a_substance_repeated_across_sections_is_one_entry() -> None:
    """The list repeats items across time-of-day sections. One substance is
    one entry; the repetition carries no extra temporal information."""
    entries = parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)

    assert len([e for e in entries if e.name == "Magnesium glycinate"]) == 1


def test_the_document_date_is_recorded_as_an_attestation_not_a_start() -> None:
    """The document says what was being taken THAT DAY. Inventing a start date
    to make the interval work would fabricate exactly the temporal claim this
    record exists to prevent."""
    entry = parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)[0]

    assert entry.started is None
    assert entry.attested_on == [date(2026, 8, 1)]
    assert entry.overlaps(date(2026, 8, 1)) == "active"
    # ...and it says nothing about any other date.
    assert entry.overlaps(date(2026, 7, 1)) == "unknown"
    assert entry.sources == [SOURCE]


def test_rerunning_the_backfill_does_not_duplicate() -> None:
    """`merge_entries` updates an open interval rather than appending, so the
    command is idempotent."""
    first = parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)
    regimen = merge_entries(Regimen(), first)
    again = merge_entries(
        regimen, parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)
    )

    assert len(again.entries) == len(regimen.entries)


def test_a_second_document_adds_its_own_attestation(tmp_path: Path) -> None:
    """Two dated documents mean two attested dates for the same substance —
    which is how an interval eventually becomes knowable."""
    regimen = merge_entries(
        Regimen(), parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)
    )
    later = merge_entries(
        regimen,
        parse_regimen_bullets(BODY, attested=date(2026, 9, 1), source_ref="encounter:sept.md"),
    )
    entry = next(e for e in later.entries if e.name == "CoQ10")

    assert entry.attested_on == [date(2026, 8, 1), date(2026, 9, 1)]
    assert entry.overlaps(date(2026, 9, 1)) == "active"


def test_an_entry_with_only_an_attestation_is_not_counted_as_undated() -> None:
    """It cannot be placed on an arbitrary date, but it is not date-less —
    and the context section's "cannot be placed" count must not overstate."""
    regimen = merge_entries(
        Regimen(), parse_regimen_bullets(BODY, attested=date(2026, 8, 1), source_ref=SOURCE)
    )

    assert regimen.undated() == []
    assert regimen.attested_dates() == [date(2026, 8, 1)]

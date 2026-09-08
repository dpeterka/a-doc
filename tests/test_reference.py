"""Tests for `labs.reference` — abnormal is a comparison, not a flag (ADR 0051)."""

from __future__ import annotations

import json
from datetime import date

from adoc.labs.models import LabFlag, LabResult
from adoc.labs.reference import (
    is_abnormal,
    is_high,
    is_low,
    parse_reference_range,
    range_position,
    reference_bounds,
)


def _row(**kw: object) -> LabResult:
    fields: dict[str, object] = {
        "date": date(2026, 5, 2),
        "name": "ferritin",
        "name_raw": "Ferritin",
        "source_doc": "a" * 64,
        "raw_json": json.dumps({}),
    }
    fields.update(kw)
    return LabResult(**fields)  # type: ignore[arg-type]


def test_a_range_with_its_unit_parses() -> None:
    """The single reason 920 stored rows carry a `ref_text` and no bounds:
    `labs.db._parse_ref_range` is anchored with `$` and rejects a trailing
    unit."""
    assert parse_reference_range("16-232 ng/mL") == (16.0, 232.0)
    assert parse_reference_range("0.01-2.99 ng/mL") == (0.01, 2.99)
    assert parse_reference_range("140-400") == (140.0, 400.0)
    assert parse_reference_range("15 - 150") == (15.0, 150.0)


def test_one_sided_ranges_parse() -> None:
    """Common for antibodies and tumour markers."""
    assert parse_reference_range("<5") == (None, 5.0)
    assert parse_reference_range("> 60") == (60.0, None)
    assert parse_reference_range("<=5") == (None, 5.0)


def test_unparseable_text_yields_no_bounds() -> None:
    for text in ("", None, "see report", "negative", "not established"):
        assert parse_reference_range(text) == (None, None)


def test_the_lab_flag_wins_over_the_range() -> None:
    """A lab that says `H` has applied its own judgement and knows more
    about its assay than this code does."""
    row = _row(value=100.0, ref_low=16.0, ref_high=232.0, flag=LabFlag.HIGH)

    assert range_position(row) == "high"


def test_an_unflagged_row_is_judged_against_its_range() -> None:
    """The 42 rows this unlocks — measured on the real record, sitting above
    their range with no flag, invisible to every predicate that read only
    the flag."""
    assert range_position(_row(value=410.0, ref_low=16.0, ref_high=232.0)) == "high"
    assert range_position(_row(value=5.0, ref_low=16.0, ref_high=232.0)) == "low"
    assert range_position(_row(value=100.0, ref_low=16.0, ref_high=232.0)) == "normal"


def test_the_range_is_recovered_from_ref_text_when_the_columns_are_empty() -> None:
    """At read time rather than by migration, so rows already stored with
    unparsed text are fixed the moment this ships."""
    row = _row(value=410.0, ref_text="16-232 ng/mL")

    assert reference_bounds(row) == (16.0, 232.0)
    assert range_position(row) == "high"


def test_nothing_to_judge_against_is_none_and_never_normal() -> None:
    """The distinction the whole module exists for. 1892 of 2079 stored rows
    carry no flag; reading those as normal is what let a rule-out fire on
    absent information."""
    row = _row(value=410.0)

    assert range_position(row) is None
    assert is_abnormal(row) is False
    assert is_high(row) is False
    assert is_low(row) is False


def test_an_abnormal_flag_with_no_range_stays_undecided() -> None:
    """`A` says something is off without saying what. 46 such rows on the
    real record, 0 of them resolvable by a range — so they stay honest
    rather than becoming a coin flip."""
    row = _row(value=410.0, flag=LabFlag.ABNORMAL)

    assert range_position(row) is None


def test_an_abnormal_flag_with_a_range_gets_its_direction() -> None:
    row = _row(value=410.0, flag=LabFlag.ABNORMAL, ref_low=16.0, ref_high=232.0)

    assert range_position(row) == "high"


def test_a_qualitative_row_has_no_position() -> None:
    """No number, nothing to compare. `_is_positive` handles these."""
    assert range_position(_row(value_text="Positive", ref_text="16-232")) is None


def test_a_reversed_range_is_read_the_right_way_round() -> None:
    """A parse artefact, not a fact about the assay."""
    assert parse_reference_range("232-16") == (16.0, 232.0)

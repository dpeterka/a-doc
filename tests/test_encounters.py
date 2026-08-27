"""Tests for adoc.casefile.encounters: frontmatter model, rendering, round-trip."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from adoc.casefile.encounters import (
    SLUG_MAX_CHARS,
    Encounter,
    EncounterFrontmatter,
    encounter_filename,
    parse_encounter,
    read_encounter,
    render_encounter,
    slugify,
    write_encounter,
)
from adoc.intake.wizard import parse_approx_date_with_precision


def make_encounter(**overrides: object) -> Encounter:
    frontmatter_defaults: dict[str, object] = {
        "date": date(2026, 8, 20),
        "type": "specialist-visit",
        "provider": "Dr. Ada Rheum",
        "sources": ["doc:visit-note.pdf#p1"],
        "symptoms": ["fatigue", "joint pain"],
    }
    frontmatter_defaults.update(overrides.pop("frontmatter", {}))  # type: ignore[arg-type]
    frontmatter = EncounterFrontmatter.model_validate(frontmatter_defaults)
    body_defaults: dict[str, object] = {
        "summary": "Routine rheumatology follow-up.",
        "new_findings": "ANA titer up from 1:160 to 1:640.",
        "plan": "Order complement panel; recheck in 6 weeks.",
    }
    body_defaults.update(overrides)
    return Encounter(frontmatter=frontmatter, **body_defaults)  # type: ignore[arg-type]


# --- frontmatter model -------------------------------------------------------------------


def test_frontmatter_accepts_all_documented_types() -> None:
    for enc_type in (
        "lab-result",
        "specialist-visit",
        "imaging",
        "patient-report",
        "phone",
        "procedure",
    ):
        fm = EncounterFrontmatter(date=date(2026, 1, 1), type=enc_type)  # type: ignore[arg-type]
        assert fm.type == enc_type


def test_frontmatter_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        EncounterFrontmatter(date=date(2026, 1, 1), type="unknown-type")  # type: ignore[arg-type]


def test_frontmatter_defaults_are_empty_lists() -> None:
    fm = EncounterFrontmatter(date=date(2026, 1, 1), type="phone")
    assert fm.provider is None
    assert fm.sources == []
    assert fm.symptoms == []


# --- filename convention ------------------------------------------------------------------


def test_encounter_filename_convention() -> None:
    fm = EncounterFrontmatter(date=date(2026, 8, 20), type="specialist-visit")
    assert encounter_filename(fm, "rheum follow-up") == "2026-08-20--rheum-follow-up.md"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rheum Follow-Up", "rheum-follow-up"),
        ("  spaces  around  ", "spaces-around"),
        ("Weird!!Chars??", "weirdchars"),
        ("", "encounter"),
        ("already-a-slug", "already-a-slug"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


# --- render/parse round trip ---------------------------------------------------------------


def test_render_encounter_has_frontmatter_and_sections() -> None:
    encounter = make_encounter()
    text = render_encounter(encounter)

    assert text.startswith("---\n")
    assert "type: specialist-visit" in text
    assert "## Summary" in text
    assert "## New findings" in text
    assert "## Plan / follow-ups" in text
    assert "ANA titer up from 1:160 to 1:640." in text


def test_parse_encounter_round_trips_render_output() -> None:
    encounter = make_encounter()
    text = render_encounter(encounter)

    parsed = parse_encounter(text)

    assert parsed == encounter


def test_parse_encounter_round_trips_patient_report() -> None:
    encounter = make_encounter(
        frontmatter={
            "date": date(2026, 8, 1),
            "type": "patient-report",
            "provider": None,
            "sources": [],
            "symptoms": ["fatigue"],
        },
        summary="Patient reports new-onset joint stiffness.",
        new_findings="",
        plan="Discuss at next visit.",
    )
    text = render_encounter(encounter)
    parsed = parse_encounter(text)

    assert parsed == encounter
    assert parsed.frontmatter.type == "patient-report"
    assert parsed.frontmatter.provider is None


def test_parse_encounter_rejects_missing_frontmatter() -> None:
    with pytest.raises(ValueError, match="frontmatter"):
        parse_encounter("## Summary\n\nNo frontmatter here.\n")


def test_parse_encounter_rejects_unclosed_frontmatter() -> None:
    with pytest.raises(ValueError, match="frontmatter"):
        parse_encounter("---\ndate: 2026-08-01\ntype: phone\n\n## Summary\n\nBody.\n")


# --- file round trip -----------------------------------------------------------------------


def test_write_and_read_encounter_round_trip(tmp_path: Path) -> None:
    encounters_dir = tmp_path / "case" / "encounters"
    encounter = make_encounter()

    path = write_encounter(encounters_dir, encounter, "rheum follow-up")

    assert path == encounters_dir / "2026-08-20--rheum-follow-up.md"
    assert path.exists()
    assert read_encounter(path) == encounter


def test_write_encounter_creates_missing_directory(tmp_path: Path) -> None:
    encounters_dir = tmp_path / "does" / "not" / "exist"
    encounter = make_encounter()

    path = write_encounter(encounters_dir, encounter, "first-visit")

    assert path.exists()


def test_slugify_bounds_a_paragraph_length_title(tmp_path: Path) -> None:
    """Slugs come from model-written titles, which are unbounded. A live intake
    turn produced a paragraph-long event title and the write failed with
    `OSError: [Errno 36] File name too long`, so the encounter was lost."""
    long_title = (
        "Patient made two separate ER trips in March 2025 with nausea and weakness "
        "she attributes both to her thyroid levels thyroid stopping functioning "
        "during these hormonal coma events she remained conscious but could not "
        "function she was placed on an IV and monitored until she recovered"
    )
    slug = slugify(long_title)
    assert len(slug) <= SLUG_MAX_CHARS
    assert slug.startswith("patient-made-two-separate-er-trips")

    frontmatter = EncounterFrontmatter(date=date(2025, 1, 1), type="patient-report")
    assert len(encounter_filename(frontmatter, long_title).encode("utf-8")) < 255

    written = write_encounter(
        tmp_path / "encounters", Encounter(frontmatter=frontmatter), long_title
    )
    assert written.exists()


def test_slugify_keeps_long_titles_distinct_when_they_share_a_prefix() -> None:
    """Generated titles for related events share long prefixes, so naive
    truncation would silently overwrite one encounter with another."""
    base = "Patient made two separate ER trips in March 2025 with nausea and weakness and "
    a = slugify(base + "was admitted overnight for observation")
    b = slugify(base + "was discharged the same evening after fluids")
    assert a != b
    assert len(a) <= SLUG_MAX_CHARS
    assert len(b) <= SLUG_MAX_CHARS


def test_slugify_is_unchanged_for_ordinary_titles() -> None:
    assert slugify("ER visit for chest pain") == "er-visit-for-chest-pain"
    assert slugify("   ") == "encounter"


# --- ADR 0027: a date is stated no more precisely than it is known ---------------------


@pytest.mark.parametrize(
    ("text", "expected_precision"),
    [
        ("2021-03-15", "day"),
        ("2021-03", "month"),
        ("2021", "year"),
        ("early 2021", "approximate"),
        ("spring 2022", "approximate"),
    ],
)
def test_precision_travels_with_a_parsed_date(text: str, expected_precision: str) -> None:
    """ "2021" and "early 2021" both parse to 2021-01-01, and "spring 2022" to
    2022-01-01 — the wrong season asserted to the day. Downstream that was
    indistinguishable from a real January 1st."""
    parsed = parse_approx_date_with_precision(text)

    assert parsed is not None
    assert parsed[1] == expected_precision


def test_an_undatable_event_still_yields_nothing() -> None:
    """Patients often cannot date a hospitalization; that stays an undated
    event rather than a fabricated date."""
    assert parse_approx_date_with_precision("about six years ago") is None


def test_an_encounter_written_before_this_field_round_trips() -> None:
    """Existing encounter files have no precision or reported_on; both
    default so the committed format is unchanged."""
    markdown = (
        "---\ndate: 2025-03-26\ntype: imaging\n---\n\n"
        "## Summary\n\nCT abdomen.\n\n## New findings\n\n\n\n## Plan / follow-ups\n\n\n"
    )

    encounter = parse_encounter(markdown)

    assert encounter.frontmatter.date_precision == "day"
    assert encounter.frontmatter.reported_on is None


def test_precision_and_report_date_survive_a_round_trip() -> None:
    fm = EncounterFrontmatter(
        date=date(2021, 1, 1),
        type="patient-report",
        date_precision="year",
        reported_on=date(2026, 8, 27),
    )

    parsed = parse_encounter(render_encounter(Encounter(frontmatter=fm, summary="Thyroid crisis")))

    assert parsed.frontmatter.date_precision == "year"
    assert parsed.frontmatter.reported_on == date(2026, 8, 27)

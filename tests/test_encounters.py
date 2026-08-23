"""Tests for adoc.casefile.encounters: frontmatter model, rendering, round-trip."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from adoc.casefile.encounters import (
    Encounter,
    EncounterFrontmatter,
    encounter_filename,
    parse_encounter,
    read_encounter,
    render_encounter,
    slugify,
    write_encounter,
)


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

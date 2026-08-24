"""Tests for adoc.intake.coverage: the per-topic coverage-map state that
replaced the cursor/per-section status machine
(`docs/adr/0012-initial-visit-conversation.md`)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML

from adoc.intake.coverage import (
    CoverageState,
    TopicCoverage,
    load_coverage_state,
    save_coverage_state,
)


def test_missing_file_yields_a_fresh_empty_state(tmp_path: Path) -> None:
    state = load_coverage_state(tmp_path / "intake-state.yaml")

    assert state.topics == {}
    assert state.intake_complete is False


def test_round_trips_through_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "intake-state.yaml"
    now = datetime.now(UTC)
    original = CoverageState(
        topics={
            "basics": TopicCoverage(covered=True, covered_at=now),
            "symptoms": TopicCoverage(covered=False),
        },
        intake_complete=False,
    )

    save_coverage_state(path, original)
    loaded = load_coverage_state(path)

    assert loaded.topics["basics"].covered is True
    assert loaded.topics["symptoms"].covered is False
    assert loaded.intake_complete is False


def test_old_style_completed_sections_migrate_to_covered(tmp_path: Path) -> None:
    """An old-style `intake.wizard`-shaped state file (cursor/per-section
    status) written before the switch to the conversational engine must
    migrate on read: a `"complete"` section becomes `covered=True`, an
    incomplete one stays `covered=False`, and `covered_at` carries the old
    `completed_at` forward."""
    path = tmp_path / "intake-state.yaml"
    yaml = YAML()
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            {
                "sections": {
                    "basics": {
                        "status": "complete",
                        "draft": {"age": 41},
                        "completed_at": "2026-01-01T00:00:00+00:00",
                    },
                    "symptoms": {"status": "awaiting_confirmation", "draft": {}},
                    "events": {"status": "pending"},
                },
                "cursor": "symptoms",
            },
            fh,
        )

    state = load_coverage_state(path)

    assert state.topics["basics"].covered is True
    assert state.topics["basics"].covered_at is not None
    assert state.topics["symptoms"].covered is False
    assert state.topics["events"].covered is False
    # cursor was not None (onboarding was mid-flow) -> not complete overall
    assert state.intake_complete is False


def test_old_style_state_with_every_section_complete_and_no_cursor_migrates_to_intake_complete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intake-state.yaml"
    yaml = YAML()
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            {
                "sections": {
                    "basics": {"status": "complete", "completed_at": "2026-01-01T00:00:00+00:00"},
                    "symptoms": {"status": "complete", "completed_at": "2026-01-02T00:00:00+00:00"},
                },
                "cursor": None,
            },
            fh,
        )

    state = load_coverage_state(path)

    assert state.topics["basics"].covered is True
    assert state.topics["symptoms"].covered is True
    assert state.intake_complete is True


def test_a_new_style_file_with_no_topics_key_at_all_is_not_mistaken_for_old_style(
    tmp_path: Path,
) -> None:
    """An empty new-style file (no `sections`/`cursor`, no `topics` either
    — e.g. a stray `{}`) must load as an empty `CoverageState`, not raise."""
    path = tmp_path / "intake-state.yaml"
    path.write_text("{}\n", encoding="utf-8")

    state = load_coverage_state(path)

    assert state.topics == {}
    assert state.intake_complete is False

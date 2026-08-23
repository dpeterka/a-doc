"""Tests for adoc.reason.prompts: the versioned prompt-template loader."""

from __future__ import annotations

import pytest

from adoc.reason.prompts import Prompt, PromptError, load_prompt

ALL_TEMPLATE_NAMES = (
    "ledger_maintainer",
    "challenger",
    "test_chooser",
    "composer",
    "classifier",
    "blind_reviewer",
    "divergence_adjudicator",
    "challenge_sweep",
)


@pytest.mark.parametrize("name", ALL_TEMPLATE_NAMES)
def test_every_template_loads_and_has_a_parseable_version(name: str) -> None:
    prompt = load_prompt(name)

    assert isinstance(prompt, Prompt)
    assert prompt.name == name
    assert prompt.version  # non-empty
    int(prompt.version)  # parses as an integer version
    assert len(prompt.sha256) == 64
    assert prompt.text.startswith("<!-- version:")


def test_load_prompt_is_stable_across_calls() -> None:
    first = load_prompt("composer")
    second = load_prompt("composer")
    assert first == second


def test_load_prompt_missing_template_raises() -> None:
    with pytest.raises(PromptError):
        load_prompt("does_not_exist")


def test_load_prompt_sha256_changes_if_text_differs() -> None:
    ledger_maintainer = load_prompt("ledger_maintainer")
    challenger = load_prompt("challenger")
    assert ledger_maintainer.sha256 != challenger.sha256


# --- content requirements (PLAN.md "Anti-anchoring" / "Safety") -------------------------


def test_ledger_maintainer_instructs_probability_ranked_diff_and_source_refs() -> None:
    text = load_prompt("ledger_maintainer").text.lower()
    assert "ledgerdiff" in text
    assert "source" in text and "ref" in text
    assert "cant-miss" in text or "can't-miss" in text or "can’t-miss" in text


def test_ledger_maintainer_quarantines_patient_theories() -> None:
    text = load_prompt("ledger_maintainer").text
    assert "case/patient-theories.md" in text
    assert 'origin: "patient"' in text or "origin: patient" in text
    assert "never" in text.lower()


def test_challenger_requires_attacking_and_recording_challenges() -> None:
    text = load_prompt("challenger").text
    assert "attack" in text.lower()
    assert "record_challenge" in text
    assert "most-likely" in text.lower()
    assert "cant-miss" in text.lower() or "can't-miss" in text.lower()


def test_composer_requires_tiers_framing_and_no_dosing() -> None:
    text = load_prompt("composer").text.lower()
    assert "most likely" in text
    assert "expanded" in text
    assert "can't-miss" in text
    assert "discuss with your doctor" in text
    assert "dosing" in text or "dose" in text


def test_classifier_produces_a_turn_route() -> None:
    text = load_prompt("classifier").text.lower()
    assert "informational" in text
    assert "diagnostic" in text


def test_blind_reviewer_asserts_ledger_absence() -> None:
    text = load_prompt("blind_reviewer").text.lower()
    assert "de novo" in text
    assert "ledger" in text and "withheld" in text

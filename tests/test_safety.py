"""Tests for adoc.reason.safety: the deterministic treatment gate.
Positive/negative examples are pinned in `tests/fixtures/redteam.yaml`
(CLAUDE.md rule 2: safety behavior is a required CI check, never weakened
to make an unrelated change pass).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from adoc.reason.safety import treatment_gate

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "redteam.yaml"


def _load_fixture() -> dict[str, Any]:
    yaml = YAML(typ="safe")
    with FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert isinstance(data, dict)
    return data


FIXTURE = _load_fixture()


# --- treatment_gate: dosing/prescriptive-instruction gate, from the fixture --------------


@pytest.mark.parametrize("text", FIXTURE["treatment_gate"]["blocked"])
def test_treatment_gate_blocks_dosing_and_prescriptive_language(text: str) -> None:
    result = treatment_gate(text)
    assert result.passed is False
    assert result.spans
    assert result.rewrite_instruction is not None


@pytest.mark.parametrize("text", FIXTURE["treatment_gate"]["allowed"])
def test_treatment_gate_allows_tests_specialists_and_general_discussion(text: str) -> None:
    result = treatment_gate(text)
    assert result.passed is True
    assert result.spans == []
    assert result.rewrite_instruction is None


def test_treatment_gate_ignores_lab_result_concentration_units() -> None:
    """mg/dL, mg/L, mg/mL are lab-result units, not dosing instructions —
    the gate must never block a legitimate quantitative lab claim."""
    result = treatment_gate(
        "Your CRP was 8 mg/L and your creatinine was 1.2 mg/dL at your last visit."
    )
    assert result.passed is True


def test_treatment_gate_still_blocks_dosing_frequency_with_a_slash() -> None:
    result = treatment_gate("Take 40 mg/day of the medication your doctor prescribed.")
    assert result.passed is False


def test_treatment_gate_span_reasons_distinguish_dosage_from_imperative_verb() -> None:
    result = treatment_gate("Take 20 mg prednisone daily.")
    assert result.passed is False
    reasons = {span.reason for span in result.spans}
    assert "dosage pattern" in reasons


# --- S1 remediation: window-based imperative-verb detection ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Stop taking your prednisone.",
        "Start taking hydroxychloroquine.",
        "You should take two tablets of ibuprofen every morning.",
        "Increase your dose of metformin.",
    ],
)
def test_treatment_gate_blocks_verb_and_drug_separated_by_intervening_words(text: str) -> None:
    """The old detector only matched a drug token IMMEDIATELY after the
    imperative verb (past at most one determiner); these all slipped
    through. The window-based scan must block every one."""
    result = treatment_gate(text)
    assert result.passed is False
    assert result.spans


def test_treatment_gate_allows_clause_that_defers_to_a_clinician_even_naming_a_drug() -> None:
    """A clause anchored on ask/discuss/talk with your doctor/clinician/
    specialist defers the actual decision, so it is allowed even though it
    names a drug alongside a gating verb ("taper")."""
    result = treatment_gate(
        "It's worth discussing whether to taper prednisone with your rheumatologist."
    )
    assert result.passed is True
    assert result.spans == []


def test_treatment_gate_still_blocks_imperative_verb_outside_a_deferred_clause() -> None:
    """The doctor-deferral carve-out is scoped to its own clause: a second,
    unrelated clause in the same message must still be gated."""
    result = treatment_gate(
        "Ask your doctor about your levothyroxine dose. Stop taking your prednisone now."
    )
    assert result.passed is False
    assert any("prednisone" in span.text.lower() for span in result.spans)


def test_treatment_gate_imperative_window_does_not_reach_across_a_sentence_boundary() -> None:
    """A drug name in the NEXT sentence must not link back to an imperative
    verb in the prior one."""
    result = treatment_gate("Stop worrying about your labs. Prednisone was mentioned once.")
    assert result.passed is True


# --- ADR 0020: g/mL/units bare measurements are not doses on their own -------------------


def test_treatment_gate_allows_a_bare_ultrasound_volume() -> None:
    """The real production incident that motivated ADR 0020: a diagnostic
    turn was blocked on a bare "106.0 mL" ultrasound volume, a legitimate
    finding with nothing to do with dosing."""
    result = treatment_gate("Your pelvic ultrasound showed a 106.0 mL ovarian cyst.")
    assert result.passed is True


def test_treatment_gate_blocks_a_liquid_dose_with_frequency_context() -> None:
    """The mL narrowing must not become a hole: a liquid dose with dosing
    frequency context still blocks, with or without an imperative verb."""
    assert treatment_gate("Take 5 mL twice daily.").passed is False
    assert treatment_gate("The recommended dose is 5 mL twice daily.").passed is False


def test_treatment_gate_mg_mcg_iu_still_fire_without_context() -> None:
    """Unlike g/mL/units, mg/mcg/iu fire unconditionally — a bare
    denominator-free amount in these units is overwhelmingly a dose."""
    assert treatment_gate("5000 IU").passed is False
    assert treatment_gate("50 mcg").passed is False
    assert treatment_gate("20 mg").passed is False


# --- recording_only: intake is a scribe, not an advisor -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Are you still taking levothyroxine 50 mcg daily?",
        "You take vitamin D 5000 IU and B12 1000 mcg — is that right?",
        "I recorded it as thyroid replacement hormone, with the dose not remembered.",
        "Were you on prednisone at the time?",
    ],
)
def test_recording_mode_allows_asking_about_and_restating_medications(text: str) -> None:
    """Intake's job includes asking "which medication, and what dose?" and
    reading a list back. Blocking that made intake withhold its own reply to
    a patient who had just said she could not remember her medication."""
    assert treatment_gate(text, recording_only=True).passed


@pytest.mark.parametrize(
    "text",
    [
        "Start taking 50 mcg of levothyroxine daily.",
        "Take 20 mg prednisone every morning.",
        "I recommend tapering your prednisone.",
        "You should take 5000 IU of vitamin D.",
    ],
)
def test_recording_mode_still_blocks_actual_instructions(text: str) -> None:
    """The narrowing must not become a hole: an instruction is still an
    instruction, with or without a subject in front of the verb."""
    assert not treatment_gate(text, recording_only=True).passed


@pytest.mark.parametrize(
    "text",
    [
        "Are you still taking levothyroxine 50 mcg daily?",
        "Start taking 50 mcg of levothyroxine daily.",
    ],
)
def test_default_gate_is_unchanged_and_still_blocks_dosing(text: str) -> None:
    """Diagnostic and informational replies keep the full gate — only the
    intake path opts into recording mode."""
    assert not treatment_gate(text).passed

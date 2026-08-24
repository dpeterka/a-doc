"""Tests for adoc.reason.safety: deterministic red-flag screen + treatment
gate. No LLM calls anywhere in this module — that is the entire point of
`safety.py`. Positive/negative examples are pinned in
`tests/fixtures/redteam.yaml` (CLAUDE.md rule 2: safety behavior is a
required CI check, never weakened to make an unrelated change pass).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from adoc.reason.safety import (
    RedFlagResult,
    guarded_turn,
    red_flag_screen,
    treatment_gate,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "redteam.yaml"


def _load_fixture() -> dict[str, Any]:
    yaml = YAML(typ="safe")
    with FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert isinstance(data, dict)
    return data


FIXTURE = _load_fixture()
CATEGORY_CASES: list[dict[str, Any]] = FIXTURE["red_flag_categories"]


# --- red_flag_screen: category coverage, from the fixture -------------------------------


@pytest.mark.parametrize("case", CATEGORY_CASES, ids=lambda c: c["category"])
def test_red_flag_category_positive_examples_flag(case: dict[str, Any]) -> None:
    for text in case["positive"]:
        result = red_flag_screen(text)
        assert result.flagged is True, f"expected a flag for {text!r}"
        assert result.category == case["category"]
        assert result.message is not None
        assert "911" in result.message or "988" in result.message


@pytest.mark.parametrize("case", CATEGORY_CASES, ids=lambda c: c["category"])
def test_red_flag_category_conservative_false_positives_still_flag(case: dict[str, Any]) -> None:
    """Documented conservative policy (no negation parsing): "no chest pain"
    and "the chest pain went away years ago" still flag. A false positive
    here costs a little friction; a false negative on a real emergency does
    not get a second chance."""
    for text in case["conservative_false_positive"]:
        result = red_flag_screen(text)
        assert result.flagged is True, f"expected the conservative policy to still flag {text!r}"


@pytest.mark.parametrize("case", CATEGORY_CASES, ids=lambda c: c["category"])
def test_red_flag_category_benign_examples_do_not_flag(case: dict[str, Any]) -> None:
    for text in case["benign"]:
        result = red_flag_screen(text)
        assert result.flagged is False, f"unexpectedly flagged benign text: {text!r}"
        assert result.category is None
        assert result.message is None


def test_red_flag_screen_every_category_is_independently_reachable() -> None:
    """Sanity check that the fixture actually exercises all seven categories
    named in PLAN.md "Safety" (cardiac chest pain, stroke, anaphylaxis,
    suicidality, severe bleeding, sepsis/meningitis, anticoagulant
    emergencies) — not a subset."""
    categories = {c["category"] for c in CATEGORY_CASES}
    assert categories == {
        "cardiac_chest_pain",
        "stroke_signs",
        "anaphylaxis",
        "suicidality_self_harm",
        "severe_bleeding",
        "sepsis_meningitis",
        "anticoagulant_emergency",
    }


def test_red_flag_screen_ordinary_clinical_text_does_not_flag() -> None:
    result = red_flag_screen("My CRP was 8 mg/L last month at my rheumatology follow-up.")
    assert result == RedFlagResult(flagged=False)


def test_red_flag_result_matched_terms_are_populated() -> None:
    result = red_flag_screen("I want to kill myself")
    assert result.flagged is True
    assert result.matched_terms  # non-empty: something concrete was matched


# --- guarded_turn: zero-API-call wiring --------------------------------------------------


def test_guarded_turn_never_calls_on_pass_when_flagged() -> None:
    calls: list[str] = []

    def on_pass() -> str:
        calls.append("called")
        return "should never happen"

    result = guarded_turn("crushing chest pain radiating to my left arm", on_pass)

    assert calls == []
    assert isinstance(result, RedFlagResult)
    assert result.flagged is True
    assert result.category == "cardiac_chest_pain"


def test_guarded_turn_calls_on_pass_when_clear() -> None:
    result = guarded_turn("what was my last CRP result?", lambda: "answer")
    assert result == "answer"


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

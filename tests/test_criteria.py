"""Tests for adoc.knowledge.criteria — deterministic classification scorers.

No LLM anywhere in this module or its tests (CLAUDE.md: deterministic logic
is never delegated to a model).
"""

from __future__ import annotations

import json
from datetime import date

from adoc.knowledge.criteria import (
    CLASSIFICATION_DISCLAIMER,
    score_all,
    score_sle_2019,
)
from adoc.labs.models import LabFlag, LabResult

SHA = "e" * 64


def _row(
    name: str,
    *,
    value: float | None = None,
    value_text: str | None = None,
    flag: LabFlag | None = None,
    when: date = date(2026, 5, 2),
) -> LabResult:
    return LabResult(
        date=when,
        name=name,
        name_raw=name,
        value=value,
        value_text=value_text,
        flag=flag,
        source_doc=SHA,
        raw_json=json.dumps({"name_raw": name}),
    )


def test_no_ana_on_file_leaves_the_entry_criterion_unevaluated() -> None:
    """The 2019 set has an entry criterion. Absent an ANA the set does not
    apply, and saying so is different from saying the patient failed it."""
    result = score_sle_2019([_row("CRP", value=8.5)])

    assert result.entry_met is None
    assert "cannot be evaluated" in result.entry_note
    assert not result.meets_threshold


def test_a_titer_denominator_decides_the_entry_criterion() -> None:
    """1:640 is a HIGHER titre than 1:80 even though 640 reads like a bigger
    denominator — the comparison is on the number after the colon."""
    assert score_sle_2019([_row("ANA", value_text="1:640")]).entry_met is True
    assert score_sle_2019([_row("ANA", value_text="1:80")]).entry_met is True
    assert score_sle_2019([_row("ANA", value_text="1:40")]).entry_met is False


def test_negated_serology_is_not_read_as_positive() -> None:
    """ "Not detected" contains "detected"; negation must be checked first."""
    result = score_sle_2019(
        [_row("ANA", value_text="1:320"), _row("anti-dsDNA", value_text="Not Detected")]
    )
    antibody = next(i for i in result.items if i.name == "Anti-dsDNA or anti-Smith")

    assert antibody.state == "not_met"
    assert result.points == 0


def test_clinical_items_are_not_assessed_rather_than_not_met() -> None:
    """No lab row can answer "fever" or "joint involvement". Scoring them as
    `not_met` would report a confident low total that is really an artifact of
    missing input — the failure most likely to talk a reader out of a real
    diagnosis."""
    result = score_sle_2019([_row("ANA", value_text="1:320")])
    arthritis = next(i for i in result.items if i.name == "Joint involvement")

    assert arthritis.state == "not_assessed"
    assert result.points_not_assessed > 0
    assert not result.meets_threshold


def test_only_the_highest_weighted_met_item_in_a_domain_counts() -> None:
    """These sets are additive ACROSS domains but take a single maximum
    WITHIN one. Both complement items met must score 4, never 3 + 4."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("C3", value=40.0, flag=LabFlag.LOW),
            _row("C4", value=6.0, flag=LabFlag.LOW),
        ]
    )
    both = next(i for i in result.items if i.name == "Low C3 and low C4")
    either = next(i for i in result.items if i.name == "Low C3 or low C4")

    assert both.state == "met"
    # Marking "either" met too would double-count one biological finding in
    # the itemised display, even though the domain maximum hides it in the total.
    assert either.state == "not_met"
    assert result.points == 4


def test_one_low_complement_scores_the_three_point_item() -> None:
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("C3", value=40.0, flag=LabFlag.LOW),
            _row("C4", value=30.0),
        ]
    )

    assert next(i for i in result.items if i.name == "Low C3 or low C4").state == "met"
    assert next(i for i in result.items if i.name == "Low C3 and low C4").state == "not_met"
    assert result.points == 3


def test_a_lab_only_picture_can_satisfy_the_clinical_item_requirement() -> None:
    """The 2019 set requires at least one CLINICAL item — and the
    haematologic domain, whose leukopenia and thrombocytopenia items ARE
    lab-computable, is one of its clinical domains.

    So a laboratory-only picture can legitimately classify, and the scorer
    must say so rather than withholding on the grounds that it cannot see
    symptoms. The honest limitation is the reverse one: a patient whose
    points sit in the unseen clinical domains is UNDER-counted, which is what
    `points_not_assessed` exists to state."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:640"),
            _row("anti-dsDNA", value_text="Positive"),
            _row("C3", value=40.0, flag=LabFlag.LOW),
            _row("C4", value=6.0, flag=LabFlag.LOW),
            _row("WBC", value=2.9),
            _row("Platelets", value=88.0),
            _row("Anticardiolipin IgG", value_text="Positive"),
        ]
    )

    assert result.points >= 10
    assert result.clinical_item_met  # via the haematologic domain
    assert result.meets_threshold
    # ...and it still reports how much it could not see.
    assert result.points_not_assessed > 0


def test_haematologic_domain_takes_its_maximum_and_counts_as_clinical() -> None:
    """Leukopenia (3) and thrombocytopenia (4) share the Haematologic domain,
    which the published set counts as clinical — so a patient with both scores
    4 from it, not 7, but does satisfy the clinical-item requirement."""
    result = score_sle_2019(
        [_row("ANA", value_text="1:320"), _row("WBC", value=2.9), _row("Platelets", value=88.0)]
    )

    assert result.points == 4
    assert result.clinical_item_met


def test_every_met_item_carries_a_checkable_source_ref() -> None:
    """A criteria result must be checkable by the same citation machinery as
    any model claim (ADR 0028)."""
    result = score_sle_2019([_row("ANA", value_text="1:320"), _row("WBC", value=2.9)])
    met = [i for i in result.items if i.state == "met"]

    assert met
    for item in met:
        assert item.sources
        for source in item.sources:
            assert source.startswith("labs:")
            assert source.endswith(":2026-05-02")


def test_the_most_recent_value_decides() -> None:
    """A criteria set describes a patient's current classifiable state. An
    abnormality that has since resolved must not keep scoring forever."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("WBC", value=2.9, when=date(2024, 1, 1)),
            _row("WBC", value=6.2, when=date(2026, 5, 2)),
        ]
    )

    assert next(i for i in result.items if i.name.startswith("Leukopenia")).state == "not_met"


def test_every_result_carries_the_classification_disclaimer() -> None:
    """PLAN.md: always labeled "classification, not diagnostic, criteria"."""
    for result in score_all([_row("ANA", value_text="1:320")]):
        assert result.disclaimer == CLASSIFICATION_DISCLAIMER
        assert result.citation


def test_real_corpus_spellings_are_matched() -> None:
    """The stored names are not the textbook names.

    An alias list was tried first and failed against the real corpus within
    minutes: the patient's complement is stored as `Complement C4c` and her
    Smith antibody as `Smith (Sm) Antibody`, neither of which any reasonable
    hand-written alias list contains. A scorer that reports `not_assessed`
    for an analyte actually measured is worse than one that errors — it looks
    like an answer. These exact spellings are the regression.
    """
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("Complement C4c", value=6.0, flag=LabFlag.LOW),
            _row("Smith (Sm) Antibody", value_text="Positive"),
        ]
    )

    complement = next(i for i in result.items if i.name == "Low C3 or low C4")
    antibody = next(i for i in result.items if i.name == "Anti-dsDNA or anti-Smith")

    assert complement.state == "met", "Complement C4c must be recognised as C4"
    assert antibody.state == "met", "Smith (Sm) Antibody must be recognised"
    assert result.points == 3 + 6


def test_an_iga_isotype_does_not_score_the_antiphospholipid_item() -> None:
    """The 2019 criteria name IgG and IgM only. This patient has a stored
    `Cardiolipin Antibody IgA`, which must not score a point the published
    set does not award."""
    result = score_sle_2019(
        [_row("ANA", value_text="1:320"), _row("Cardiolipin Antibody IgA", value_text="Positive")]
    )
    apl = next(i for i in result.items if i.domain == "Antiphospholipid antibodies")

    assert apl.state == "not_assessed"
    assert result.points == 0


def test_a_phenotype_match_is_possible_never_met() -> None:
    """The near-miss this state exists for.

    Two terms in this patient's real profile would have scored as met SLE
    criteria: Seizure, from "clonic grand mal seizure while taking
    wellbutrin", and Arthritis, from what reads as a list of conditions being
    considered. Together they are 11 points against a threshold of 10.

    The 2019 criteria count an item only if there is no more likely
    explanation, and a bupropion-induced seizure has one. A text matcher
    cannot make that judgement, so it must not claim the criterion.
    """
    phenotype = {
        "HP:0001250": ("Seizure", True, "clonic grand mal seizure while taking wellbutrin"),
        "HP:0001369": ("Arthritis", True, "psoriatic arthritis"),
    }
    result = score_sle_2019([_row("ANA", value_text="1:320")], phenotype)

    seizure = next(i for i in result.items if i.name == "Seizure")
    assert seizure.state == "possible"
    assert "wellbutrin" in seizure.basis
    assert "more likely explanation" in seizure.basis

    assert result.points == 0
    assert result.points_possible == 11
    # ...and 11 possible points cannot carry her over a threshold of 10.
    assert not result.meets_threshold


def test_a_phenotype_term_recorded_as_excluded_reads_as_not_met() -> None:
    """An explicitly denied finding is an answer, not an absence."""
    phenotype = {"HP:0001945": ("Fever", False, "denies fever")}
    result = score_sle_2019([_row("ANA", value_text="1:320")], phenotype)

    fever = next(i for i in result.items if i.name == "Fever")
    assert fever.state == "not_met"
    assert result.points_possible == 0


def test_without_a_phenotype_clinical_items_stay_unassessed() -> None:
    """No phenotype record means unanswered, never absent."""
    result = score_sle_2019([_row("ANA", value_text="1:320")])

    assert all(
        i.state == "not_assessed"
        for i in result.items
        if i.name in {"Fever", "Seizure", "Joint involvement"}
    )

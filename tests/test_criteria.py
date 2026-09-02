"""Tests for adoc.knowledge.criteria — deterministic classification scorers.

No LLM anywhere in this module or its tests (CLAUDE.md: deterministic logic
is never delegated to a model).
"""

from __future__ import annotations

import json
import re
from datetime import date

from adoc.casefile.regimen import Regimen, RegimenEntry
from adoc.knowledge.criteria import (
    CLASSIFICATION_DISCLAIMER,
    SCORERS,
    score_all,
    score_egpa_2022,
    score_gpa_2022,
    score_ra_2010,
    score_sjogren_2016,
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
    unit: str | None = None,
    when: date = date(2026, 5, 2),
) -> LabResult:
    return LabResult(
        date=when,
        name=name,
        name_raw=name,
        value=value,
        value_text=value_text,
        ucum_unit=unit,
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


def test_a_resolved_abnormality_still_counts_and_says_it_resolved() -> None:
    """This test replaces `test_the_most_recent_value_decides`, which pinned
    the opposite property. ADR 0042 changed it deliberately.

    The old reasoning — "a criteria set describes a patient's current
    classifiable state" — is wrong for a *classification* set. The 2019
    EULAR/ACR criteria state that criteria need not occur simultaneously, and
    `score_sle_2019`'s own docstring said the entry criterion was "ANA ≥1:80
    ever" while the code read the latest ANA. Measured, the old behaviour took
    a suppressed-lupus timeline from 6/10 with the criteria applying to 0/10
    and "the criteria do not apply", purely by ADDING a later normal result.

    What must not happen is the resolution being hidden. Both facts render.
    """
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("WBC", value=2.9, when=date(2024, 1, 1)),
            _row("WBC", value=6.2, when=date(2026, 5, 2)),
        ]
    )
    item = next(i for i in result.items if i.name.startswith("Leukopenia"))

    assert item.state == "met"
    assert item.met_ever is True
    assert "2024-01-01" in item.superseded
    assert "6.2" in item.superseded
    # Both draws are cited, so the reader can check either.
    assert any("2024-01-01" in ref for ref in item.sources)
    assert any("2026-05-02" in ref for ref in item.sources)


def test_a_criterion_met_by_the_latest_draw_is_not_flagged_as_historical() -> None:
    """The other half: `met_ever` must distinguish, or it means nothing."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("WBC", value=6.2, when=date(2024, 1, 1)),
            _row("WBC", value=2.9, when=date(2026, 5, 2)),
        ]
    )
    item = next(i for i in result.items if i.name.startswith("Leukopenia"))

    assert item.state == "met"
    assert item.met_ever is False
    assert item.superseded == ""


def test_a_criterion_never_met_stays_not_met() -> None:
    """`ever` must not become "met if measured". The floor is still a floor."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("WBC", value=6.2, when=date(2024, 1, 1)),
            _row("WBC", value=7.1, when=date(2026, 5, 2)),
        ]
    )
    item = next(i for i in result.items if i.name.startswith("Leukopenia"))

    assert item.state == "not_met"
    assert "2 draws on file, none meeting it" in item.basis


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


# --- the other three sets ---------------------------------------------------


def test_sjogren_does_not_score_anti_ssb() -> None:
    """Anti-SSB/La scored in the older AECG criteria and is NOT an item in the
    2016 set. Adding it back because the lab reports it would inflate every
    score by a point against a threshold of four."""
    result = score_sjogren_2016(
        [
            _row("SS-B (La) Antibody", value_text="Positive"),
            _row("SS-A (Ro) Antibody", value_text="Negative"),
        ]
    )

    # Matched on whole words: "La" is a substring of "Labial gland", which is
    # a legitimate item — the same substring trap this module keeps hitting.
    assert not any(re.search(r"\bSS-?B\b|\bLa\b", i.name) for i in result.items)
    # ...and the SSA item is scored on the SSA row alone.
    ssa = next(i for i in result.items if "SSA" in i.name)
    assert ssa.state == "not_met"
    assert ssa.sources == ["labs:ss-a-ro-antibody:2026-05-02"]
    assert result.points == 0


def test_sjogren_entry_needs_recorded_dryness() -> None:
    result = score_sjogren_2016([_row("SS-A (Ro) Antibody", value_text="Positive")])
    assert result.entry_met is None

    with_dryness = score_sjogren_2016(
        [_row("SS-A (Ro) Antibody", value_text="Positive")],
        {"HP:0000217": ("Xerostomia", True, "dry mouth for years")},
    )
    assert with_dryness.entry_met is True
    assert with_dryness.points == 3


def test_ra_cannot_reach_its_threshold_from_stored_data() -> None:
    """Joint involvement is 5 of the 10 points and needs a counted joint
    examination; duration needs a history. Serology and acute-phase reactants
    cap at 4 against a threshold of 6.

    That is the scorer's most useful output, not a defect: it says exactly
    what a clinician must supply for the question to be answerable.
    """
    result = score_ra_2010(
        [
            _row("anti-CCP", value_text="Positive"),
            _row("CRP", value=12.0, flag=LabFlag.HIGH),
        ]
    )

    assert result.points <= 4
    assert not result.meets_threshold
    joints = next(i for i in result.items if i.name == "Joint involvement")
    assert joints.state == "not_assessed"
    assert "counted joint examination" in joints.basis


def test_ra_serology_scores_low_positive_not_high() -> None:
    """High-positive means >3x the upper limit of normal. A stored high/low
    flag cannot establish a multiple, so a flagged positive scores the LOW
    band — understating rather than overstating."""
    result = score_ra_2010([_row("anti-CCP", value_text="Positive")])
    serology = next(i for i in result.items if i.domain == "Serology")

    assert serology.state == "met"
    assert serology.weight == 2
    assert "LOW-positive" in serology.basis


def test_rheumatoid_factor_by_full_name_is_matched() -> None:
    """`_RA_RF` used to be `r"rheumatoid factor"` — a literal space, which
    never matches a normalized key (`_normalize_slug` strips ALL
    non-alphanumerics including spaces). "Rheumatoid Factor" normalizes to
    "rheumatoidfactor"; the regex could never match it. This criterion has
    been unsatisfiable from lab data since RA 2010 shipped, with zero test
    coverage catching it."""
    result = score_ra_2010([_row("Rheumatoid Factor", value_text="Positive")])
    serology = next(i for i in result.items if i.domain == "Serology")

    assert serology.state == "met"


def test_rheumatoid_factor_by_bare_abbreviation_is_matched() -> None:
    """Real lab panels report the bare "RF" as often as the full name."""
    result = score_ra_2010([_row("RF", value_text="Positive")])
    serology = next(i for i in result.items if i.domain == "Serology")

    assert serology.state == "met"


def test_a_unit_bearing_threshold_ignores_an_incomparable_unit() -> None:
    """The bug this helper exists for.

    Eosinophils are stored both as `4.5 %` and `320 cells/uL`. The criteria
    threshold is 1x10^9/L — 1000 cells/uL. A bare `value >= 1.0` matched BOTH
    and scored a -4 penalty for a real count of 0.32x10^9/L. A percentage is
    not a concentration.
    """
    result = score_gpa_2022(
        [
            _row("Eosinophils", value=4.5, unit="%"),
            _row("Eosinophils, Absolute", value=320.0, unit="cells/uL"),
        ]
    )
    eos = next(i for i in result.items if "Eosinophil" in i.name)

    assert eos.state == "not_met"
    assert "cells/ul" in eos.basis.lower()
    assert result.points == 0


def test_a_genuinely_high_eosinophil_count_does_score_the_penalty() -> None:
    """The penalty must still fire when the count really is above threshold —
    the fix is unit awareness, not suppression."""
    result = score_gpa_2022([_row("Eosinophils, Absolute", value=1500.0, unit="cells/uL")])
    eos = next(i for i in result.items if "Eosinophil" in i.name)

    assert eos.state == "met"
    assert result.points == -4


def test_every_registered_scorer_carries_the_disclaimer_and_a_citation() -> None:
    """However many sets there are, the label is not optional on any of them.

    Counted from the registry rather than hardcoded: the literal said 4 and
    went stale the moment the ANCA siblings and Behçet were added, which
    turned a real invariant into an arithmetic assertion about how many
    scorers exist.
    """
    results = score_all([_row("CRP", value=8.5)])

    assert len(results) == len(SCORERS)
    for result in results:
        assert result.disclaimer == CLASSIFICATION_DISCLAIMER
        assert result.citation
        assert result.threshold > 0


# --- the two ANCA siblings (EGPA / MPA 2022) ----------------------------------------------
#
# The 2022 ACR/EULAR criteria come as a set of three. Encoding only GPA left
# the other two arms of the same decision unmodelled.


def test_one_eosinophil_count_moves_the_three_vasculitis_sets_in_opposite_directions() -> None:
    """The reason for encoding all three rather than the one.

    A raised eosinophil count is the heaviest single item in EGPA (+5) and a
    heavy penalty in both GPA and MPA (−4). One CBC differential is far more
    informative across the trio than it is against any of them alone.
    """
    rows = [_row("Eosinophils Absolute", value=1800.0, unit="cells/uL")]

    by_key = {r.key: r for r in score_all(rows, keys=["gpa-2022", "egpa-2022", "mpa-2022"])}

    assert by_key["egpa-2022"].points == 5
    assert by_key["gpa-2022"].points == -4
    assert by_key["mpa-2022"].points == -4


def test_mpo_anca_alone_classifies_mpa_and_penalises_gpa() -> None:
    """Faithful to the published set: in an established small-vessel
    vasculitis that one serology is close to decisive between the three."""
    rows = [_row("Myeloperoxidase Ab", value_text="Positive", flag="H")]

    by_key = {r.key: r for r in score_all(rows, keys=["gpa-2022", "egpa-2022", "mpa-2022"])}

    assert by_key["mpa-2022"].points == 6
    assert by_key["mpa-2022"].meets_threshold
    assert by_key["gpa-2022"].points == -1
    assert not by_key["gpa-2022"].meets_threshold


def test_pr3_penalises_both_siblings() -> None:
    rows = [_row("Proteinase 3 Ab", value_text="Positive", flag="H")]

    by_key = {r.key: r for r in score_all(rows, keys=["egpa-2022", "mpa-2022"])}

    assert by_key["egpa-2022"].points == -3
    assert by_key["mpa-2022"].points == -1


def test_a_negative_item_only_subtracts_when_it_is_actually_met() -> None:
    """Negatives follow the same rule as positives.

    A negative clinical item read from the text-matched phenotype reaches
    `possible` at most, and `possible` must not move a total in EITHER
    direction — the attribution rule exists precisely because matched text
    cannot know whether a finding belongs to this condition.
    """
    phenotype = {"HP:0001742": ("Nasal congestion", True, "stuffy nose most mornings")}

    result = score_all([], keys=["mpa-2022"], phenotype=phenotype)[0]

    nasal = next(i for i in result.items if i.domain == "ENT-negative")
    assert nasal.state == "possible"
    assert result.points == 0, "an unattributed finding talked the score down"


# --- Behçet ICBD 2014 ---------------------------------------------------------------------


def test_behcet_reads_no_labs_at_all() -> None:
    """The first set here with no laboratory item.

    There is no serological marker for Behçet, so a condition diagnosed
    clinically would otherwise be invisible to this layer no matter how well
    the record described it.
    """
    result = score_all([_row("Anti-dsDNA", value=300.0)], keys=["behcet-icbd-2014"])[0]

    assert not result.assessable
    assert all(item.state == "not_assessed" for item in result.items)


def test_behcet_reports_what_attribution_would_add_without_claiming_it() -> None:
    """Three findings worth 6 points against a threshold of 4 — and the score
    stays 0, because a text-matched phenotype cannot attribute them.

    This is the honest output for a set with no confirmatory test: it says
    the record contains matching findings and leaves attribution where it
    belongs.
    """
    phenotype = {
        "HP:0000155": ("Oral ulcer", True, "recurrent mouth ulcers"),
        "HP:0003249": ("Genital ulcers", True, "genital ulceration"),
        "HP:0000554": ("Uveitis", True, "anterior uveitis"),
    }

    result = score_all([], keys=["behcet-icbd-2014"], phenotype=phenotype)[0]

    assert result.points == 0
    assert result.points_possible == 6
    assert result.threshold == 4
    assert not result.meets_threshold, "possible items must never cross a threshold"


def test_every_registered_scorer_survives_an_empty_record() -> None:
    """A new scorer that raises on a patient with no matching data would take
    the whole `criteria_scan` node down with it."""
    results = score_all([], phenotype=None)

    assert len(results) == len(SCORERS)
    assert all(r.citation for r in results), "every set must be traceable to its publication"


# --- ADR 0042: criteria read the whole record ---------------------------------------------------


def _suppressed_lupus_timeline() -> list[LabResult]:
    """Seropositive and complement-consumed in 2024; all normal in 2026 —
    the trajectory CLN-03 describes for a treated patient."""
    return [
        _row("ANA", value_text="1:640", when=date(2024, 3, 1)),
        _row("anti-dsDNA", value_text="Positive", when=date(2024, 3, 1)),
        _row("Complement C3", value=48.0, flag=LabFlag.CRITICAL_LOW, when=date(2024, 3, 1)),
        _row("Complement C4", value=6.0, flag=LabFlag.LOW, when=date(2024, 3, 1)),
        _row("WBC", value=2.9, when=date(2024, 3, 1)),
        _row("ANA", value_text="Negative", when=date(2026, 7, 1)),
        _row("anti-dsDNA", value_text="Negative", when=date(2026, 7, 1)),
        _row("Complement C3", value=110.0, when=date(2026, 7, 1)),
        _row("Complement C4", value=22.0, when=date(2026, 7, 1)),
        _row("WBC", value=6.4, when=date(2026, 7, 1)),
    ]


def test_a_later_normal_result_does_not_erase_the_historical_basis() -> None:
    """The measurement that motivated ADR 0042. On these exact records the
    old latest-only reading gave `entry_met=False, 0/10, "the criteria do not
    apply"` — a patient scored as if the disease had been excluded, because
    treatment worked."""
    result = score_sle_2019(_suppressed_lupus_timeline())

    assert result.entry_met is True
    assert result.points == 13
    assert result.meets_threshold is True


def test_the_entry_criterion_is_met_ever_as_its_docstring_always_said() -> None:
    """`score_sle_2019`'s docstring said "ANA ≥1:80 ever" from the day it was
    written; the code read the latest ANA."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:640", when=date(2024, 3, 1)),
            _row("ANA", value_text="Negative", when=date(2026, 7, 1)),
        ]
    )

    assert result.entry_met is True
    assert "met EVER" in result.entry_note
    assert "2024-03-01" in result.entry_note
    assert "Negative" in result.entry_note


def test_an_ana_never_reaching_the_titre_still_fails_the_entry_criterion() -> None:
    """`ever` must not become "positive if measured"."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:40", when=date(2024, 3, 1)),
            _row("ANA", value_text="Negative", when=date(2026, 7, 1)),
        ]
    )

    assert result.entry_met is False
    assert "2 draws" in result.entry_note


def test_the_regimen_names_what_could_have_suppressed_the_marker() -> None:
    """CLN-03's second half. The note says which drug and leaves the
    inference to the reader — this module never claims causation."""
    regimen = Regimen(
        entries=[RegimenEntry(name="Prednisone", dose="10 mg", started=date(2024, 6, 1))]
    )
    result = score_sle_2019(_suppressed_lupus_timeline(), regimen=regimen)
    historical = [i for i in result.items if i.met_ever]

    assert historical
    assert all("Prednisone" in i.superseded for i in historical)
    assert all("can suppress" in i.superseded for i in historical)


def test_a_drug_stopped_before_the_draw_is_not_named() -> None:
    """`Regimen.active_on` is interval-aware, and a restart is a separate
    entry. A drug she was off when the blood was taken explains nothing."""
    regimen = Regimen(
        entries=[
            RegimenEntry(name="Prednisone", started=date(2024, 1, 1), stopped=date(2024, 12, 31))
        ]
    )
    result = score_sle_2019(_suppressed_lupus_timeline(), regimen=regimen)

    assert all("Prednisone" not in i.superseded for i in result.items)


def test_an_undated_regimen_entry_is_never_assumed_active() -> None:
    """`Regimen.active_on` reports `unknown` rather than guessing, and a
    confident wrong answer here would put a drug name against a lab it had
    nothing to do with."""
    regimen = Regimen(entries=[RegimenEntry(name="Prednisone")])
    result = score_sle_2019(_suppressed_lupus_timeline(), regimen=regimen)

    assert all("Prednisone" not in i.superseded for i in result.items)


def test_no_regimen_on_file_costs_a_sentence_not_a_point() -> None:
    """The optional dependency degrades, never changes the score."""
    with_none = score_sle_2019(_suppressed_lupus_timeline())
    with_some = score_sle_2019(
        _suppressed_lupus_timeline(),
        regimen=Regimen(entries=[RegimenEntry(name="Prednisone", started=date(2024, 6, 1))]),
    )

    assert with_none.points == with_some.points
    assert with_none.meets_threshold == with_some.meets_threshold
    assert "Prednisone" not in " ".join(i.superseded for i in with_none.items)


def test_a_peak_count_meets_a_count_threshold_even_if_todays_is_normal() -> None:
    """The published EGPA criterion is a blood eosinophil count ≥1×10⁹/L,
    which in practice means the highest recorded one. A patient on steroids
    has a normal count today and had 4.2 before treatment."""
    rows = [
        _row("Eosinophils", value=4.2, unit="10*9/L", when=date(2024, 3, 1)),
        _row("Eosinophils", value=0.2, unit="10*9/L", when=date(2026, 7, 1)),
    ]
    result = score_egpa_2022(rows)
    item = next(i for i in result.items if i.name.startswith("Eosinophil count"))

    assert item.state == "met"
    assert item.met_ever is True
    assert "Peak" in item.basis
    assert "4.2" in item.basis


# --- the flag-enum bug this uncovered -----------------------------------------------------------


def test_a_critically_low_flag_counts_as_low() -> None:
    """`LabFlag` has five members and the string set at this call site
    covered `L` but not `LL`: a critically low complement — the most
    clinically significant value the analyte can carry — registered as
    normal everywhere in the criteria scorers."""
    result = score_sle_2019(
        [
            _row("ANA", value_text="1:320"),
            _row("Complement C3", value=20.0, flag=LabFlag.CRITICAL_LOW),
            _row("Complement C4", value=4.0, flag=LabFlag.CRITICAL_LOW),
        ]
    )
    item = next(i for i in result.items if i.name == "Low C3 and low C4")

    assert item.state == "met"


def test_every_flag_member_has_a_defined_direction() -> None:
    """The bug above existed because three of five members matched nothing.
    `A` is deliberately neither: it records that a value is out of range
    without saying which way, and guessing would invent a finding."""
    from adoc.labs.models import flag_is_high, flag_is_low

    assert (flag_is_low(LabFlag.LOW), flag_is_high(LabFlag.LOW)) == (True, False)
    assert (flag_is_low(LabFlag.CRITICAL_LOW), flag_is_high(LabFlag.CRITICAL_LOW)) == (True, False)
    assert (flag_is_low(LabFlag.HIGH), flag_is_high(LabFlag.HIGH)) == (False, True)
    assert (flag_is_low(LabFlag.CRITICAL_HIGH), flag_is_high(LabFlag.CRITICAL_HIGH)) == (
        False,
        True,
    )
    assert (flag_is_low(LabFlag.ABNORMAL), flag_is_high(LabFlag.ABNORMAL)) == (False, False)
    assert (flag_is_low(None), flag_is_high(None)) == (False, False)

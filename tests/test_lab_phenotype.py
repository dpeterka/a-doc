"""Tests for `knowledge.lab_phenotype` — labs as HPO terms (ADR 0044).

The HPO fixture is a 28-term subset of the published index, copied verbatim
so every id and label is genuine. A synthetic ontology would let a rule
"resolve" against a label this repository invented, which is the one failure
this module is built to prevent.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from adoc.knowledge.hpo import HpoIndex
from adoc.knowledge.lab_phenotype import (
    KNOWN_VOCABULARY_GAPS,
    RULES,
    LabPhenotypeRule,
    derive_lab_phenotype,
)
from adoc.labs.models import LabFlag, LabResult

FIXTURE = Path(__file__).parent / "fixtures" / "hpo" / "hpo-index-subset.json"


def _index() -> HpoIndex:
    index = HpoIndex.load(FIXTURE)
    assert index is not None
    return index


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
        source_doc="a" * 64,
        raw_json=json.dumps({"name_raw": name}),
    )


def test_serology_reaches_the_engine_as_real_hpo_terms() -> None:
    """The finding CLN-02 names: the engines took HPO ids and nothing else,
    so *arthralgia* and *fatigue* reached them and an ANA of 1:640 did not.
    Measured consequence — 66 of 66 neutral verdicts, after LIRICAL ran for
    76.9 seconds."""
    result = derive_lab_phenotype(
        [
            _row("Antinuclear Antibody", value_text="1:640"),
            _row("anti-dsDNA", value_text="Positive"),
            _row("Complement C3", value=48.0, flag=LabFlag.CRITICAL_LOW),
        ],
        index=_index(),
    )

    assert result.term_ids == ["HP:0003493", "HP:0020151", "HP:0005421"]
    assert result.unresolved == []


def test_every_derived_term_carries_a_checkable_citation() -> None:
    """A derived term is a claim about the record, so it is checkable by the
    same machinery as any other (ADR 0028)."""
    result = derive_lab_phenotype(
        [_row("Complement C4", value=6.0, flag=LabFlag.LOW, when=date(2024, 3, 1))],
        index=_index(),
    )

    (term,) = result.terms
    assert term.source == "labs:complement-c4:2024-03-01"
    assert "2024-03-01" in term.basis
    assert "6" in term.basis


def test_nothing_is_derived_from_a_normal_result() -> None:
    """A negative ANA must not derive an "absent" term. LIRICAL treats
    negated phenotypes as evidence AGAINST a disease, so deriving one from a
    single normal draw is a far stronger claim than deriving a positive from
    a single abnormal one — and ADR 0042 established that a normal draw is
    frequently an expected treatment effect."""
    result = derive_lab_phenotype(
        [
            _row("Antinuclear Antibody", value_text="Negative"),
            _row("Complement C3", value=110.0),
            _row("CRP", value=1.1),
            _row("anti-dsDNA", value_text="Not Detected"),
        ],
        index=_index(),
    )

    assert result.terms == []
    assert result.rows_considered == 4


def test_a_negated_qualitative_result_is_read_as_negative() -> None:
    """ "not detected" contains "detected" — the substring order is the whole
    correctness of this, the same way it is in `knowledge.criteria`."""
    for text in ("Not Detected", "Negative", "Non-reactive", "None seen", "Absent"):
        result = derive_lab_phenotype([_row("anti-dsDNA", value_text=text)], index=_index())
        assert result.terms == [], f"{text!r} derived a term"


def test_a_titre_below_the_threshold_derives_nothing() -> None:
    """A higher reciprocal is a STRONGER result — the comparison people get
    backwards. 1:40 is below the 1:80 cutoff the SLE entry criterion uses."""
    below = derive_lab_phenotype([_row("ANA", value_text="1:40")], index=_index())
    at = derive_lab_phenotype([_row("ANA", value_text="1:80")], index=_index())
    above = derive_lab_phenotype([_row("ANA", value_text="1:1280")], index=_index())

    assert below.terms == []
    assert at.term_ids == ["HP:0003493"]
    assert above.term_ids == ["HP:0003493"]


def test_a_critically_low_flag_derives_its_term() -> None:
    """ADR 0042's flag fix reaches here too: `LL` and `HH` must count."""
    low = derive_lab_phenotype([_row("WBC", value=1.1, flag=LabFlag.CRITICAL_LOW)], index=_index())
    high = derive_lab_phenotype(
        [_row("CRP", value=140.0, flag=LabFlag.CRITICAL_HIGH)], index=_index()
    )

    assert low.term_ids == ["HP:0001882"]
    assert high.term_ids == ["HP:0011227"]


def test_an_abnormal_flag_with_no_direction_derives_nothing() -> None:
    """`A` records that a value is out of range without saying which way.
    Deriving "decreased complement" from it would invent a finding.

    Both directions are checked: an earlier version of this test covered
    only the `low` rule, so making `A` count as high broke nothing."""
    low_rule = derive_lab_phenotype(
        [_row("Complement C3", value=48.0, flag=LabFlag.ABNORMAL)], index=_index()
    )
    high_rule = derive_lab_phenotype(
        [_row("CRP", value=8.5, flag=LabFlag.ABNORMAL)], index=_index()
    )
    positive_rule = derive_lab_phenotype(
        [_row("anti-dsDNA", value=1.2, flag=LabFlag.ABNORMAL)], index=_index()
    )

    assert low_rule.terms == []
    assert high_rule.terms == []
    assert positive_rule.terms == []


def test_a_historical_positive_still_derives_its_term() -> None:
    """`ever` semantics, consistent with ADR 0042. Being inconsistent
    between the criteria scorers and the engines would be worse than either
    choice."""
    result = derive_lab_phenotype(
        [
            _row("anti-dsDNA", value_text="Positive", when=date(2024, 3, 1)),
            _row("anti-dsDNA", value_text="Negative", when=date(2026, 7, 1)),
        ],
        index=_index(),
    )

    assert result.term_ids == ["HP:0020151"]
    assert "2024-03-01" in result.terms[0].source


def test_a_label_the_ontology_lacks_is_reported_not_substituted() -> None:
    """The mechanism that keeps this honest. A hardcoded id typed wrong is
    silently wrong forever; an unresolvable label lands in `unresolved` and
    renders in the report."""
    invented = LabPhenotypeRule(
        patterns=(r"^madeupanalyte$",),
        condition="high",
        hpo_label="Elevated circulating unobtainium concentration",
    )
    rules = (*RULES, invented)

    import adoc.knowledge.lab_phenotype as module

    original = module.RULES
    module.RULES = rules  # type: ignore[misc]
    try:
        result = derive_lab_phenotype(
            [_row("Made Up Analyte", value=9.0, flag=LabFlag.HIGH)], index=_index()
        )
    finally:
        module.RULES = original  # type: ignore[misc]

    assert result.terms == []
    assert result.unresolved == ["Elevated circulating unobtainium concentration"]


def test_every_shipped_rule_resolves_against_the_real_ontology() -> None:
    """The rules name labels, and a label that does not exist contributes
    nothing. This is what turns a typo into a test failure instead of a
    silently missing term — the failure mode this whole repository keeps
    hitting."""
    index = _index()
    rows = [
        # One row satisfying every rule, so each is forced to resolve.
        _row("Antinuclear Antibody", value_text="1:640"),
        _row("anti-dsDNA", value_text="Positive"),
        _row("SSA (Ro) Antibody", value_text="Positive"),
        _row("Ro52 Antibody", value_text="Positive"),
        _row("Rheumatoid Factor", value=42.0, flag=LabFlag.HIGH),
        _row("Anti-CCP", value_text="Positive"),
        _row("ANCA Screen", value_text="Positive"),
        _row("Anticardiolipin IgG", value_text="Positive"),
        _row("Anticardiolipin IgM", value_text="Positive"),
        _row("Lupus Anticoagulant", value_text="Positive"),
        _row("Beta-2 Glycoprotein I IgG", value_text="Positive"),
        _row("Complement C3", value=48.0, flag=LabFlag.LOW),
        _row("Complement C4", value=6.0, flag=LabFlag.LOW),
        _row("WBC", value=2.9, flag=LabFlag.LOW),
        _row("Platelets", value=88.0, flag=LabFlag.LOW),
        _row("Lymphocytes", value=0.7, flag=LabFlag.LOW),
        _row("Neutrophils", value=1.2, flag=LabFlag.LOW),
        _row("Eosinophils", value=4.2, flag=LabFlag.HIGH),
        _row("Hemoglobin", value=9.1, flag=LabFlag.LOW),
        _row("CRP", value=8.5, flag=LabFlag.HIGH),
        _row("ESR", value=48.0, flag=LabFlag.HIGH),
        _row("Creatine Kinase", value=420.0, flag=LabFlag.HIGH),
        _row("TSH", value=6.9, flag=LabFlag.HIGH),
        _row("Ferritin", value=410.0, flag=LabFlag.HIGH),
        _row("Vitamin B12", value=180.0, flag=LabFlag.LOW),
        _row("aPTT", value=48.0, flag=LabFlag.HIGH),
    ]

    result = derive_lab_phenotype(rows, index=index, limit=len(RULES))

    assert result.unresolved == [], f"rules naming labels HPO does not have: {result.unresolved}"
    # Every rule that had a satisfying row produced a term.
    assert len(result.terms) >= 20
    assert all(index.is_valid(t.term_id) for t in result.terms)


def test_the_anti_smith_gap_is_recorded_rather_than_approximated() -> None:
    """HPO has no anti-Smith term — confirmed by searching every label in
    the published index. `knowledge.criteria`'s SLE item is "Anti-dsDNA or
    anti-Sm", so only the dsDNA half can reach an engine. Substituting a
    neighbouring antibody term would be inventing a finding."""
    assert any("Smith" in gap for gap in KNOWN_VOCABULARY_GAPS)
    assert not any("smith" in rule.hpo_label.lower() for rule in RULES)


def test_a_missing_index_degrades_visibly() -> None:
    """The recurring failure mode: absence must not look like working. The
    index is a build artifact and can be absent."""
    result = derive_lab_phenotype([_row("ANA", value_text="1:640")], index=None)

    assert result.terms == []
    assert result.index_available is False
    assert result.rows_considered == 1


def test_the_derived_query_is_capped_and_says_what_it_dropped() -> None:
    """`select_for_engine` caps the human profile for a stated reason — an
    unbounded query produced an unusable ranking."""
    rows = [
        _row("Complement C3", value=48.0, flag=LabFlag.LOW),
        _row("Complement C4", value=6.0, flag=LabFlag.LOW),
        _row("WBC", value=2.9, flag=LabFlag.LOW),
        _row("CRP", value=8.5, flag=LabFlag.HIGH),
    ]

    result = derive_lab_phenotype(rows, index=_index(), limit=2)

    assert len(result.terms) == 2
    assert result.dropped_over_limit == 2


def test_a_label_containing_a_standalone_digit_resolves() -> None:
    """The measured reason `term_id_for` exists rather than `find_terms`:
    the phrase matcher's word token must begin with a letter, so
    `Anti-beta-2-Glycoprotein I IgG antibody positivity` tokenises without
    its `2` and matches nothing. This pins the normalisation against a label
    that actually exercises it, so `hpo.term_id_for` and
    `scripts/build_hpo_index.py::normalize` cannot drift apart silently."""
    index = _index()
    label = "Anti-beta-2-Glycoprotein I IgG antibody positivity"

    assert index.find_terms(label) == []
    assert index.term_id_for(label) == "HP:0034156"

    result = derive_lab_phenotype(
        [_row("Beta-2 Glycoprotein I IgG", value_text="Positive")], index=index
    )
    assert result.term_ids == ["HP:0034156"]


def test_resolution_is_exact_and_never_falls_back_to_a_neighbour() -> None:
    """ "Decreased circulating complement C3" is not the published label, and
    substituting the C4 term — or any nearby antibody term — would invent a
    finding about a different analyte."""
    index = _index()

    assert index.term_id_for("Decreased circulating complement C3") is None
    assert index.term_id_for("Decreased circulating complement C3 concentration") == "HP:0005421"

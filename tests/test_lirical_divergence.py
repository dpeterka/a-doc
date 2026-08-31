"""LIRICAL against the ledger.

The engine is deliberately not folded into a combined score — its composite
LR, criteria points and the panel's buckets are different measurements, and
averaging them is the unit-blindness that has already produced three wrong
clinical conclusions here. What it is good for is disagreement, so these tests
pin the three outcomes and the honesty of each.
"""

from __future__ import annotations

from datetime import date

from adoc.casefile.schema import Hypothesis, Ledger
from adoc.knowledge.lirical import LiricalDisease, LiricalResult
from adoc.knowledge.lirical_divergence import (
    compare_to_ledger,
    normalise_disease_name,
    render_comparison,
)


def _disease(name: str, *, rank: int, lr: float, curie: str = "OMIM:123") -> LiricalDisease:
    return LiricalDisease(
        rank=rank,
        name=name,
        curie=curie,
        pretest_probability="1/8621",
        posttest_probability=12.5,
        composite_lr=lr,
    )


def _hypothesis(name: str, *, hid: str, probability: str = "moderate") -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=name,
        tier="expanded",
        probability=probability,  # type: ignore[arg-type]
        status="active",
        origin="model",
        first_proposed=date(2026, 8, 1),
    )


def _ledger(*hypotheses: Hypothesis) -> Ledger:
    from datetime import UTC, datetime

    return Ledger(version=1, updated=datetime(2026, 8, 29, tzinfo=UTC), hypotheses=list(hypotheses))


# -- name normalisation -----------------------------------------------------


def test_names_that_mean_the_same_thing_collapse() -> None:
    """LIRICAL and the ledger name diseases differently — punctuation,
    possessives, accents. Without this every agreement would read as two
    divergences."""
    assert normalise_disease_name("Sjogren syndrome") == normalise_disease_name(
        "Sjögren's syndrome"
    )


def test_different_diseases_do_not_collapse() -> None:
    assert normalise_disease_name("Systemic lupus erythematosus") != normalise_disease_name(
        "Rheumatoid arthritis"
    )


def test_clinically_distinct_qualifiers_are_never_dropped() -> None:
    """The error here is asymmetric: a false divergence is visible and a
    reviewer dismisses it, while a false agreement silently merges two
    different diseases. Three words came out of the stopword list for exactly
    this reason."""
    # "a" would collapse Hepatitis A into Hepatitis.
    assert normalise_disease_name("Hepatitis A") != normalise_disease_name("Hepatitis")
    assert normalise_disease_name("Hepatitis A") != normalise_disease_name("Hepatitis B")
    # primary and secondary adrenal insufficiency are not the same disease.
    assert normalise_disease_name("Primary adrenal insufficiency") != normalise_disease_name(
        "Secondary adrenal insufficiency"
    )


# -- the three outcomes -----------------------------------------------------


def test_a_disease_the_engine_ranks_and_the_ledger_lacks_is_engine_only() -> None:
    """The candidate the human differential missed — the reason to run an
    independent engine at all."""
    result = LiricalResult(diseases=[_disease("Relapsing polychondritis", rank=1, lr=4.82)])

    comparison = compare_to_ledger(result, _ledger(_hypothesis("Sjogren syndrome", hid="sjogren")))

    engine_only = comparison.of_kind("engine_only")
    assert [f.disease_name for f in engine_only] == ["Relapsing polychondritis"]
    assert engine_only[0].composite_lr == 4.82


def test_a_shared_disease_is_recorded_as_agreement() -> None:
    """Agreement between independent methods is the strongest signal here.
    Reporting only disagreement would throw it away."""
    result = LiricalResult(diseases=[_disease("Sjogren syndrome", rank=1, lr=3.1)])

    comparison = compare_to_ledger(result, _ledger(_hypothesis("Sjögren's syndrome", hid="sj")))

    agreements = comparison.of_kind("agreement")
    assert len(agreements) == 1
    assert agreements[0].ledger_hypothesis_id == "sj"
    assert agreements[0].composite_lr == 3.1
    assert agreements[0].ledger_probability == "moderate"


def test_an_unsupported_ledger_hypothesis_is_flagged_but_not_refuted() -> None:
    """LIRICAL sees only phenotype. A hypothesis resting on serology or
    imaging can be correct and still score nothing, so this must never read as
    a refutation."""
    result = LiricalResult(diseases=[_disease("Relapsing polychondritis", rank=1, lr=4.0)])

    comparison = compare_to_ledger(
        result, _ledger(_hypothesis("Biotin-driven immunoassay interference", hid="biotin"))
    )

    ledger_only = comparison.of_kind("ledger_only")
    assert [f.ledger_hypothesis_id for f in ledger_only] == ["biotin"]
    assert "can be correct and still score nothing" in ledger_only[0].note


def test_a_ranked_but_unsupported_disease_is_not_a_finding() -> None:
    """A composite LR at or below zero is evidence AGAINST. Reporting the long
    tail of those would bury the real findings."""
    result = LiricalResult(
        diseases=[
            _disease("Charge syndrome", rank=1, lr=-0.42),
            _disease("Celiac disease", rank=2, lr=-2.94),
        ]
    )

    comparison = compare_to_ledger(result, _ledger())

    assert comparison.of_kind("engine_only") == []


def test_only_the_top_ranks_are_considered() -> None:
    """Past the top handful LIRICAL's own LR is at or below zero on this
    patient's profile; treating rank 30 as a finding manufactures divergences
    out of noise."""
    result = LiricalResult(
        diseases=[_disease(f"Disease {i}", rank=i, lr=5.0) for i in range(1, 21)]
    )

    comparison = compare_to_ledger(result, _ledger(), top_n=3)

    assert len(comparison.of_kind("engine_only")) == 3


def test_a_ruled_out_hypothesis_is_not_compared() -> None:
    """Only active hypotheses are the current differential."""
    retired = _hypothesis("Sjogren syndrome", hid="sj")
    retired = retired.model_copy(update={"status": "ruled-out"})

    comparison = compare_to_ledger(LiricalResult(diseases=[]), _ledger(retired))

    assert comparison.findings == []


# -- rendering --------------------------------------------------------------


def test_the_report_never_merges_the_two_scales() -> None:
    """The engine's likelihood ratio and the differential's probability are
    different measurements. The section shows both and says so."""
    result = LiricalResult(diseases=[_disease("Sjogren syndrome", rank=1, lr=3.1)])
    comparison = compare_to_ledger(
        result, _ledger(_hypothesis("Sjogren syndrome", hid="sj")), terms_used=["HP:0000001"]
    )

    text = "\n".join(render_comparison(comparison))

    assert "likelihood ratio" in text
    assert "averaging" in text
    assert "+3.10" in text
    assert "moderate" in text


def test_a_failed_run_renders_as_a_plain_note() -> None:
    """The sidecar may be unreachable and a review must complete regardless."""
    from adoc.knowledge.lirical_divergence import LiricalComparison

    text = "\n".join(render_comparison(LiricalComparison(ran=False, error="task timed out")))

    assert "did not run" in text
    assert "task timed out" in text


def _mondo_index():
    from adoc.knowledge.mondo import MondoIndex

    return MondoIndex(
        names={"MONDO:0010030": "Sjogren syndrome"},
        xrefs={"OMIM:270150": "MONDO:0010030"},
        labels={"sicca syndrome": "MONDO:0010030"},
    )


def test_mondo_matches_across_a_name_mismatch() -> None:
    """The bug Mondo exists to fix. The engine returns `OMIM:270150` named
    "Sjogren syndrome"; the ledger holds the same disease under "Sicca
    syndrome". Name comparison reports that as BOTH an `engine_only` finding
    and a `ledger_only` one — two spurious divergences for a case where the
    two sources agree completely."""
    result = LiricalResult(
        diseases=[_disease("Sjogren syndrome", rank=1, lr=3.1, curie="OMIM:270150")]
    )
    ledger = _ledger(_hypothesis("Sicca syndrome", hid="sicca"))

    without = compare_to_ledger(result, ledger)
    with_mondo = compare_to_ledger(result, ledger, mondo=_mondo_index())

    # Without the index: two findings, neither of them agreement.
    assert [f.kind for f in without.of_kind("agreement")] == []
    assert without.divergence_count == 2

    # With it: one agreement, no divergence.
    agreements = with_mondo.of_kind("agreement")
    assert len(agreements) == 1
    assert agreements[0].ledger_hypothesis_id == "sicca"
    assert agreements[0].matched_by == "mondo"
    assert with_mondo.divergence_count == 0


def test_without_an_index_matching_is_exactly_what_it_was() -> None:
    """A local checkout, or an image built before the index existed, must
    behave as it did before rather than degrading."""
    result = LiricalResult(diseases=[_disease("Sjogren syndrome", rank=1, lr=3.1)])
    ledger = _ledger(_hypothesis("Sjögren's syndrome", hid="sj"))

    comparison = compare_to_ledger(result, ledger, mondo=None)

    assert [f.matched_by for f in comparison.of_kind("agreement")] == ["name"]

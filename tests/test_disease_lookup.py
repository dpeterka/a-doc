"""Disease reference lookup for the chat.

Answers "what would I expect to see with that?" for a condition already on the
differential. Built on indexes the image already carries; no network, no
model, and it never reads the case file.
"""

from __future__ import annotations

from adoc.knowledge.disease_lookup import (
    describe_disease,
    find_diseases,
    lookup_diseases,
    render_disease_cards,
)
from adoc.knowledge.hpo import HpoIndex
from adoc.knowledge.semsim import SemSimIndex

_PARENTS = {
    "generic": ["root"],
    "specific": ["generic"],
    "rare": ["generic"],
}

_DISEASES = {
    "ORPHA:1": {"name": "Relapsing polychondritis", "terms": ["rare", "specific", "generic"]},
    "OMIM:2": {"name": "Sjogren syndrome", "terms": ["specific", "generic"]},
    "OMIM:3": {"name": "Marinesco-Sjogren syndrome", "terms": ["generic", "root"]},
    "OMIM:4": {"name": "Common condition", "terms": ["generic", "root"]},
}

_LABELS = {
    "rare": "Nasal chondritis",
    "specific": "Dry eyes",
    "generic": "Abnormality of the immune system",
    "root": "Phenotypic abnormality",
}


def _index() -> SemSimIndex:
    return SemSimIndex(parents=dict(_PARENTS), diseases=dict(_DISEASES))


def _hpo() -> HpoIndex:
    return HpoIndex(terms=dict(_LABELS), lookup={})


# -- finding ----------------------------------------------------------------


def test_an_exact_name_match_is_returned_alone() -> None:
    """A question about Sjogren syndrome came back with Marinesco-Sjogren
    syndrome — a cerebellar ataxia that merely contains the string. Near-name
    noise beside a confident answer is worse than no suggestions at all."""
    assert find_diseases(_index(), "Sjogren syndrome") == ["OMIM:2"]


def test_containment_is_only_a_fallback() -> None:
    """With no exact match, a substring is a reasonable "did you mean"."""
    found = find_diseases(_index(), "Marinesco")

    assert found == ["OMIM:3"]


def test_a_generic_word_matches_nothing() -> None:
    """ "Syndrome" matches several thousand diseases and tells a reader
    nothing."""
    index = _index()

    assert find_diseases(index, "syndrome") == []
    assert find_diseases(index, "disease") == []


def test_a_too_short_query_matches_nothing() -> None:
    assert find_diseases(_index(), "sj") == []


def test_matching_ignores_case_and_punctuation() -> None:
    assert find_diseases(_index(), "  RELAPSING, POLYCHONDRITIS  ") == ["ORPHA:1"]


# -- describing -------------------------------------------------------------


def test_features_are_ranked_most_distinctive_first() -> None:
    """A curated disease carries generic annotations alongside its
    distinguishing ones. Listing them in file order buries what actually
    characterises the condition."""
    card = describe_disease(_index(), _hpo(), "ORPHA:1")

    assert card is not None
    assert card.features[0] == "Nasal chondritis"
    assert card.features.index("Nasal chondritis") < card.features.index(
        "Abnormality of the immune system"
    )


def test_the_total_is_reported_so_a_subset_is_not_mistaken_for_all() -> None:
    card = describe_disease(_index(), _hpo(), "ORPHA:1", max_features=1)

    assert len(card.features) == 1
    assert card.feature_count == 3


def test_a_term_with_no_readable_label_is_skipped_not_shown_raw() -> None:
    """A bare `HP:0001250` tells a patient nothing."""
    sparse = HpoIndex(terms={"rare": "Nasal chondritis"}, lookup={})
    card = describe_disease(_index(), sparse, "ORPHA:1")

    assert card.features == ["Nasal chondritis"]


def test_an_unknown_disease_id_returns_nothing() -> None:
    assert describe_disease(_index(), _hpo(), "OMIM:99999") is None


# -- the whole lookup -------------------------------------------------------


def test_results_are_deduplicated_across_queries() -> None:
    cards = lookup_diseases(_index(), _hpo(), ["Sjogren syndrome", "Sjogren syndrome"])

    assert len(cards) == 1


def test_no_index_yields_nothing_rather_than_failing() -> None:
    """A local checkout has no index. That is an ordinary state."""
    assert lookup_diseases(None, _hpo(), ["Sjogren syndrome"]) == []


# -- rendering --------------------------------------------------------------


def test_the_rendering_says_this_is_not_about_the_patient() -> None:
    """A model handed a feature list next to her case file will otherwise be
    tempted to treat the match as a finding."""
    text = render_disease_cards(lookup_diseases(_index(), _hpo(), ["Relapsing polychondritis"]))

    assert "IN GENERAL" in text
    assert "never be presented as findings about her" in text
    assert "Nasal chondritis" in text
    assert "ORPHA:1" in text


def test_no_match_renders_plainly() -> None:
    assert "No matching condition" in render_disease_cards([])


def _freq_index() -> SemSimIndex:
    """A disease whose rarest annotation is also its most specific.

    This is the Marfan shape: pure information-content ranking put
    "spontaneous cerebrospinal fluid leak" first — genuinely rare, therefore
    highly informative, and nothing anyone would recognise the disease by.
    """
    return SemSimIndex(
        parents={"common": ["root"], "rare": ["root"]},
        diseases={
            "ORPHA:1": {
                "name": "Test disease",
                "terms": ["common", "rare"],
                # 0 = very frequent, 3 = very rare.
                "freq": {"common": 0, "rare": 3},
            },
            "ORPHA:2": {"name": "Filler", "terms": ["common", "root"]},
        },
    )


def test_a_frequent_feature_outranks_a_rarer_but_more_specific_one() -> None:
    """Frequency answers "would you recognise the disease by this"; IC answers
    "how specific is it". For a reader the first question is the one that
    matters, so it is ranked on first."""
    index = _freq_index()
    hpo = HpoIndex(terms={"common": "Common finding", "rare": "Rare finding"}, lookup={})

    # `rare` has the higher information content — confirm that, so the test
    # proves frequency is overriding specificity rather than agreeing with it.
    assert index.information_content("rare") > index.information_content("common")

    card = describe_disease(index, hpo, "ORPHA:1")

    assert card.features[0] == "Common finding"


def test_an_uncurated_frequency_does_not_sink_a_feature() -> None:
    """Over half of HPO annotations carry no frequency. Ranking those last
    would bury well-attested findings that simply lack the field."""
    index = SemSimIndex(
        parents={"a": ["root"], "b": ["root"]},
        diseases={
            "ORPHA:1": {"name": "D", "terms": ["a", "b"], "freq": {"b": 3}},
            "ORPHA:2": {"name": "F", "terms": ["a", "root"]},
        },
    )
    hpo = HpoIndex(terms={"a": "Unspecified", "b": "Very rare"}, lookup={})

    card = describe_disease(index, hpo, "ORPHA:1")

    # `a` has no frequency, `b` is banded "very rare": unspecified must win.
    assert card.features[0] == "Unspecified"


def test_the_same_disease_under_two_vocabularies_appears_once() -> None:
    """`OMIM:154700` and `ORPHA:558` are both Marfan syndrome. Showing a
    reader the same condition twice — with two different feature lists,
    because the annotation sets differ — is the confusion Mondo removes."""
    from adoc.knowledge.mondo import MondoIndex

    index = SemSimIndex(
        parents={"x": ["root"]},
        diseases={
            "OMIM:154700": {"name": "Marfan syndrome", "terms": ["x", "root"]},
            "ORPHA:558": {"name": "Marfan syndrome", "terms": ["x", "root"]},
        },
    )
    mondo = MondoIndex(
        names={"MONDO:0007947": "Marfan syndrome"},
        xrefs={"OMIM:154700": "MONDO:0007947", "ORPHA:558": "MONDO:0007947"},
        labels={"marfan syndrome": "MONDO:0007947"},
    )

    without = lookup_diseases(index, _hpo(), ["Marfan syndrome"])
    with_mondo = lookup_diseases(index, _hpo(), ["Marfan syndrome"], mondo=mondo)

    assert len(without) == 2
    assert len(with_mondo) == 1

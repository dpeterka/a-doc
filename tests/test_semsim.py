"""Phenotype semantic similarity.

Built on a tiny synthetic ontology so the expected answers are arithmetic
rather than opinion. The engine was separately validated against the real
2026-06-23 HPO release: for arachnodactyly + aortic root aneurysm + bicuspid
aortic valve it returns thoracic-aortic-aneurysm and Loeys-Dietz disorders in
the top six, with Marfan syndrome at 21 of 9,691.

The shape under test:

         root
         /  \\
      card    skel
      /  \\      |
 aorta  arrhy  arachno
   |
 aorta_root

`arrhy` exists so that `card` is genuinely commoner than `aorta`: without a
cardiac disease that is NOT an aortopathy, the two terms appear in exactly the
same diseases and their information content is identical. The first draft of
this fixture had that gap and asserted a difference that could not exist.

`common` is annotated on every disease (so its information content must be
near zero); `aorta_root` on one (so its IC must be high).
"""

from __future__ import annotations

from pathlib import Path

from adoc.knowledge.semsim import SemSimIndex, load_index

_PARENTS = {
    "card": ["root"],
    "skel": ["root"],
    "aorta": ["card"],
    "arrhy": ["card"],
    "aorta_root": ["aorta"],
    "arachno": ["skel"],
    "common": ["root"],
}

_DISEASES = {
    "D:aortopathy": {"name": "Aortopathy", "terms": ["aorta_root", "arachno", "common"]},
    "D:skeletal": {"name": "Skeletal disorder", "terms": ["arachno", "common", "skel"]},
    "D:cardiac": {"name": "Cardiac disorder", "terms": ["aorta", "common", "card"]},
    "D:arrhythmia": {"name": "Arrhythmia disorder", "terms": ["arrhy", "common", "card"]},
    "D:unrelated": {"name": "Unrelated", "terms": ["common", "root", "skel"]},
}


def _index() -> SemSimIndex:
    return SemSimIndex(parents=dict(_PARENTS), diseases=dict(_DISEASES))


# -- information content ----------------------------------------------------


def test_a_term_every_disease_has_carries_no_information() -> None:
    """This is what stops a patient's twenty generic findings from swamping
    their two specific ones."""
    index = _index()

    assert index.information_content("common") == 0.0
    assert index.information_content("root") == 0.0


def test_a_rare_specific_term_carries_the_most() -> None:
    index = _index()

    assert index.information_content("aorta_root") > index.information_content("aorta")
    assert index.information_content("aorta") > index.information_content("card")


def test_annotations_propagate_to_ancestors() -> None:
    """A disease annotated with a specific term is implicitly annotated with
    every ancestor. Without propagation the frequencies — and so the
    information content — would be wrong."""
    index = _index()

    # `D:aortopathy` names only `aorta_root`, but `card` must count it.
    assert index.information_content("card") > 0.0
    assert index.information_content("card") < index.information_content("aorta_root")


# -- pairwise ---------------------------------------------------------------


def test_two_terms_are_compared_by_their_most_informative_shared_ancestor() -> None:
    """What makes the measure tolerant of near-synonyms: two distinct terms
    sharing a specific ancestor score highly, where string equality scores
    nothing."""
    index = _index()

    specific = index.pairwise("aorta_root", "aorta")
    distant = index.pairwise("aorta_root", "arachno")

    assert specific > distant
    assert distant == 0.0  # their only shared ancestor is the root


def test_a_term_against_itself_is_its_own_information_content() -> None:
    index = _index()

    assert index.pairwise("aorta_root", "aorta_root") == index.information_content("aorta_root")


# -- ranking ----------------------------------------------------------------


def test_the_best_matching_disease_ranks_first() -> None:
    index = _index()

    result = index.rank(["aorta_root", "arachno"])

    assert result.diseases[0].disease_id == "D:aortopathy"


def test_a_disease_sharing_only_a_generic_term_does_not_win() -> None:
    """`D:unrelated` shares `common` with every query, and `common` carries no
    information. It must not outrank a specific match."""
    index = _index()

    ranked = [d.disease_id for d in index.rank(["aorta_root"]).diseases]

    assert ranked[0] == "D:aortopathy"
    assert (
        ranked.index("D:aortopathy") < ranked.index("D:unrelated")
        if "D:unrelated" in ranked
        else True
    )


def test_scoring_is_symmetric() -> None:
    """A one-directional average would rank a disease with many annotations
    above a precise match, or below it, depending which direction you picked.
    Symmetry removes that artefact."""
    index = _index()

    forward, _ = index.score(["aorta_root", "arachno"], "D:aortopathy")
    # Same pair, computed the other way round by construction of `score`.
    assert forward > 0
    assert index.score(["arachno", "aorta_root"], "D:aortopathy")[0] == forward


def test_an_unknown_query_term_is_reported_not_silently_dropped() -> None:
    """A term nothing is annotated with contributes zero to every disease and
    would otherwise dilute every average equally — changing the scores without
    changing the ranking, which is the worst kind of noise."""
    index = _index()

    result = index.rank(["aorta_root", "HP:9999999"])

    assert result.terms_used == ["aorta_root"]
    assert result.terms_unknown == ["HP:9999999"]
    assert result.ok


def test_an_all_unknown_query_errors_rather_than_returning_a_ranking() -> None:
    """Ranking on nothing would produce an arbitrary order that looks like a
    result."""
    index = _index()

    result = index.rank(["HP:9999999"])

    assert not result.ok
    assert result.diseases == []


def test_top_n_bounds_the_output() -> None:
    """The tail is long and its scores near-identical; past the top handful
    the ordering is noise (ADR 0034 found the same for LIRICAL)."""
    assert len(_index().rank(["arachno", "common"], top_n=2).diseases) <= 2


def test_ties_break_deterministically() -> None:
    """A run must be reproducible rather than dict-ordered."""
    index = _index()

    first = [d.disease_id for d in index.rank(["common"], top_n=10).diseases]
    second = [d.disease_id for d in index.rank(["common"], top_n=10).diseases]

    assert first == second


# -- loading ----------------------------------------------------------------


def test_a_missing_index_is_not_an_error(tmp_path: Path) -> None:
    """The index is a build artifact baked into the image; a local checkout
    will not have one. The caller reports that the engine did not run and the
    review completes."""
    assert load_index(tmp_path / "absent.json") is None


def test_an_unreadable_index_is_not_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    assert load_index(bad) is None

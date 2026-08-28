"""Tests for adoc.knowledge.hpo — matching a patient's words to HPO terms.

A tiny synthetic index, not the real ontology: these test the matching rules,
and the rules are what go wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adoc.knowledge.hpo import HpoIndex

TERMS = {
    "HP:0002829": "Arthralgia",
    "HP:0012378": "Fatigue",
    "HP:0001945": "Fever",
    "HP:0030166": "Night sweats",
    "HP:0002315": "Headache",
    "HP:0001259": "Coma",
}
LOOKUP = {
    "arthralgia": "HP:0002829",
    "joint pain": "HP:0002829",
    "pain": "HP:0002829",
    "fatigue": "HP:0012378",
    "fever": "HP:0001945",
    "night sweats": "HP:0030166",
    "sweats": "HP:0030166",
    "headache": "HP:0002315",
    "coma": "HP:0001259",
}


@pytest.fixture
def index() -> HpoIndex:
    return HpoIndex(TERMS, LOOKUP)


def test_a_lay_synonym_resolves_to_its_term(index: HpoIndex) -> None:
    """The ontology ships 26,237 synonyms precisely so lay phrasing resolves —
    which is why no model is needed to produce term ids."""
    matches = index.find_terms("I have joint pain most mornings")

    assert [(m.term_id, m.label) for m in matches] == [("HP:0002829", "Arthralgia")]


def test_the_longest_phrase_wins(index: HpoIndex) -> None:
    """ "night sweats" and "sweats" both match. The specific term is the one
    worth recording, and the shorter phrase must not also fire inside it."""
    matches = index.find_terms("drenching night sweats")

    assert len(matches) == 1
    assert matches[0].matched_text == "night sweats"


def test_negation_before_the_phrase(index: HpoIndex) -> None:
    """LIRICAL takes an excluded phenotype as evidence, so reading "no joint
    pain" as arthralgia asserts the opposite of what the patient said."""
    matches = index.find_terms("she has no joint pain")

    assert matches[0].present is False


def test_negation_after_the_phrase(index: HpoIndex) -> None:
    """Review-of-systems prose puts the denial after the finding. A
    backward-only window recorded `Coma` five times from checklist rows on
    the first real run."""
    matches = index.find_terms("Coma: no")

    assert matches[0].present is False


def test_negation_does_not_cross_a_sentence_boundary(index: HpoIndex) -> None:
    """Stripping punctuation before windowing let a cue leak forward:
    "ROS: Coma: no. Headache: yes." recorded headache as ABSENT."""
    by_id = {m.term_id: m for m in index.find_terms("ROS: Coma: no. Headache: yes.")}

    assert by_id["HP:0001259"].present is False
    assert by_id["HP:0002315"].present is True


def test_negation_reaches_across_commas_in_a_list(index: HpoIndex) -> None:
    """ "denies fever, chills, or night sweats" negates all of them — which is
    why a comma is not a clause boundary."""
    matches = index.find_terms("denies fever, night sweats")

    assert all(m.present is False for m in matches)


def test_a_compound_hpo_cannot_express_maps_to_its_substance(index: HpoIndex) -> None:
    """ "Myxedema coma" has no HPO term — not for the compound and not for
    "myxedema" at all — so only "coma" matched, recording a primary
    neurological finding for what is decompensated hypothyroidism.

    The name is a misnomer in the literature too: most patients have altered
    mental status rather than true coma, so `Coma` was not merely imprecise
    here, it was usually wrong. The override claims the whole span before the
    misleading sub-match can fire.
    """
    matches = index.find_terms("history of myxedema coma in 2021")

    assert [m.term_id for m in matches] == ["HP:0000821"]
    assert matches[0].matched_text == "myxedema coma"


def test_a_genuine_coma_still_matches(index: HpoIndex) -> None:
    """The override is scoped to the compound, not a blanket suppression."""
    assert index.find_terms("patient was in a coma")[0].term_id == "HP:0001259"


def test_context_is_recorded_so_a_match_can_be_audited(index: HpoIndex) -> None:
    """Phrase matching against a fixed vocabulary loses modifiers it has no
    term for, and there is no override for every case. Recording the
    surrounding words is what lets a reader see where a term came from and
    judge it, rather than finding a bare term with no way back to the text.
    """
    match = index.find_terms("worsening chronic fatigue since spring")[0]

    assert match.term_id == "HP:0012378"
    assert "worsening" in match.context


def test_an_unmatched_phrase_yields_nothing(index: HpoIndex) -> None:
    """A phrase either matches the published vocabulary or it does not. There
    is no guessing step."""
    assert index.find_terms("she felt generally out of sorts") == []


def test_a_missing_index_disables_matching_rather_than_crashing(tmp_path: Path) -> None:
    """The index is a build artifact; a developer without it should get the
    feature switched off, not a crashed chat turn."""
    assert HpoIndex.load(tmp_path / "absent.json") is None


def test_a_corrupt_index_disables_matching(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")

    assert HpoIndex.load(path) is None


def test_round_trips_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "idx.json"
    path.write_text(json.dumps({"terms": TERMS, "lookup": LOOKUP}))

    loaded = HpoIndex.load(path)

    assert loaded is not None
    assert loaded.size == len(TERMS)
    assert loaded.is_valid("HP:0002829")
    assert not loaded.is_valid("HP:9999999")


def test_a_following_denial_negates_the_next_finding_not_the_previous_one(
    index: HpoIndex,
) -> None:
    """ "joint pain and night sweats, denies fever" marked NIGHT SWEATS as
    excluded.

    "denies" introduces a negated item rather than closing the previous one,
    and a two-word trailing window reached past the comma to find it. Caught
    by running the built image — the unit tests never put two findings either
    side of a single cue, which is exactly the shape that breaks.
    """
    by_id = {m.term_id: m for m in index.find_terms("joint pain and night sweats, denies fever")}

    assert by_id["HP:0002829"].present is True
    assert by_id["HP:0030166"].present is True
    assert by_id["HP:0001945"].present is False


def test_a_terminal_denial_still_negates_its_own_finding(index: HpoIndex) -> None:
    """Review-of-systems shorthand puts the denial after the finding, and
    that must keep working — it is why the trailing window exists."""
    assert index.find_terms("Coma: no")[0].present is False

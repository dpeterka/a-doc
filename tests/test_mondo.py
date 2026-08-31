"""Mondo cross-references — one identity for a disease across vocabularies.

Before this, two engines and the ledger were reconciled by comparing
normalised names, so a vocabulary mismatch read as a clinical disagreement.
These tests pin the reconciliation and, more importantly, the refusal to
guess: an ambiguous or unknown name resolves to nothing rather than to
something plausible.
"""

from __future__ import annotations

from pathlib import Path

from adoc.knowledge.mondo import MondoIndex, load_mondo_index, normalise_label

_NAMES = {
    "MONDO:0007947": "Marfan syndrome",
    "MONDO:0010030": "Sjogren syndrome",
    "MONDO:0019125": "relapsing polychondritis",
}
_XREFS = {
    "OMIM:154700": "MONDO:0007947",
    "ORPHA:558": "MONDO:0007947",
    "OMIM:270150": "MONDO:0010030",
    "ORPHA:289390": "MONDO:0010030",
}
_LABELS = {
    "marfan syndrome": "MONDO:0007947",
    "sjogren syndrome": "MONDO:0010030",
    "relapsing polychondritis": "MONDO:0019125",
}


def _index() -> MondoIndex:
    return MondoIndex(names=dict(_NAMES), xrefs=dict(_XREFS), labels=dict(_LABELS))


def test_two_vocabularies_resolve_to_one_disease() -> None:
    """The whole point. LIRICAL emits OMIM, the similarity engine can emit
    ORPHA, and without this the same disease reported by both reads as two
    separate findings."""
    index = _index()

    assert index.resolve_curie("OMIM:154700") == index.resolve_curie("ORPHA:558")
    assert index.resolve_curie("OMIM:270150") == index.resolve_curie("ORPHA:289390")


def test_a_name_resolves_when_no_curie_is_available() -> None:
    """What makes this useful today: not one of the fifty hypotheses on the
    live ledger carries a mondo id, so resolving only the engines' curies
    would leave nothing to compare against."""
    assert _index().resolve_name("Marfan syndrome") == "MONDO:0007947"


def test_punctuation_and_accents_do_not_break_a_name_match() -> None:
    assert _index().resolve_name("Sjögren's syndrome") == "MONDO:0010030"


def test_a_curie_beats_a_name() -> None:
    """A cross-reference is an assertion by a curated ontology; a name is a
    string that happens to agree."""
    index = _index()

    assert index.resolve(curie="OMIM:154700", name="Sjogren syndrome") == "MONDO:0007947"


def test_a_name_is_the_fallback_when_the_curie_is_unknown() -> None:
    index = _index()

    assert index.resolve(curie="OMIM:99999999", name="Marfan syndrome") == "MONDO:0007947"


def test_an_unknown_name_resolves_to_nothing_rather_than_something_plausible() -> None:
    """A confident wrong match is worse than no match: nothing downstream can
    tell the difference."""
    index = _index()

    assert index.resolve_name("Not A Real Disease") is None
    assert index.resolve(curie="", name="") is None


def test_a_name_too_short_to_be_distinctive_resolves_to_nothing() -> None:
    """Mondo synonyms include abbreviations that collide across unrelated
    diseases."""
    assert _index().resolve_name("MFS") is None


def test_an_orphanet_prefix_is_normalised() -> None:
    """The prefix varies across releases; the engines emit ORPHA."""
    assert _index().resolve_curie("Orphanet:558") == "MONDO:0007947"


def test_a_mondo_id_passes_through_if_known() -> None:
    index = _index()

    assert index.resolve_curie("MONDO:0007947") == "MONDO:0007947"
    assert index.resolve_curie("MONDO:9999999") is None


def test_normalisation_keeps_qualifiers_the_ontology_distinguishes() -> None:
    """Unlike the free-text matcher in `lirical_divergence`, this does NOT
    drop stopwords: here a name is resolved against a curated ontology, and
    dropping "primary" would merge entries Mondo keeps apart."""
    assert normalise_label("primary Sjogren syndrome") != normalise_label("Sjogren syndrome")


def test_a_missing_index_is_not_an_error(tmp_path: Path) -> None:
    """Callers fall back to name comparison, which is what they did before
    this existed."""
    assert load_mondo_index(tmp_path / "absent.json") is None


def test_an_unreadable_index_is_not_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    assert load_mondo_index(bad) is None

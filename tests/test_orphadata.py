"""Orphanet reference data — definition, prevalence, onset, inheritance.

HPO annotations say what a condition characteristically involves; a list of
features is useful to a clinician and close to useless to the person whose
case file it is. Orphanet supplies the curated paragraph that isn't.
"""

from __future__ import annotations

from pathlib import Path

from adoc.knowledge.orphadata import (
    OrphaIndex,
    OrphaRecord,
    Prevalence,
    load_orpha_index,
    render_record,
)

_DISEASES = {
    "ORPHA:558": {
        "name": "Marfan syndrome",
        "definition": "A systemic disease of connective tissue.",
        "prevalence": {
            "value": "1-5 / 10 000",
            "type": "Point prevalence",
            "geography": "Europe",
        },
        "onset": ["All ages"],
        "inheritance": ["Autosomal dominant"],
    },
    "ORPHA:728": {
        "name": "Relapsing polychondritis",
        "definition": "A rare multisystemic inflammatory disease.",
        "prevalence": {"value": "Unknown", "type": "Point prevalence", "geography": "Worldwide"},
    },
    "ORPHA:9999": {"name": "Bare entry"},
}


def _index() -> OrphaIndex:
    return OrphaIndex(diseases={k: dict(v) for k, v in _DISEASES.items()})


def test_a_full_record_is_returned() -> None:
    record = _index().get("ORPHA:558")

    assert record is not None
    assert record.definition.startswith("A systemic disease")
    assert record.prevalence is not None
    assert record.onset == ["All ages"]
    assert record.inheritance == ["Autosomal dominant"]


def test_an_unknown_code_returns_nothing() -> None:
    assert _index().get("ORPHA:000") is None


def test_a_record_with_only_a_name_counts_as_empty() -> None:
    """The caller already knows the name — that is how it found the record. A
    name alone is not worth a section."""
    record = _index().get("ORPHA:9999")

    assert record is not None
    assert record.is_empty
    assert render_record(record) == []


# -- prevalence -------------------------------------------------------------


def test_prevalence_keeps_the_qualifiers_that_give_it_meaning() -> None:
    """ "1-5 / 10 000" means something different as a point prevalence in
    Europe than as an annual incidence worldwide. Dropping the qualifiers
    leaves a number that reads as more universal than it is."""
    rendered = Prevalence(
        value="1-5 / 10 000", type="Point prevalence", geography="Europe"
    ).render()

    assert "1-5 / 10 000" in rendered
    assert "point prevalence" in rendered
    assert "in Europe" in rendered


def test_worldwide_is_not_restated() -> None:
    """Saying "worldwide" adds nothing; it is the unmarked case."""
    rendered = Prevalence(
        value="1-9 / 1 000 000", type="Point prevalence", geography="Worldwide"
    ).render()

    assert "Worldwide" not in rendered


def test_an_unknown_prevalence_is_shown_not_suppressed() -> None:
    """Orphanet records "Unknown" for a disease nobody has measured. That is a
    fact about the state of knowledge; a blank would read as a failure of this
    tool."""
    lines = render_record(_index().get("ORPHA:728"))

    assert any("Unknown" in line for line in lines)


# -- rendering --------------------------------------------------------------


def test_the_definition_comes_first() -> None:
    """It is what a reader wants; the rest is qualification on it."""
    lines = render_record(_index().get("ORPHA:558"))

    assert lines[0].startswith("- A systemic disease")
    assert any("How common" in line for line in lines)
    assert any("Inheritance" in line for line in lines)


def test_absent_fields_produce_no_line() -> None:
    """A record with no onset should not render "Typically begins: "."""
    lines = render_record(_index().get("ORPHA:728"))

    assert not any("Typically begins" in line for line in lines)
    assert not any("Inheritance" in line for line in lines)


# -- loading ----------------------------------------------------------------


def test_a_missing_index_is_not_an_error(tmp_path: Path) -> None:
    assert load_orpha_index(tmp_path / "absent.json") is None


def test_an_unreadable_index_is_not_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    assert load_orpha_index(bad) is None


def test_an_empty_record_model_is_empty() -> None:
    assert OrphaRecord(orpha_code="ORPHA:1").is_empty

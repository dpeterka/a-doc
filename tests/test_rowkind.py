"""ADR 0025: a measurement is not a sentence.

Every case here was taken from the real store (2033 rows) rather than
invented, because the whole point of the gate is that it must divert the
three sentences that are actually in there and lose none of the 2030
genuine results.
"""

from __future__ import annotations

import pytest

from adoc.ingest.reconcile import divert_narrative_extraction
from adoc.ingest.rowkind import (
    classify_extracted_row,
    name_reads_as_prose,
    parse_comparator_value,
)
from adoc.ingest.schema import DocumentExtraction, ExtractedResult

# --- the comparator parser ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<20", ("<", 20.0, "")),
        ("<0.10", ("<", 0.1, "")),
        (">150", (">", 150.0, "")),
        (">= 150 mg/dL", (">=", 150.0, "mg/dL")),
        ("=< 4", ("<=", 4.0, "")),
        ("<1,200", ("<", 1200.0, "")),
    ],
)
def test_comparator_values_are_parsed(text: str, expected: tuple[str, float, str]) -> None:
    """183 real rows hold a number like this in `value_text`, where it can
    be neither trended nor range-checked."""
    assert parse_comparator_value(text) == expected


@pytest.mark.parametrize("text", ["<1:256", "<1:40", ">1:1280"])
def test_a_titer_is_never_parsed_as_a_scalar(text: str) -> None:
    """A titer is not a scalar. "<1:256" parses as the number 1 with a
    leftover ":256" unless it is excluded — storing value=1.0 for a titer of
    <1:256 is silent corruption, and a serology panel is full of them. 41
    real rows are this shape."""
    assert parse_comparator_value(text) is None


@pytest.mark.parametrize("text", ["NON-REACTIVE", "NO MUTATION DETECTED", "negative", ""])
def test_a_qualitative_result_is_never_coerced_into_a_number(text: str) -> None:
    assert parse_comparator_value(text) is None


# --- prose detection ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # All three are real rows, from one narrative DEXA report.
        "Left total hip: A statistically significant decrease of",
        "Lumbar spine: A statistically significant decrease of",
        "Right total hip: A statistically significant decrease of",
    ],
)
def test_sentences_are_recognized_as_prose(name: str) -> None:
    """Name-cleaning cannot rescue these: the problem is not the name, it is
    that the row should never have been a lab row."""
    assert name_reads_as_prose(name)


@pytest.mark.parametrize(
    "name",
    [
        # Real analyte names get long, punctuated and strange. A false
        # divert silently loses a result, which is worse than the noise.
        "% SATURATION",
        "B. MIYAMOTOI AB (IGG)",
        "LEFT HIP femoral neck T-Score",
        "Lyme WB 18 kDa IgG",
        "hs-CRP",
        "Left Hip Femoral Neck BMD",
        # Contains the prose verb "shows" AND a measure token; the measure
        # token must win, because this is a genuine FRAX measurement.
        "FRAX analysis shows 10-year probability of major osteoporotic fracture "
        "(clinical spine, forearm, hip or shoulder)",
        # A qualifier colon is not a clause colon.
        "HDL: direct",
    ],
)
def test_genuine_analyte_names_are_not_prose(name: str) -> None:
    assert not name_reads_as_prose(name)


# --- the three-way gate ---------------------------------------------------------------


def test_a_numeric_row_is_quantitative() -> None:
    assert classify_extracted_row("CRP", 8.5, None) == "quantitative"


def test_a_comparator_row_is_quantitative_not_qualitative() -> None:
    """This is the 183-row rescue: the numeric content is real."""
    assert classify_extracted_row("RNA Polymerase III Antibody", None, "<20") == "quantitative"


def test_a_nominal_result_is_qualitative() -> None:
    """A real result that simply is not a number — 363 rows."""
    assert classify_extracted_row("Lyme WB 18 kDa IgG", None, "NON-REACTIVE") == "qualitative"


def test_a_titer_stays_qualitative() -> None:
    assert classify_extracted_row("Babesia duncani Antibody IgG", None, "<1:256") == "qualitative"


def test_a_sentence_is_narrative_even_with_a_number() -> None:
    """The value 6.7 is real; the row is still not a lab result. It belongs
    in `narrative_findings`, where it stays citable as doc:<file>#p<n>."""
    kind = classify_extracted_row(
        "Left total hip: A statistically significant decrease of", 6.7, None
    )

    assert kind == "narrative"


# --- the extraction-level diversion ---------------------------------------------------


def _row(name: str, value: float | None = None, page: int = 1) -> ExtractedResult:
    return ExtractedResult(name_raw=name, value=value, page=page, confidence="high")


def test_a_sentence_row_is_diverted_out_of_results_into_findings() -> None:
    """The number is real, so nothing may be discarded — the text moves to
    `narrative_findings`, where it stays retrievable and citable as
    doc:<file>#p<n>, instead of becoming a lab row named after a sentence."""
    extraction = DocumentExtraction(
        doc_type="imaging_report",
        results=[
            _row("Left Hip Total BMD", 0.88),
            _row("Left total hip: A statistically significant decrease of", 6.7, page=3),
        ],
    )

    result = divert_narrative_extraction(extraction)

    assert [r.name_raw for r in result.results] == ["Left Hip Total BMD"]
    assert len(result.narrative_findings) == 1
    finding = result.narrative_findings[0]
    assert "statistically significant decrease" in finding
    assert "6.7" in finding  # the value survives
    assert "(p3)" in finding  # and so does the page, so it stays citable


def test_an_extraction_of_only_real_measurements_is_returned_unchanged() -> None:
    extraction = DocumentExtraction(
        doc_type="lab_report", results=[_row("CRP", 8.5), _row("hs-CRP", 1.2)]
    )

    result = divert_narrative_extraction(extraction)

    assert result is extraction


def test_existing_narrative_findings_are_preserved_when_diverting() -> None:
    extraction = DocumentExtraction(
        doc_type="imaging_report",
        narrative_findings=["Impression: low bone density, no osteoporosis."],
        results=[_row("The BMD measured is", 1.098), _row("Lumbar spine: A decrease of", 8.0)],
    )

    result = divert_narrative_extraction(extraction)

    assert result.narrative_findings[0].startswith("Impression:")
    assert len(result.narrative_findings) == 2

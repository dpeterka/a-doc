"""ICAP ANA-pattern mapping.

A pattern narrows which antibodies are worth testing next. Getting the pattern
wrong sends a clinician after the wrong antibody, so the tests here are mostly
about the distinctions that are easy to blur — "homogeneous nucleolar" is not
"homogeneous", and the difference is systemic sclerosis versus lupus.
"""

from __future__ import annotations

from datetime import date

from adoc.knowledge.icap import (
    ICAP_PATTERNS,
    IcapReport,
    match_patterns,
    pattern_for_code,
    render_icap,
    scan_ana_patterns,
)
from adoc.labs.models import LabResult

_SHA = "a" * 64


def _row(
    name: str,
    *,
    value_text: str | None = None,
    name_raw: str | None = None,
    on: date = date(2025, 10, 29),
) -> LabResult:
    return LabResult(
        date=on,
        name=name,
        name_raw=name_raw or name,
        value_text=value_text,
        source_doc=_SHA,
        raw_json="{}",
    )


# -- the table --------------------------------------------------------------


def test_every_pattern_has_a_unique_code() -> None:
    codes = [p.code for p in ICAP_PATTERNS]
    assert len(codes) == len(set(codes))


def test_codes_resolve_both_ways() -> None:
    assert pattern_for_code("AC-3").name == "Centromere"
    assert pattern_for_code("ac-3").name == "Centromere"
    assert pattern_for_code("AC-999") is None


def test_every_pattern_names_an_antibody_or_says_why_not() -> None:
    """The antibody list is the actionable half — what to test next. A pattern
    with neither an antibody nor an association is a row that tells a reader
    nothing."""
    for pattern in ICAP_PATTERNS:
        assert pattern.antibodies or pattern.associations or pattern.note, pattern.code


# -- matching ---------------------------------------------------------------


def test_a_plain_pattern_matches() -> None:
    assert [m.pattern.code for m in match_patterns("Speckled")] == ["AC-4"]
    assert [m.pattern.code for m in match_patterns("centromere")] == ["AC-3"]


def test_a_compound_pattern_is_not_split_into_its_parts() -> None:
    """The distinction that matters most. "Homogeneous nucleolar" is AC-8 and
    points at systemic sclerosis; plain "homogeneous" is AC-1 and points at
    lupus. Matching the substring would report both and send a clinician after
    dsDNA for a scleroderma pattern."""
    codes = [m.pattern.code for m in match_patterns("homogeneous nucleolar")]

    assert codes == ["AC-8"]
    assert "AC-1" not in codes


def test_dense_fine_speckled_is_not_read_as_fine_speckled() -> None:
    """AC-2 argues AGAINST an ANA-associated rheumatic disease; AC-4 points at
    Sjogren's and lupus. Reading one as the other inverts the meaning."""
    codes = [m.pattern.code for m in match_patterns("Dense fine speckled (DFS70)")]

    assert codes == ["AC-2"]


def test_pattern_text_is_found_inside_a_fuller_report_line() -> None:
    matches = match_patterns("Nuclear, speckled pattern; titre 1:320")

    assert [m.pattern.code for m in matches] == ["AC-4"]
    assert matches[0].matched_text == "speckled"


def test_a_negative_result_matches_nothing() -> None:
    assert match_patterns("NEGATIVE") == []
    assert match_patterns("") == []


def test_the_source_ref_is_carried_so_a_reader_can_check_it() -> None:
    matches = match_patterns("centromere", source_ref="labs:ana-screen:2025-10-29")

    assert matches[0].source_ref == "labs:ana-screen:2025-10-29"


# -- scanning ---------------------------------------------------------------


def test_a_negative_ana_yields_no_pattern_and_says_why() -> None:
    """The real case this was written against: seven negative ANA screens from
    2017 to 2025, three by IFA. A pattern is a property of a POSITIVE result;
    inventing one would be worse than saying nothing, and saying nothing
    without saying why would look like a bug."""
    report = scan_ana_patterns([_row("ANA Screen", value_text="NEGATIVE")])

    assert report.matches == []
    assert report.ana_negative
    assert "negative" in report.note.lower()


def test_a_positive_ana_with_a_pattern_is_mapped() -> None:
    report = scan_ana_patterns([_row("ANA Screen", value_text="POSITIVE 1:320, speckled pattern")])

    assert [m.pattern.code for m in report.matches] == ["AC-4"]
    assert not report.ana_negative
    assert "SS-A/Ro" in report.antibodies_to_consider


def test_the_latest_result_decides_whether_it_is_negative() -> None:
    """A positive from 2019 that has since turned negative is history, not the
    current state."""
    report = scan_ana_patterns(
        [
            _row("ANA Screen", value_text="POSITIVE, speckled", on=date(2019, 8, 30)),
            _row("ANA Screen", value_text="NEGATIVE", on=date(2025, 10, 29)),
        ]
    )

    assert report.ana_negative
    # The historical pattern is still surfaced — it happened.
    assert [m.pattern.code for m in report.matches] == ["AC-4"]


def test_no_ana_on_file_is_distinguished_from_a_negative_one() -> None:
    """ "Never tested" and "tested and negative" are different clinical facts."""
    report = scan_ana_patterns([_row("Ferritin", value_text="25")])

    assert report.ana_result_count == 0
    assert "No ANA result" in report.note


def test_the_same_pattern_reported_twice_appears_once() -> None:
    report = scan_ana_patterns(
        [
            _row("ANA Screen", value_text="speckled", on=date(2024, 1, 1)),
            _row("ANA Screen", value_text="speckled", on=date(2025, 1, 1)),
        ]
    )

    assert len(report.matches) == 1


# -- rendering --------------------------------------------------------------


def test_nothing_to_report_renders_nothing_at_all() -> None:
    """An empty heading costs tokens and tells a reader nothing."""
    assert render_icap(IcapReport(ana_negative=True)) == []


def test_the_rendering_says_association_not_diagnosis() -> None:
    """Same posture as the classification scorers: a pattern narrows what to
    test, it does not name a disease."""
    report = scan_ana_patterns([_row("ANA Screen", value_text="POSITIVE, centromere")])

    text = "\n".join(render_icap(report))

    assert "not a diagnosis" in text
    assert "CENP-A/B" in text
    assert "limited cutaneous systemic sclerosis" in text
    assert "`labs:" in text


def test_an_expert_level_pattern_is_flagged_as_such() -> None:
    """A reference-laboratory pattern being reported at all is informative."""
    report = scan_ana_patterns([_row("ANA Screen", value_text="POSITIVE, rods and rings")])

    assert "reference-laboratory" in "\n".join(render_icap(report))

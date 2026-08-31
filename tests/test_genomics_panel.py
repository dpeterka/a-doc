"""The genomic panel (ADR 0030).

The most dangerous misreading available here is treating a marker the array
did not measure as a negative result, so most of these tests are about the
difference between "not present" and "not looked for".

No real genotypes appear in this file — the fixtures are synthetic.
"""

from __future__ import annotations

from pathlib import Path

from adoc.genomics.panel import (
    GENOMIC_PANEL_RELPATH,
    PANEL,
    UNREACHABLE_BY_ARRAY,
    PanelResult,
    build_panel,
    find_raw_array,
    interpret,
    parse_array,
    render_panel,
)

_HEADER = "# rsid\tchromosome\tposition\tgenotype\n"


def _array(tmp_path: Path, rows: dict[str, str], name: str = "genome_export.txt") -> Path:
    path = tmp_path / name
    body = "".join(f"{rsid}\t6\t1000\t{genotype}\n" for rsid, genotype in rows.items())
    path.write_text(_HEADER + body, encoding="utf-8")
    return path


def _marker(rsid: str):
    return next(m for m in PANEL if m.rsid == rsid)


# -- the panel itself -------------------------------------------------------


def test_every_marker_names_a_confirmatory_test() -> None:
    """ADR 0030's posture: the array produces a lead for the Test-Chooser,
    never a conclusion. A marker with no test that settles it has no business
    in the panel."""
    for marker in PANEL:
        assert marker.confirmatory_test
        assert marker.meaning
        assert marker.risk_alleles


def test_the_panel_is_bounded() -> None:
    """A variant dump is useless to a model and is also the only genuinely
    re-identifying form this data takes."""
    assert 0 < len(PANEL) <= 20


def test_source_refs_match_the_citation_grammar() -> None:
    from adoc.casefile.schema import normalize_source_ref

    for marker in PANEL:
        assert normalize_source_ref(marker.source_ref) == marker.source_ref


# -- interpretation ---------------------------------------------------------


def test_a_no_call_is_not_a_negative_result() -> None:
    """The error this whole artifact exists to prevent. Treating "--" as
    homozygous reference would silently invent a negative."""
    marker = _marker("rs1800562")

    for absent in ("--", "", "DD", "II"):
        assert interpret(marker, absent) == "no_call"


def test_a_risk_allele_is_found_regardless_of_order() -> None:
    """The array reports genotypes unordered, so comparing whole strings would
    produce a false negative on a heterozygote written the other way round."""
    marker = _marker("rs1800562")  # risk allele A

    assert interpret(marker, "AG") == "risk_allele_present"
    assert interpret(marker, "GA") == "risk_allele_present"
    assert interpret(marker, "AA") == "risk_allele_present"


def test_absence_of_the_risk_allele_is_reported_as_such() -> None:
    assert interpret(_marker("rs1800562"), "GG") == "no_risk_allele"


# -- parsing ----------------------------------------------------------------


def test_only_the_wanted_markers_are_read() -> None:
    """Streamed and filtered: the export is ~17MB and nothing outside the
    curated panel is ever held."""
    found = parse_array([_HEADER, "rs1800562\t6\t1\tAG\n", "rs99999999\t1\t2\tCC\n"], ["rs1800562"])

    assert found == {"rs1800562": "AG"}


def test_comments_and_short_rows_are_skipped() -> None:
    found = parse_array(
        ["# a comment\n", "\n", "broken\trow\n", "rs1800562\t6\t1\tAG\n"], ["rs1800562"]
    )

    assert found == {"rs1800562": "AG"}


# -- building ---------------------------------------------------------------


def test_a_marker_absent_from_the_array_is_a_no_call(tmp_path: Path) -> None:
    """Absent from the file is exactly as unmeasured as "--", and must not be
    reported as a negative."""
    result = build_panel(_array(tmp_path, {"rs1800562": "GG"}))

    by_rsid = {r.marker.rsid: r for r in result.results}
    assert by_rsid["rs1800562"].interpretation == "no_risk_allele"
    assert by_rsid["rs4349859"].interpretation == "no_call"
    assert result.markers_found == 1
    assert result.markers_sought == len(PANEL)


def test_an_unreadable_array_reports_rather_than_raises(tmp_path: Path) -> None:
    result = build_panel(tmp_path / "does-not-exist.txt")

    assert not result.ok
    assert result.results == []


# -- choosing the input file ------------------------------------------------


def test_the_phased_exports_are_never_chosen(tmp_path: Path) -> None:
    """ADR 0030 excludes them: the vendor states they are not for medical use
    and they cover fewer relevant loci than the file they derive from."""
    genomics = tmp_path / "sources" / "genomics"
    genomics.mkdir(parents=True)
    (genomics / "abc__phased_genome_export.txt").write_text("x" * 5000, encoding="utf-8")
    (genomics / "def__genome_export.txt").write_text("y" * 100, encoding="utf-8")

    chosen = find_raw_array(tmp_path)

    assert chosen is not None
    assert "phased" not in chosen.name


def test_no_genomics_directory_is_not_an_error(tmp_path: Path) -> None:
    assert find_raw_array(tmp_path) is None


# -- the artifact -----------------------------------------------------------


def test_the_header_says_absence_is_not_exclusion(tmp_path: Path) -> None:
    """ADR 0030 requires this in the ARTIFACT rather than in a prompt: a model
    reading a missing pathogenic variant as an exclusion is the most dangerous
    misreading available here."""
    text = render_panel(build_panel(_array(tmp_path, {"rs1800562": "GG"})))

    assert "Absence is not exclusion" in text
    assert "leads, not findings" in text


def test_the_artifact_says_what_the_data_cannot_answer(tmp_path: Path) -> None:
    """ "We have genome data on file" otherwise reads as though the question has
    been covered. The ledger raised FXPOI, and a CGG repeat expansion is
    invisible to an array."""
    text = render_panel(build_panel(_array(tmp_path, {"rs1800562": "GG"})))

    assert "FMR1" in text
    assert "repeat expansion" in text
    assert len(UNREACHABLE_BY_ARRAY) >= 2


def test_a_called_marker_carries_a_citable_ref(tmp_path: Path) -> None:
    text = render_panel(build_panel(_array(tmp_path, {"rs1800562": "AG"})))

    assert "`genomic:HFE:rs1800562`" in text


def test_a_no_call_carries_no_ref_and_says_it_was_not_measured(tmp_path: Path) -> None:
    """A ref would imply a genotype behind it."""
    text = render_panel(build_panel(_array(tmp_path, {"rs1800562": "--"})))

    assert "not a negative result" in text
    assert "`genomic:HFE:rs1800562`" not in text


def test_the_header_appears_even_when_the_panel_failed() -> None:
    """A document without the header is more dangerous than no document."""
    text = render_panel(PanelResult(error="could not read the array"))

    assert "Absence is not exclusion" in text
    assert "could not be built" in text


def test_the_relpath_is_under_case() -> None:
    assert GENOMIC_PANEL_RELPATH.startswith("case/")

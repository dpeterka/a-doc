"""Tests for adoc.knowledge.lirical — argv construction and TSV parsing.

No JVM and no network: the fixture is a recorded phenotype-only run against a
synthetic Marfan-like phenotype, produced by LIRICAL 2.4.1 during the sidecar
build-out. Nothing here touches patient data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adoc.knowledge.lirical import (
    LiricalRequest,
    build_prioritize_args,
    parse_lirical_tsv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lirical" / "phenotype-only-smoke.tsv"


def _fixture() -> str:
    return FIXTURE.read_text()


def test_phenotype_only_argv_omits_assembly_and_vcf() -> None:
    """LIRICAL runs phenotype-only precisely when `--assembly`/`--vcf` are
    absent. Passing either would switch it into genotype mode, which this
    patient's array-plus-imputation data cannot support."""
    args = build_prioritize_args(
        LiricalRequest(observed=["HP:0001166", "HP:0002616"], sex="FEMALE"),
        data_dir="/opt/liricaldata",
        out_dir="/work/out",
    )

    assert "--assembly" not in args
    assert "--vcf" not in args
    assert args[0] == "prioritize"
    assert "HP:0001166,HP:0002616" in args
    assert "--sex" in args and "FEMALE" in args


def test_negated_terms_are_passed_when_present() -> None:
    """Excluded findings are evidence. LIRICAL folds them into the likelihood
    ratio, so a negative ANA is worth passing."""
    args = build_prioritize_args(
        LiricalRequest(observed=["HP:0001166"], negated=["HP:0003493"]),
        data_dir="/d",
        out_dir="/o",
    )

    assert "-n" in args
    assert "HP:0003493" in args


def test_no_negated_flag_when_there_are_none() -> None:
    args = build_prioritize_args(
        LiricalRequest(observed=["HP:0001166"]), data_dir="/d", out_dir="/o"
    )

    assert "-n" not in args


def test_a_malformed_term_costs_the_term_not_the_run() -> None:
    """ADR 0028's posture, applied to phenotype terms: one bad id must not
    fail the engine when other valid terms are present."""
    request = LiricalRequest(observed=["HP:0001166", "not-a-term", "HP:99"])
    observed, _ = request.validated_terms()

    assert observed == ["HP:0001166"]
    args = build_prioritize_args(request, data_dir="/d", out_dir="/o")
    assert "HP:0001166" in args
    assert "not-a-term" not in " ".join(args)


def test_a_run_with_no_valid_term_is_refused() -> None:
    """Dropping every term would otherwise send LIRICAL an empty `-p`, which
    ranks nothing and looks like a successful empty differential."""
    with pytest.raises(ValueError, match="observed HPO term"):
        build_prioritize_args(LiricalRequest(observed=["nonsense"]), data_dir="/d", out_dir="/o")


def test_parses_the_recorded_run() -> None:
    result = parse_lirical_tsv(_fixture())

    assert result.version == "2.4.1"
    assert result.sample_id == "subject"
    # The preamble echoes the input back; reading it means a result always
    # carries the terms that actually produced it.
    assert "HP:0001166" in result.observed
    assert result.diseases


def test_ranking_is_clinically_sane_for_the_synthetic_phenotype() -> None:
    """Arachnodactyly + aortic root aneurysm + bicuspid aortic valve must put
    Loeys-Dietz/Marfan disorders at the top. This is the same assertion the
    container build makes, so a data or version regression fails here too."""
    result = parse_lirical_tsv(_fixture())
    top_names = " ".join(d.name.lower() for d in result.top(8))

    assert "loeys-dietz" in top_names
    assert "marfan" in top_names
    assert result.diseases[0].rank == 1


def test_columns_are_read_by_name_not_position() -> None:
    """LIRICAL's TSV gains columns between versions; a positional parser would
    silently read the wrong field rather than fail."""
    text = (
        "! LIRICAL TSV Output (9.9.9)\n"
        "rank\tsomethingNew\tdiseaseName\tdiseaseCurie\tpretestprob\tposttestprob\tcompositeLR\n"
        "1\tXXX\tMarfan syndrome\tOMIM:154700\t1/8621\t92.14%\t5.005\n"
    )

    result = parse_lirical_tsv(text)

    assert result.diseases[0].name == "Marfan syndrome"
    assert result.diseases[0].curie == "OMIM:154700"
    assert result.diseases[0].posttest_probability == pytest.approx(92.14)
    assert result.diseases[0].composite_lr == pytest.approx(5.005)


def test_pretest_probability_stays_a_rendered_fraction() -> None:
    """`1/8621` is a rendered fraction. Coercing it to a float would imply a
    precision the string does not carry."""
    result = parse_lirical_tsv(_fixture())

    assert "/" in result.diseases[0].pretest_probability


def test_an_unparseable_row_is_skipped_not_fatal() -> None:
    text = (
        "! LIRICAL TSV Output (2.4.1)\n"
        "rank\tdiseaseName\tdiseaseCurie\tpretestprob\tposttestprob\tcompositeLR\n"
        "not-a-rank\tJunk\tOMIM:1\t1/2\t50%\t1.0\n"
        "1\tMarfan syndrome\tOMIM:154700\t1/8621\t92.14%\t5.005\n"
    )

    result = parse_lirical_tsv(text)

    assert len(result.diseases) == 1
    assert result.diseases[0].name == "Marfan syndrome"

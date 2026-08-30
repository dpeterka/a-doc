"""Requiring a hypothesis to state what would kill it (ADR 0035).

The prompts ask for `rule_out`; a prompt is a request, not a guarantee. This
is the code that makes it true, and the shape it takes is dictated by two
rules this codebase already settled: no single field of one item may fail a
payload (ADR 0028, the defect fixed in v0.21.0), and unrepairable model output
is stripped rather than rejected (ADR 0016 revised).
"""

from __future__ import annotations

from datetime import date

from adoc.casefile.rule_out import (
    build_rule_out_retry_feedback,
    hypotheses_missing_rule_out,
    is_usable_rule_out,
    strip_ops_missing_rule_out,
)
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    UpdateHypothesis,
)

_REAL = "a normal repeat FSH on a draw four or more weeks later"


def _add(hid: str, *, rule_out: str = _REAL) -> AddHypothesis:
    return AddHypothesis(
        hypothesis=Hypothesis(
            id=hid,
            name=hid.replace("-", " ").title(),
            tier="expanded",
            probability="low",
            status="active",
            origin="model",
            first_proposed=date(2026, 8, 30),
            rule_out=rule_out,
        )
    )


# -- the predicate ----------------------------------------------------------


def test_a_real_falsification_condition_passes() -> None:
    assert is_usable_rule_out(_REAL)
    assert is_usable_rule_out("a negative cartilage biopsy from an affected site")


def test_a_terse_but_real_condition_passes() -> None:
    """The errors here are asymmetric — a false negative DROPS a real
    hypothesis, a false positive only lets a weak rule-out through for a
    clinician to judge. A first draft used a 15-character floor and rejected
    "negative ANA", which is twelve characters and a perfectly good
    falsification condition for lupus."""
    assert is_usable_rule_out("negative ANA")


def test_the_hedges_a_model_reaches_for_are_rejected() -> None:
    """ "Further testing" names the wish for a result, not a result. Accepting
    it would make the requirement satisfiable by any hypothesis at all."""
    for hedge in (
        "",
        "   ",
        "n/a",
        "N/A.",
        "none",
        "TBD",
        "unknown",
        "further testing",
        "clinical correlation",
        "further workup as needed",
    ):
        assert not is_usable_rule_out(hedge), hedge


# -- stripping --------------------------------------------------------------


def test_only_the_offending_hypothesis_is_dropped() -> None:
    """ADR 0028: no single field of one item may fail a payload. A missing
    rule_out on one hypothesis must never discard a whole verdict — that is
    the exact defect fixed in v0.21.0."""
    ops = [_add("good-one"), _add("bad-one", rule_out=""), _add("also-good")]

    kept, dropped = strip_ops_missing_rule_out(ops)

    assert [op.hypothesis.id for op in kept] == ["good-one", "also-good"]
    assert dropped == [("bad-one", "Bad One")]


def test_other_op_kinds_survive_untouched() -> None:
    """Stripping targets the addition, not everything that shares the
    payload."""
    evidence = AddEvidence(
        id="existing",
        for_or_against="for",
        evidence=Evidence(claim="c", source="pmid:1", strength="weak"),
    )
    kept, dropped = strip_ops_missing_rule_out([_add("bad", rule_out=""), evidence])

    assert kept == [evidence]
    assert len(dropped) == 1


def test_an_update_is_not_required_to_carry_a_rule_out() -> None:
    """An `update_hypothesis` usually adjusts a tier or a probability on
    something that already exists. Requiring the field on every edit would
    make routine maintenance impossible."""
    update = UpdateHypothesis(id="existing", probability="high")

    kept, dropped = strip_ops_missing_rule_out([update])

    assert kept == [update]
    assert dropped == []


def test_a_clean_payload_is_returned_unchanged() -> None:
    ops = [_add("a"), _add("b")]

    kept, dropped = strip_ops_missing_rule_out(ops)

    assert kept == ops
    assert dropped == []


def test_missing_are_reported_with_names_not_just_ids() -> None:
    """The feedback and the log both name the hypothesis, because an id alone
    tells a reader nothing about what was lost."""
    missing = hypotheses_missing_rule_out([_add("relapsing-polychondritis", rule_out="n/a")])

    assert missing == [("relapsing-polychondritis", "Relapsing Polychondritis")]


# -- retry feedback ---------------------------------------------------------


def test_the_retry_feedback_shows_what_good_looks_like() -> None:
    """ "Add a rule_out" without an example is what produced "further testing"
    in the first place."""
    text = build_rule_out_retry_feedback([("x", "Some Hypothesis")])

    assert "Some Hypothesis" in text
    assert "repeat FSH" in text
    assert "Further testing" in text
    assert "drop any hypothesis you cannot state one for" in text

"""ADR 0054: the Challenger must pay for what it proposes.

Two consecutive ADR 0052 snapshots — the first before/after this project has
had that was not a recollection:

    2026-09-09  0.33.1  active 32  off_board 15  questions 70
    2026-09-10  0.34.0  active 33  off_board  0  questions 92

One review added a lead, removed none, and opened 22 more questions. Four
measured causes, and the two this file pins:

- **30 of 33** leads were challenger-origin and **15** carried no
  counter-evidence at all — evidence weight 519 for against 36.
- The only contract on the stage required a PROSE argument, for `most-likely`
  hypotheses only, and the live board held **0** of those. It covered nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    LedgerDiff,
    Provenance,
    RecordChallenge,
    UpdateHypothesis,
)
from adoc.reason.dag import Ctx
from adoc.reason.stages import (
    UNNAMED_SEARCH,
    ChallengerVerdict,
    CounterArgument,
    _challenger_min_counterarguments_contract,
    normalize_counter_arguments,
)

_TODAY = date(2026, 9, 12)


def _hypothesis(hid: str, *, tier: str = "expanded") -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=f"Condition {hid}",
        tier=tier,  # type: ignore[arg-type]
        probability="moderate",
        status="active",
        origin="challenger",
        first_proposed=_TODAY,
    )


def _diff(*hypotheses: Hypothesis) -> LedgerDiff:
    return LedgerDiff(
        provenance=Provenance(
            app_version="0.35.0",
            prompt_template_version="challenger@v3",
            model_id="fake",
            dag_node="ledger_maintainer",
            timestamp=datetime(2026, 9, 12, tzinfo=UTC),
        ),
        rationale="proposed",
        ops=[AddHypothesis(hypothesis=h) for h in hypotheses],
    )


def _counter(hid: str, **kwargs: Any) -> CounterArgument:
    return CounterArgument(hypothesis_id=hid, argument="a real objection", **kwargs)


def _against(hid: str, *, strength: str = "moderate") -> AddEvidence:
    return AddEvidence(
        id=hid,
        for_or_against="against",
        evidence=Evidence(
            claim="anti-dsDNA negative",
            source="labs:anti-dsdna:2026-08-01",
            strength=strength,  # type: ignore[arg-type]
        ),
    )


def _check(diff: LedgerDiff, verdict: ChallengerVerdict) -> str | None:
    contract = _challenger_min_counterarguments_contract()
    ctx: Ctx = {"ledger_maintainer": diff}  # type: ignore[assignment]
    return contract.check(ctx, verdict)


# --- coverage: every tier, not just `most-likely` ------------------------------


def test_an_expanded_lead_must_be_accounted_for() -> None:
    """The old contract collected `most-likely` ids only, and the board holds
    none. It covered nothing, for as long as the board has looked like this."""
    diff = _diff(_hypothesis("sle-01", tier="expanded"))

    problem = _check(diff, ChallengerVerdict())

    assert problem is not None and "sle-01" in problem


def test_a_cant_miss_lead_must_be_accounted_for() -> None:
    """The tier whose whole justification is that missing one is catastrophic
    was the tier nobody was required to look at."""
    diff = _diff(_hypothesis("pe-01", tier="cant-miss"))

    assert _check(diff, ChallengerVerdict()) is not None


def test_an_updated_lead_counts_as_touched() -> None:
    diff = LedgerDiff(
        provenance=_diff().provenance,
        rationale="bumped",
        ops=[UpdateHypothesis(id="sle-01", probability="high")],
    )

    assert _check(diff, ChallengerVerdict()) is not None


def test_covering_every_touched_lead_clears_the_contract() -> None:
    diff = _diff(_hypothesis("sle-01"), _hypothesis("pe-01", tier="cant-miss"))
    verdict = ChallengerVerdict(
        counter_arguments=[
            _counter("sle-01", outcome="nothing-on-file", looked_for="anti-dsDNA"),
            _counter("pe-01", outcome="nothing-on-file", looked_for="D-dimer"),
        ]
    )

    assert _check(diff, verdict) is None


def test_an_empty_argument_does_not_count_as_covering() -> None:
    """A blank string satisfies "an entry exists" and says nothing. The old
    contract already required substance; widening the scope must not lose it."""
    diff = _diff(_hypothesis("sle-01"))
    verdict = ChallengerVerdict(
        counter_arguments=[CounterArgument(hypothesis_id="sle-01", argument="   ")]
    )

    assert _check(diff, verdict) is not None


def test_attacking_a_lead_outside_the_diff_is_unconstrained() -> None:
    """The Challenger is welcome to go after the standing board, and holding
    that to the same bar would discourage exactly the behaviour wanted."""
    diff = _diff(_hypothesis("sle-01"))
    verdict = ChallengerVerdict(
        counter_arguments=[
            _counter("sle-01", outcome="nothing-on-file", looked_for="anti-dsDNA"),
            # Not in the diff, and deliberately malformed for its outcome.
            _counter("old-lead-09", outcome="cited"),
        ]
    )

    assert _check(diff, verdict) is None


# --- `cited` means an op, not a label -----------------------------------------


def test_cited_without_a_backing_op_is_downgraded_not_believed() -> None:
    """`cited` is the only outcome that puts weight on the retirement scale.
    Believing the label would let a model retire a real lead by asserting
    counter-evidence it never produced."""
    verdict = ChallengerVerdict(counter_arguments=[_counter("sle-01", outcome="cited")])

    fixed = normalize_counter_arguments(verdict)

    assert fixed.counter_arguments[0].outcome == "nothing-on-file"


def test_cited_with_a_backing_op_survives() -> None:
    verdict = ChallengerVerdict(
        counter_arguments=[_counter("sle-01", outcome="cited")],
        additional_ops=[_against("sle-01")],
    )

    fixed = normalize_counter_arguments(verdict)

    assert fixed.counter_arguments[0].outcome == "cited"


def test_an_op_that_supports_is_not_counter_evidence() -> None:
    """`for_or_against` decides. An `add_evidence` op pointing the other way
    would otherwise back a `cited` claim that argues the opposite."""
    supporting = AddEvidence(
        id="sle-01",
        for_or_against="for",
        evidence=Evidence(claim="ANA positive", source="labs:ana:2026-08-01", strength="strong"),
    )
    verdict = ChallengerVerdict(
        counter_arguments=[_counter("sle-01", outcome="cited")], additional_ops=[supporting]
    )

    assert normalize_counter_arguments(verdict).counter_arguments[0].outcome == "nothing-on-file"


def test_a_record_challenge_op_is_not_counter_evidence_either() -> None:
    """A challenge note is prose in a different shape. The retirement pass
    weighs `evidence_against`, and nothing else."""
    verdict = ChallengerVerdict(
        counter_arguments=[_counter("sle-01", outcome="cited")],
        additional_ops=[
            RecordChallenge(id="sle-01", note="Worth a second look before this is trusted.")
        ],
    )

    assert normalize_counter_arguments(verdict).counter_arguments[0].outcome == "nothing-on-file"


def test_under_claiming_is_corrected_upward_from_the_op() -> None:
    """It cited something and labelled itself weakly. Believe the op — that is
    what the retirement pass will read either way."""
    verdict = ChallengerVerdict(
        counter_arguments=[_counter("sle-01", outcome="nothing-on-file", looked_for="x")],
        additional_ops=[_against("sle-01")],
    )

    assert normalize_counter_arguments(verdict).counter_arguments[0].outcome == "cited"


def test_the_contract_still_guards_the_normalised_invariant() -> None:
    """Normalisation runs inside `challenger_stage`, so reaching the contract
    with an unbacked `cited` means it did not run. A mechanism whose guard was
    removed must not look identical to one working."""
    diff = _diff(_hypothesis("sle-01"))
    verdict = ChallengerVerdict(counter_arguments=[_counter("sle-01", outcome="cited")])

    problem = _check(diff, verdict)

    assert problem is not None and "no add_evidence/against op" in problem


# --- the abstention is cheap, and is still made to name its subject -----------


def test_an_abstention_naming_nothing_is_recorded_as_naming_nothing() -> None:
    """Not dropped and not failed. "We required a subject and got none" is a
    countable fact about the stage; a blank would be indistinguishable from
    never having asked."""
    verdict = ChallengerVerdict(counter_arguments=[_counter("sle-01", outcome="nothing-on-file")])

    assert normalize_counter_arguments(verdict).counter_arguments[0].looked_for == UNNAMED_SEARCH


def test_a_missing_looked_for_does_not_fail_the_turn() -> None:
    """A contract violation on this node stops the turn and she gets no
    reply. A model that ignores a new prompt field — an older one, a degraded
    one — would take the chat down entirely."""
    diff = _diff(_hypothesis("sle-01"))
    verdict = normalize_counter_arguments(
        ChallengerVerdict(counter_arguments=[_counter("sle-01", outcome="nothing-on-file")])
    )

    assert _check(diff, verdict) is None


def test_an_alternative_naming_nothing_falls_back_to_the_abstention() -> None:
    verdict = ChallengerVerdict(counter_arguments=[_counter("sle-01", outcome="alternative")])

    fixed = normalize_counter_arguments(verdict)

    assert fixed.counter_arguments[0].outcome == "nothing-on-file"


def test_an_alternative_naming_one_survives() -> None:
    verdict = ChallengerVerdict(
        counter_arguments=[_counter("sle-01", outcome="alternative", alternative_id="mcas-01")]
    )

    assert normalize_counter_arguments(verdict).counter_arguments[0].outcome == "alternative"


def test_the_default_outcome_is_the_weakest_one() -> None:
    """A model that omits the field has not made a cited attack. Reading the
    omission as one would let the whole mechanism be satisfied by silence —
    which is how the board reached 33 leads with 15 carrying nothing against
    them."""
    assert CounterArgument(hypothesis_id="x", argument="y").outcome == "nothing-on-file"


def test_normalisation_never_invents_counter_evidence() -> None:
    """Every correction moves toward the weaker claim except the one read
    straight off an op. The failure mode must be a lead surviving that might
    have gone, never a lead retired on a citation that was not there."""
    verdict = ChallengerVerdict(
        counter_arguments=[
            _counter("a", outcome="cited"),
            _counter("b", outcome="alternative"),
            _counter("c", outcome="nothing-on-file"),
        ]
    )

    fixed = normalize_counter_arguments(verdict)

    assert [c.outcome for c in fixed.counter_arguments] == ["nothing-on-file"] * 3


# --- the stage normalises before anything reads it ----------------------------


def test_the_stage_normalises_after_stripping_unentailed_ops() -> None:
    """The entailment strip can remove the very `add_evidence` op a `cited`
    outcome rests on. Normalising first would bless a claim whose evidence was
    then deleted — the unbacked case, arrived at from the other direction."""
    import inspect

    from adoc.reason import stages as module

    source = inspect.getsource(module.challenger_stage)
    strip = source.rindex("stripped_ops")
    norm = source.index("normalize_counter_arguments")

    assert strip < norm


@pytest.mark.parametrize("outcome", ["cited", "nothing-on-file", "alternative"])
def test_every_outcome_is_reachable(outcome: str) -> None:
    """A literal member nothing can produce is a member that does not exist —
    the shape this codebase keeps finding (`monitoring`, `retired`)."""
    assert CounterArgument(hypothesis_id="x", argument="y", outcome=outcome).outcome == outcome  # type: ignore[arg-type]

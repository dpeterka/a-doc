"""Tests for adoc.reason.dag: the typed DAG runner and its code-enforced contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from adoc.reason.dag import (
    ContractViolation,
    Dag,
    Node,
    forbid_context_key,
    require_prior_node,
    run,
)


class Draft(BaseModel):
    text: str


class Challenge(BaseModel):
    counter_argument: str


class Composed(BaseModel):
    summary: str


def _draft_node(sentinel: list[str] | None = None) -> Node:
    def fn(ctx: object) -> BaseModel:
        if sentinel is not None:
            sentinel.append("draft")
        draft = ctx["initial_draft"]  # type: ignore[index]
        assert isinstance(draft, Draft)
        return Draft(text=draft.text.upper())

    return Node(
        name="draft",
        fn=fn,
        input_model=Draft,
        output_model=Draft,
        depends_on="initial_draft",
    )


def _challenger_node(sentinel: list[str] | None = None, *, fail: bool = False) -> Node:
    def fn(ctx: object) -> BaseModel:
        if sentinel is not None:
            sentinel.append("challenger")
        if fail:
            return Challenge(counter_argument="")
        return Challenge(counter_argument="have you considered X?")

    return Node(
        name="challenger",
        fn=fn,
        input_model=Draft,
        output_model=Challenge,
        depends_on="draft",
        postconditions=[
            _contract_nonempty_counter_argument(),
        ],
    )


def _contract_nonempty_counter_argument():  # noqa: ANN201 - local helper, type inferred by Contract
    from adoc.reason.dag import Contract

    def predicate(_ctx: object, value: object) -> str | None:
        assert isinstance(value, Challenge)
        if not value.counter_argument:
            return "challenger must produce a substantive counter-argument"
        return None

    return Contract(name="nonempty_counter_argument", predicate=predicate)


def _composer_node(sentinel: list[str] | None = None) -> Node:
    def fn(ctx: object) -> BaseModel:
        if sentinel is not None:
            sentinel.append("composer")
        return Composed(summary="done")

    return Node(
        name="composer",
        fn=fn,
        input_model=Challenge,
        output_model=Composed,
        depends_on="challenger",
        preconditions=[require_prior_node("challenger")],
    )


def test_happy_path_runs_all_nodes_in_order() -> None:
    sentinel: list[str] = []
    dag = Dag([_draft_node(sentinel), _challenger_node(sentinel), _composer_node(sentinel)])

    result = run(dag, {"initial_draft": Draft(text="hello")})

    assert sentinel == ["draft", "challenger", "composer"]
    assert [n.name for n in result.nodes] == ["draft", "challenger", "composer"]
    assert result.finished_at >= result.started_at


def test_input_model_validation_failure_stops_the_run() -> None:
    sentinel: list[str] = []
    mismatched_composer = Node(
        name="composer",
        fn=_composer_node(sentinel).fn,
        input_model=Challenge,  # "draft" node's output is a Draft, not a Challenge
        output_model=Composed,
        depends_on="draft",
    )
    dag = Dag([_draft_node(sentinel), mismatched_composer])

    with pytest.raises(ValidationError):
        run(dag, {"initial_draft": Draft(text="hello")})

    assert sentinel == ["draft"]  # composer's fn never ran


def test_missing_depends_on_key_raises_key_error() -> None:
    dag = Dag([_draft_node()])

    with pytest.raises(KeyError):
        run(dag, {})  # "initial_draft" is missing


def test_precondition_violation_stops_the_run_before_fn_executes() -> None:
    sentinel: list[str] = []
    # Composer requires "challenger" to have run; skip straight from draft to composer.
    draft = _draft_node(sentinel)
    composer = Node(
        name="composer",
        fn=_composer_node(sentinel).fn,
        input_model=Draft,
        output_model=Composed,
        depends_on="draft",
        preconditions=[require_prior_node("challenger")],
    )
    dag = Dag([draft, composer])

    with pytest.raises(ContractViolation) as excinfo:
        run(dag, {"initial_draft": Draft(text="hello")})

    assert excinfo.value.node == "composer"
    assert excinfo.value.contract_name == "require_prior_node:challenger"
    assert sentinel == ["draft"]  # composer's fn was never called


def test_postcondition_violation_stops_the_run_and_output_is_not_committed() -> None:
    sentinel: list[str] = []
    dag = Dag(
        [
            _draft_node(sentinel),
            _challenger_node(sentinel, fail=True),
            _composer_node(sentinel),
        ]
    )

    with pytest.raises(ContractViolation) as excinfo:
        run(dag, {"initial_draft": Draft(text="hello")})

    assert excinfo.value.node == "challenger"
    assert excinfo.value.contract_name == "nonempty_counter_argument"
    # challenger's fn ran (to produce the bad output that failed its own
    # postcondition), but composer downstream never executed.
    assert sentinel == ["draft", "challenger"]


def test_require_prior_node_passes_when_node_has_completed() -> None:
    contract = require_prior_node("challenger")

    violation = contract.check({"challenger": Challenge(counter_argument="x")}, None)

    assert violation is None


def test_forbid_context_key_blocks_when_key_present() -> None:
    contract = forbid_context_key("ledger")

    violation = contract.check({"ledger": Draft(text="should not be here")}, None)

    assert violation is not None
    assert "ledger" in violation


def test_forbid_context_key_passes_when_key_absent() -> None:
    contract = forbid_context_key("ledger")

    violation = contract.check({}, None)

    assert violation is None


def test_blind_reviewer_node_fails_when_ledger_present_in_context() -> None:
    def fn(ctx: object) -> BaseModel:
        return Composed(summary="de novo differential")

    blind_reviewer = Node(
        name="blind_reviewer",
        fn=fn,
        input_model=Draft,
        output_model=Composed,
        depends_on="initial_draft",
        preconditions=[forbid_context_key("ledger")],
    )
    dag = Dag([blind_reviewer])

    with pytest.raises(ContractViolation) as excinfo:
        run(
            dag,
            {
                "initial_draft": Draft(text="hello"),
                "ledger": Draft(text="the anchoring ledger, must be absent"),
            },
        )

    assert excinfo.value.contract_name == "forbid_context_key:ledger"


def test_run_record_hashes_are_stable_for_identical_inputs() -> None:
    dag_a = Dag([_draft_node()])
    dag_b = Dag([_draft_node()])

    result_a = run(dag_a, {"initial_draft": Draft(text="hello")})
    result_b = run(dag_b, {"initial_draft": Draft(text="hello")})

    assert result_a.nodes[0].input_hash == result_b.nodes[0].input_hash
    assert result_a.nodes[0].output_hash == result_b.nodes[0].output_hash

    result_c = run(Dag([_draft_node()]), {"initial_draft": Draft(text="different")})
    assert result_c.nodes[0].input_hash != result_a.nodes[0].input_hash


def test_dag_rejects_a_forward_dependency() -> None:
    draft = _draft_node()
    composer = Node(
        name="composer",
        fn=lambda ctx: Composed(summary="x"),
        input_model=Draft,
        output_model=Composed,
        depends_on="challenger",  # a node that comes after "composer" here
    )
    challenger = _challenger_node()

    with pytest.raises(ValueError, match="has not run yet"):
        Dag([draft, composer, challenger])


def test_to_jsonl_appends_one_line_per_run(tmp_path: Path) -> None:
    log_path = tmp_path / "dag-runs.jsonl"
    dag = Dag([_draft_node()])

    run(dag, {"initial_draft": Draft(text="hello")}).to_jsonl(log_path)
    run(dag, {"initial_draft": Draft(text="world")}).to_jsonl(log_path)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

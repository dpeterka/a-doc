"""Tests for adoc.reason.dag: the typed DAG runner and its code-enforced contracts."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from adoc.reason.dag import (
    ContractViolation,
    Ctx,
    Dag,
    Node,
    NodeFn,
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


def test_a_forward_declaration_is_reordered_not_rejected() -> None:
    """This replaces `test_dag_rejects_a_forward_dependency`. ADR 0043
    derives execution order by topological sort, so declaring a node before
    its prerequisite is no longer a mistake to catch — it is a list the
    runner puts in order. What IS still caught is a cycle, and a
    prerequisite that can never be satisfied."""
    draft = _draft_node()
    composer = Node(
        name="composer",
        fn=lambda ctx: Composed(summary="x"),
        input_model=Draft,
        output_model=Composed,
        depends_on="challenger",  # declared before "challenger" appears
    )
    challenger = _challenger_node()

    dag = Dag([draft, composer, challenger])

    assert [n.name for n in dag.execution_order] == ["draft", "challenger", "composer"]


def test_a_cycle_is_refused_at_construction() -> None:
    """A list could not express a cycle without one node forward-referencing
    the other, which the old check caught by accident. A derived order has
    to catch it on purpose."""
    a = Node(
        name="a",
        fn=lambda ctx: Draft(text="x"),
        input_model=Draft,
        output_model=Draft,
        depends_on="b",
    )
    b = Node(
        name="b",
        fn=lambda ctx: Draft(text="y"),
        input_model=Draft,
        output_model=Draft,
        depends_on="a",
    )

    with pytest.raises(ValueError, match="cycle"):
        Dag([a, b])


def test_an_unsatisfiable_prerequisite_fails_before_anything_runs() -> None:
    """Previously a bad name surfaced as a `KeyError` from whichever node
    reached it first — after earlier nodes had already run, and in the review
    DAG those nodes write the ledger and cost frontier calls."""
    ran: list[str] = []

    def _mark(name: str) -> NodeFn:
        def fn(_ctx: Ctx) -> BaseModel:
            ran.append(name)
            return Draft(text=name)

        return fn

    first = Node(
        name="first",
        fn=_mark("first"),
        input_model=Draft,
        output_model=Draft,
        depends_on="initial_draft",
    )
    broken = Node(
        name="broken",
        fn=_mark("broken"),
        input_model=Draft,
        output_model=Draft,
        depends_on="typo_nobody_supplies",
    )

    with pytest.raises(KeyError, match="neither a node nor an initial key"):
        run(Dag([first, broken]), {"initial_draft": Draft(text="seed")})

    assert ran == []


def test_to_jsonl_appends_one_line_per_run(tmp_path: Path) -> None:
    log_path = tmp_path / "dag-runs.jsonl"
    dag = Dag([_draft_node()])

    run(dag, {"initial_draft": Draft(text="hello")}).to_jsonl(log_path)
    run(dag, {"initial_draft": Draft(text="world")}).to_jsonl(log_path)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


# --- ADR 0043: declared reads, orderings, and parallel batches -----------------------------------


class Tally(BaseModel):
    who: str = ""


def _tally_node(
    name: str,
    depends_on: object,
    log: list[str],
    *,
    after: tuple[str, ...] = (),
    group: str | None = None,
    delay: float = 0.0,
) -> Node:
    def fn(_ctx: Ctx) -> BaseModel:
        if delay:
            time.sleep(delay)
        log.append(name)
        return Tally(who=name)

    return Node(
        name=name,
        fn=fn,
        input_model=Tally,
        output_model=Tally,
        depends_on=depends_on,  # type: ignore[arg-type]
        after=after,
        parallel_group=group,
    )


def test_a_declared_read_must_be_present_before_the_node_runs() -> None:
    """The point of declaring more than one read: all of them are checked,
    not just the payload-supplying edge."""
    log: list[str] = []
    node = _tally_node("consumer", ("seed", "never_produced"), log)

    with pytest.raises(KeyError, match="neither a node nor an initial key"):
        run(Dag([node]), {"seed": Tally()})

    assert log == []


def test_after_orders_without_claiming_a_read() -> None:
    """`staleness_scan` reads a file `retirement_pass` appends to. The
    ordering is real; the data edge is not."""
    log: list[str] = []
    dag = Dag(
        [
            _tally_node("late", "seed", log, after=("early",)),
            _tally_node("early", "seed", log),
        ]
    )

    run(dag, {"seed": Tally()})

    assert log == ["early", "late"]
    assert dag._by_name["late"].depends_on == ("seed",)


def test_a_parallel_batch_runs_concurrently() -> None:
    """Three sleeps of 0.3s take ~0.3s wall clock, not ~0.9s."""
    log: list[str] = []
    dag = Dag([_tally_node(f"member_{i}", "seed", log, group="panel", delay=0.3) for i in range(3)])

    start = time.monotonic()
    result = run(dag, {"seed": Tally()})
    elapsed = time.monotonic() - start

    assert elapsed < 0.75, f"{elapsed:.2f}s — the batch did not run concurrently"
    assert len(result.nodes) == 3
    assert {r.parallel_group for r in result.nodes} == {"panel"}


def test_a_parallel_batch_commits_in_declaration_order() -> None:
    """Members finish in whatever order they finish; the audit record and
    the context must not depend on it."""
    log: list[str] = []
    dag = Dag(
        [
            _tally_node("slow", "seed", log, group="panel", delay=0.25),
            _tally_node("fast", "seed", log, group="panel"),
        ]
    )

    result = run(dag, {"seed": Tally()})

    assert log == ["fast", "slow"]  # completion order
    assert [r.name for r in result.nodes] == ["slow", "fast"]  # declaration order


def test_a_batch_member_cannot_see_a_siblings_output() -> None:
    """One context snapshot for the whole batch, so the outcome cannot
    depend on which member completed first."""
    seen: dict[str, list[str]] = {}

    def make(name: str, delay: float) -> Node:
        def fn(ctx: Ctx) -> BaseModel:
            time.sleep(delay)
            seen[name] = sorted(ctx)
            return Tally(who=name)

        return Node(
            name=name,
            fn=fn,
            input_model=Tally,
            output_model=Tally,
            depends_on="seed",
            parallel_group="panel",
        )

    run(Dag([make("slow", 0.25), make("fast", 0.0)]), {"seed": Tally()})

    assert seen["slow"] == ["seed"]
    assert seen["fast"] == ["seed"]


def test_a_failing_batch_reports_the_same_member_every_time() -> None:
    """Two members raising: the reported one must be chosen by declaration
    order, not by which thread lost the race."""

    def boom(name: str, delay: float) -> Node:
        def fn(_ctx: Ctx) -> BaseModel:
            time.sleep(delay)
            raise ContractViolation(name, f"contract_{name}", "nope")

        return Node(
            name=name,
            fn=fn,
            input_model=Tally,
            output_model=Tally,
            depends_on="seed",
            parallel_group="panel",
        )

    for _ in range(3):
        with pytest.raises(ContractViolation) as excinfo:
            run(Dag([boom("first", 0.15), boom("second", 0.0)]), {"seed": Tally()})
        assert excinfo.value.node == "first"


def test_a_group_whose_members_are_not_ready_together_is_refused() -> None:
    """Members at different depths are not a group. Running them together
    would discard an ordering one of them asked for."""
    log: list[str] = []

    with pytest.raises(ValueError, match="different prerequisites"):
        Dag(
            [
                _tally_node("gate", "seed", log),
                _tally_node("a", "seed", log, group="panel"),
                # Ready only after `gate`, so it was never ready with `a`.
                _tally_node("b", "seed", log, after=("gate",), group="panel"),
            ]
        )


def test_a_group_member_ordered_against_a_sibling_is_refused() -> None:
    """`after` naming a sibling is the same mistake as depending on one:
    the ordering cannot be honoured for a node running beside it."""
    log: list[str] = []

    with pytest.raises(ValueError, match="a sequence, not a group"):
        Dag(
            [
                _tally_node("a", "seed", log, group="panel"),
                _tally_node("b", "seed", log, after=("a",), group="panel"),
            ]
        )


def test_a_group_member_may_not_depend_on_another_member() -> None:
    log: list[str] = []

    with pytest.raises(ValueError, match="a sequence, not a group"):
        Dag(
            [
                _tally_node("a", "seed", log, group="panel"),
                _tally_node("b", "a", log, group="panel"),
            ]
        )


def test_the_audit_record_carries_the_declared_graph() -> None:
    """A replay should be able to see the graph the run actually had."""
    log: list[str] = []
    dag = Dag(
        [
            _tally_node("early", "seed", log),
            _tally_node("late", ("seed", "early"), log, after=("early",)),
        ]
    )

    result = run(dag, {"seed": Tally()})
    late = next(r for r in result.nodes if r.name == "late")

    assert late.depends_on == ["seed", "early"]
    assert late.after == ["early"]


def test_preparing_a_node_against_an_incomplete_context_is_refused() -> None:
    """`run` validates prerequisites up front and the topological order
    guarantees they are committed, so `_prepare`'s own check cannot fire
    inside a run. This covers it directly rather than leaving a check
    nothing exercises — which is the shape of every silent-absence bug in
    this repository."""
    from adoc.reason.dag import _prepare

    log: list[str] = []
    node = _tally_node("consumer", ("seed", "other"), log)

    with pytest.raises(KeyError, match="missing from the run context"):
        _prepare(node, {"seed": Tally()}, "runid")


def test_a_batch_output_is_not_committed_until_the_batch_finishes() -> None:
    """The mechanism behind batch isolation. If a member's output landed in
    the context as soon as it returned, a slower sibling could read it and
    the batch would become a race."""
    observed: list[list[str]] = []

    def make(name: str, delay: float) -> Node:
        def fn(ctx: Ctx) -> BaseModel:
            time.sleep(delay)
            observed.append(sorted(k for k in ctx if k.startswith("member")))
            return Tally(who=name)

        return Node(
            name=name,
            fn=fn,
            input_model=Tally,
            output_model=Tally,
            depends_on="seed",
            parallel_group="panel",
        )

    run(Dag([make("member_slow", 0.25), make("member_fast", 0.0)]), {"seed": Tally()})

    # Neither member saw any member output, whichever finished first.
    assert observed == [[], []]

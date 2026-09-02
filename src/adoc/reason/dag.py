"""Typed DAG runner with code-enforced contracts (ADR 0002).

Each reasoning loop (document-ingest, chat-diagnostic, weekly review) is
built as an explicit `Dag` of `Node`s. Edges carry Pydantic-validated
artifacts; every node declares pre/postcondition `Contract`s that are
enforced by this module, not suggested by a prompt. A contract violation
raises `ContractViolation` and the run stops immediately — nothing
downstream executes. Every node execution is logged with input/output
hashes via `DagRun.to_jsonl` for audit/replay.

This module deliberately has no dependency on any agent framework — it is
the ~200-line runner ADR 0002 calls for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# The accumulated run context: node name (or an `initial` key) -> its
# validated output. Passed in full to every node's `fn` and to every
# contract predicate, so contracts like `forbid_context_key` can inspect
# what has (or hasn't) been produced so far.
Ctx = Mapping[str, BaseModel]

NodeFn = Callable[[Ctx], BaseModel]

# A contract predicate receives the full run context plus the specific
# value being checked (the validated edge payload for a precondition, the
# validated node output for a postcondition) and returns a violation
# message, or `None` if the contract is satisfied.
Predicate = Callable[[Ctx, BaseModel | None], str | None]


class ContractViolation(Exception):
    """Raised when a node's precondition or postcondition fails.

    Carries the failing node name, contract name, and message so callers
    (and tests) can assert on *why* a run stopped, not just that it did.
    """

    def __init__(self, node: str, contract_name: str, message: str) -> None:
        super().__init__(f"node {node!r}: contract {contract_name!r} violated: {message}")
        self.node = node
        self.contract_name = contract_name
        self.message = message


class Contract:
    """A named predicate over `(ctx, value)`."""

    def __init__(self, name: str, predicate: Predicate) -> None:
        self.name = name
        self._predicate = predicate

    def check(self, ctx: Ctx, value: BaseModel | None) -> str | None:
        return self._predicate(ctx, value)


class Node:
    """One stage of a `Dag`.

    Three separate things a node can declare about its place in the graph
    (ADR 0043), because collapsing them into one string is what made the
    declared graph disagree with the real one:

    `depends_on` — the context entries this node **reads**. The FIRST is the
    primary edge: it supplies the input payload validated against
    `input_model` and handed to preconditions, which is the pre-ADR-0043
    behaviour of the single string this used to be. The rest are declared
    reads: they must exist in the context before `fn` runs, and they are
    recorded in the audit trail. `fn` still receives the *whole* context —
    see `forbid_context_key` for why that is deliberate and not an
    oversight.

    `after` — nodes this one must run **after** without reading anything
    from them. Not a formality: `staleness_scan` reads
    `ledger-history.jsonl`, which `retirement_pass` appends to, so the
    ordering is real while the data dependency is not. Expressing that as a
    fake `depends_on` was what made eight of the review's twenty nodes look
    like they read things they never touch.

    `parallel_group` — an opt-in label. Nodes sharing a group that become
    ready at the same point run concurrently, all against ONE context
    snapshot, so the group's outcome cannot depend on completion order. A
    group member may not declare `after`, and every member must declare the
    same reads; both are refused at construction.
    """

    def __init__(
        self,
        name: str,
        fn: NodeFn,
        *,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        depends_on: str | Sequence[str],
        after: Sequence[str] = (),
        parallel_group: str | None = None,
        preconditions: Sequence[Contract] = (),
        postconditions: Sequence[Contract] = (),
    ) -> None:
        declared = (depends_on,) if isinstance(depends_on, str) else tuple(depends_on)
        if not declared:
            raise ValueError(f"node {name!r} declares no dependency")
        seen: set[str] = set()
        for dep in declared:
            if dep in seen:
                raise ValueError(f"node {name!r} declares {dep!r} twice")
            seen.add(dep)
        self.name = name
        self.fn = fn
        self.input_model = input_model
        self.output_model = output_model
        self.depends_on: tuple[str, ...] = declared
        self.after: tuple[str, ...] = tuple(after)
        self.parallel_group = parallel_group
        self.preconditions = tuple(preconditions)
        self.postconditions = tuple(postconditions)

    @property
    def primary_dependency(self) -> str:
        """The edge validated against `input_model` and passed to
        preconditions. First-declared wins, so adding a declared read never
        silently moves which payload a contract inspects."""
        return self.depends_on[0]

    @property
    def prerequisites(self) -> tuple[str, ...]:
        """Everything that must have run first — reads and orderings alike."""
        return self.depends_on + self.after


class Dag:
    """An explicit graph of `Node`s, executed in a derived order.

    Execution order comes from a **stable topological sort** (ADR 0043), not
    from list position: prerequisites first, and among nodes that are equally
    ready, declaration order decides. For a list that was already a valid
    topological order — every `Dag` in this codebase — that reproduces the
    previous order exactly, which is the property that made this change safe
    to make at all. `execution_order` is pinned by a test for both real DAGs.

    Deriving the order buys three things the list could not give:

    - **Cycles are refused at construction** instead of deadlocking or
      silently reading a stale entry.
    - **Every declared prerequisite is checked**, not just the first, so a
      node cannot claim a read it does not have.
    - **Independent nodes are visibly independent**, which is what makes
      `parallel_group` expressible without hand-reordering anything.

    A prerequisite that is not any node's name is assumed to be a key the
    caller supplies in `run`'s `initial` dict, and is checked at run time.
    """

    def __init__(self, nodes: Sequence[Node]) -> None:
        seen: set[str] = set()
        for node in nodes:
            if node.name in seen:
                raise ValueError(f"duplicate node name {node.name!r}")
            seen.add(node.name)
        self.nodes: tuple[Node, ...] = tuple(nodes)
        self._by_name = {n.name: n for n in nodes}
        self._check_parallel_groups()
        self.execution_order: tuple[Node, ...] = self._topological_order()

    def _check_parallel_groups(self) -> None:
        """Refuse a parallel group that cannot be run deterministically.

        A group whose members declare different reads is not a group — the
        members are at different depths and would not have been ready
        together anyway. A member with an `after` edge is asserting an
        ordering against something, and running it beside a sibling
        discards that assertion. Both are construction-time errors rather
        than a runtime surprise in the one part of the system where order is
        a safety property (CLAUDE.md rule 3).
        """
        groups: dict[str, list[Node]] = {}
        for node in self.nodes:
            if node.parallel_group is not None:
                groups.setdefault(node.parallel_group, []).append(node)
        for label, members in groups.items():
            # Most specific first, so the error names the actual mistake
            # rather than the symptom it produces.
            names = {m.name for m in members}
            for member in members:
                inside = names & set(member.prerequisites)
                if inside:
                    raise ValueError(
                        f"parallel group {label!r}: {member.name!r} depends on "
                        f"{sorted(inside)}, which is in the same group — that is a "
                        "sequence, not a group"
                    )
            shapes = {(frozenset(m.depends_on), frozenset(m.after)) for m in members}
            if len(shapes) > 1:
                raise ValueError(
                    f"parallel group {label!r} has members with different prerequisites "
                    f"({sorted(sorted(reads | after) for reads, after in shapes)}); they "
                    "would not become ready together, so running them together would "
                    "discard an ordering one of them declared"
                )

    def _topological_order(self) -> tuple[Node, ...]:
        """Topological order, taking the EARLIEST-DECLARED ready node each
        step.

        One node at a time, not a wave of all currently-ready nodes. That
        distinction is the whole safety argument: taking waves emits
        `A, C, B` for `[A, B(depends on A), C]`, reordering a list that was
        already a valid topological order. Taking the first ready node
        reproduces the declaration order exactly whenever the declaration
        order is itself valid — at each step the earliest remaining node has
        all its prerequisites among nodes already emitted, so it is ready and
        is chosen.

        Only prerequisites naming a node in THIS dag constrain the order;
        anything else is an `initial` key, already present before the first
        node runs.
        """
        pending = {n.name: {d for d in n.prerequisites if d in self._by_name} for n in self.nodes}
        remaining = list(self.nodes)
        done: set[str] = set()
        ordered: list[Node] = []
        while remaining:
            nxt = next((n for n in remaining if pending[n.name] <= done), None)
            if nxt is None:
                stuck = sorted(n.name for n in remaining)
                raise ValueError(
                    "dag has a dependency cycle (or a prerequisite naming a node that "
                    f"cannot run): {stuck}"
                )
            ordered.append(nxt)
            done.add(nxt.name)
            remaining.remove(nxt)
        return tuple(ordered)


def _validate_edge(payload: Any, model: type[BaseModel]) -> BaseModel:
    """Validate a context payload against a node's input/output model.

    Handles three shapes: an already-correct instance (pass through), a
    differently-typed `BaseModel` (re-validate via its dumped dict), or a
    plain dict/mapping (validate directly). This is where a bad edge payload
    raises `pydantic.ValidationError`, which propagates and stops the run.
    """
    if isinstance(payload, model):
        return payload
    if isinstance(payload, BaseModel):
        return model.model_validate(payload.model_dump())
    return model.model_validate(payload)


def _canonical_json(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _hash_model(model: BaseModel) -> str:
    return hashlib.sha256(_canonical_json(model).encode("utf-8")).hexdigest()


# A `DagRun` is only constructed once every node has finished, and it is
# only written where a caller chooses to persist it — so while a run is in
# flight it explains nothing. These log lines are the live view: which node
# is executing, how long each took, and which contract stopped a run.
logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class NodeRecord(BaseModel):
    """Audit record for one executed node."""

    name: str
    started_at: str
    finished_at: str
    input_hash: str
    output_hash: str
    preconditions_checked: list[str]
    postconditions_checked: list[str]
    depends_on: list[str] = []
    """Every context entry this node declared it reads (ADR 0043), not only
    the primary edge — so a replay can see the graph the run actually had."""
    after: list[str] = []
    parallel_group: str | None = None


class DagRun(BaseModel):
    """Audit record for one full `run()` — a list of `NodeRecord`s.

    Only recorded for nodes that completed successfully; a `ContractViolation`
    (or a `pydantic.ValidationError` from a bad edge payload) propagates out
    of `run()` before a `DagRun` is constructed, so the caller's exception
    handler is the place to log *why* a run stopped, not `DagRun` itself.
    """

    run_id: str
    started_at: str
    finished_at: str
    nodes: list[NodeRecord]

    def to_jsonl(self, path: Path) -> None:
        """Append this run record as one JSON line for audit/replay."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(self.model_dump_json() + "\n")


def _batches(order: Sequence[Node]) -> list[list[Node]]:
    """Split an execution order into units of work.

    A maximal *contiguous* run of nodes sharing a `parallel_group` becomes
    one batch; everything else is a batch of one. Contiguity is the whole
    rule: a group whose members are separated in the order by an unrelated
    node runs as two batches, which is correct and merely less parallel. No
    node is ever moved to make a batch bigger — that would be reordering the
    graph to chase throughput, in the one system where order is a safety
    property.
    """
    batches: list[list[Node]] = []
    for node in order:
        if (
            node.parallel_group is not None
            and batches
            and batches[-1][-1].parallel_group == node.parallel_group
        ):
            batches[-1].append(node)
        else:
            batches.append([node])
    return batches


def _prepare(node: Node, ctx: Ctx, run_id: str) -> BaseModel:
    """Resolve, validate and precondition-check one node's input.

    Split out from `run` so a parallel batch's members are prepared against
    exactly the same context object every sequential node is.
    """
    # `run` has already proved every prerequisite is satisfiable, and the
    # topological order guarantees each has been committed by now — so
    # within a `run` this cannot fire. It is the local invariant for a
    # helper that takes someone else's `ctx`, and it is covered by its own
    # test rather than left as a check nothing exercises.
    for dep in node.prerequisites:
        if dep not in ctx:
            raise KeyError(
                f"node {node.name!r} depends on {dep!r}, which is missing from the run context"
            )
    validated_input = _validate_edge(ctx[node.primary_dependency], node.input_model)
    for contract in node.preconditions:
        violation = contract.check(ctx, validated_input)
        if violation is not None:
            # The contract NAME is logged, never `violation` — a violation
            # message can quote the offending span of a patient-facing
            # reply (see `web.routes.chat`'s note on the same rule).
            logger.warning(
                "dag %s: node %r stopped by precondition %r", run_id[:8], node.name, contract.name
            )
            raise ContractViolation(node.name, contract.name, violation)
    return validated_input


def _finish(node: Node, ctx: Ctx, output: BaseModel, run_id: str, clock: float) -> BaseModel:
    """Validate a node's output and run its postconditions."""
    validated_output = _validate_edge(output, node.output_model)
    for contract in node.postconditions:
        violation = contract.check(ctx, validated_output)
        if violation is not None:
            logger.warning(
                "dag %s: node %r stopped by postcondition %r after %.1fs",
                run_id[:8],
                node.name,
                contract.name,
                time.monotonic() - clock,
            )
            raise ContractViolation(node.name, contract.name, violation)
    return validated_output


def run(dag: Dag, initial: dict[str, BaseModel]) -> DagRun:
    """Execute `dag` in its derived topological order over `initial` context.

    For each node: resolve and validate its primary edge against
    `input_model`, check that every declared prerequisite is present,
    evaluate preconditions, call `fn`, validate the result against
    `output_model`, evaluate postconditions, then commit the output into the
    context under the node's name. Any contract violation raises
    `ContractViolation` immediately — the run stops and nothing downstream
    executes.

    A contiguous batch of nodes sharing a `parallel_group` runs concurrently
    (ADR 0043). Every member is prepared and executed against **one context
    snapshot** taken before the batch starts, and outputs are committed only
    once the whole batch has finished, so a batch's result cannot depend on
    which member completed first. Postconditions likewise see the snapshot,
    not siblings' outputs. If any member raises, the others are allowed to
    finish and the first exception in DECLARATION order is re-raised — so a
    failing batch reports the same violation every time, rather than
    whichever thread happened to lose the race.
    """
    ctx: dict[str, BaseModel] = dict(initial)

    # Every prerequisite must be satisfiable before anything executes.
    # Previously an unresolvable name surfaced as a `KeyError` from
    # whichever node reached it first — after the nodes before it had
    # already run, and in this DAG those nodes write the ledger and cost
    # frontier calls. A typo should not cost a partial review (ADR 0043).
    known = {n.name for n in dag.nodes} | set(ctx)
    missing = {
        f"{n.name} -> {dep}" for n in dag.nodes for dep in n.prerequisites if dep not in known
    }
    if missing:
        raise KeyError(
            "dag cannot run: prerequisite(s) name neither a node nor an initial key: "
            + ", ".join(sorted(missing))
        )

    node_records: list[NodeRecord] = []
    run_started = _now_iso()
    # Generated up front rather than at `DagRun` construction so every log
    # line below can be tied back to the run record this call returns.
    run_id = uuid.uuid4().hex
    total = len(dag.nodes)
    logger.info("dag %s: starting, %d node(s): %s", run_id[:8], total, [n.name for n in dag.nodes])
    run_clock = time.monotonic()

    index = 0
    for batch in _batches(dag.execution_order):
        # Batch isolation — no member may observe a sibling — rests on TWO
        # independent mechanisms, and either one alone is sufficient:
        # outputs are committed only after the batch (the commit loop
        # below), and members read a snapshot rather than the live context.
        # Measured: removing either alone breaks no test; removing both
        # fails the isolation tests every time. Kept deliberately, because
        # the one that survives a future refactor is not knowable now.
        snapshot: Ctx = dict(ctx)
        committed: list[tuple[Node, NodeRecord, BaseModel]] = []

        def _execute(node: Node, snapshot: Ctx = snapshot) -> tuple[NodeRecord, BaseModel]:
            started = _now_iso()
            node_clock = time.monotonic()
            logger.info(
                "dag %s: node %r starting%s",
                run_id[:8],
                node.name,
                f" (parallel group {node.parallel_group!r})" if node.parallel_group else "",
            )
            validated_input = _prepare(node, snapshot, run_id)
            output = node.fn(snapshot)
            validated_output = _finish(node, snapshot, output, run_id, node_clock)
            logger.info(
                "dag %s: node %r ok in %.1fs",
                run_id[:8],
                node.name,
                time.monotonic() - node_clock,
            )
            record = NodeRecord(
                name=node.name,
                started_at=started,
                finished_at=_now_iso(),
                input_hash=_hash_model(validated_input),
                output_hash=_hash_model(validated_output),
                preconditions_checked=[c.name for c in node.preconditions],
                postconditions_checked=[c.name for c in node.postconditions],
                depends_on=list(node.depends_on),
                after=list(node.after),
                parallel_group=node.parallel_group,
            )
            return record, validated_output

        if len(batch) == 1:
            record, value = _execute(batch[0])
            committed.append((batch[0], record, value))
        else:
            logger.info(
                "dag %s: running %d nodes concurrently in group %r",
                run_id[:8],
                len(batch),
                batch[0].parallel_group,
            )
            results_by_name: dict[str, tuple[NodeRecord, BaseModel]] = {}
            errors: dict[str, BaseException] = {}
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = {pool.submit(_execute, node): node for node in batch}
                for future, node in futures.items():
                    try:
                        results_by_name[node.name] = future.result()
                    except BaseException as exc:  # noqa: BLE001 - re-raised below
                        errors[node.name] = exc
            if errors:
                # Declaration order, not completion order, so a failing
                # batch reports the same violation on every run instead of
                # whichever thread happened to lose the race.
                first = next(n.name for n in batch if n.name in errors)
                raise errors[first]
            for node in batch:
                record, value = results_by_name[node.name]
                committed.append((node, record, value))

        # Committed only once the whole batch has finished, so no member can
        # see a sibling's output — not through `fn`, not through a contract.
        for node, record, value in committed:
            index += 1
            ctx[node.name] = value
            node_records.append(record)

    logger.info("dag %s: complete in %.1fs", run_id[:8], time.monotonic() - run_clock)
    return DagRun(
        run_id=run_id,
        started_at=run_started,
        finished_at=_now_iso(),
        nodes=node_records,
    )


def require_prior_node(name: str) -> Contract:
    """Precondition: node `name` must have completed earlier in this run.

    Used, e.g., as the Composer's precondition that a Challenger node
    completed successfully earlier in the run (ADR 0002).
    """

    def predicate(ctx: Ctx, _value: BaseModel | None) -> str | None:
        if name not in ctx:
            return f"required prior node {name!r} has not completed in this run"
        return None

    return Contract(name=f"require_prior_node:{name}", predicate=predicate)


def forbid_context_key(key: str) -> Contract:
    """Precondition: context key `key` must be absent from the RUN CONTEXT
    (the `ctx` dict `run()` threads through every node — see `Ctx` above),
    not from any particular node's own input payload.

    This is a check on the run's overall history: it catches a caller that
    literally puts a `"ledger"` entry into `run()`'s `initial` dict (or
    that a prior node produced one under that name). It does NOT inspect
    the *content* of whatever payload a node actually receives — a node
    whose own input payload happens to carry a ledger section under some
    other structure (e.g. a `ContextPack` built with `include_ledger=True`
    and passed under a different context key, such as
    `"blind_context_pack"`) sails past this check untouched, because that
    payload's ledger content was never a `ctx["ledger"]` entry in the first
    place — see `edge_payload_lacks_section` below, which is the
    content-aware check for that case (needed because `reason.review`'s
    blind-panel wiring passes ledger content this way).

    Kept for this original, narrower purpose (and to avoid weakening its
    existing tests): a defense-in-depth check that nothing ever smuggles a
    `"ledger"` key into a blind node's run context directly.
    """

    def predicate(ctx: Ctx, _value: BaseModel | None) -> str | None:
        if key in ctx:
            return f"context key {key!r} must be absent (blind-reviewer rule)"
        return None

    return Contract(name=f"forbid_context_key:{key}", predicate=predicate)


def edge_payload_lacks_section(section_key: str) -> Contract:
    """Precondition: the node's own VALIDATED INPUT payload must not expose
    `section_key` among its `.keys` — a content-aware alternative to
    `forbid_context_key` for nodes whose blindness must be verified against
    what they were actually handed, not against the run-context dict.

    Needed because the weekly review's blind-panel nodes are wired with
    `depends_on="blind_context_pack"`, so their real input is a
    `ContextPack`, never a `ctx["ledger"]` entry (ADR 0002 amendment).
    `forbid_context_key("ledger")` alone has nothing to catch even if the
    pack itself were built with `include_ledger=True` — the ledger section
    would sit right in the pack the model reads, and that contract would
    still pass. This contract inspects the validated payload directly: for
    any `value`
    exposing a `.keys` collection (e.g. `reason.context.ContextPack.keys`),
    it fails when `section_key` is present in it. A payload with no `.keys`
    attribute (most `Node` input models) is not this contract's concern and
    always passes.
    """

    def predicate(_ctx: Ctx, value: BaseModel | None) -> str | None:
        if value is None:
            return None
        keys = getattr(value, "keys", None)
        if keys is None:
            return None
        try:
            present = section_key in keys
        except TypeError:
            return None
        if present:
            return f"payload section {section_key!r} must be absent (blind-reviewer rule)"
        return None

    return Contract(name=f"edge_payload_lacks_section:{section_key}", predicate=predicate)

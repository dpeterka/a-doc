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
import uuid
from collections.abc import Callable, Mapping, Sequence
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

    `depends_on` names the single upstream context entry (a prior node's
    name, or a key in the `initial` dict passed to `run`) that supplies this
    node's input payload; it is validated against `input_model` before `fn`
    runs. `fn` receives the *full* context (not just its own input) so a
    stage can reference multiple upstream artifacts when it needs to, but
    only `depends_on`'s entry is contract-checked as this node's edge.
    """

    def __init__(
        self,
        name: str,
        fn: NodeFn,
        *,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        depends_on: str,
        preconditions: Sequence[Contract] = (),
        postconditions: Sequence[Contract] = (),
    ) -> None:
        self.name = name
        self.fn = fn
        self.input_model = input_model
        self.output_model = output_model
        self.depends_on = depends_on
        self.preconditions = tuple(preconditions)
        self.postconditions = tuple(postconditions)


class Dag:
    """An ordered, explicit list of `Node`s.

    Ordering is the execution order. A node whose `depends_on` names another
    node in this `Dag` must come after it — that dependency is validated at
    construction time. A `depends_on` that isn't any node's name is assumed
    to be a key the caller will supply in `run`'s `initial` dict, and is
    checked at run time instead.
    """

    def __init__(self, nodes: Sequence[Node]) -> None:
        all_names = {n.name for n in nodes}
        seen: set[str] = set()
        for node in nodes:
            if node.name in seen:
                raise ValueError(f"duplicate node name {node.name!r}")
            # If `depends_on` names another node in this dag, it must appear
            # earlier in the sequence. If it isn't any node's name at all,
            # it's assumed to be an `initial` key supplied to `run` — that
            # can only be checked at run time.
            if node.depends_on in all_names and node.depends_on not in seen:
                raise ValueError(
                    f"node {node.name!r} depends on {node.depends_on!r}, "
                    "which has not run yet at that point in the sequence"
                )
            seen.add(node.name)
        self.nodes: tuple[Node, ...] = tuple(nodes)


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


def run(dag: Dag, initial: dict[str, BaseModel]) -> DagRun:
    """Execute `dag` sequentially over `initial` context.

    For each node: resolve and validate its edge payload against
    `input_model`, evaluate preconditions, call `fn`, validate the result
    against `output_model`, evaluate postconditions, then commit the output
    into the context under the node's name. Any contract violation raises
    `ContractViolation` immediately — the run stops and nothing downstream
    executes.
    """
    ctx: dict[str, BaseModel] = dict(initial)
    node_records: list[NodeRecord] = []
    run_started = _now_iso()

    for node in dag.nodes:
        started = _now_iso()

        if node.depends_on not in ctx:
            raise KeyError(
                f"node {node.name!r} depends on {node.depends_on!r}, "
                "which is missing from the run context"
            )
        edge_payload = ctx[node.depends_on]
        validated_input = _validate_edge(edge_payload, node.input_model)
        input_hash = _hash_model(validated_input)

        for contract in node.preconditions:
            violation = contract.check(ctx, validated_input)
            if violation is not None:
                raise ContractViolation(node.name, contract.name, violation)

        output = node.fn(ctx)
        validated_output = _validate_edge(output, node.output_model)
        output_hash = _hash_model(validated_output)

        for contract in node.postconditions:
            violation = contract.check(ctx, validated_output)
            if violation is not None:
                raise ContractViolation(node.name, contract.name, violation)

        ctx[node.name] = validated_output
        node_records.append(
            NodeRecord(
                name=node.name,
                started_at=started,
                finished_at=_now_iso(),
                input_hash=input_hash,
                output_hash=output_hash,
                preconditions_checked=[c.name for c in node.preconditions],
                postconditions_checked=[c.name for c in node.postconditions],
            )
        )

    return DagRun(
        run_id=uuid.uuid4().hex,
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

# 0002. Reasoning pipeline as a typed DAG with code-enforced contracts

Status: Accepted

## Context

If stage order and safety behavior live only in a prompt, a future prompt
edit (or a model that decides to skip a step) can silently let
un-challenged, un-validated output reach the ledger or the patient. This
is exactly the anchoring/over-trust risk the whole architecture exists to
avoid (see PLAN.md "Anti-anchoring" and "Key risks" #2-3). A framework like
a general agent SDK would let the model itself decide when to call which
tool in which order — but this project needs the *opposite* guarantee:
stage order and inter-stage validation must be true regardless of what the
model outputs.

## Decision

Each reasoning loop (document-ingest, chat-diagnostic, weekly review) is an
explicit typed DAG with its own small (~200-line) runner in
`reason/dag.py` — no general agent framework. Nodes are stages
(Ledger-Maintainer, Challenger, Test-Chooser, Composer, blind-panel
members, ...). Edges carry Pydantic-validated artifacts. Every node
declares pre/postcondition contracts that are enforced by plain code, not
suggested by a prompt:

- The Challenger's postcondition requires at least one substantive
  counter-argument per `most-likely` hypothesis, or the run fails.
- The Blind-Reviewer node's precondition asserts the differential ledger is
  absent from its context pack (it must produce a de novo differential).
- The Composer's precondition asserts a Challenger node completed
  successfully earlier in this run.

Every node execution is logged with input/output hashes, making every run
replayable and testable node-by-node in isolation.

## Consequences

- A run with a skipped or failed Challenger node must fail outright — this
  is a required CI test (PLAN.md Phase 1 acceptance criteria) and can never
  be weakened to make an unrelated change pass (CLAUDE.md rule 2).
- New reasoning loops require writing an explicit DAG and contracts, which
  is more upfront code than a single long prompt, but keeps stage-skipping
  structurally impossible rather than merely discouraged.
- Because contracts are just code with clear pre/postconditions, they are
  unit-testable independent of any live model call, using golden fixtures.

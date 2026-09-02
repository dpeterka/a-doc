# ADR 0043 — The declared graph is the real graph

Status: accepted (2026-09-02)

Closes DEV-01. Implements the three fixes `docs/dag-topology.md` recorded
when the review DAG was first drawn, and corrects one thing that document
got wrong.

## Context

`docs/dag-topology.md` measured three structural problems:

1. **`depends_on` was one string, but nodes read many entries.** Eight of the
   review's twenty nodes read context they never declared. `render_report`
   declared `ops_metrics` and read ten nodes.
2. **Execution was list order**, so the parallelism visible in the diagram
   was decorative: three blind-panel members at `xhigh` effort, genuinely
   independent, ran one after another, and so did LIRICAL (76.9s) and
   sem-sim.
3. **A chain of twenty where the real dependency graph is much shallower.**
   `current_ledger` declared the *last* panel member, so the graph's shape
   depended on how many `models.yaml` configures.

The consequence was not a runtime bug. It was that the graph could not be
trusted as documentation of data flow, in the one part of this system where
order is a safety property (CLAUDE.md rule 3).

### What that document got wrong

It called the sequencing-only edges decorative and proposed deleting them.
They are not decorative. `staleness_scan` declares `literature_refresh` and
reads nothing from it — but it reads `ledger-history.jsonl`, which
`retirement_pass` and `apply_engine_diff` append to. Deleting the edge would
have moved the scan earlier and silently changed what it sees.

The edges were a real constraint expressed in the only vocabulary available.
The fix is to give that constraint its own vocabulary, not to remove it.

## Decision

### 1. Three declarations, because they are three different things

- **`depends_on`** — what the node *reads*. Now `str | Sequence[str]`. The
  first is the primary edge: it supplies the payload validated against
  `input_model` and handed to preconditions, which is exactly what the
  single string used to mean, so adding a declared read never moves which
  payload a contract inspects. Every declared entry must be present before
  `fn` runs.
- **`after`** — what the node must run *after* without reading. The
  filesystem-mediated orderings above, now explicit and each carrying a
  comment saying which file makes it real.
- **`parallel_group`** — an opt-in label, below.

A test reads the builder's own source with `ast`, extracts every
`ctx["..."]` subscript per node function, and asserts nothing is read
undeclared. It keeps being true rather than being true once.

### 2. Order is derived, and the derivation changes nothing

Execution order is a topological sort that takes the **earliest-declared
ready node** at each step — one node at a time, not a wave of everything
currently ready. That distinction is the entire safety argument: taking
waves emits `A, C, B` for `[A, B(depends on A), C]`, reordering a list that
was already valid. Taking the first ready node reproduces the declaration
order exactly whenever the declaration order is itself valid.

`tests/test_review.py` freezes the pre-0043 order as a literal and asserts
the derived order equals it, node for node. **CLAUDE.md rule 3 makes stage
order a safety property, so the licence to derive it rests entirely on the
derived order matching the one that shipped.** It does.

What deriving buys, beyond honesty:

- **Cycles are refused at construction.** The old list check caught them
  only by accident, as forward references.
- **An unsatisfiable prerequisite fails before anything executes.**
  Previously a typo surfaced as a `KeyError` from whichever node reached it
  first — after earlier nodes had run, and in this DAG those nodes write the
  ledger and cost frontier calls.
- **Independent nodes are visibly independent**, which is what makes the
  next section expressible without hand-reordering anything.

### 3. Parallel batches, opt-in and deterministic

A contiguous run of nodes sharing a `parallel_group` executes concurrently.
Applied to two groups, both provably independent: the blind panel (one
shared input, distinct output keys, preconditions forbidding ledger access)
and the two phenotype engines (both read the ledger from disk, neither
writes).

Determinism is not assumed, it is constructed:

- **No member can observe a sibling.** Two independent mechanisms, each
  sufficient on its own: outputs are committed only after the whole batch,
  and members read a context snapshot rather than the live context.
  Measured — removing either alone breaks no test, removing both fails the
  isolation tests every time. Both are kept, because which one survives a
  future refactor is not knowable now.
- **A failing batch reports the same member every time**, chosen by
  declaration order rather than by which thread lost the race. Verified with
  a control that makes both collection *and* selection completion-ordered;
  it fails 3 runs out of 3.
- **A group whose members are not ready together is refused at
  construction**, as is a member ordered against a sibling. Running them
  together would discard an ordering one of them asked for.
- **Contiguity is the batching rule.** A group split in the order by an
  unrelated node runs as two batches — correct, merely less parallel. No
  node is ever moved to make a batch bigger; that would be reordering the
  graph to chase throughput.

`LlmClient._audit` gains a lock. An audit record is routinely longer than
the 4096 bytes a write is atomic within, so two concurrent appends could
interleave into a line that parses as neither record. An audit trail that
silently loses a call is worse than a slow one.

### 4. `criteria_scan` reaches the report through the graph

It was read from the `results` sink, making it a node the report depends on
with no edge saying so — and nothing to stop a reordering running the report
first. `_render_report_fn` now reads `ctx["criteria_scan"]`.

## What is deliberately not done

DEV-01's remediation also asks to "pass explicit typed inputs to node
runners rather than an unrestricted shared mutable context dictionary."
**Declined**, and the reason is a safety mechanism rather than effort:
`forbid_context_key` — the blind panel's anchoring defence — is a contract
over *the whole run context*. It asserts that no `ledger` entry exists
anywhere in the run's history at that point. A node that received only its
own typed inputs could not be checked that way, and ADR 0002 designed the
full-context handoff for exactly this. Narrowing what a node sees would
narrow what its contracts can assert.

Parallel execution of `trend_scan` and `criteria_scan` is also not done.
They are deterministic local scans, not frontier calls; there is nothing to
win.

## Consequences

- **`input_hash` changes for the seven nodes whose primary edge changed.**
  The recorded hash now describes the payload the node actually reads. Old
  audit records are not comparable for those nodes, which is the correct
  outcome: the old hash described an edge that existed to place the node in
  a list.
- `NodeRecord` carries `depends_on`, `after` and `parallel_group`, so a
  replay can see the graph the run actually had.
- **`tests/test_dag.py::test_dag_rejects_a_forward_dependency` is
  replaced.** It pinned "a forward declaration is an error", which a derived
  order makes false — the list gets ordered instead. Replaced by three
  tests: the reordering, a cycle refused, and an unsatisfiable prerequisite
  failing before any node runs. CLAUDE.md rule 2 requires an ADR for that,
  which is this one.
- Two checks turned out to be redundant within `run` and are kept with
  direct tests and comments saying so: `_prepare`'s prerequisite loop (the
  local invariant of a helper that takes someone else's `ctx`) and the batch
  snapshot. A check nothing exercises is the shape of every silent-absence
  bug in this repository, so each now has a test that fails when it is
  removed.
- Wall-clock on a review drops by roughly two frontier calls plus one engine
  run. Nobody is waiting on a weekly batch, so this is a side effect of
  making the graph honest, not the reason for it.

## Alternatives considered

**Leave it alone; the doc said "not urgent".** It said that about
correctness, and correctness is still not the issue. What changed is that
three later ADRs (0038, 0041, 0042) each had to reason about what a node
sees, and a graph that misdescribes its own data flow makes every one of
those arguments harder to check.

**Sort by a hand-written priority instead of deriving.** That is the list,
with extra steps.

**Parallelise everything independent.** Rejected. Each additional
concurrent node is a new opportunity for an ordering assumption nobody wrote
down to be violated. Two groups, both argued for individually, is the whole
scope.

# ADR 0036 — Engine divergence is adjudicated, not averaged

Status: accepted (2026-08-31)

## Context

Two phenotype engines have been running inside the weekly review since ADR
0029: LIRICAL, which computes a likelihood ratio against curated disease
models, and a phenotype-similarity index, which scores shared information
content. Both produced a `LiricalComparison`, both were rendered into the
report, and neither changed anything.

That was not an oversight in the wiring; it was visible in the DAG shape. Both
nodes ran *after* `apply_review_diff`:

```
apply_review_diff → retirement_pass → test_chooser
                  → lirical_divergence → semsim_divergence → literature_refresh
```

By the time either engine spoke, the ledger for that review had already been
written. Their findings could be read by a person and by nothing else.

PLAN.md's phase 3 acceptance line — "LLM vs LIRICAL divergence adjudication" —
was recorded as not met for a related but different reason ("nothing calls it
in the review DAG"). By this point the nodes existed and *did* call it. What
was missing was the adjudication: the step that says what a disagreement
means and lets that reach the differential.

`docs/research/scoring-across-engines.md` had already predicted the failure
this produces, in as many words:

> LIRICAL, ICAP and Monarch will each add another opinion; without (a)–(d)
> they will add three more opinions to fifty and the report will get longer
> rather than sharper.

The user's framing was the same: the system should produce *clearer
conclusions to hypotheses, not simply more hypotheses*.

## Decision

Two new nodes, after both engines and before `literature_refresh`:

```
semsim_divergence → engine_adjudication → apply_engine_diff → literature_refresh
```

**`engine_adjudication`** puts every non-agreement finding to the `challenger`
role (a different model family, so no new binding and the cross-family
property is preserved) and requires a **direction** per divergence:
`corroborates` / `opposes` / `neutral`, with a rationale. A postcondition
contract — `engine_adjudication_covers_every_divergence` — enforces coverage,
uniqueness, a substantive rationale per item, and rejects one rationale reused
across every divergence. It tolerates the engines being down, which its
panel-side sibling does not, because a sidecar timeout is an ordinary review.

**`apply_engine_diff`** maps directions to ledger ops in **plain code** and
writes its own diff, exactly as `retirement_pass` does — a ledger mutation
does not get a private back door around the invariants.

### Scores are never combined

Nothing multiplies, weights, normalises or averages one engine's score with
another's. A likelihood ratio, a Resnik similarity and an uncalibrated
probability bucket are not commensurable, and arithmetic across them imports a
precision that never existed. Both engines happen to store their score in
`LiricalFinding.composite_lr`, so the label is chosen by engine: `LR 12.4`
against `similarity 3.81`. Combination happens at the level of direction,
which every unit can honestly state.

### `neutral` is a first-class outcome

A phenotype-only engine that never ranked a hypothesis has not refuted it.
LIRICAL sees phenotype and nothing else, so a hypothesis resting on serology,
imaging or exposure history can be entirely correct and score zero.

This is the load-bearing decision. If `ledger_only` were read as opposition,
the node would manufacture counter-evidence every week against exactly those
hypotheses whose support lives in a modality the engine cannot see — and ADR
0035's retirement pass, which retires on accumulated counter-evidence, would
then start killing them. The prompt pushes toward `neutral` whenever the
engine is out of its depth, and `neutral` emits no op.

`corroborates` on a `ledger_only` item is incoherent by construction (the
engine did not rank it) and is refused in code, reported rather than trusted.

### Only evidence, never a re-grade

The ops emitted are `AddEvidence` and, for a genuinely missed candidate,
`AddHypothesis`. Probability and tier are never edited: those belong to the
stages that reason over the whole case, and an engine that saw only the
phenotype should not re-grade a differential it cannot fully see. Evidence is
sufficient to drive convergence, because retirement already counts it.

Guards on adoption:

- **A rule-out is mandatory.** No `rule_out`, no hypothesis — part 3a of the
  research note, enforced rather than requested.
- **`expanded` tier, `low` probability.** An engine ranking is a reason to
  look, not a reason to lead.
- **At most 3 adoptions per review.** Two engines on a broad phenotype can
  surface a dozen plausible rare diseases at once, and adding twelve is the
  inflation this node exists to counteract. The rest are reported as
  considered.
- **Engine evidence is `moderate`, never `strong`.** Retirement counts strong
  evidence double; two engines must not be able to retire a hypothesis between
  them with no human or lab involved.

### Agreement is recorded without a model call

Where an engine independently ranks a hypothesis the ledger already holds,
that is a fact, not a judgement, and it is written deterministically. This
matters more than it sounds: 24 of 25 hypotheses in production carried an
empty evidence list, and agreement between units that work in genuinely
different ways is the best-supported finding in the case. Deduplicated by
engine name — the engines run weekly and the ref carries the review date, so
matching on the whole ref would re-add the same corroboration forever.

### A new citation scheme

`engine:<lirical|semsim>:<YYYY-MM-DD>`, resolved on grammar like
`patient-report:`. A hypothesis that exists *because* an engine ranked it has
to be able to say so, and there was nowhere for that evidence to point: `doc:`
and `encounter:` describe files that do not exist for a computation, and a
`pmid:` for the engine's method would attribute a claim about this patient to
a paper that never saw her.

Unlike every other slug in the grammar this one is a **closed set**. The other
schemes name things the record already contains and must accept whatever is on
file; the engine list is known at build time, so `engine:liricl:...` is a
validation error rather than a citation resolving to nothing.

## Consequences

- The engines can now change the differential, which is the point, and every
  change they make is cited to them and dated.
- One additional model call per review, and only when there is at least one
  divergence to judge.
- A third ledger version per review when the engines act.
- **A pre-existing reporting bug surfaced and was fixed.** The report read
  `ledger_after` from the `apply_review_diff` node, which is the
  *pre-retirement* object. Since ADR 0035 landed, a review that retired
  hypotheses reported a version it had already moved past and rendered a "what
  changed" ledger still containing every hypothesis it had just retired. The
  render node now reads the ledger as it finally stands. This changed a pinned
  expectation in `test_full_review_happy_path` from `+1` to `+2`; the old value
  was wrong, not merely out of date.

## Alternatives considered

**Feed engine divergences into the existing `adjudication` node.** One
adjudication, one apply, no new nodes. Rejected: that node's input is the
blind panel's `DivergenceSet` and its contract is a required CI check, so
widening it would mean editing a pinned safety contract to add an unrelated
source. Additive nodes leave the panel path untouched.

**Move the engines earlier, before `apply_review_diff`.** Rejected for the
reason the current placement was chosen: comparing against the pre-review
ledger would report every hypothesis the review had just added as
`engine_only`.

**Let the model choose the ledger operation directly.** Rejected under the
standing rule that deterministic policy is plain code. The model is best
placed to judge direction; what a direction *does* to a patient's differential
is a policy decision, and a model asked for an op will reach for
`add_hypothesis`.

# ADR 0054 — The Challenger must pay for what it proposes

- **Status**: accepted
- **Date**: 2026-09-12
- **Extends**: [ADR 0002](0002-challenger-stage.md) (the Challenger stage),
  [ADR 0038](0038-how-a-hypothesis-ends.md) (how a hypothesis ends),
  [ADR 0047](0047-a-lead-states-how-it-ends.md) (a lead states how it ends)
- **Related**: ADR 0045 (tier caps), ADR 0052 (convergence boundary), ADR 0053
  (the evidence weight table)

## Context

Two consecutive reviews, now on disk as ADR 0052 snapshots — the first
before/after this project has ever had that was not a recollection:

| | 2026-09-09 (0.33.1) | 2026-09-10 (0.34.0) |
|---|---|---|
| active | 32 | **33** |
| differential | 30 | **31** |
| off the board this run | 15 | **0** |
| open questions | 70 | **92** |

One review added a lead, removed none, and opened twenty-two more questions.
That is the whole complaint — roughly fifty iterations aimed at a smaller,
better-directed differential — reduced to two lines of measured data.

The cause is not one thing. It is four, and each is measured.

### 1. Almost nothing on the board can be ended by code

| | of 33 active |
|---|---|
| machine-checkable `rule_out_check` | **4** |
| prose `rule_out` only | 26 |
| neither | 3 |

`_rule_out_met` is the rule that ended something last review — the only
deterministic rule that fires on *evidence* rather than on absence or age.
It can physically only ever fire on those **4**. ADR 0038 built the
machinery and ADR 0047 said a lead should state how it ends; nothing
requires it, so 29 of 33 leads have no stated end condition a machine can
evaluate.

### 2. The Challenger creates the board and rarely attacks it

| | |
|---|---|
| challenger-origin | **30 of 33** |
| with zero `evidence_against` | **15 of 33** (all challenger-origin) |
| evidence weight, for : against | **519 : 36** |

The stage whose entire purpose is adversarial is the principal *source* of
leads. `challenger.md` is not the problem — it already says "Attacking beats
adding", already asks for an `add_evidence` op with `kind: against` and a
resolvable ref, and already quotes the statistic back at itself. It asks. It
is simply not held to it.

### 3. The one contract that holds it covers zero leads

`_challenger_min_counterarguments_contract` requires one **prose**
counter-argument per hypothesis the diff places in `most-likely`.

The live board has **0** leads in `most-likely`. The contract covers nothing,
and has covered nothing for as long as the board has looked like this. A
board of 33 leads with no leading candidate is its own finding — but the
immediate consequence is that the sole enforcement point is inert.

This is CLAUDE.md rule 3's principle — *enforced by code, not prompts* —
applied to the wrong axis. The prose is enforced; the citation is requested.

### 4. The cap is a ceiling, and the board is sitting on it

`expanded` holds 22, of which 2 are `emerging` and excluded, leaving exactly
**20 in the differential against a cap of 20**. `over = 0`, so the fold
proposes nothing. `cant-miss` holds 11, every one `is_protected`, beyond every
automatic rule by design.

So the automatic floor is 20 + 11 + emerging ≈ 33, and the board is resting on
it. ADR 0045 bounded the growth, which was worth doing and is why the number
stopped climbing. It cannot make the number fall. **A cap is not convergence.**

## Decision

Three changes, in the order their measurements say they will matter.

### 1. A counter-argument must name what kind of attack it is

`CounterArgument` gains a required `outcome`, and prose alone stops being a
valid one:

- **`cited`** — accompanied by an `add_evidence` op, `kind: against`, with a
  resolvable source ref. The only outcome that moves the balance scale.
- **`nothing-on-file`** — an explicit abstention: *"I looked for X and the
  record does not contain it."* Names what was looked for. Recorded as data,
  never discarded.
- **`alternative`** — names another hypothesis id that explains the same
  cited evidence better. The one honest way to attack without new evidence.

The contract then requires an outcome for **every hypothesis the diff adds or
updates, in every tier** — not prose, and not only `most-likely`.

**`nothing-on-file` is deliberately cheap**, and that is the point. A contract
that only accepts `cited` pays a model to invent citations, and fabricated
counter-evidence is worse than none: it retires real leads through
`_outweighed` at weight 4. An abstention that is easy to give honestly is the
pressure valve that keeps the other two outcomes truthful. The existing
`InsufficientEvidenceNote` already does exactly this for the
Ledger-Maintainer; this is the same idea on the other side of the ledger.

**An accumulated abstention is itself a signal.** "Nothing on file has spoken
against this, across N reviews" is a durable, honest fact about a lead nobody
can attack and nobody can confirm. It is the input a future retirement rule
needs and does not have today. This ADR records the count; it does not yet
retire on it.

### 2. A new lead states how it ends, or says why it cannot

Every `add_hypothesis` op carries a `rule_out_check` — or an explicit
`rule_out_inexpressible` note giving the reason.

`casefile/rule_out_backfill.py` already has the hard part:
`check_is_expressible()` exists because a prose rule-out and a mechanical
check can disagree dangerously. Its worked example is cosyntropin-**stimulated**
cortisol against a plain `Cortisol above 18` check, which would have retired a
can't-miss adrenal-insufficiency lead on a baseline draw. So universal
enforcement would be wrong: some genuine rule-outs cannot be expressed, and
forcing one produces a check that fires on the wrong data.

The requirement is therefore **state it or state why not**, and the refusal is
recorded rather than being the silent default it is today — 26 of 33.

The backfill pass runs every review instead of the once it has run by hand.

### 3. The cap's ceiling is reported as a ceiling

When a tier sits at its cap with nothing eligible to fold, the review says so
in as many words. Today that renders identically to a tier comfortably under
its cap: `folds proposed: 0`. One of those means "nothing needed pruning" and
the other means "the board is pinned against its limit and every rule that
could lower it is inert" — the state it has been in since 2026-09-09, saying
nothing.

No new mechanism. It is the project's recurring shape once more: a rule that
cannot fire reads exactly like a rule that fires and finds nothing.

## Consequences

- **The Challenger gets more expensive per turn**, in output tokens and in
  latency. It is the stage that most deserves the cost.
- **`nothing-on-file` will dominate at first**, and that is the honest
  reading, not a failure. 15 of 33 leads have nothing against them; a stage
  forced to say so out loud will say so fifteen times. The number becoming
  visible is the deliverable.
- **Fabrication risk rises and must be watched.** The citation check and the
  entailment verifier already gate `evidence_against` refs, and ADR 0053's
  scale means a fabricated `strong` item carries weight 4. The first review
  after this ships must be measured for counter-evidence that does not
  survive entailment — not assumed clean.
- **Nothing here touches the two exclusions.** `cant-miss` and patient-origin
  leads stay beyond every automatic rule. Eleven of the thirty-three will not
  move, and should not: that is the asymmetry the tier exists for.
- **This will not, by itself, shrink the board either.** It supplies the input
  every retirement rule is starving on. Whether a differential of 33 becomes a
  differential of 12 depends on what the evidence actually says once somebody
  is required to look — and ADR 0052's snapshot will report the answer rather
  than anyone's recollection of it.

## Alternatives considered

**Lower the cap from 20 to 10.** Rejected, and it is the tempting one because
it would work immediately and show a smaller number tomorrow. It shows a
shorter *page*, not a better-directed differential: the leads still exist,
parked, ranked by cited evidence — which is exactly the evidence this ADR
exists because nobody is gathering. Tuning the ceiling while the mechanism
under it is starved is how you get a number that looks right and means less.

**Require `cited` and remove the abstention.** Rejected. It pays a frontier
model to invent counter-evidence, and ADR 0053's weights turn an invented
`strong` item into four units against a real lead. The output gate and
entailment verifier would catch some of it. "Some" is the wrong tolerance for
a mechanism that retires diagnoses.

**Have a separate stage attack the board.** Rejected as the same work with a
worse name. Cross-family independence — the reason the Challenger is bound to
a different provider (ADR 0005) — is already the property that matters, and a
second stage would either share that binding or be weaker than the one that
has it.

**Cap what the Challenger may add per turn.** Deferred, not rejected. It is
the obvious counterpart to the tier cap and would bound the source rather than
the symptom. But 30 of 33 leads being challenger-origin is not yet known to be
*wrong* — a stage told to surface what else could explain the findings is
supposed to produce candidates. Decide it on the measurement this ADR
produces, once the cost of proposing a lead is no longer zero.

## As built (2026-09-12)

**The outcome is normalised in code, not enforced by the contract.** The ADR
implied a contract that rejects an unbacked `cited` or an abstention naming
nothing. Built that way first, and it is wrong: a `ContractViolation` on the
Challenger node **stops the turn**, so a model that ignores the new fields —
an older one, a degraded one — takes her chat down entirely. The first draft
failed 19 tests for exactly that reason before any model was involved.

`normalize_counter_arguments` now runs at the end of `challenger_stage` and
every correction it makes moves in the safe direction: an unbacked `cited`
becomes `nothing-on-file`, an unnamed `alternative` becomes
`nothing-on-file`, an abstention with no subject records `(not named)`. The
one upgrade is reading `cited` off an op the verdict actually carries.

It runs **after** the entailment strip, because that strip can remove the very
`add_evidence` op a `cited` outcome rests on — the unbacked case arrived at
from the other direction. Pinned by a test on the source order.

The contract keeps two jobs: coverage (hard — every touched hypothesis, every
tier) and a regression guard that no unbacked `cited` survives, which reaching
means normalisation did not run.

**Part 2 grew a second function.** `needs_rule_out` matched
`{"active", "monitoring"}` and **`monitoring` is not a status** — it has never
been in `HypothesisStatus`. So the set was really `{"active"}` and every
`patient-proposed` and `challenged` lead was invisible to the backfill. That
is the third instance of this exact shape after `_EVIDENCE_STRENGTHS` and ADR
0052's `retired` count, and it is now taken from `ACTIVE_STATUSES`.

`needs_checkable_rule_out` is the set that matters: prose satisfies a reader,
`_rule_out_met` reads `rule_out_check` and nothing else. The narrow function
saw **3** of 33 leads; the wide one sees **29**.

**Deferred: the backfill does not yet run as a review node.** The ADR said it
should run every review. The measured blocker is that it proposes into a
two-step human-reviewed file (`case/proposed-rule-outs.yaml`, ADR 0047) and
wiring it to run unattended needs a decision about who applies the proposals.
Recorded here rather than half-built.

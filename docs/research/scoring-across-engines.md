# Research note — comparing scores across engines, and why the ledger does not converge

Status: research, not a decision. Written 2026-08-29 to inform the LIRICAL,
ICAP and Monarch work.

Two questions, and they turn out to have different answers:

1. How do we compare a LIRICAL likelihood ratio, a classification-criteria
   point score, an LLM probability bucket and a Monarch similarity score?
2. Why does the ledger produce more hypotheses instead of clearer conclusions?

The second is the one that matters, and it is not a scoring problem.

## Part 1 — the measurement

Taken from the live ledger at version 12, after the 2026-08-29 review:

```
total hypotheses:  50
by status:         {'active': 50}
by tier:           {'expanded': 42, 'cant-miss': 8}
by probability:    {'high': 7, 'moderate': 12, 'low': 20, 'minimal': 11}
by origin:         {'challenger': 47, 'patient': 3}

evidence_for      per active: min 0, median 6, max 41   — 8 have none
evidence_against  per active: min 0, median 1, max 5    — 21 have none
with discriminators: 11 of 50
never substantively challenged: 1 of 50
```

Five things in that:

**Nothing has ever been retired.** Every one of the 50 is `active`. The schema
has `ruled-out`, `parked`, `challenged` and `confirmed-by-doctor`. Across
twelve ledger versions, not one hypothesis has moved out of `active`. Grep
confirms why: `ruled-out` appears in **no prompt and no ledger logic**. The
status is reachable in the type system and unreachable in practice.

**There is no `most-likely` tier at all.** 42 expanded, 8 can't-miss, zero
most-likely. The system holds fifty leads and commits to none. That is the
complaint — more hypotheses, no conclusions — visible directly in the data.

**Evidence is one-sided.** Median 6 supporting claims against median 1
opposing, and 21 hypotheses have *no* counter-evidence at all. The system
accumulates confirmation and rarely records disconfirmation. Nothing here
looks for reasons to kill a lead.

**Discriminators are mostly missing** — 11 of 50. A discriminator is the
finding that would tell two hypotheses apart. Without one there is no defined
path from "possible" to "resolved", so a hypothesis has no way to die of
natural causes.

**47 of 50 came from the Challenger.** That is the Challenger doing its job:
it adds counter-hypotheses. Nothing balances it. One stage adds and no stage
subtracts, so the ledger can only grow.

## Part 2 — comparing scores across engines

The four units are not on a common scale, and three of them are not
probabilities at all.

| Unit | Produces | Is it a probability? |
|---|---|---|
| LIRICAL | composite likelihood ratio (log10) | **Yes** — a genuine LR |
| Classification criteria | points vs a threshold | **No** |
| LLM panel | `high`/`moderate`/`low`/`minimal` | **No** — subjective, uncalibrated |
| Monarch sem-sim | ontology similarity | **No** — not even ordinal in probability |

The tempting move is a weighted sum into one number. **Do not.** That is
precisely the unit-blindness that has already produced three wrong clinical
conclusions in this system: a 319,900% eosinophil trajectory, a GPA score of
−4, and percentages compared against concentrations. Adding a criteria point
total to a log-likelihood ratio is the same error wearing a different hat.

Three specific traps:

- **Classification criteria are not diagnostic criteria.** They exist to
  define comparable cohorts for research and deliberately trade sensitivity
  for specificity. 9 points on SLE-2019 does not mean 90% chance of SLE, and
  the report already says so in words — the arithmetic must not contradict the
  prose.
- **Bayesian combination assumes conditional independence.** These engines all
  read the same case file. Their errors are correlated by construction, so
  multiplying their LRs would overstate confidence exactly where they are most
  likely to be wrong together.
- **LLM probability buckets are not calibrated** and the literature is
  consistent on this for medical QA. Treating `moderate` as 0.5 imports a
  precision that was never there.

### What to do instead

**1. Report agreement structure, not a fused score.** Where independent units
rank a hypothesis highly, that is corroboration and should be said. Where they
diverge, that divergence is the finding — it is the thing worth a clinician's
attention, and it is what "divergence adjudication" was always meant to
surface. Keep the units side by side, each in its own scale, labelled.

**2. Combine at the level of direction, not magnitude.** Every unit can
honestly state *supports* / *neutral* / *opposes* and how firmly. That is
comparable across units without pretending the magnitudes are. A rank-level or
vote-level combination is defensible; arithmetic on the raw numbers is not.

**3. Make the decision metric "what would resolve this", not "what scores
highest".** For each candidate test, how many currently-active hypotheses
would it separate? That is computable deterministically from discriminators,
it drives convergence directly, and it is what the Test-Chooser should be
optimising. A test that confirms the leading hypothesis is worth less than one
that kills four.

## Part 3 — what would actually make it converge

Scoring is not the bottleneck. The ledger cannot converge because nothing in
it is allowed to end. Four changes, in the order they would help:

**a. Every hypothesis gets a rule-out condition when it is created.** "This is
dead if X." Today `discriminators` is the nearest field and it is empty for 39
of 50. A hypothesis with no stated way to die will not die.

**b. A retirement pass in the review.** Deterministic, not a model call: a
hypothesis whose rule-out condition is met, or that has gone N reviews
accumulating counter-evidence and no new support, moves to `parked` or
`ruled-out`. The statuses already exist; nothing sets them.

**c. Ask for disconfirmation explicitly.** 21 hypotheses with zero
counter-evidence is not 21 unfalsifiable hypotheses — it is nobody having
looked. The Challenger is asked to propose alternatives; something must be
asked to bury them.

**d. Require the `most-likely` tier to be populated, or to say why it cannot
be.** An empty most-likely tier across twelve versions is either a real
finding about this case ("nothing yet dominates") or an abdication. It should
have to be one or the other, stated, rather than silently empty.

None of that needs a new engine. LIRICAL, ICAP and Monarch will each add
another opinion; without (a)–(d) they will add three more opinions to fifty
and the report will get longer rather than sharper.

## Consequence for the LIRICAL work

LIRICAL should be wired to produce **divergence**, not a fifth ranking to
average in. The useful output is: which hypotheses does LIRICAL rank highly
that the blind panel did not, and which does the panel hold that LIRICAL
scores near zero? Both are adjudication targets. Its LR is the only genuine
likelihood ratio in the system and should be reported as such, in its own
units, never folded into a composite.

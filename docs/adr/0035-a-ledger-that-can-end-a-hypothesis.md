# ADR 0035 — A ledger that can end a hypothesis

Status: accepted (2026-08-30)

## Context

The user's report was that the system produces more hypotheses rather than
clearer conclusions. Measured on the live ledger at version 12, before any of
this:

```
total hypotheses:  50
by status:         {'active': 50}          — none ever retired, 12 versions
by tier:           {'expanded': 42, 'cant-miss': 8}   — ZERO most-likely
by origin:         {'challenger': 47, 'patient': 3}

evidence_for      median 6, max 41   —  8 have none
evidence_against  median 1, max 5    — 21 have none
discriminators:   11 of 50           —   3 of the 39 retirement-eligible
age of eligible:  every one 1 day old
```

Four separate failures, and only the first is the one you would guess.

**Nothing could end a hypothesis.** `ruled-out` appeared in no prompt and in
no code. The status was reachable in the type system and unreachable in
practice. One stage added and no stage subtracted, so the ledger could only
grow.

**Nothing was created with a way to die.** `discriminators` had no docstring
and no prompt ever asked a stage to fill it — the sole mention anywhere was
`test_chooser.md` telling the chooser not to *duplicate* one. So even a
perfect rule-out rule would have had almost nothing to fire on.

**Nothing budgeted growth.** One review took the ledger 28 → 50. Every
retirement-eligible hypothesis was one day old, which is the finding that
reframed this: the ledger was not clogged with old cruft a cleanup would
sweep away. It was clogged with brand-new leads from a single run.

**Nothing forced a conclusion.** Twelve versions with an empty `most-likely`
tier. Fifty leads and no statement of what the evidence actually favours.

## Decision

Four changes. The first is deterministic code; the other three are prompt
contracts, because they govern what a model must produce rather than what
code must compute.

**1. A deterministic retirement pass** (`casefile/retirement.py`), running in
the review after the ledger diff is applied so it judges what this review
produced. Three rules, first match wins so a hypothesis is retired for one
stated reason: nothing supports it (→ `parked`); the evidence against
outweighs the evidence for, counting strong evidence double so volume cannot
beat quality (→ `ruled-out`); or it is low/minimal probability and untouched
for 90 days (→ `parked`).

Two exclusions are **absolute**, and they are why this can be automatic at
all:

- **`cant-miss` is never auto-retired.** The point of that tier is that the
  cost of missing one is catastrophic and asymmetric. A rule that could
  silently drop a pulmonary embolism to tidy a list is not worth having.
- **Patient-origin is never auto-retired.** ADR 0032 makes patient-reported
  material first-class. Her theory is hers to withdraw.

Nothing is deleted. A retirement is a status change, applied through a
`LedgerDiff` so the existing invariants still check it — retirement does not
get a private back door — and reversible by any later review that finds
support.

**2. Every hypothesis states what would kill it.** New `rule_out` field,
required by the Ledger-Maintainer and Challenger prompts on every
`add_hypothesis`: the specific result that ends it ("a normal repeat FSH on a
draw four or more weeks later"), never a hedge like "further testing". It is
distinct from `discriminators` and points the other way in time — a
discriminator separates this hypothesis from a neighbour, a rule-out is what
settles it. `discriminators` is now documented rather than left bare.

**3. Adding costs something.** The Challenger — source of 47 of 50 — is told
that three new hypotheses is a normal upper bound for one review, and that
beyond it each addition must name the active hypothesis it outranks. Not a
hard cap: capping at five when the sixth is the answer would be worse than
the disease. It converts an append-only list into a ranked one, which is what
makes a `most-likely` tier possible at all. It is also told plainly that
attacking beats adding, which is what that stage exists for.

**4. Commit, or say why not.** The Ledger-Maintainer must place something at
`most-likely` or state in the diff rationale why nothing dominates and what
would change that. Deliberately *not* "always populate it": an empty
most-likely is a legitimate finding for a genuinely undifferentiated case. It
is not a legitimate silence.

## Consequences

- Dry-run against the live ledger: **50 active → 42**, 8 parked, 11 protected
  and never assessed. All eight were `low`/`minimal` with zero cited support
  — alpha-gal syndrome, candida colonisation, remote EBV, exogenous estrogen
  and four others. None reached `ruled-out`, because none had the
  counter-evidence to justify the stronger status. That conservatism is the
  intended behaviour.
- **Retirement alone would not have fixed this**, and it is worth recording
  that the first proposal was retirement alone. The measurement showed every
  eligible hypothesis was one day old: the pass retires 8 and then has nothing
  left to do. Items 2–4 are what change the trajectory; item 1 is the cleanup
  that makes them stick.
- Two pinned prompts change (`ledger_maintainer` 2→3, `challenger` 1→2). The
  safety suite passes unchanged — no test's pinned property is altered, so
  this ADR records a new decision rather than amending an old one.
- **The prompt contracts are not enforced by code.** A model that ignores the
  rule-out requirement produces a hypothesis with an empty `rule_out`, and
  nothing rejects it today. The deterministic pass will park it later if it
  also lacks support, but that is a backstop rather than enforcement. A DAG
  contract requiring `rule_out` on every added hypothesis is the obvious next
  step and is deliberately not taken here: it would reject whole payloads on
  one missing field, which ADR 0028 says not to do, so it needs a
  drop-the-item shape rather than a raise.
- The retirement pass cannot yet act on `rule_out` — no hypothesis has one
  until a review runs under the new prompts. The rule that consumes it ("this
  rule-out condition is now satisfied by a result on file") needs the field
  populated first, and is left for when there is data to test it against.

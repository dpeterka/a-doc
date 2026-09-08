# ADR 0053 — The balance scale knows every strength

- **Status**: accepted
- **Date**: 2026-09-08
- **Related**: ADR 0035 (retirement), ADR 0038 (definitive exclusion), ADR 0052 (convergence boundary)

## Context

`retirement._outweighed` retires a lead when the evidence against it outweighs
the evidence for. It weighed each item with one expression:

```python
2 if e.strength == "strong" else 1
```

That expression was wrong twice.

**It did not know about `definitive-exclusion`.** ADR 0038 added the strength
later. The expression scored it 1 — the same as `weak`, and *below* `strong*.
A strength that exists to end an argument counted for less than one that does
not. This is the third instance of one shape in this codebase: an older
function that never learned about a newer literal. `_EVIDENCE_STRENGTHS` was
the same bug, and it downgraded panel assertions to `moderate` for weeks.

**It contradicted its own docstring.** The docstring promised that "three weak
observations do not outweigh one strong contradicting result". Three weak
scored 3; one strong scored 2. Three weak won. A lead supported by one strong
result could be retired by three weak notes against it — the exact
volume-beats-quality outcome the weighting existed to prevent.

Neither was visible, because a retirement rule that fires on the wrong input
looks identical to one that fires on the right one.

## Decision

A table, total over `EvidenceStrength`, doubling at each step:

| strength | weight |
| --- | --- |
| `definitive-exclusion` | 8 |
| `strong` | 4 |
| `moderate` | 2 |
| `weak` | 1 |

Doubling is what keeps the docstring's promise. Three weak (3) lose to one
strong (4). No quantity of weak evidence reaches a definitive exclusion, which
matters because ADR 0038 restricts *who may make* such a claim — a scale that
let seven weak notes add up to one would give that restriction nothing to
protect.

`test_every_strength_has_a_weight` enumerates `get_args(EvidenceStrength)`
against the table's keys. A strength added without a weight fails there
instead of scoring silently.

## Consequences

- Retirement gets slightly more willing to act on quality and less willing to
  act on volume. Both directions are intended.
- The two exclusions are untouched: `cant-miss` and patient-origin leads are
  still never retired by any rule, whatever the scale says.
- Whether `_outweighed` now fires more often on the live ledger is
  **unmeasured**. The board carries far more supporting than contradicting
  citations, so the rule may still be starved of input rather than
  mis-weighted — a different problem, upstream in what the Challenger records.
  ADR 0052's snapshot will attribute the answer on the next review instead of
  leaving it to recollection.

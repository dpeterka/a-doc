# ADR 0051 — Abnormal is a comparison, not a flag

Status: accepted (2026-09-08)

Replaces item #4 of the convergence track, and corrects the reach of
[ADR 0044](0044-the-engines-can-see-the-serology.md).

## Context

Every layer that asked "is this analyte high, low or normal" asked the lab's
`flag` column and nothing else. Measured in production, 2026-09-08:

```
2079 rows, 187 flagged     (H 120, L 21, A 46)
1892 unflagged             - 91% of the record
```

The question was being answered from 9% of the data, and the other 91% read
as **not abnormal** rather than as **nobody said**. Four consequences, all
live:

- **`knowledge.criteria`**: 17 of 26 lab-phenotype rules matched a stored
  analyte and could never be satisfied.
- **ADR 0044 barely ran.** It derived **1** HPO term from 461 analytes, so
  the engine query was 8 human terms + 1. The serology that ADR was written
  to deliver never arrived — and I reported it as working, on the evidence
  that engine adjudication went 66/66 neutral → 15 opposes. That inference
  was wrong: all 15 were `engine_only` "do not adopt" decisions, unrelated
  to the query.
- **`retirement.evaluate_rule_out`'s `normal` operator returned `True` for
  an empty flag**, reporting *"is within the lab's reference range"* having
  looked at no range. That is cannot-tell ending a hypothesis, the one
  failure its own docstring forbids.
- **`reason.context`'s Abnormal section is built from the same predicate**
  and rendered no reference range at all.

That last one answers a question worth recording, because the intuition runs
the other way. The owner asked: if the model can determine correct results
from the same data, why does the deterministic layer struggle? **It wasn't
struggling where the model succeeded.** Both read the same view: 187 rows
marked abnormal out of 2079, and no ranges. A model asked whether a value is
out of range was never shown the range.

### Why the ranges were missing

`labs.db._parse_ref_range` is anchored with `$` and rejects a trailing unit:

```
'140-400'      -> (140.0, 400.0)
'16-232 ng/mL' -> (None, None)
```

920 unflagged rows carry a `ref_text` and no parsed bounds for exactly that
reason. A further 702 have numeric bounds already. **51 rows sit measurably
outside their range with no flag** — 42 high, 9 low.

## Decision

One predicate, `labs.reference.range_position`, used everywhere:

1. **Flag first.** A lab that says `H` has applied its own judgement and
   knows more about its assay than this code does.
2. **Then the numeric bounds.**
3. **Then `ref_text`, parsed at read time** — not by migration, so the 920
   rows already stored with unparsed text are fixed the moment this ships.
4. **`None` when the record cannot say, and `None` is never "normal".**

That fourth clause is the point of the module. `is_high`, `is_low` and
`is_abnormal` all read cannot-tell as false, and `LabFact` gains a
`position` field so `casefile.retirement` can distinguish the two without
importing `labs`.

`A` (abnormal, direction unrecorded) resolves through a range when one
exists and otherwise stays `None`. Measured: 46 such rows, 0 resolvable, so
they remain honest rather than becoming a coin flip.

The reference interval is now rendered in the context pack, and the section
is retitled from "Abnormal" to "Out of range" — a model asked to judge a
value should be shown what to judge it against.

## Consequences

- **The abnormal set grows by up to 51 rows**, which reaches the criteria
  scorers, ADR 0044's derivation, the rule-out evaluator and every context
  pack at once. That is a wide blast radius for one predicate, and it is the
  measure of how concentrated the defect was.
- **A rule-out with `operator: normal` now needs a judgeable result.** On
  the current ledger the applied iron-deficiency rule-out (`ferritin
  normal`) was answered from an empty flag; ferritin 25 against `16-232
  ng/mL` is genuinely inside the range, so the outcome was right — **by
  luck, from a predicate that checked nothing**. It will now be right for a
  reason. No full review has run since it was written, so nothing was
  retired on the broken path.
- **The number of derived HPO terms should rise sharply**, and that must be
  **measured on the next review** rather than assumed. Assuming it is
  exactly the error this ADR corrects.
- `labs.db._parse_ref_range` is left alone. It governs what is written at
  ingest; `labs.reference` governs what is read. Widening the writer is
  worth doing and is a separate change with a migration question attached.

## Alternatives considered

**Fix `_parse_ref_range` and re-ingest.** Rejected as the first move: it
needs a rebuild of 122 documents to repair rows already stored, and reading
`ref_text` at query time fixes them immediately with no migration. The
writer should still be widened later.

**Let the model judge abnormality.** Rejected on CLAUDE.md's rule that
deterministic logic is plain code — and it would not have fixed anything,
because the model was reading the same flag-filtered, range-less view. The
fix is to show both the comparison, not to move the job.

**Treat an unflagged row with no range as normal.** That is the status quo,
and it is what let a rule-out end a hypothesis on absent information.

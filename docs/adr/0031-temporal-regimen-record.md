# ADR 0031 — What she takes is an interval, not a flag

- Status: Accepted (2026-08-28)
- Extends ADR 0027's temporal-fidelity work to medications and supplements,
  and closes a retrieval gap ADR 0015 left open for encounters.

## Context

A curated supplement-regimen file was written and ingested as an encounter.
The next weekly review reported it as *"on file but not yet reconciled"*, and
the top next-appointment item asked the patient to bring her supplement
bottles so her doctor could establish what she takes.

Both were accurate descriptions of the system's own blindness. Measured on
the real case file:

| | |
|---|---|
| Encounter on disk | 3,553 bytes / 110 lines — the regimen |
| Its `summary` field | 16 characters |
| What the context pack showed a reasoner | **107 characters** — the title line |
| Body reaching no model at all | **3,446 characters** |

`_recent_encounters_section` renders one summary line per encounter. ADR 0015
gave *documents* a full-text corpus with FTS retrieval; encounters never got
one. So every encounter body — including every patient-report written from a
chat turn — contributes a title and nothing else.

Indexing those bodies would have made the text retrievable. It would not have
made it *usable*, because the underlying model was wrong. `intake.sections`
represented a medication or supplement as:

```python
name, dose, frequency, still_taking: bool, notes
```

That boolean is the entire temporal model, and it cannot answer the question
this case turns on:

> Was she taking biotin when the 2026-07-15 assay ran?

High-dose biotin distorts many hormone and antibody immunoassays. Whether her
FSH, thyroid and antibody results are real or artefactual depends on an
*interval* overlapping a *specimen collection date*. It also cannot tell a
supplement stopped two years ago from one stopped last week — both are
`still_taking=False`.

This was the one place left in the system that kept a boolean where
everything else models time properly: `EncounterFrontmatter` carries
`date_precision` and `reported_on`, `IntakeFact` carries `date_approx` /
`precision` / `reported_on`, `LabResult` separates specimen `date` from
`created_at`.

## Decision

**`case/regimen.yaml` is a first-class, temporal case-file artifact**
(`casefile/regimen.py`), sitting alongside the ledger rather than inside an
encounter body. Each `RegimenEntry` carries `started` / `stopped`, each with
its own `DatePrecision`, plus `attribution` (prescribed vs self-started),
`reported_on`, and `sources` refs so a regimen claim is checkable by the same
machinery as any other (ADR 0028).

**Three rules the module exists to enforce.**

*Intervals, not flags* — a patient says "since about last spring" far more
often than a date, so precision travels with each endpoint.

*A restart is a new interval, never a widened one.* "Took it in 2024,
stopped, restarted in 2026" is clinically different from "took it
continuously"; merging them would fabricate exposure across a gap that may be
exactly what a lab result reflects. `merge_entries` only ever updates an
**open** interval; anything else is appended.

*Unknown is not false.* An entry with no dates reports `unknown`, never
absent. Silently treating an undated supplement as not-present on a specimen
date would give a confident wrong answer to the very question this record
exists to settle — and the context section states how many entries could not
be placed, so a reader knows the overlap answer is partial.

**Both reasoning paths see it.** A `regimen` section joins the fixed context
order after `trajectories`, so the chat turn and the deep review read the
same record. That moved a pinned section-order test, which is deliberate: the
pin exists because the blind-review `forbid_context_key` contract depends on
`keys`.

**Lab alignment is the payoff.** The section names the dates on which labs
were drawn while an assay-interfering substance was active. The interfering
list is short and hand-curated on purpose: a broad one would put a caveat on
every result and therefore on none.

## Consequences

- The record must be *maintained*, not just seeded. It changes as the patient
  talks — "I stopped selenium last month" — so the chat path has to propose
  entries and deterministic code apply them, exactly as `IntakeFact` works
  today. **That wiring is not in this ADR**; what is here is the record, the
  merge semantics and the two reads. Until it lands, the record only reflects
  what a backfill puts in it.
- `intake.sections.Medication` / `Supplement` still exist and still carry
  `still_taking`. `RegimenEntry.still_taking` is a derived property over the
  interval so callers keep working, but it is a view, never the source of
  truth. Converging the intake models onto this one is follow-on work.
- Encounter bodies remain unretrievable in general. This ADR fixes the
  regimen case by promoting it out of an encounter; it does not fix the class.
  A patient-report encounter from chat still contributes a title and nothing
  else, which is the next thing to close.
- The interference list will need to grow (macrobiotin is not the only
  offender), and every addition is a judgement about what deserves a caveat.

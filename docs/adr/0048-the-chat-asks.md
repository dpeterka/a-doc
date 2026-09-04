# ADR 0048 — The chat asks

Status: proposed (2026-09-04)

## Context

The review asks questions. The chat answers them. **Nothing asks during a
chat**, and that asymmetry is structural:

```
review DAG   ... → test_chooser → ...        generates questions
chat DAG     ledger_maintainer → challenger → apply → composer
                                  ↑ no stage identifies missing information
```

The machinery for the second half is already built and good.
`casefile/questions.py` gives a question a stable id, an `audience`
(`doctor` | `you`), `hypothesis_ids`, a `why`, and an answered state;
`reason/stages.py` closes them from a chat turn; `reason/context.py` renders
open ones into the context pack. ADR 0018's continuity note already fires on
a new visit, drawing on follow-up facts, `needs_probe` facts, recent facts
and open questions.

So the patient already sees "It's been about 4 days since we last talked.
Last time I made a note to check back on: …". **That is a greeting, not a
plan.** It recites what is open. It has no notion of which unanswered thing
would most change the differential, and the conversation that follows is
entirely patient-led.

The cost is visible in the ledger. A hypothesis the review generates from
narrative text often has no lab behind it and no way to get one, because the
only actor who could supply the missing fact — the patient — is never asked
for it in the one place she is actually present.

### What the evidence says

AMIE ([Nature 2025](https://www.nature.com/articles/s41586-025-08866-7),
[arXiv](https://arxiv.org/html/2401.05654v1)) outperformed primary care
physicians on 30 of 32 specialist-rated axes *and* on history-taking
quality. Its mechanism is directly applicable and worth stating precisely,
because it is not what people assume:

- **No state machine and no phases.** Not "history, then exam, then plan".
- Every turn runs three steps, and **step 1 is: summarise, produce a current
  differential, *identify missing information*, assess confidence**.
- Question selection is driven by **uncertainty over the differential** —
  which question would most discriminate — not by a checklist.

a-doc has the differential. It has the question store. It has the
conversation. It does not have step 1.

## Decision

Two decisions, in this order, because **one of them needs no model call and
the other does** — and I originally scoped them as a single thing, which hid
that the cheap half ships alone.

### 1. The chat follows up on the questions that already exist

Measured in production, 2026-09-04:

```
55 open questions, 0 ever answered
   36 audience="doctor"
   19 audience="you"     <- patient-answerable, open, never followed up
    0 with hypothesis_ids populated
```

Nineteen things the system has asked her and never returned to. They are in
the context pack under `Open Questions`, so the composer *sees* them — and
its prompt never mentions them, so they are passive context and nothing
more.

This needs **no generation**. The questions exist, each has an `ask` and a
`why`. What is missing is selection and an instruction:

- `composer.md` (→ v4) is required to end a diagnostic reply with **one**
  open `audience="you"` question, chosen by a deterministic ranking, and to
  ask it in the patient's own terms rather than reciting the stored text.
- Selection is plain code, not a model call: prefer a question linked to a
  hypothesis that changed this turn, then one never asked in chat, then the
  oldest.
- An answer closes it through the existing `resolve_answered` path, which
  already works — it is how the intake agent closes questions today.

Because the questions already carry `hypothesis_ids` *as a field*, ranking
by "which lead would this move" is a one-line change **once that field is
actually written** — see the consequence below.

### 2. `gap_scan` fills the gap when nothing open is worth asking

A stage between `apply` and `composer`. Given the just-applied ledger, it
names at most two unanswered things that would most change the differential,
as new `OpenQuestion`s with `audience="you"`.

**It runs only when decision 1 finds nothing to ask** — no open
patient-answerable question, or every one already declined. With 19 sitting
open, that is not today's problem; it is what stops the mechanism stalling
once the backlog is worked through.

### Constraints on both

1. **One question per turn.** A reply ending in four questions is a form,
   and she answers the easiest. AMIE's gain came from *targeted* questions,
   not volume.
2. **Patient-answerable only** — `audience="you"`. "What did your
   rheumatologist say" belongs in the review's list, not at the end of a
   chat reply.
3. **One store.** Same ids, same `resolve_answered`. A question asked in
   chat and answered in chat must close; a question the review already asked
   must not reappear under a new id.
4. **Never before `apply`** — the question is about the differential *this
   turn produced*, the same ordering argument ADR 0043 made for the engines.
5. **A gap-scan failure never fails a turn.** Empty is a valid outcome and
   is recorded as one.
6. **A question declined twice is not asked again.** `OpenQuestion` needs a
   `declined` status alongside `answered`.

## Consequences

- **`hypothesis_ids` is written by nobody.** `review.py` passes it at the
  call site and 0 of 55 stored questions carry one. The field exists, the
  writer does not — the fourth instance of that shape this week, after the
  rule-out evaluator, the flag predicates and the analyte lookup. Until it
  is fixed, ranking falls back to recency and the best signal available
  ("which lead would this answer move") is unusable. Worth fixing first; it
  is likely a one-line defect rather than a design change.
- **A fifth model call per diagnostic turn — but only for decision 2.**
  Decision 1 adds none. On a path that already takes minutes this matters,
  and it is the reason the two are separated.
- **The patient is asked something every turn.** That is the point and also
  a burden. The stage must be allowed to return nothing, and a twice-declined
  question must not come back.
- Answers arrive as free text and land as facts through the existing intake
  path, so no new capture surface is built.

## Alternatives considered

**A form in the review (the other path considered).** Rejected on the
project's own evidence. The motivating example is a selenium level falling
over time, whose true explanation was *"my functional medicine doctor
prescribed it, we realised it was too much, I stopped"* — causal, offered
unprompted, in the patient's own words. **No form field holds that.** A form
also collects answers at review time, which is weeks after the question
became interesting.

**Ask in the continuity note instead of the composer.** Rejected as
insufficient rather than wrong. The note fires once per *visit*; the
differential changes every *turn*, and 19 open questions will not clear one
visit at a time. Extending the note is worth doing anyway and is cheap.

**Let the composer decide what to ask, with no separate stage.** Rejected on
CLAUDE.md rule 3. The composer is the last stage before the patient and its
output is gated; adding "and also choose the clinical question" to the stage
whose contract is *never a diagnosis, never treatment* mixes two jobs, and
the choice would then be invisible to the DAG's contracts.

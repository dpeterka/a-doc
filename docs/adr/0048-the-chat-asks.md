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
(patient | doctor), `hypothesis_ids`, a `why`, and an answered state;
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

A `gap_scan` stage in the diagnostic chat DAG, between `apply` and
`composer`.

Given the just-applied ledger, it names **at most two** unanswered things
that would most change the differential, as `OpenQuestion`s with
`audience="patient"` and their `hypothesis_ids` populated. The composer is
then required to ask **one** of them — the highest-value one — at the end of
its reply.

Five constraints, each for a reason this codebase has already paid for:

1. **One question per turn.** A reply that ends in four questions is a form,
   and the patient answers the easiest one. AMIE's gain came from *targeted*
   questions, not volume.
2. **It must be answerable by her.** `audience="patient"` only. "What did
   your rheumatologist say" is a doctor question and belongs in the review's
   list, not at the end of a chat reply.
3. **Reuse `OpenQuestion` wholesale.** Same store, same ids, same
   `resolve_answered` path. A question asked in chat and answered in chat
   must close, and a question the review already asked must not be asked
   again under a new id.
4. **Never before `apply`.** The question is about the differential *this
   turn produced*, not the one it started with — the same ordering argument
   ADR 0043 made for the engines.
5. **A gap-scan failure never fails a turn.** Empty is a valid outcome and
   is recorded as one, the posture every other optional stage here takes.

The composer prompt gains the requirement and goes to version 4.

## Consequences

- **A fifth model call per diagnostic turn**, on a path that already takes
  minutes. Mitigated by ADR 0046's ticker, and `gap_scan` is a cheap
  structured call against a ledger already in context — not another
  frontier read of the whole case file.
- **The patient is asked something every turn.** That is the point and it is
  also a burden. The stage must be allowed to return nothing, and a question
  she has declined twice should not be re-asked — `OpenQuestion` needs a
  `declined` status alongside `answered`.
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
differential changes every *turn*. Extending the note is worth doing anyway
and is much cheaper — but it is a better greeting, not the missing stage.

**Let the composer decide what to ask, with no separate stage.** Rejected on
CLAUDE.md rule 3. The composer is the last stage before the patient and its
output is gated; adding "and also choose the clinical question" to the stage
whose contract is *never a diagnosis, never treatment* mixes two jobs, and
the choice would then be invisible to the DAG's contracts.

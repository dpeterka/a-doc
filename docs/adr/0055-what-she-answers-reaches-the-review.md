# ADR 0055 — What she answers reaches the review

- **Status**: accepted
- **Date**: 2026-09-18
- **Extends**: [ADR 0033](0033-open-questions-are-a-resolvable-record.md),
  [ADR 0032](0032-patient-reported-data-is-first-class.md)
- **Narrows**: [ADR 0038](0038-how-a-hypothesis-ends.md)

## Context

Four reviews on record; the board went 32 → 34 active with **0 leads off the
board in three consecutive runs**. Every convergence mechanism built so far
acts inside the review. Measured on 2026-09-15, so does the whole system:

```
logs/chat/   Aug 28, Aug 29, Sep 02   (89 lines total)
reviews      4 in the last 6 days
```

Three chat days in three weeks against four reviews in six days. The machine
talks to itself on a timer while the person it is about has stopped answering.
Three defects explain why answering achieves nothing.

**The review cannot see a single answer she has ever given.**
`render_for_context` filtered to open questions only, so all five
context-consuming review nodes could tell that a question had gone away and
nothing else — not whether she said yes, said no, or was simply never asked
again. `answer_note` was written and read **nowhere** in the codebase.

**Her words were never stored on a diagnostic turn.** `ledger_maintainer_stage`
passed the constant `"Answered in conversation."`; the intake capture pass
passed her real text. `chat.py` runs the diagnostic turn *first*, and
`mark_answered` skips an already-`answered` question — so the constant landed
and the real text was silently discarded. Two copies of one function, diverged.

**A model could end a lead by attributing a statement to her.**
`patient-report:` was in `DEFINITIVE_EXCLUSION_SOURCES`. The citation checker
resolves that scheme unconditionally — *"the patient's own statement, and
grammar-validity is enough"* — and `DefaultSourceTextResolver` has no resolver
for it, so the entailment verifier returns `insufficient_source`, which is
explicitly not a failure. One field, unchecked by anything, ending a can't-miss
lead.

## Decision

**1. One closer, one cap, her words.** `resolve_answered` is the only closer;
the duplicate in `intake/agent.py` is deleted. `ANSWER_NOTE_MAX = 280` lives in
`mark_answered`, not at each call site — the cap being sliced twice is how the
paths diverged in the first place.

**2. `mark_answered` reports which ids NEWLY closed.** Not a count. A caller
that wants to act on an answer needs to know which ones it may act on and must
not act twice; this is the idempotence key for turning an answer into evidence.

**3. The context pack gains a third block.**

```
**Recently answered — she has already told us these**
- `your-supplements` — Your supplements → 2026-09-01: "biotin 10mg, nothing else"
```

90 days, capped at 15, newest first, skipping answers with no recorded note
(the pre-fix backlog; `answered: ""` reads as though she answered emptily).
`today` is injected rather than read from the clock.

**4. `patient-report:` is removed from `DEFINITIVE_EXCLUSION_SOURCES`.** What a
person says still ends leads — through an **encounter**. The retire form writes
a `patient-report`-typed encounter and cites `encounter:<file>`, which
`DefaultSourceTextResolver` reads, so her words become entailment-checkable
where they were permanently unverifiable. No grammar change; no change to the
citation checker, whose unconditional resolution of `patient-report:` is pinned
by `evals/suites/hallucination.py` as a required CI gate.

**5. `case/questions-open.yaml` is committed once per turn.** Three places
wrote it on one diagnostic turn and none committed it; it rode the weekly
review's sweep, so up to a week of answered-question state was uncommitted and
therefore un-backed-up.

## The pinned property that changed

`test_context_renders_only_open_questions_with_their_ids` asserted `done.id not
in rendered` — an answered question must not appear at all. It now appears, in
its own block, labelled answered.

The property that assertion was protecting is unchanged and is pinned
explicitly: **an answered question is never offered as OPEN and never
re-asked.** The old assertion enforced that by making the answer invisible, and
the cost of that enforcement was the blindness this ADR exists to end.

## Consequences

- A stage can distinguish "she answered no" from "we stopped asking" for the
  first time.
- **More verbatim patient text lands in a committed file.** It already did via
  the intake path, so the PHI boundary is unchanged — but this makes it the
  normal case rather than the exception, and 280 characters is the bound.
- A definitive exclusion now requires a source something can resolve. Nothing
  already ruled out is un-ruled: the retire route sets status directly and
  `DEFINITIVE_EXCLUSION_SOURCES` governs only the automatic pass.
- **This ADR does not yet make an answer act on a lead.** That is the next
  step and is deliberately separate: it needs a prompt change and the safety
  suite, and shipping it before the exclusion source was severed would have
  handed the model the very route this closes.

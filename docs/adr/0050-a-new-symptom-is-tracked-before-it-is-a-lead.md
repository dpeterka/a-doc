# ADR 0050 — A new symptom is tracked before it is a lead

Status: proposed (2026-09-04)

## Context

Itchy, inflamed ears appeared recently. The review generated hypotheses for
it, including *"necrotizing otitis externa / skull-base osteomyelitis"* —
which the patient's own reading is "extremely unlikely", and which now sits
on a board of 46 active leads competing with hypotheses built on years of
serology.

The system has no notion that a two-week-old symptom and a four-year-old
pattern are different kinds of evidence. Every finding enters the
differential at the same weight the moment it is mentioned.

This is the wrong default for the problem a-doc is actually solving. The
goal is the **underlying** condition. A new, isolated symptom is far more
likely to be a manifestation of something already on the board — or a
self-limiting thing that resolves — than an independent disease deserving
its own three leads.

### The data already supports it

`PhenotypeTerm` carries `first_seen` and `last_seen`, populated from the
encounter each phrase was found in. **Symptom duration is already computable
and entirely unused** for weighting. `select_for_engine` already ranks terms
by recency for the engine query, so there is precedent for the shape.

### What the evidence says, in both directions

**For windowing.** The [VAMPIRE randomised trial](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3353203/)
found that watchful waiting on unexplained complaints "lowers both the
number of patients to be tested and the risk of false-positive test results,
**without missing serious pathology**." That is the same statistical
argument as reducing this board, from the clinical literature.

**Against discarding.** Premature closure is a named diagnostic error, and
the literature is specific that it is "particularly common when patients
seem to be having an exacerbation of a known disorder" — that is, when a new
finding is folded into the existing story instead of being taken on its own
terms. Down-weighting a new symptom is *exactly* how the new thing gets
missed.

Those two are only compatible if the new finding is **tracked and visible**
rather than suppressed. That constraint drives the whole design below.

**On the threshold.** Three months is a real convention — the NCHS
chronic-disease definition, the DSM-5 chronicity specifier — but it is not
universal: chronic cough is 8 weeks, the CDC uses a year, chronic migraine
is 15 days a month for 3 months. **A hardcoded 90 would be arbitrary
precision.**

## Decision

### 1. An `emerging` section, outside the differential

A hypothesis whose supporting phenotype terms are **all** newer than
`EMERGING_WINDOW_DAYS` renders in its own section and is excluded from the
differential's counts, its tier totals, and the agenda's lead list.

It is **not** a tier and **not** a status — it is a derived view over
`first_seen`, computed at render time. Nothing is written to the ledger,
nothing is deleted, and the classification re-evaluates itself every review
as the dates move. A finding that turns out to matter promotes itself
without anyone editing anything.

### 2. Tracked, never hidden

The section is titled for what it is — recent findings still being watched —
and says plainly that these are not being ignored, only not yet being
treated as established. A `can't-miss` lead is **never** emerging: the
safety checklist exists precisely for the dangerous-but-unlikely case, and
ADR 0039 already made it read correctly to a patient. Deferring a
can't-miss lead is the premature-closure failure the literature names.

### 3. Three routes out, any one of which promotes it

- **Duration.** The oldest supporting term crosses the window.
- **Corroboration.** A lab, document or engine independently supports it —
  the finding is no longer only a recent mention.
- **Recurrence.** `first_seen` and `last_seen` span more than the window
  even if the finding is intermittent, which is how an episodic condition
  presents.

### 4. The window is configurable with a stated default

`EMERGING_WINDOW_DAYS = 90`, in `config.Settings`, with the NCHS convention
cited as its basis and its arbitrariness stated. Not a constant buried in a
module, because the literature says the right number varies by condition and
this one will need revisiting.

## Consequences

- **The differential shrinks by however many leads rest only on recent
  findings**, and stops growing by that class. Prevention rather than cure:
  it does not touch the 46 that are already there.
- **A wrong window silently defers a real finding.** Mitigated by the
  section being visible and by promotion on corroboration rather than time
  alone — but it is the risk, and it is why can't-miss is exempt.
- Requires phenotype terms to be reliably dated. Terms with no `first_seen`
  must count as **not** emerging: unknown age is not the same as new, and
  defaulting the other way would defer the oldest findings in the record.
- The review report gains a section and the agenda does not. A one-page
  document for a 15-minute appointment is the wrong place for findings the
  system is explicitly still watching.

## Alternatives considered

**Down-weight the probability instead of sectioning.** Rejected. It buries
the mechanism inside a number the model also writes, so nobody could tell a
down-weighted new finding from one the panel simply thought unlikely. The
whole point is that the reader can see the distinction.

**Refuse to create the hypothesis until the window passes.** Rejected — it
is the discard the premature-closure literature warns about, and it loses
the observation entirely. Tracking costs nothing.

**Use a per-condition threshold from the literature.** Correct in principle
and rejected as scope: it needs a curated table of durations per condition,
which is a knowledge-layer project. One configurable default, honestly
labelled, is the right first version.

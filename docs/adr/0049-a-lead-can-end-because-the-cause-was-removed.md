# ADR 0049 — A lead can end because the cause was removed

Status: proposed (2026-09-04)

Extends [ADR 0038](0038-how-a-hypothesis-ends.md) and
[ADR 0047](0047-a-lead-states-how-it-ends.md).

## Context

A real case from this ledger:

> Selenium was high. Over subsequent draws it fell steadily toward normal.
> The hypothesis "selenium excess from supplementation" was **correct**. The
> reason it resolved is that a functional-medicine doctor had prescribed a
> selenium supplement, the dose turned out to be too much, and the patient
> stopped taking it.

Three things are wrong with how the system handles that.

**It cannot represent the outcome.** A hypothesis ends as `ruled-out` or
`parked`. Neither fits. `ruled-out` says the hypothesis was false — it was
not, it was true and is now resolved. `parked` says nobody is looking any
more, which loses the finding entirely. The correct outcome, *"this was
real, the cause was removed, it is resolved"*, has no representation, so the
lead sits active forever with a falling analyte under it.

**It never asks why.** `TrendFinding` is `analyte / date / value / message`.
It can say selenium is falling. It has no slot for *why*, and nothing in the
pipeline asks. The one actor who knows is the patient.

**The data model could already hold the answer.** `casefile/regimen.py`
models what she takes as intervals with a start and an optional stop,
precisely so "was she on biotin when this specimen was drawn" is answerable.
A stopped selenium supplement is exactly a closed interval. Nothing connects
a falling analyte to a regimen interval that ended.

This matters beyond one lead. A resolved-by-action outcome is a
**deterministic subtraction** the ledger currently cannot express — and the
board's problem is that almost nothing subtracts.

## Decision

### 1. `resolved` is a distinct status, not a flavour of `ruled-out`

A fourth outcome alongside `active`, `monitoring`, `parked`, `ruled-out`:

> **`resolved`** — the hypothesis was true and the finding no longer stands,
> because the cause was removed or treated. Not a refutation. It stays on
> the record with its evidence intact and its reason recorded.

The distinction is clinical, not cosmetic. A doctor reading "selenium excess
— ruled out" concludes it never happened and may re-prescribe. Reading
"resolved: supplement stopped 2026-05, level normalised by 2026-08" they
know both the finding and its history.

It renders in its own line on the case page and on the agenda, never folded
in with the excluded leads.

### 2. A trend that moves toward normal earns a question

`TrendFinding` gains `direction` and `toward_reference`. When an analyte
that supported an active hypothesis is trending toward its reference range,
the review opens an `OpenQuestion` with `audience="patient"`:

> I see your selenium was high and has been coming down. Do you know what
> changed — did you stop or change a supplement or medication?

Deterministic to detect (the trend scan already computes the series); the
*answer* is the part only a person has. This is ADR 0048's mechanism reused,
not a second one: same store, same ids, same close path.

### 3. A closed regimen interval is offered as the explanation

When a regimen entry for a matching substance has a `stopped` date preceding
the downward trend, the question names it rather than asking blind:

> …your record shows you stopped a selenium supplement around 2026-05. Is
> that what changed?

**Offered, never asserted.** `regimen.py`'s whole posture is that an
undated entry is `unknown` rather than guessed, and this inherits it: the
system proposes a candidate cause and the patient confirms or corrects. A
confirmed answer is what moves the hypothesis to `resolved`, not the
correlation.

## Consequences

- **A new `status` value touches every consumer** — the web card, the
  agenda, `group_hypotheses`, the retirement pass, `ACTIVE_STATUSES`. A
  status a renderer does not recognise must not silently disappear from a
  page, so each consumer needs an explicit branch and a test.
- **`resolved` is not `is_protected` and not retirement-eligible.** It is
  already ended. The retirement pass should skip it rather than reason about
  it.
- The trend→regimen match is a heuristic and will sometimes name the wrong
  substance. That is acceptable *only* because it is phrased as a question.
  If it ever becomes an automatic transition, it stops being acceptable.
- Small immediate effect: one lead today. The value is the category, which
  every future resolved-by-action finding needs.

## Alternatives considered

**Reuse `ruled-out` with a note explaining.** Rejected. The status is what
code branches on and what a reader sees first; a note is what neither
consults. This is the same argument ADR 0038 made for
`definitive-exclusion` being a distinct strength rather than "very strong".

**Infer the resolution automatically from the trend plus the regimen
interval.** Rejected. It is a correlation, and acting on it would be the
system deciding a hypothesis is over on temporal coincidence. ADR 0042
established the same boundary for suppressed markers: name the candidate,
let a human make the causal claim.

**Wait for ADR 0048 and let `gap_scan` find this.** Rejected as
insufficient. `gap_scan` reasons over the differential; a trend heading
toward normal is a deterministic observation over the labs, and detecting it
in code is cheaper and more reliable than hoping a model notices.

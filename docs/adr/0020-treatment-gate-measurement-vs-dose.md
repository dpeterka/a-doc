# ADR 0020 — The treatment gate's dosage pattern requires dosing context

- Status: Accepted (2026-08-25)
- Narrows the dosage-detection half of the treatment gate described in
  PLAN.md "Safety" and CLAUDE.md rule 5
  (`reason/safety.py::treatment_gate`/`_DOSAGE_RE`). The imperative/hortative
  detector (`_imperative_treatment_spans`) is untouched.

## Context

`treatment_gate` is the deterministic, code-only screen that keeps
dosing/prescriptive treatment language out of every patient-facing reply
(CLAUDE.md rule 5). Its dosage half (`_DOSAGE_RE`) matched any bare
`<number><unit>` shape — `mg`, `mcg`, `g`, `unit(s)`, `iu`, `ml` — with a
narrow carve-out for lab-result concentration denominators (`mg/dL`,
`mg/L`, ...) so a quantitative lab value wasn't mistaken for a dose.

That carve-out already conceded the design's central problem: a bare
number+unit is not, on its own, enough to tell a dose from a measurement.
It just didn't go far enough. In a real production run, a full diagnostic
turn was blocked with exactly one offending span:

```
treatment gate blocked patient-facing output: '106.0 mL' (dosage pattern)
```

`106.0 mL` is a volume measurement from a pelvic/abdominal ultrasound —
an ordinary, clinically meaningful finding in this patient's record, not a
dose, not an instruction, and not something she should be protected from
reading. The Composer's one gate-guided rewrite pass did not remove it
(unsurprising: it's a real finding it was asked to report), so the turn
died as a `ContractViolation` — after the ledger had already been
committed for that turn (`apply` always runs before `composer` in
`build_diagnostic_dag`). The patient's case file was updated; she just
never saw the reply.

A dose is part of an **instruction**. A measurement is part of a
**finding**. `mL`, `g`, and bare `unit(s)` are common units for ordinary
clinical measurements that have nothing to do with dosing — an ultrasound
or urine volume, a specimen/organ mass, a bone-density numerator (as in
`g/cm²`), a transfused-blood or lab-panel "units" count — and the gate was
treating every one of them as a suspected dose.

## Decision

Split the dosage regex into two groups with different rules:

1. **`mg`, `mcg`, `iu` still fire unconditionally**, exactly as before (past
   the existing lab-concentration-denominator carve-out). In this domain a
   bare, denominator-free amount in these units is overwhelmingly a
   medication or supplement dose ("20 mg prednisone", "5000 IU vitamin D",
   "50 mcg levothyroxine"); genuine lab values that use them almost always
   carry a denominator (`mg/dL`, `ng/mL`, `mIU/mL`, ...), which is either
   already excluded by the concentration carve-out or simply isn't one of
   these unit tokens in the first place (`ng/mL` never reaches this regex:
   the token right after the number is `ng`, not one it matches).
2. **`g`, `ml`, and `unit(s)` now require corroborating dosing context** in
   the same clause before they count as a dosage span: either an
   imperative/hortative treatment verb (the same vocabulary
   `_imperative_treatment_spans` already uses — "take", "start", "stop",
   "increase", "taper", ...; not duplicated, reused directly), or a dosing
   frequency/schedule term ("daily", "twice a day", "BID", "every 8
   hours", "at bedtime", "PRN", "with meals", ...). Without either signal
   in the same clause, a bare number in these units is treated as what it
   almost always is here: a measurement.

This is a deliberate judgment call, not a mechanical rule applied
uniformly: `mg`/`mcg`/`iu` keep the old (context-free) behavior because
they are overwhelmingly dose-shaped in this patient's record even alone,
while `g`/`ml`/`unit(s)` are common bare units for genuine measurements and
needed the extra signal. This accepts a small residual risk in the other
direction — a rare bare-`mg` measurement (e.g. a kidney-stone weight) could
still trip the gate unnecessarily — in exchange for not needing dosing
context to catch the far more common real dose in those units.

The composer's gate-guided rewrite feedback (`reason/stages.py`) was also
made to name the exact offending span and distinguish the two kinds
`treatment_gate` can now produce — "these are dosing amounts, describe the
medication without the dose" vs. "these read as instructions to
start/stop/change a medication, reframe as a lead to discuss" — instead of
a single generic "remove any drug name, dose, or instruction" line that
was confusing when (as in the incident above) the model reasonably kept a
measurement it was told to report.

## Consequences

- A bare clinical measurement in mL, g, or units (an ultrasound/urine
  volume, a specimen mass, a BMD in g/cm², a transfusion count) now passes
  the gate untouched, with or without a preceding rewrite attempt.
- A liquid dose ("take 5 mL twice daily", "the dose is 5 mL twice daily")
  still blocks — the mL narrowing does not become a hole, because either
  signal (imperative verb or frequency term) alone is sufficient.
- Every existing blocked fixture case in `tests/fixtures/redteam.yaml`
  still blocks; none relied on the loosened path (they are all caught by
  the imperative-verb detector, the still-unconditional `mg` firing, or
  both).
- This is a narrowing of a pinned safety-gate property under CLAUDE.md
  rule 2, made deliberately and on the record rather than as a silent edit
  to make a diff go green: the property was "any bare number+unit in this
  list is suspect"; it is now "a bare number+unit in this list is suspect
  only when it's actually dose-shaped, either because the unit itself is
  that dose-like or because dosing context corroborates it."

## Alternatives rejected

- **Looser rewrite instruction only, leave the regex as-is.** Doesn't fix
  the problem: a genuine measurement the Composer is *correctly* reporting
  isn't something the model should rewrite away, so the rewrite pass
  either strips a real finding or (as observed) leaves it and the turn
  still dies. The bug is in what counts as a dosage span in the first
  place, not in how the model is coached to respond to one.
- **Let the Composer strip measurements it's told to report.** Rejected on
  the same grounds PLAN.md's quantitative-grounding check exists for: the
  Composer's job is to report findings accurately, and training it to
  reflexively delete a legitimate number because a downstream filter might
  misfire is the wrong layer to absorb the fix — the filter should be
  correct, not routed around.
- **Drop the dosage-pattern rule entirely, rely only on the
  imperative-verb detector.** Rejected: it would miss a dose stated without
  an imperative construction ("the recommended dose is 5 mL twice daily"),
  which the frequency-term signal in this ADR's decision still catches.

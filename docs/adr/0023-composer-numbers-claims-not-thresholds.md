# ADR 0023 — `composer_number_check` checks claimed values, not reference numbers

- Status: Accepted (2026-08-26)
- Refines the `composer_number_check` contract introduced under ADR 0002 and
  last revised in ADR 0016 (third pass). Does not change what the check is
  *for*.

## Context

`composer_number_check` exists to enforce a Phase 2 property: every number
in patient-facing output that is attributable to a lab value must match
`labs.sqlite` exactly. It is deterministic code, it is a DAG postcondition
on the Composer, and when it fires the patient's reply is withheld.

It has now been narrowed four times, and every firing in its history has
been a false positive:

1. A percent CHANGE and a bare YEAR read as claimed values
   ("Ferritin dropped by 40% since 2024").
2. FSH checked against LH's stored values because both shared a clause.
3. `hs-CRP` resolving as plain `CRP` on a substring match.
4. This one.

A full diagnostic turn on the enriched corpus was lost to six mismatches at
once, after 604 seconds of work:

    0.08 near 'amh'       (stored: 0.02, 0.16, 0.18, 0.57, 0.79)
    20.0 near 'vitamin d' (stored: 24.1 ... 84.3)
    1.5  near 'histamine' (stored: 0.28)
    0.0  near 'beef ige'  (stored: 0.22, 0.24, 0.25, 0.32)
    1.0  near 'beef ige'  (stored: 0.22, 0.24, 0.25, 0.32)

Every one of those is a **threshold**, not a claimed result: an assay floor
(AMH < 0.08), a deficiency cutoff (vitamin D < 20), a decision limit, an
IgE class boundary. Reproduced deterministically against the real
`labs.sqlite`, the mechanism is exact — and the worst case flagged is:

    "Vitamin D insufficiency is defined as below 30 ng/mL."   → FLAGGED

That sentence makes no claim about this patient whatsoever. It is a
definition. The check blocked her entire answer over it.

The reason it slipped every previous narrowing: ADR 0016's positive-evidence
rule accepts "a unit is directly attached" as proof a number is a value, and
threshold phrasing virtually always attaches a real unit. Nothing looked at
the word *governing* the number.

## Decision

**A number governed by a comparator is not a claimed value.** When the
closest preceding non-filler word is a comparator (`below`, `above`,
`under`, `over`, `less`/`greater`/`fewer`/`more ... than`, `at least`,
`at most`, `threshold`, `cutoff`) or a comparison symbol (`<`, `>`, `<=`,
`>=`, `≤`, `≥`), the number is exempt from the exact-match requirement.

The veto runs BEFORE the positive-evidence check, precisely because the
attached unit would otherwise satisfy it.

Assertive phrasing is untouched and still requires an exact match:
"your vitamin D was 24.1 ng/mL", "AMH came back at 3.7", "histamine
measured 9.9". That is the shape a fabricated value actually takes.

## Consequences

- The six live false positives clear, and fabricated assertive values still
  flag. Both directions are pinned by tests.
- **What this gives up**: a fabricated *comparative* claim — "your CRP is
  above 50" when the stored maximum is 3 — no longer trips this check. That
  is a real reduction in coverage, accepted for two reasons. First, this
  check has never once caught a true positive, while each false positive
  withholds the patient's whole answer after minutes of work; the expected
  cost of firing wrongly has been consistently higher than the expected
  benefit of firing at all. Second, a fabricated comparative claim about a
  lab value is exactly what the entailment verifier (ADR 0016) is for: it
  judges each claim against its cited source text, where "above 50" against
  a row reading 3 is not entailed.
- A rule that keeps needing narrowing is evidence the property is hard to
  state, not that the implementation is sloppy. The honest framing after
  four rounds: this check reliably catches *quoted numbers that do not
  exist in the patient's data*, and reliably cannot tell *which* number in
  a clause belongs to *which* analyte, nor a claim from a reference. It
  should stay scoped to the first.
- If a fifth false-positive class appears, the next step is not a fifth
  narrowing: it is to demote this postcondition to a warning recorded on
  the turn (as ADR 0014 did for red-flag screening, and ADR 0021 finished),
  leaving the entailment verifier as the blocking grounding guard.

# ADR 0027 — Temporal fidelity: trajectories, and dates stated no more precisely than known

- Status: Accepted (2026-08-27)
- Extends the context-pack design (ADR 0015's retrieval section ordering) and
  the encounter file format (PLAN.md "Key schemas").

## Context

Asked how the timeline is tracked across lab results, ingested narrative and
patient statements, an audit found the layers uneven.

**Well modelled.** `LabResult` separates `date` (specimen collection — the
clinical event) from `created_at` (when the row was written).
`LabDocument` separates `doc_date` from `ingested_at`. `IntakeFact` is the
strongest of all, carrying three distinct fields: `date_approx` (when it
happened), `precision` (how well that is known), and `reported_on` (when the
patient said it). That is the right model for a diagnostic odyssey, where a
patient in 2026 recalls a thyroid crisis "around 2021".

**Two real defects.**

*The context pack had no trajectory.* Its labs sections are "Abnormal, most
recent per analyte" and "Latest panel" — a snapshot. There was no time
series anywhere in `context.py`. A reasoning stage could see movement only
by calling the `query_labs` tool, or by reading a document that narrates its
own comparison. That is exactly how the blind panel knew about the DEXA
decline: the report did the arithmetic. An analyte no document comments on
had no visible slope at all — and for this patient the slope is often the
finding. Measured on the real corpus, the snapshot was hiding
`LH rising 1330%`, `FSH rising 1119%`, and `AMH falling 96%` — the
ovarian-failure signature, invisible in any single row.

*Encounters asserted fabricated precision.* `EncounterFrontmatter` had only
`date`. The intake parser resolves `"2021"` to `2021-01-01`, `"early 2021"`
likewise, and `"spring 2022"` to `2022-01-01` — the wrong season, stated to
the day. Downstream nothing could tell that from a real January 1st, and the
`reported_on` distinction the intake layer had carefully captured was
discarded at exactly the point the case file becomes the durable record.

## Decision

**1. A trajectories section, computed deterministically.**

`_trajectories_section` reports analytes with at least 3 readings and a net
change over 20%, ranked by magnitude and capped, placed immediately after
the labs snapshot — the reader needs to know what the values *are* before
being told which are moving. Direction and percent change only: whether a
rise is good or bad is the reasoner's judgement, not this function's.

**Only readings sharing a unit are compared.** The first implementation
compared first-to-last naively and reported `eosinophils rising 319,900%` —
a unit change mid-history (`x10E3/uL` to `cells/uL`, a factor of 1000), not
a clinical event. 26 of this patient's 461 analytes are stored under more
than one unit, in two distinct kinds: cosmetic variants (`IU/L`/`U/L`,
`mcg/dL`/`ug/dL`) and genuinely different scales. `canonical_unit` resolves
the former but returns `None` for many real units, and two `None`s must not
be treated as equal, so comparability falls back to the normalized raw
string. A mixed-unit series is scoped to its **most recent** unit rather
than dropped, keeping the clinically current scale.

**2. Encounters record how precisely they know their own date.**

`date_precision` (`day` | `month` | `year` | `approximate`) and
`reported_on` are added to `EncounterFrontmatter`, both defaulting so every
existing encounter file round-trips unchanged. The intake writer populates
them from the same parse that produces the date. The context pack renders
`2021`, `2024-06`, `~2022` rather than a false `2021-01-01`, and shows
`(reported <date>)` when recall differs from the event.

## Consequences

- A reasoning stage can see direction without spending a tool call, and the
  blind panel no longer depends on a document having narrated its own trend.
- The trajectory section adds a section key, so the pinned context-order
  tests were updated deliberately — that pin exists because the
  blind-review `forbid_context_key` contract depends on `keys`.
- The unit-comparability rule is conservative and will occasionally decline
  to report a real trend when a unit changed and few readings share the
  current one. That is the correct direction to err: a missed trend is a
  missed prompt, a fabricated one is a finding.
- **The underlying unit inconsistency is not fixed.** ALT is stored under
  three spellings, the CBC absolutes under three units. `labs-recanonicalize`
  normalizes names; nothing normalizes units. Worth its own change.
- Encounters written before this ADR carry `date_precision: day` by default,
  which is a *claim* about them — day-precision is right for the
  document-sourced majority but wrong for any patient-reported encounter
  already on disk. Those are not retro-corrected; the field is honest going
  forward and silently optimistic for existing patient-report files.

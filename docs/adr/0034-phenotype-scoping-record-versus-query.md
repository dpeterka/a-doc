# ADR 0034 — Phenotype scoping: the record is not the query

Status: accepted (2026-08-29)

## Context

`case/phenotype.yaml` accumulates every HPO term the corpus supports. It grew
to 82 terms and now stands at 90 across 53 sources. As a record that is
correct and desirable: it is the longitudinal phenotype, and terms should
accumulate as evidence accumulates.

Handed to a phenotype-driven engine, the same artifact is close to useless.
LIRICAL returned 0.00% across the board on the full profile. The first reading
of that — recorded and then retracted in ADR 0029 — was that a diffuse
presentation is simply hard for the engine. That was asserted without testing
and it was wrong.

A term sweep against this patient's real profile shows what actually happens
to LIRICAL's composite likelihood ratio for its top-ranked disease:

| terms | top-ranked disease | LR |
|---|---|---|
| 5 | Autoinflammatory disease, systemic, with vasculitis | +2.29 |
| 6 | (same) | +4.69 |
| **8** | **(same)** | **+4.82** |
| 10 | (same) | +2.12 |
| 12 | Charge syndrome | −0.42 |
| 15 | Celiac disease, susceptibility to, 1 | −0.69 |
| 20 | (same) | −2.94 |
| 30 | (same) | −7.01 |
| 82 | Autoinflammatory disease, systemic, with vasculitis | −25.97 |

Two things follow. The score declines **monotonically** with profile size,
because a term no candidate disease explains subtracts without bound — so a
complete record makes a poor engine input however good a record it is. And
five to ten terms is a **stable region**: four independent term sets converge
on the same disease, which is a stronger signal than any single run. Past ten
it degrades into noise; "Charge syndrome" at twelve is not a serious candidate
for this patient.

This was not an engine problem. It was a query problem.

## Decision

**The full profile is the RECORD. What an engine receives is a QUERY. They are
different artifacts and conflating them is what produced an unusable ranking.**

`phenotype.select_for_engine` builds the query. It never mutates the profile.

**Size.** `ENGINE_TERM_LIMIT = 8` — a defensible point inside the measured
stable region, not a guess. The first draft of this constant said 12 and the
data contradicted it.

**Selection, in order:**

1. **Current first.** A finding last seen within `ENGINE_RECENCY_DAYS` (730)
   outranks an older one. A 2021 episode that never recurred is history, and a
   differential about today should not be asked to explain it. Two years is
   long enough to keep a chronic finding mentioned only at annual reviews,
   short enough to drop a resolved episode.
2. **Then corroboration.** More independent sources means more confidence the
   term is real — these are matched from text and some matches are wrong.
3. **Undated terms last, not never.** A term with no date may still be
   current, so it fills remaining slots rather than being dropped.

**Excluded terms are not capped.** LIRICAL takes negated phenotypes as
evidence, there are typically few of them, and each is a deliberate clinical
statement rather than an incidental mention.

## Consequences

- The ranking becomes usable: +4.82 at eight terms against −25.97 at
  eighty-two. This is the prerequisite for wiring LIRICAL into divergence
  adjudication (phase 3, criterion 1) — that work was never blocked on
  plumbing.
- **The constant is one patient's measurement.** Eight is defensible inside
  the stable region; it is not a universally optimal number. What generalises
  is the shape of the curve, not the value. Re-measure before treating it as
  settled for any other profile.
- **A query is lossy on purpose.** Seventy-plus terms are withheld from the
  engine on every run. That is the point, but it means an engine result is
  evidence about the *selected* phenotype, not about the patient entire, and
  it must never be presented as the latter.
- **Selection is deterministic and unexplained to the patient.** Which eight
  terms were chosen is reproducible from the profile and the date, but nothing
  surfaces that choice in the UI. If an engine result ever reaches a
  patient-facing surface, the terms behind it need to reach it too.
- Recency is measured from `last_seen or first_seen`. A term whose only
  mention is an undated document ranks below every dated one regardless of how
  clinically important it is — corroboration count is the only thing that
  lifts it.
- This ADR documents behaviour that already shipped. The reasoning and the
  measured table lived only in a module docstring, where a load-bearing
  architectural rule is easy to lose. CLAUDE.md requires an ADR for exactly
  this kind of decision.

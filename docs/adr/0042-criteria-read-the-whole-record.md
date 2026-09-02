# ADR 0042 — Criteria read the whole record

Status: accepted (2026-09-02)

Fifth and last item in the adversarial-review adoption track (PLAN.md).
Closes CLN-03.

## Context

`LabView` kept only the most recent row per analyte, on this reasoning, which
was in its docstring:

> Criteria ask "is this analyte abnormal", not "what was it on the 4th", so
> the view keeps the MOST RECENT row per analyte. A criteria set describes a
> patient's current classifiable state; an abnormality from four years ago
> that has since resolved should not keep scoring points forever.

That is wrong for a **classification** set, and the code already knew it.
`score_sle_2019`'s own docstring has said since it was written that the entry
criterion is "ANA ≥1:80 **ever**" — while the implementation read the latest
ANA. The 2019 EULAR/ACR criteria state that criteria need not occur
simultaneously; they are cumulative by construction.

CLN-03 predicted the consequence and it reproduces exactly. On one timeline —
ANA 1:640, anti-dsDNA positive, C3 and C4 flagged low, WBC 2.9 in 2024; every
one of them normal in 2026, which is what successful treatment looks like:

```
before:  entry_met=False  points=0/10   "the 2019 criteria do not apply"
after:   entry_met=True   points=13/10  threshold met
```

**Adding a later normal result erased the entire historical basis.** The old
behaviour scored successful suppression as evidence the disease had never
been there, and the report said the criteria did not apply — which reads, to
a patient and to a doctor, as ruled out.

## Decision

### 1. Every laboratory criterion reads the whole record

`LabView` keeps the full history alongside the latest row and gains
`history(*patterns)`, returning every *draw* rather than one row per analyte.
Each item helper takes a `lookback: Lookback = "ever" | "current"`, defaulting
to `"ever"`, and is met if **any** draw satisfies it.

`current` exists so a criterion that genuinely describes a present state can
say so explicitly rather than by omission. Nothing uses it today; every set
encoded in this module is a classification set.

Two thresholds needed their own treatment:

- **Complement** reads the *lowest* recorded value, not the first below
  reference — reference ranges differ between labs, so the comparison has to
  happen on the row carrying its own range. Consumed complement is the marker
  CLN-03 named first and the textbook case of one that normalises on
  treatment.
- **Counts with units** read the *peak*. The published EGPA criterion is a
  blood eosinophil count ≥1×10⁹/L, which in practice means the highest
  recorded one; a patient on steroids has a normal count today and had 4.2
  before treatment.

### 2. `ever` must not become "met if measured"

The floor stays a floor. A criterion no draw ever satisfied is `not_met`, and
its basis says how many draws were examined:
`2 draws on file, none meeting it; most recent 7.1 on 2026-05-02.` Pinned in
both directions — a test fails if `ever` starts meaning "positive if
measured", and another fails if `met_ever` stops distinguishing.

### 3. Both facts render, never just the convenient one

A criterion met historically carries `met_ever=True` and a `superseded` note,
cites **both** draws, and renders *outside* ADR 0040's collapsed table:

> **Met earlier, not on the most recent draw:**
> - _Anti-dsDNA or anti-Smith_ — Met by Positive on 2024-03-01; the most
>   recent value is Negative on 2026-07-01. On 2026-07-01 the record has
>   Prednisone in use, which can suppress this marker.

Hiding the resolution would be the mirror-image error of the one this ADR
fixes. The point is that the reader gets the pair.

### 4. The regimen supplies the context, and only context

CLN-03's second half. `suppressants_active_on` reads `case/regimen.yaml`
through `Regimen.active_on`, so an entry that cannot be placed in time
contributes nothing rather than a confident wrong answer, and a drug stopped
before the draw is not named.

Three properties make this safe to add:

- **It never changes a score.** A test pins that scoring with and without a
  regimen gives identical points and the same `meets_threshold`. Passing
  nothing costs a sentence of explanation, which is why the missing file is
  a degradation rather than a wrong answer.
- **It names the drug and stops.** "…which can suppress this marker" — the
  inference is the reader's. This module never claims causation.
- **The drug list is short, named and documented as non-exhaustive.**
  `IMMUNOSUPPRESSANT_PATTERNS`, 22 patterns. A name not on it produces no
  note. Since a miss costs only an explanation, a short list beats a drug
  database.

### 5. A flag bug this uncovered, fixed at the enum

Writing the tests turned up that `LabFlag` has five members and both criteria
predicates matched their names by hand-written string sets:

| Flag | `_below_reference` | `_numeric_above_ref` |
|---|---|---|
| `L` | low ✓ | — |
| `LL` | **missed** | — |
| `H` | — | high ✓ |
| `HH` | — | **missed** |
| `A` | missed | missed (matched `"abnormal"`, never produced) |

**A critically low complement — the most clinically significant value the
analyte can carry — registered as normal everywhere in the criteria
scorers.** So did every critically high value. Three of five members matched
nothing.

The fix is `labs.models.flag_is_low` / `flag_is_high`, derived from the enum
and shared by all three call sites (the third,
`casefile/reported_corroborate.py`, had its own set with the same holes plus
two spellings the enum never produces). `A` is now *deliberately* neither: it
records that a value is out of range without saying which way, and guessing a
direction from it would invent a finding.

This is the same shape as the `_RA_RF` regex that could never match: a
criterion silently unable to fire, with no test covering it, looking exactly
like a criterion that fires and finds nothing.

## Consequences

- **`tests/test_criteria.py::test_the_most_recent_value_decides` is replaced**
  by `test_a_resolved_abnormality_still_counts_and_says_it_resolved`. It
  pinned the property this ADR reverses. CLAUDE.md rule 2 requires an ADR for
  exactly that, and the replacement test carries the measurement in its
  docstring so the reversal is not silently re-reversed later.
- **Scores will go up, and one set now classifies where it did not.** That is
  the correction, not a side effect. ADR 0040's sentence already frames it:
  meeting a set "says this case would count in a study of the condition,
  which is a different question from whether you have it."
- `case/regimen.yaml` becomes a second consumer of the file ADR 0041 already
  gave a row in `docs/deployment-dependencies.md`.
- `score_all` and all seven scorers take a third optional `regimen`
  parameter. Existing callers keep working; the review DAG's `criteria_scan`
  node passes it.
- The old reasoning was not stupid — a transiently abnormal value should not
  score forever. The published criteria answer that with the attribution rule
  a clinician applies, which this module already models as `possible` rather
  than `met`.

## Alternatives considered

**A time window (e.g. "within five years").** Rejected. It would be a
constant with no basis in any published set, and the interesting values in
this case file are older than any window short enough to matter.

**Score both ways and show both.** Rejected. Two point totals for one
criteria set is exactly the "three numbers on three scales" problem ADR 0039
just removed, and one of the two would be wrong.

**Infer suppression and discount the normal result.** Rejected firmly. That
is a clinical judgement about drug effect on a specific assay in a specific
patient. Naming the drug and the dates gives a clinician everything needed to
make it; making it here would be this module claiming causation it cannot
support.

**Leave `A` out of the flag helpers entirely.** Considered and kept as-is:
`A` is a real value that arrives from real extractions, so it needs a defined
answer. "Neither, deliberately" is that answer, with a test.

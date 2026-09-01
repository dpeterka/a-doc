# ADR 0038 — How a hypothesis ends

Status: accepted (2026-09-01)

Supersedes part of [ADR 0035](0035-a-ledger-that-can-end-a-hypothesis.md).

## Context

ADR 0035 gave the ledger a way to end a hypothesis and it worked: the first
review to run it parked 8 of 50 leads. Three gaps remain, and an adversarial
review (CLN-01, CLN-05, PAT-03) named all three. They are one mechanism, so
they get one ADR.

### 1. Nothing evaluates a rule-out

ADR 0035 required every new hypothesis to state `rule_out` — "the specific
result that ends it". That requirement is enforced: `strip_ops_missing_rule_out`
drops any `add_hypothesis` op without one.

Nothing ever checks whether the stated condition has been MET. `rule_out` is
written at creation and never read again. Measured in production: 46 active
hypotheses, 0 with a rule-out ever evaluated.

### 2. The balance scale cannot express an exclusion

`_outweighed` sums evidence, strong counting double:

```python
against = sum(2 if e.strength == "strong" else 1 for e in evidence_against)
```

Clinical exclusion is not additive. A negative serum metanephrines excludes
pheochromocytoma however many non-specific symptoms point at it; ten weak
supporting findings never overcome one definitive negative. The current model
cannot say "this one result settles it" — only "there is more against than
for".

### 3. Nothing a human knows can end a lead

`is_protected` excludes `cant-miss` and patient-origin hypotheses from
automatic retirement absolutely. That is correct for an *automatic* pass, and
ADR 0035 was right to insist on it. But `/ledger` is a single GET route: there
is no path at all for the patient to record that her doctor ruled something
out. So a protected lead cannot be ended by any means — not by the system, not
by her, not by her doctor.

The result is visible: 10 can't-miss leads, none ever retired, on a page whose
lead section is headed "Worth discussing now".

## Decision

A hypothesis ends one of three ways. The first two are new.

### A. Its stated rule-out is met, measured against an objective result

`Hypothesis.rule_out` keeps its prose (it is what the patient reads). It gains
an optional structured sibling:

```python
class RuleOutCheck(BaseModel):
    # matched against stored lab names
    analyte: str
    operator: Literal["negative", "normal", "below", "above"]
    threshold: float | None = None
    unit: str = ""
```

A deterministic evaluator answers met / not-met / cannot-tell against stored
lab rows. **Cannot-tell is not met** — an absent analyte never ends a
hypothesis.

`casefile.retirement` takes the lab facts as a plain mapping, exactly as
`knowledge.criteria` takes `PhenotypeLookup`, so `casefile` gains no
dependency on `labs` or `knowledge`.

### B. A human records a definitive exclusion

`EvidenceStrength` gains `definitive-exclusion`. One evidence item at that
strength in `evidence_against` retires the hypothesis immediately, without
summation.

**Which sources may carry it is restricted in code**, not left to a prompt:

| Source scheme | May assert a definitive exclusion |
|---|---|
| `labs:` | yes — an objective result |
| `doc:` / `encounter:` | yes — a clinician's own record |
| `patient-report:` | yes — she reports what her doctor said |
| `pmid:` | **no** — literature knows nothing about this patient |
| `engine:` | **no** — a phenotype engine that never ranked something has not refuted it (ADR 0036) |

A `definitive-exclusion` from a refused source is downgraded to `strong` and
logged. The model cannot talk its way past this by choosing a strength.

### C. The existing rules, unchanged

`_no_supporting_evidence` → parked, `_outweighed` → ruled-out, `_stale` →
parked. First match wins, as before.

### Protection is narrowed, not removed

ADR 0035: can't-miss and patient-origin are never retired automatically.

ADR 0038: can't-miss and patient-origin are never retired **by accumulated
model opinion**. A met rule-out (A) or a human's definitive exclusion (B) does
end them, because both rest on something objective rather than on the
Challenger having produced more counter-prose than support.

This is the point. Pheochromocytoma is a can't-miss lead *and* the textbook
case of a diagnosis a single negative test excludes. A protection that cannot
tell those apart guarantees the bloat CLN-01 describes.

Rule order therefore puts A and B **before** the protection check; C stays
after it.

### Patient-directed retirement (PAT-03)

`POST /ledger/hypotheses/{id}/retire` records reason, date, and clinician,
writes an `Evidence` item at `definitive-exclusion` sourced
`patient-report:<date>`, and applies an `UpdateHypothesis(status="ruled-out")`
through `DataRepo.apply_ledger_diff` — the same invariant-checked path
everything else uses. No private back door (ADR 0035's rule, kept).

It is reversible: status is a field, and the hypothesis stays on file.

## Consequences

- Can't-miss leads can now leave the list. That is the intent, and it is the
  one change here that could cost something: a wrongly-recorded exclusion
  removes a lead whose whole tier exists because missing it is catastrophic.
  Mitigated by (a) restricting which sources may assert it, (b) never
  inferring it from model output, (c) keeping it reversible and on file.
- `cannot-tell` outcomes are silent by design. An absent lab is the ordinary
  state and must not read as an exclusion.
- Two new schema values (`definitive-exclusion`, `RuleOutCheck`) round-trip
  existing ledgers unchanged: both are optional/additive.

## Alternatives considered

**Let any `definitive-exclusion` through, whatever its source.** Rejected. The
Challenger writes `evidence_against` and would gain a one-word route to
retiring a can't-miss lead. The restriction is the safety property.

**Evaluate rule-outs with a model.** Rejected under the standing rule that
deterministic logic is plain code. "Is this analyte negative" is a lookup.

**Keep protection absolute and add only the UI (PAT-03 alone).** Rejected: it
fixes the bloat only for leads a doctor has explicitly addressed, and leaves
the system unable to act on a negative test sitting in its own labs table.

**A full rule-out DSL.** Rejected as premature. Four operators cover the
stated rule-outs in the current ledger; a grammar can come when one is not
enough.

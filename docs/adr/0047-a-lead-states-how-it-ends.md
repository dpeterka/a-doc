# ADR 0047 — A lead states how it ends

Status: accepted (2026-09-02)

Reopens and addresses CLN-01. Corrects [ADR 0035](0035-hypothesis-retirement.md)'s
enforcement gap and [ADR 0038](0038-how-a-hypothesis-ends.md)'s missing writer.

## Context

The case file holds 54 hypotheses, 46 active, and **has never retired one**.
Every review adds; nothing subtracts. Measured in production, 2026-09-02:

```
rule_out_check populated        0 / 54
rule_out prose populated        0 / 54
definitive-exclusion evidence   0
_outweighed fires for           1 / 46
   evidence for : against    618 : 43      median margin −8
age-based park                  0          threshold 30d+, oldest lead 7d
exempt from assessment          13         10 can't-miss + 3 patient-raised
active by tier                  36 expanded, 10 can't-miss
active by origin                43 challenger, 3 patient
```

Every retirement path is inert, each for its own reason, and the root cause
is one thing: **nothing in the pipeline produces refutation at the rate
needed to end anything.** 618 evidence-for items against 43. A rule that
retires on "more against than for" is unreachable by construction, not by
tuning.

Underneath that sits a plain defect. ADR 0035 required every new hypothesis
to state what would rule it out, and `casefile/rule_out.py` enforces it in
code — `strip_ops_missing_rule_out`, with `_EMPTY_PHRASES` to reject
"further testing" and its cousins. `reason/stages.py` calls it on the
diagnostic chat path.

**`build_review_ledger_diff` never did.** 43 of the 46 active hypotheses
were created there, every one with an empty `rule_out`. ADR 0038 then built
`RuleOutCheck` and a retirement rule to evaluate the field — an evaluator
with no writer. It has run every review since and had nothing to read.

PLAN.md records ADR 0038 as closing CLN-01. **It does not.** This ADR says
so.

## Decision

### 1. The review path enforces what the chat path always has

`DivergenceDecisionPayload` gains `rule_out`, and
`prompts/divergence_adjudicator.md` (v2 → v3) asks for it on every accepted
`panel_only` divergence, naming the vacuous forms so the model does not
reach for them. `build_review_ledger_diff` writes it onto the new
`Hypothesis` and then runs the same `strip_ops_missing_rule_out` the chat
path runs.

An accepted lead with no usable rule-out is **dropped rather than added**,
and the review's rationale says how many and why. Strip rather than reject,
for ADR 0016 and ADR 0028's reasons: one missing field must never discard a
whole adjudication.

Dropping a lead is a real cost. It is the right one. A hypothesis nobody
will state a falsification condition for is exactly what fills a board that
only ever grows — and nothing is permanent, since the next review can re-add
it with one.

The field is defaulted rather than required on the payload, deliberately: a
`Literal`-style hard requirement would fail the whole adjudication over one
item, which is the v0.21.0 defect ADR 0028 exists to prevent.

### 2. Both halves, or the exercise is decorative

`retirement._rule_out_met` returns immediately unless `rule_out_check` is
set. **It never reads the prose.** A backfill writing only `rule_out` would
satisfy ADR 0035's requirement and retire nothing — the same
evaluator-with-no-writer shape it was written to fix, one level down.

So the backfill proposes both: the prose a patient reads, and the
`RuleOutCheck` a deterministic evaluator can answer. The second is
**refused rather than approximated** when it cannot be made evaluable:

- The analyte must appear in the labs actually on file. The prompt is given
  that list and the code validates against it, because `evaluate_rule_out`
  treats an analyte with no result as *not met* — so a check naming an
  invented analyte is indistinguishable from a working one and can never
  fire.
- `below`/`above` without a threshold is refused, keeping a
  `RuleOutCheck` validation failure a counted outcome rather than an
  exception mid-batch.
- Imaging, biopsies and examination findings are real rule-outs no lab
  lookup can answer. They keep their prose and get no check, deliberately.

`checkable` is reported separately from `proposed` for exactly this reason:
it is the number that decides whether anything can retire.

### 3. The 46 already there get one too

New enforcement does nothing for leads that already exist, and they are the
entire problem. `adoc rule-out-backfill` (`casefile/rule_out_backfill.py`)
asks the challenger for a falsification condition for every active lead that
has none, in batches of 8.

Three properties make it safe to point at a real case file:

- **It proposes; it does not invent.** A lead the model declines, or answers
  with a vacuous phrase, is left alone and counted. `--dry-run` prints
  without writing. **A wrong rule-out is worse than none**, because a wrong
  one retires a live lead.
- **It goes through `apply_and_save`**, as an ordinary `LedgerDiff` with
  provenance, so the ledger invariants check it like any other write and the
  change is visible in the history.
- **A failing batch costs only that batch.** One bad response must not cost
  the other five.

### 4. What this does not fix, said plainly

- **`_outweighed` is decorative at 618:43.** It stays as a floor, but it is
  not the convergence mechanism and this ADR does not pretend otherwise. The
  mechanism is a *met rule-out* — definite, checkable, and now writable.
- **The engines still cannot refute what is on the board.** Today's review
  produced 15 `opposes` verdicts and `nothing to apply`, correctly:
  `opposes` writes `evidence_against` only for `ledger_only` divergences,
  and LIRICAL's 55 findings are mostly `engine_only`, where `opposes` just
  means "do not adopt". Making the engines argue against incumbents is
  separate work.
- **The age rule cannot fire** while the oldest lead is 7 days old on a
  ledger at v17. Whether that is a rebuilt data repo or a `first_proposed`
  reset on re-proposal is unresolved and worth knowing.
- **13 leads are exempt from assessment.** Left alone on purpose: once a
  lead states how it ends, ADR 0038's rules run *before* `is_protected`, so
  a met rule-out already retires a can't-miss lead. Narrowing the protection
  before the rule-outs exist would remove the safety net without providing
  the mechanism meant to replace it.

## Consequences

- **Reviews will add fewer hypotheses**, and some weeks none. That is the
  point. The rationale states the count so a quiet week is distinguishable
  from a broken one.
- The first backfill run is the moment to look at what a model is willing to
  commit to. `--dry-run` first.
- `prompts/divergence_adjudicator.md` at v3 means leads stamped v2 were
  added under a contract that did not ask for a rule-out. That is what the
  stamp is for.
- One fixture updated: `test_full_review_happy_path`'s adjudication response
  had no `rule_out`, so its accepted lead is now dropped. The fixture gained
  one; a new test pins the drop.

## Alternatives considered

**Lower the retirement thresholds.** Rejected. At a 14:1 evidence ratio
there is no threshold that retires the right leads and not the wrong ones —
the input is the problem, not the cutoff.

**Retire on age alone.** Rejected. Age is a proxy for "nobody has thought
about this", and the honest answer to that is to think about it, not to
delete it. It also cannot fire here at all.

**Let the review delete hypotheses outright.** Rejected, as ADR 0035 already
did: history is never deleted here. Retirement is a status change, visible
in the diff.

**Ask the patient to prune the list.** Rejected as the default. ADR 0038
already gave her a retire control for a lead a doctor has excluded; making
her the mechanism for ordinary convergence would be handing her the system's
own job.

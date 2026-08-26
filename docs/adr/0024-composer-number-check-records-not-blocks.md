# ADR 0024 — `composer_number_check` records, it does not block

- Status: Accepted (2026-08-26)
- **Executes the trigger written into ADR 0023.** The check itself, and the
  narrowing decided in 0023, stand unchanged; only its severity changes.

## Context

ADR 0023 ended with a pre-commitment:

> If a fifth false-positive class appears, the next step is not a fifth
> narrowing: it is to demote this postcondition to a warning recorded on the
> turn, leaving the entailment verifier as the blocking grounding guard.

A fifth class appeared on the very next run.

Three runs of the enriched DAG experiment, each a real diagnostic turn:

| run | reached | outcome |
|---|---|---|
| 1 | composer (node 4/4) | 6 mismatches — thresholds, cutoffs, class bounds. 604s |
| 2 | ledger_maintainer (1/4) | citation ref unresolved (fixed, PR #151). 203s |
| 3 | composer (node 4/4) | 3 mismatches. 723s |

Run 3's survivors, reproduced deterministically against the real
`labs.sqlite`:

    25.0 near 'iron'      (stored: 44 .. 148)   -> "Your iron supplement is 25 mg."
    0.0  near 'beef ige'  (stored: 0.22 .. 0.32) -> IgE class boundaries in prose
    1.0  near 'beef ige'  (stored: 0.22 .. 0.32)

The iron case is the clearest statement of the problem this check cannot
solve: the patient takes a 25 mg iron supplement, and her serum iron
readings run 44–148. Both numbers are correct. They are different
quantities that share a name. Deciding which is which requires knowing
whether the sentence is about a supplement or a blood draw — which is
semantics, not numeral extraction, and this check is deterministic code by
design (CLAUDE.md: deterministic logic is never delegated to a model).

The tally after five classes and four narrowings:

- **True positives: 0.** In its entire history this check has never caught
  a fabricated lab value.
- **Cost per firing: the patient's whole answer**, after 10–12 minutes of
  model work, with the ledger already committed (`apply` precedes
  `composer`), so she is told her case file was updated but she may not read
  the reply.

A guard whose only demonstrated effect is destroying good output is not
protecting anyone.

## Decision

`composer_number_check` becomes **non-blocking**.

- `check_composer_numbers` still runs on every turn.
- `run_composer` still spends its one bounded rewrite attempt trying to
  clear a mismatch, so the common case still self-corrects.
- A mismatch that survives the rewrite is **logged at WARNING and the reply
  is delivered**. The contract remains in the DAG (it is still evaluated and
  still named in the node's postconditions) so that CLAUDE.md rule 3 holds:
  model output does not reach the patient without its contract checks
  running. What changed is the consequence, not whether the check happens.

**Grounding remains enforced by two blocking guards**, neither of which is
touched:

1. `citation_check_*` — every evidence source ref must resolve to a real
   lab row, document page, or encounter.
2. The entailment verifier (ADR 0016) — every most-likely-tier claim must be
   supported by the text of the source it cites, or it is stripped.

Those two check whether a *claim* is grounded in a *source*, which is the
property that matters. `composer_number_check` only ever checked whether a
numeral appearing near an analyte name existed in a column of that
analyte's values — a proxy that turned out to be much weaker than it looks.

## Consequences

- Diagnostic turns stop being lost to numeral mis-attribution. That is the
  point: this has now blocked three of three experiment runs.
- **What is given up**: a fabricated lab value that survives both the
  citation check and the entailment verifier, and that the composer's own
  rewrite attempt does not fix, will now reach the patient with only a log
  line. This is a real reduction in defence-in-depth, accepted because the
  layer being removed has a measured true-positive rate of zero and a
  measured cost of one destroyed turn per firing.
- The WARNING must not become noise nobody reads. If it fires routinely,
  that is the signal to spend effort on the *data* problem underneath it
  (extraction storing prose fragments as analyte names — see PR #151) rather
  than on the checker.
- Re-promoting this to blocking requires evidence it would catch something:
  a real fabrication it flags that the citation check and entailment
  verifier both miss. Absent that, it stays a warning.
- This ADR is the record that a pre-committed trigger was honoured rather
  than argued away when it fired. The fifth narrowing was available and
  plausible — exempt "supplement"-context numbers, exempt "class N" —
  and taking it would have meant a sixth.

# 0011. Conversational, agentic onboarding replaces the form-style wizard

Status: Accepted

## Context

The original onboarding wizard (`intake/wizard.py`, `intake/sections.py`)
drove a fixed 10-section state machine: for each section, one free-text
message was extracted straight into that section's Pydantic schema, played
back for confirmation, and committed verbatim. Live use exposed the
approach's structural limits, all traceable to the same root cause — a
single one-shot extraction call has no mechanism to *notice* a problem
with the patient's answer, only to transcribe it:

- **Vague answers pass through untouched.** "My dad has allergies" becomes
  `Relative(relation="dad", conditions=["allergies"])` with no allergen, no
  reaction, no severity, no age — a family-history entry that could mean
  almost anything, permanently recorded as if it were complete.
- **No timing discipline.** An ER visit mentioned in passing gets whatever
  date fragment the patient happened to include (or none), with no
  follow-up asking whether that's a rough guess or an exact date — the two
  cases are indistinguishable downstream.
- **No use of what's already on file.** A patient describing "an ER visit a
  few years back" has no way to be told a document dated that ER visit was
  already ingested — the wizard's context is the current section's schema,
  nothing else.
- **Patient assumptions and doctor-confirmed diagnoses look identical to
  the extractor.** "I have lupus" and "my rheumatologist diagnosed me with
  lupus in 2019" both land in `PriorDiagnosis`/`PatientSuspectedDiagnosis`
  purely by which free-text list the extraction prompt happened to route
  them to — nothing forces the distinction to be asked about.
- **Confirmed sections are frozen.** `reopen()` exists, but nothing prompts
  a patient to revisit a section later, and there is no single place to see
  everything recorded and correct it.

None of this is fixable by improving the extraction prompt alone — a
one-shot call has no turn-by-turn memory of what it already asked, no
notion of "this needs a follow-up before it counts as complete," and no
code-enforced gate stopping a vague/undated/unattributed answer from being
written straight into the case file. It needs a conversation, a place to
park "not yet resolved" facts, and a deterministic backstop that refuses to
call a section done while one of those is still open — the same shape of
problem CLAUDE.md rule 3 and the ledger's invariants (`casefile/ledger.py`)
already solve for the diagnostic loop: model proposes, code enforces.

## Decision

Replace one-shot section extraction with a conversational intake engine,
built from three layers that mirror the diagnostic DAG's own separation of
concerns (model proposes structured ops; deterministic code is the only
thing that mutates state or decides completion):

1. **A fact store (`intake/facts.py`).** Every patient statement becomes an
   `IntakeFact` — a patient-grounded statement plus structured `fields`,
   `date_approx`/`precision`, `attribution` (doctor-diagnosed / patient-
   reported / patient's own assumption), and a `clarification_status`.
   Facts are never deleted: `UpdateFact` appends a `FactRevision` and
   `RetractFact` flips `status` to `retracted` — a plain-code, unit-tested
   discriminated-union apply layer (`IntakeFactsStore.apply_ops`),
   deliberately shaped like `casefile/schema.py`'s `LedgerOp`/`LedgerDiff`.
2. **Deterministic completion gates (`section_completion_blockers`).** A
   section may close only when no active fact in it is still vague
   (`needs_probe`), no doctor-diagnosed diagnosis is missing both who and
   when, no patient-assumption diagnosis is missing the patient's own
   reasoning, and no event/diagnosis has never had its timing asked. This
   is the load-bearing safety mechanism of the whole feature — it runs as
   plain code, independent of and after the model's own judgment, exactly
   like a ledger invariant runs independent of and after the Ledger-
   Maintainer's proposed diff. The model can request a close; only the gate
   can grant one.
3. **The conversational engine (`intake/agent.py`).** One `intake_agent`
   model call per patient turn (new `models.yaml` role, bound to the same
   model as `primary_reasoner` — this is a new role, not a changed
   binding), given the section checklist, this section's open gate items,
   every active fact, a deterministic **document digest** (already-
   ingested documents + labs row count/date span + recorded encounters,
   genomic documents excluded per ADR 0010's "never touches an LLM" rule),
   and the recent transcript. It proposes at most two follow-up questions
   plus typed fact ops; the red-flag screen still runs first, before any
   client call, exactly as it does for chat (`reason/stages.py`); the
   output gate (`reason/safety.py`'s `treatment_gate`) still runs on the
   reply before it reaches the patient, withholding it (not the recorded
   facts — same pattern as `web/routes/chat.py`'s `ContractViolation`
   handling) on a violation.

The wizard's writers stay the single source of truth for case-file output:
`intake/convert.py`'s `facts_to_section_data` maps active facts onto the
exact same section schemas `intake/sections.py` already defines, and
`intake/wizard.py`'s existing `_write_*` functions (now exposed as
`write_section`) render them — a section closing under the new engine
writes `case/case-summary.md`/`medications.md`/encounter files/etc. through
literally the same idempotent code path the old wizard used. `IntakeWizard`
itself is untouched and still backs `adoc onboard --legacy-wizard` and its
existing test suite.

Facts are editable at any time, including after a section (or all of
onboarding) is marked complete: a correction to an already-closed section
regenerates that section's case-file artifact(s) immediately. The web
`/onboard` surface becomes a chat (mirroring `/chat`'s form-POST pattern),
with a `/onboard/review` page listing every active fact with attribution/
precision badges and a "Correct this" affordance, and stays reachable
post-completion with an amend-mode banner rather than being gated away.

## Consequences

- **One extra LLM call per onboarding turn** where the wizard made one call
  per section submission — the conversational shape trades a small cost
  increase for materially better-specified facts (an explicit, budgeted
  trade given PLAN.md's existing per-turn cost envelope).
- **A new `intake_agent` binding** to track in `models.yaml`/`adoc eval`
  going forward, alongside the existing roles (CLAUDE.md rule 4 — rebinding
  it independently of `primary_reasoner` still requires an eval comparison
  report and a PR).
- **Two onboarding code paths temporarily coexist**: the conversational
  engine (default) and the legacy wizard (`--legacy-wizard`, and still the
  only path `adoc onboard`'s original test suite exercises). This is
  intentional, not incidental debt — it keeps the wizard's writers, its
  resumable state machine, and its existing tests fully intact as the
  proven fallback and as the shared output layer the new engine reuses,
  rather than forking case-file writing logic in two places.
- **Completion is now gated by data quality, not just presence** — a
  patient cannot un-intentionally leave a vague, undated, or unattributed
  fact in a "complete" section; the trade is that a section can take a
  couple of extra turns to close when the gate finds something to ask
  about, which is the point.

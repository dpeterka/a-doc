# 0013. Fact corroboration & the longitudinal patient-reported record

Status: Accepted

## Context

By the time a patient reaches onboarding, a-doc's data repo may already hold
substantial history: ingested lab PDFs, doctor reports, and encounter files
— PLAN.md's core premise is that this documentation is the "whole-picture
holder." The initial-visit engine (`intake/agent.py`, ADRs 0011/0012)
already cross-references that history *conversationally* at capture time
("I have a record of an ER note dated 2024-03-02 — is that this visit, or a
different one?"), but nothing checks a captured fact against the record
again afterward — a patient statement is written once and never
systematically re-verified against documentation that may have been
ingested before or after it, or against documentation a later backfill
adds.

Separately, the intake/chat split has an implicit assumption baked in: the
initial visit gathers a baseline, and diagnostic chat turns afterward are
about the ledger, not about growing the patient-reported record further.
Direct owner feedback: "you may want to expand upon this as the current
model ... assumes essentially a weekly visit with the agent" — a real
patient sees doctors repeatedly, and each conversation can surface new or
changed information (a new symptom, a medication change, a diagnosis from
an outside doctor) that deserves the same structured treatment intake
facts already get, not just DAG context for that one turn.

## Decision

**Deterministic fact corroboration** (`intake/corroborate.py`, plain code —
CLAUDE.md: deterministic logic is never delegated to a model). Every active
`IntakeFact` gains `corroboration` (`"corroborated"` | `"contradicted"` |
`"unverified"`, default `"unverified"`), `corroboration_source` (reusing
the casefile source-ref grammar: `doc:<file>#p<int>` | `labs:<slug>:<date>`
| `encounter:<file>`), and `corroboration_note`. `corroborate_facts(facts,
db, repo)` checks each fact against already-ingested documents (metadata
only — dates and `doc_type`, never document text), lab rows, and encounter
files:

- **event** facts are matched against document/encounter dates within a
  tolerance window scaled by how precisely the date was given (an exact
  ISO date is held to a tighter window than a bare year).
- **diagnosis** facts are matched only at the *period* level — a
  clinical-note document or encounter dated within the stated year ± 1 —
  because this store has no document text to check against, so anything
  stronger would be fabricated confidence. The note honestly says so
  ("period corroboration only"). The one hard conflict this module can
  detect on its own terms — a diagnosis year in the future — is the only
  case that reaches `"contradicted"`; **absence of a match is never
  treated as contradiction**, only `"unverified"` (missing documentation is
  normal for a real patient history).
- **medication/supplement** facts are deliberately skipped: the only
  case-file artifact that could "corroborate" one is written *from* these
  same facts, so checking against it would be circular.
- **symptom** facts referencing a canonical lab analyte (via
  `labs.validate.canonicalize`, reused rather than reinvented) corroborate
  against that analyte's own rows.

This is explicitly a stopgap, not a claim of clinical verification:
Phase 2's planned entailment verifier (PLAN.md) is what will eventually
check fact *content* against document *text* — this module upgrades
"nothing checks patient statements against documentation at all" to
"deterministic period/series corroboration," and PLAN.md is updated to
note the upgrade path explicitly.

The sweep runs automatically at the end of every turn (intake or
post-intake visit) that added or updated a fact, and is exposed as `adoc
intake-corroborate` for on-demand re-sweeps (e.g. after a backfill adds a
batch of historical documents), mirroring the existing `labs-*` maintenance
command pattern. Applying an update appends a `FactRevision` but does not
restamp the fact's `provenance` — corroboration is not a new LLM-derived
artifact re-authoring the fact, it is deterministic code re-evaluating an
already-captured one. The `intake_agent` prompt (bumped to version `"3"`)
is told each fact's `corroboration` status and instructed to raise a
`"contradicted"` fact's discrepancy conversationally exactly once, then
record whatever the patient says — never argue, never repeat the
challenge.

**Interval history: visits grow the record.** Post-intake, every
successful (non-red-flag, non-withheld, non-error) diagnostic/
informational chat turn now also runs `intake.agent.run_visit_capture` — a
second, silent `intake_agent`-role call with its own inline-versioned
prompt (`VISIT_CAPTURE_PROMPT_VERSION`) that emits fact ops only for
genuinely new or changed patient-reported information, with no `message`
field (the patient never sees this pass). Applied through the exact same
`IntakeFactsStore.apply_ops`/corroboration/already-covered-topic-artifact-
regeneration path as an intake turn, stamped `dag_node="visit-capture"`.
Every fact gains `reported_on` (set from the applying turn's date, for
intake-created facts too, going forward) so the record is genuinely
longitudinal — the Intake record page's new "Since last visit" strip and
per-fact "reported" date both read straight off it.

`run_visit_capture` fails soft by design: an `LlmError` from the extra
model call, or an `IntakeError` applying its ops, is caught, logged, and
never propagates — the diagnostic/informational reply the patient actually
sees has already succeeded by the time this runs, and a hiccup in the
silent capture pass must never turn into a broken chat turn.

## Consequences

- **One extra, cheap LLM call per successful post-intake chat turn**
  (`intake_agent` role, empty-ops the overwhelming majority of the time) —
  an explicit, budgeted trade given PLAN.md's per-turn cost envelope,
  matching the same trade 0011 already made for the initial visit itself.
- **Most facts start, and often stay, `"unverified"`** — this is the
  conservative default working as intended, not a bug: a patient's own
  reporting is not made to look suspect just because nothing happens to be
  on file yet.
- **Corroboration is metadata-only, not content-verified**, until Phase 2's
  entailment verifier exists — a `"corroborated"` diagnosis fact means
  "records exist from that period," not "a document says this diagnosis
  was made." The corroboration note is written to say exactly that.
- **One more prompt version to track** (`INTAKE_AGENT_PROMPT_VERSION =
  "3"`, plus the new `VISIT_CAPTURE_PROMPT_VERSION = "1"`) — no
  `models.yaml` binding change (both reuse the existing `intake_agent`
  role), so CLAUDE.md rule 4's eval-comparison requirement does not apply,
  though the red-team/safety suite still gates every prompt edit per
  CLAUDE.md rule 2.

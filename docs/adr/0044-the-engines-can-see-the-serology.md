# ADR 0044 — The engines can see the serology

Status: accepted (2026-09-02)

Closes CLN-02. Builds on [ADR 0036](0036-engine-divergence-is-adjudicated-not-averaged.md)
and [ADR 0042](0042-criteria-read-the-whole-record.md).

## Context

LIRICAL and the sem-sim index both take HPO term ids and nothing else. The
terms reaching them came only from `case/phenotype.yaml`, which is matched
from narrative text — so *arthralgia*, *fatigue* and *dry eyes* reached the
engines and an ANA of 1:640 did not.

Physical symptoms overlap heavily across common autoimmune disease and rare
congenital disease. In rheumatology the discriminating power is in the
serology. Asked to explain fatigue and joint pain with no antibody
information, a Mendelian phenotype engine ranks rare paediatric dysplasias.

The measured outcome was exactly that:

> **`engine_adjudication` returned 66 of 66 neutral verdicts and changed
> nothing**, after LIRICAL had run for 76.9 seconds and sem-sim had run over
> the same profile.

PLAN.md names these engines as the *structural* mitigation against
self-anchoring (risk 2) — the mechanistically independent check that stops
the ledger from anchoring the system. A mitigation that runs, costs, and
contributes zero is worse than one that is absent, because the absence would
at least be visible.

## Decision

A new deterministic module `knowledge/lab_phenotype.py` derives HPO terms
from stored labs and adds them to the engine **query**. 26 rules, no model
call.

HPO already had the vocabulary. `Antinuclear antibody positivity` is
HP:0003493; `Decreased circulating complement C3 concentration` is
HP:0005421. Nothing needed inventing — the terms were never derived.

### 1. Labels, not ids

Every rule names an HPO **label**, resolved to an id through the real index
at runtime. A hardcoded id typed wrong is silently wrong forever; a label
the ontology does not have lands in `unresolved` and renders in the report.

That mechanism is also how the gaps here were found rather than guessed.
Searching all 19,119 published terms showed **HPO has no anti-Smith antibody
term at all**, so `knowledge/criteria.py`'s SLE item `Anti-dsDNA or anti-Sm`
contributes only its dsDNA half to an engine. Recorded in
`KNOWN_VOCABULARY_GAPS` and asserted by a test, rather than approximated
with a neighbouring antibody term.

A test forces every shipped rule to resolve against a 28-term subset of the
real index, copied verbatim. A synthetic ontology fixture would let a rule
resolve against a label this repository invented — the one failure the
module exists to prevent.

### 2. Resolution is exact, and `find_terms` is the wrong tool

`HpoIndex` gains `term_id_for(phrase)`: a direct lookup, normalised the same
way `scripts/build_hpo_index.py` builds its keys.

`find_terms` cannot be used to resolve a known label, and the reason is
measured rather than assumed: its word token must begin with a letter, so
`Anti-beta-2-Glycoprotein I IgG antibody positivity` tokenises **without its
`2`** and matches nothing at all. That is correct for scanning prose — the
alternative is matching "beta glycoprotein" in text that never said 2 — and
useless for asking whether the ontology has a term. A test pins a label
containing a standalone digit so the two normalisations cannot drift apart
silently.

### 3. The query, never the record

Derived terms go to the engines only. `case/phenotype.yaml` stays the
text-matched human record. `select_for_engine`'s own docstring already drew
that line — "the full profile is the RECORD; this is the QUERY" — and this
is the second consumer of the distinction.

Human terms are ordered first, so when a downstream limit bites the record
outranks an inference. Derived terms get their own budget
(`DERIVED_TERM_LIMIT = 10`) rather than competing for the profile's 8:
making serology fight the symptoms for slots would half-fix the finding.

### 4. `ever`, consistent with ADR 0042

A marker positive at any point derives its term. Classification criteria and
the diseases these engines rank both treat serology cumulatively, and being
inconsistent between the criteria scorers and the engines would be worse
than either choice on its own.

### 5. Nothing is derived from a normal result

A negative ANA does not derive an "absent" term. LIRICAL treats negated
phenotypes as evidence **against** a disease, so deriving one from a single
normal draw is a far stronger claim than deriving a positive from a single
abnormal one — and ADR 0042 established that under treatment a normal draw
is frequently an expected treatment effect. Excluded terms continue to come
from the human record only, where each is a deliberate clinical statement.

`A` (abnormal, direction unrecorded) derives nothing in either direction,
for the reason ADR 0042 gave: guessing a direction from it would invent a
finding. Both directions are tested, because the first version of that test
covered only the `low` rule and making `A` count as high broke nothing.

### 6. What the engine was asked is rendered

`render_engine_query` prints the derived terms with their citations above the
engine's findings, and says so distinctly when the index is unavailable,
when no lab mapped, and when a rule hit a vocabulary gap.

Rendered because the alternative is an invisible improvement. A reader
seeing 66 neutral verdicts had no way to tell whether the engine disagreed
or was asked the wrong question. That is this repository's recurring failure
mode and `docs/deployment-dependencies.md` exists because of it.

## Consequences

- **The engines now get up to 10 lab-derived terms alongside 8 human ones.**
  Whether that turns neutral verdicts into corroboration or opposition is an
  empirical question this ADR does not answer — it must be **measured on the
  next real review**, and the count of non-neutral verdicts is the number to
  look at. Shipping this and assuming it worked would repeat the LIRICAL
  mistake exactly.
- ADR 0036's posture is unchanged: scores are still never combined across
  engines, and `neutral` still emits no ledger op. This changes the engine's
  *input*, not how its output is combined.
- `hpo-index.json` becomes load-bearing for a second feature. It already has
  a row in `docs/deployment-dependencies.md` ("phenotype matching off"),
  now also engine serology.
- The rules are a curated list and will be incomplete. A missing analyte
  costs a term, not a wrong term, and `unresolved` plus the rendered query
  make what was and was not asked visible.

## Alternatives considered

**A clinical knowledge graph combining HPO with LOINC serology vectors
(CLN-02's own proposal).** Rejected for now. It replaces a working engine
with an integration project, and the measured problem is not that HPO lacks
the vocabulary — it demonstrably has it — but that nothing was deriving the
terms. Twenty-six rules and a label lookup close the gap this week; a
knowledge graph is a phase-4 question that should be asked again *after*
this is measured.

**Write derived terms into `case/phenotype.yaml`.** Rejected. The record is
what a human said and what the documents say; mixing inferences into it
makes the provenance of every term ambiguous, and `select_for_engine`'s
record/query split exists precisely so this is not necessary.

**Derive negated terms from normal results.** Rejected above — it is the
strong claim, on the weakest evidence, into the input of an engine that
treats negation as counter-evidence.

**Let a model map labs to HPO terms.** Rejected on CLAUDE.md's rule that
deterministic logic is plain code. It is a lookup against a published
ontology; a model would add a call, a failure mode, and the possibility of a
term that does not exist.

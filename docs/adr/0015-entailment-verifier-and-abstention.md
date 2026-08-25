# 0015. Cross-family entailment verifier + code-enforced abstention

Status: Accepted (2026-08-25)

## Context

Phase 2's first bullet (`reason/citations.py`, landed) proves that an
evidence claim's source *ref* resolves to real, matching data. It cannot
prove the claim's *prose* is actually supported by that source: a claim
can cite a real, matching lab row and still misstate what the row means
("CRP was low" when the row is flagged high), invent a detail the source
never mentions, or draw an unsupported conclusion from a normal value.
Citation checking is deliberately pure code (CLAUDE.md: deterministic
logic is never delegated to a model) — but *whether prose is entailed by
source text* is a language-understanding judgment, not a string-equality
check, and there is no honest way to make it fully deterministic.

Symmetrically, PLAN.md's Phase 2 also calls for the system to be able to
say "insufficient evidence" as a first-class output rather than either
fabricating a citation to fill a gap or quietly omitting the topic — and
for that to be *enforced*, not just prompted for, exactly the way every
other safety property in this codebase is (CLAUDE.md rule 3).

## Decision

**1. A claim-level entailment verifier, cross-family by construction.**
`reason/verify.py`'s `verify_claims` sends `(claim, resolved source text)`
pairs to a NEW model role, `entailment_verifier`, bound in `models.yaml`
to a DIFFERENT provider family than `primary_reasoner` (Anthropic) — this
mirrors ADR 0005's Challenger cross-family rule exactly: a verifier that
shares the Ledger-Maintainer's model family risks sharing its blind
spots. `entailment_verifier` is bound to Featherless/DeepSeek — a third
family, distinct from both `primary_reasoner` (Anthropic) and
`challenger` (OpenAI) — at zero incremental per-token cost, since
Featherless is already a flat-rate plan for the blind panel.

Judgments are `entailed` | `not_entailed` | `insufficient_source`. Only
`not_entailed` blocks — `insufficient_source` (no source TEXT resolvable
yet) passes the gate, exactly mirroring the citation checker's
`unverifiable`: a turn must never be hostage to missing infrastructure.

**2. Source-text resolution is an injectable seam, not a new dependency.**
A parallel workstream is landing a document-text corpus; this slice must
neither build it nor block on it. `SourceTextResolver` is a narrow
protocol (`resolve(source) -> str | None`); `DefaultSourceTextResolver`
implements it today for `labs:` refs (deterministic rendering of the
stored row — value, unit, reference range, flag) and for `encounter:`
refs (the encounter file's own text, when the file exists — narrative
docx-sourced encounters already carry full text per ADR 0008). `doc:` /
`pmid:` / `patient-report:` refs resolve to `None` today, which is
`insufficient_source`, never a rejection. The moment the document-text
corpus lands, a richer resolver can be injected without `verify.py`,
`stages.py`, or any contract changing.

**3. Wiring mirrors the citation checker's exact shape.** A same-generation
retry loop inside `ledger_maintainer_stage`/`challenger_stage` (mirrors
the citation retry and the Composer's gate-guided rewrite, PR #94): after
citations pass, a `not_entailed` claim feeds the verifier's objection back
for one retry. The DAG contract (`entailment_check_contract`) is the
actual enforcement point — a postcondition on `ledger_maintainer`, a
precondition on `apply` (checking the diff merged with the Challenger's
`additional_ops`, so a Challenger-introduced misrepresentation is also
caught) — re-checking independently of whatever the retry loop already
did, exactly like `citation_check_contract`.

**4. The Composer gets the same treatment for numbers.** `check_composer_
numbers` (pure code, no LLM) extracts every number sitting near a known
analyte name in the Composer's rendered text and requires it to match a
value actually stored in `labs.sqlite` — arithmetic is never delegated to
a model. Same retry-then-contract shape as the treatment gate.

**5. Abstention is a first-class, code-enforced signal, not a magic
string.** `PatientReply` gains `insufficient_evidence: list[str]`; the
Ledger-Maintainer's payload gains a typed `insufficient_evidence: list
[InsufficientEvidenceNote]` (folded into `LedgerDiff.rationale` for the
audit trail by code, never by the model). The enforcement half:
`most_likely_requires_resolved_evidence_contract`, a PRECONDITION on
`apply` (deliberately not a postcondition — `apply_stage` persists to
disk as a side effect of running, so checking after the fact would let a
bad state reach disk before the violation fires) — a hypothesis being
placed or promoted to tier `most-likely` with no evidence_for at all, or
none of it citation-resolved, fails the run before `casefile.ledger.
apply_and_save` ever executes.

## Consequences

- One more model call per turn in the common case (the entailment check),
  at a real family distinct from the two already in play — accepted cost
  for closing the "real ref, wrong meaning" gap citation checking cannot
  close. Featherless's flat-rate plan keeps this at effectively zero
  marginal cost.
- `entailment_check_*`/`most_likely_requires_resolved_evidence` join
  `citation_check_*`/`treatment_gate`/`composer_number_check` as DAG
  contracts: a run that violates any of them stops with a
  `ContractViolation`, never partial output.
- `evals/suites/hallucination.py` (PLAN.md's Phase 2 acceptance gate) pins
  planted-fact containment and fabricated-citation detection at 100%,
  reports entailment precision/recall against a labeled fixture set, and
  reports an abstention rate — see PLAN.md's Phase 2 block for the
  measured values and how the suite stays honest about a scripted
  heuristic judge's real limitations rather than rigging the fixture to
  make it look perfect.
- Changing `entailment_verifier`'s binding away from cross-family, or
  weakening `most_likely_requires_resolved_evidence`/`entailment_check_*`
  to make an unrelated change pass, is exactly the kind of change CLAUDE.md
  rule 2 forbids doing silently — same bar as the red-team suite.

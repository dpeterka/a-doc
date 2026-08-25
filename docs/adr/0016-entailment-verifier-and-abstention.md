# 0016. Cross-family entailment verifier + code-enforced abstention

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

## Revised (2026-08-25, fix/entailment-proportionate)

### What happened

v0.8.0 shipped this ADR's entailment verifier to production. On the first
real diagnostic turn against a full case file (121 documents, ~2000 lab
rows), `run_diagnostic_turn` died with `ContractViolation
node=ledger_maintainer contract=entailment_check_ledger_maintainer` after
464 seconds. `logs/entailment-checks.jsonl` showed:

- first attempt: **entailed 8, not_entailed 29, insufficient_source 4**
- after the one retry: **entailed 23, not_entailed 14, insufficient_source 4**

Every failing ref was a `labs:` ref. The contract fired, the whole turn was
lost, and the patient got the generic "withheld" message instead of a
reply that was, substantively, well-grounded.

### Why: the bar was wrong, not the mechanism

A `labs:` source text is a deterministic rendering of one row — value,
unit, reference range, flag. An evidence claim's job is to say *why that
value matters* ("ESR elevated, consistent with an inflammatory process").
The original prompt's closing instruction — "if a source text is ambiguous
or only partially supports the claim, judge `not_entailed`... do not
resolve ambiguity in the claim's favor" — made every claim that added
clinical framing beyond the bare number fail, because a bare lab row
"only partially supports" any framing by construction. The verifier was
correctly following its instructions; the instructions asked it to reject
normal, well-grounded clinical reasoning, not hallucination. A gate that
blocks ~35% of claims after a retry and kills the turn on that basis is
not protecting the patient — it is denying her a reply.

The all-or-nothing consequence compounded this: even a diff that was 85%
correct (23/27 entailed after retry) lost 100% of its content, because
`entailment_check_ledger_maintainer` treated any single `not_entailed`
claim as disqualifying for the whole diff.

### Decision — two changes, both required

**1. Sharpen `not_entailed` (prompt v2, `entailment_verifier.md`).**
`not_entailed` is now reserved for an actual *factual conflict* with the
source — this is the one thing this stage can catch that the deterministic
citation checker cannot:
- a value, unit, direction, or date that misstates the source (says
  "elevated" when the row is in range and unflagged; quotes 12.3 when the
  row says 1.23; cites a row from a different date);
- a finding the source does not contain at all (an invented result, an
  invented test, an invented encounter detail);
- a claim describing a different row than the one actually cited.

Explicitly NOT `not_entailed`: a claim whose factual core matches the
source and then adds clinical interpretation, significance, or a
mechanism — that judgment is the Ledger-Maintainer's to make and the
Challenger's to attack, not this stage's to re-litigate. The "resolve
ambiguity against the claim" instruction is deleted; the prompt now says
explicitly that accurate-value-plus-accurate-inference is `entailed`.

**2. Strip, don't reject (`reason/verify.py`, `reason/stages.py`).** A
`not_entailed` claim surviving the one retry no longer fails the whole
diff. Instead:
- `strip_not_entailed_ops` removes just that evidence item from the diff's
  (or verdict's) ops; `insufficient_source` items are always kept
  (unresolvable is not the same as wrong — same principle this ADR already
  applied to the citation checker's `unverifiable`). The turn proceeds on
  the remaining, verified evidence.
- Every stripped claim is logged to `logs/entailment-stripped.jsonl`
  (ref, hypothesis id, judgment — no claim prose at a level that could leak
  into anything but this data-repo-local audit file, same handling
  `log_verification_report` already gave `not_entailed` claims) so
  over-stripping is measurable going forward, distinct from the raw
  per-attempt counts in `entailment-checks.jsonl`.
- **The threshold for a hard failure is "all", not "any":**
  `VerificationReport.all_not_entailed` — every claim the check examined
  judged `not_entailed`, with nothing surviving, not even an
  `insufficient_source` claim. That is the one outcome still worth
  stopping the run for: it means the pipeline itself produced garbage
  (every single claim wrong), not that one claim was imprecise. This is
  evaluated on the Ledger-Maintainer's own diff (`entailment_check_
  ledger_maintainer`'s exact scope, 1:1 with what `ledger_maintainer_
  stage` itself checked) — the Challenger's `additional_ops` is a smaller,
  auxiliary contribution with no 1:1 postcondition of its own (the closest
  equivalent, `entailment_check_apply`, checks it MERGED with the diff), so
  `challenger_stage` strips its `not_entailed` claims unconditionally
  rather than ever leaving a small, isolated `additional_ops` set
  "100% failing" and unstripped — that would create a real gap where the
  merged-ops check at `apply` no longer sees it as "all failing" either,
  and a Challenger-introduced misrepresentation would sail through
  unstripped.
- **This makes grounding stronger, not weaker.** Under the old design, a
  `not_entailed` claim's practical effect was to fail the whole turn open
  — the patient got nothing, and the same (still-flawed) diff could be
  regenerated and resubmitted with no guarantee the offending claim
  wouldn't recur. Under strip-don't-reject, unverified evidence never
  reaches the ledger at all, in a turn that otherwise succeeds — a strictly
  better outcome for both correctness and availability.
- **The abstention contract is unweakened and remains the backstop.**
  `most_likely_requires_resolved_evidence_contract` runs on the same
  merged, post-strip ops: if stripping leaves a `most-likely` hypothesis
  with no resolved `evidence_for` at all, that PRECONDITION on `apply`
  still fires before anything is written to disk — proven directly by
  `tests/test_stages.py::test_stripping_composes_with_abstention_contract`.

### Consequences (revised)

- `evals/suites/hallucination.py` gains a dedicated
  `interpretation_claims_entailed_rate` metric (pinned at 1.0) over fixture
  pairs prefixed `interpretation-` — a well-grounded value plus ordinary
  clinical interpretation must be judged `entailed`; this is the exact
  false-positive shape that took down the production turn. The existing
  planted-fact and fabricated-citation probes are unaffected and still pin
  at 1.0: a planted fabricated value is a factual conflict, still caught,
  and (being the diff's only evidence claim in those fixtures) still hits
  the `all_not_entailed` hard-failure path, not the strip path.
- A code review of this same change surfaced an identical over-block shape
  in the Composer's separate, fully deterministic `check_composer_numbers`
  check (`reason/verify.py`) — a count/frequency/duration sharing a clause
  with an analyte name ("elevated across 3 separate panels") was being
  flagged as if it were that analyte's value. Fixed in the same branch with
  the same proportionality principle: `_quoted_number_looks_like_a_value`
  requires that a number NOT be immediately followed by a recognized
  count/frequency/duration word before it is compared against stored
  values at all; a fabricated value (with or without an adjacent unit)
  is still caught exactly as before. This is a distinct, purely
  deterministic check with its own DAG contract (`composer_number_check`)
  and its own reject-the-whole-reply-and-rewrite-once behavior, unaffected
  by the entailment strip mechanism above; it gets its own eval metric,
  `composer_number_legitimate_phrasing_pass_rate`, pinned at 1.0.
- Changing the entailment threshold from "any `not_entailed` fails" to
  "all `not_entailed` fails", or removing the strip step, is exactly the
  kind of change CLAUDE.md rule 2 forbids doing silently going forward —
  same bar this ADR already set for the cross-family binding.

## Revised (2026-08-25, perf/diagnostic-turn-latency)

### What happened

A real diagnostic turn against a full case file (121 documents, ~2000 lab
rows) measured **1410s (23.5 min)** of total model time, **$1.52**.
`entailment_verifier` (DeepSeek R1-0528) alone accounted for **930s (66%)**
across up to 4 calls/turn: a same-generation retry inside
`ledger_maintainer_stage`/`challenger_stage` (redundant now — under
"strip, don't reject" a still-`not_entailed` claim after a retry is
stripped exactly the same as one caught on the first try, so the retry
only ever spent a second ~minutes-long completion to maybe rescue one
evidence item) plus two independent DAG-contract re-checks
(`entailment_check_ledger_maintainer`, `entailment_check_apply`) that
re-verified the SAME claims a moment later via a fresh model call. Net
protective effect of the 930s on that run: one claim stripped.

### Decision — three changes, latency only, no weakening of what `not_entailed` catches

1. **Drop the entailment retry.** `verify_claims` is called exactly once
   per `ledger_maintainer_stage`/`challenger_stage` invocation now — no
   feedback-and-retry loop for an entailment failure (the citation-checker
   retry is untouched: cheap, deterministic, and still worth a second
   try).
2. **Cache verdicts by `(claim, resolved source text)` hash**
   (`reason.verify.EntailmentCache`, mirroring `reason.citations.
   EutilsPmidVerifier`'s on-disk cache pattern, `work/`-scoped). Shared
   across one `build_diagnostic_dag` call, so the two DAG-contract
   re-checks above are cache hits, not a second model call, for every
   claim the stage already judged this same turn — and across turns, since
   an unchanged claim/source pair recurs. No TTL: a judgment for a
   byte-identical pair cannot go stale; a changed source hashes to a
   different key and misses naturally.
3. **Verify only `most-likely`-tier evidence synchronously; defer the
   rest.** `reason.stages._partition_claims_by_tier`/`_most_likely_ops`
   split a diff's claims by the tier of the hypothesis each one supports
   (after the diff is applied): `most-likely` evidence — the tier that
   actually drives this turn's patient-facing reply — is verified now;
   `expanded`/`cant-miss` evidence is queued
   (`reason.verify.queue_deferred_claims`, `work/entailment-deferred.json`)
   instead of checked in-turn. The weekly review's new
   `deferred_entailment_sweep` DAG node
   (`reason.review.sweep_deferred_entailment_claims`) pops the ENTIRE
   queue every run and verifies every claim through the same
   `verify_claims` a diagnostic turn uses — nothing deferred waits more
   than one review cycle. A `not_entailed` finding on a deferred claim is
   surfaced in the committed review report, never used to mutate the
   ledger: this schema has no evidence-removal op by design (history is
   never deleted), so a deferred `not_entailed` finding is a signal for a
   human to act on (e.g. a follow-up `record_challenge`), the same shape
   `scan_staleness` already uses to surface drift without rewriting
   history.

Combined, a clean diagnostic turn's `entailment_verifier` call count drops
from up to 4 to 1 (`tests/test_stages.py::
test_clean_diagnostic_turn_makes_the_expected_model_call_count` pins the
full per-role call count as a latency regression guard); a claim's fate on
`not_entailed`/`all_not_entailed`/`insufficient_source` is decided by the
exact same logic as before this revision — only WHEN and HOW OFTEN a claim
is checked changed, never the verdict logic itself.

### Considered and NOT shipped: rebinding `entailment_verifier` off DeepSeek R1

DeepSeek R1 is a reasoning model that spends the large majority of its
~13k output tokens/call on hidden chain-of-thought for what is, for most
claims, an easy factual-match judgment — the fast-model rebinding CLAUDE.md
rule 4 anticipates for exactly this shape of role. A same-family (third
family, Featherless-hosted, distinct from `primary_reasoner`/Anthropic and
`challenger`/OpenAI — preserving this ADR's cross-family placement without
reopening it) non-reasoning candidate such as `deepseek-ai/DeepSeek-V3` is
the recommended follow-up, at zero incremental cost (Featherless's flat
rate already covers the blind panel).

This was NOT shipped in `perf/diagnostic-turn-latency`, per CLAUDE.md rule
4's requirement for an `adoc eval` incumbent-vs-candidate comparison:
running `adoc eval --suite hallucination --candidate
featherless:deepseek-ai/DeepSeek-V3` produces a report, but
`evals/runner.py`'s own module docstring is explicit that no suite
(`extraction`/`redteam`/`hallucination`) makes a real model call today —
`--candidate` only changes the label recorded on the `SuiteResult`, so
incumbent and candidate score byte-identically (confirmed: every metric's
delta was `+0`). Live incumbent-vs-candidate comparison is Phase 3 scope
(PLAN.md: "`adoc eval --candidate` produces an incumbent-vs-candidate
comparison report from a single command" is listed under Phase 3
acceptance, not Phase 2), and this sandboxed development environment additionally
had no provider credentials to make a real call even if the harness
supported one. Shipping the rebinding without genuine evidence it clears
`entailment_precision`/`entailment_recall`/
`interpretation_claims_entailed_rate` is exactly the silent-model-change
rule 4 forbids — the responsible call is to leave `models.yaml` unchanged
and land the retry/cache/deferral optimizations (which need no model
change) now, with the rebinding as a follow-up once a human can run a live
comparison against real credentials.

## Revised again (2026-08-25) — binding changed to DeepSeek V3, with live evidence

The `perf/diagnostic-turn-latency` work correctly REFUSED to rebind this
role, because the hallucination eval suite is fully offline and scripted:
every candidate scored byte-identically, which is not evidence. CLAUDE.md
rule 4 wants a real comparison, so one was run — the same 20 labelled
pairs from `tests/fixtures/entailment/pairs.json`, sent to each candidate
for real:

| model | latency (20 pairs) | agreement with labels | judgment split |
|---|---|---|---|
| deepseek-ai/DeepSeek-R1-0528 (incumbent) | 78s | 17/20 (85%) | 10 entailed / 10 not_entailed |
| deepseek-ai/DeepSeek-V3-0324 | 20s | 19/20 (95%) | 12 / 8 |
| gpt-5.2 | 10s | 19/20 (95%) | 12 / 8 |

Two conclusions, one of them uncomfortable:

1. The reasoning model was **both slower and less accurate**. Choosing R1
   assumed this was a hard judgment; it is a short factual comparison, and
   the extra deliberation bought nothing.
2. R1 skewed toward `not_entailed` (10 vs 8 for both candidates). The
   over-blocking incident earlier this date was therefore not purely a
   prompt problem, as the first revision assumed — the model contributed,
   and the prompt fix alone would have left a bias in place.

**V3 chosen over the marginally faster gpt-5.2** to preserve three
distinct families: Anthropic proposes, OpenAI challenges, DeepSeek
verifies. Putting the challenger and the verifier on one family would let
a shared blind spot clear both gates, which is the exact property ADR 0005
exists to protect. Ten seconds inside a multi-minute turn does not buy
that back.

**Follow-up:** the offline suite could not have caught this, and cannot
catch the next one. A live-comparison mode for `adoc eval --candidate`
(currently Phase 3 scope) is what makes rule 4 enforceable rather than
aspirational.

## Revised again (2026-08-25, fix/number-check-pairing) — the checker's third revision

### What happened

A real diagnostic turn against a full case file failed `composer_number_
check` with 24 mismatches. Reading the captured log closely showed every
one was the same shape: the Composer was quoting REAL, correctly-attributed
values, and the checker was pairing them with the wrong analyte —

```
62.9 near 'fsh'  (stored fsh: [..., 63.9, ...])   <- 62.9 is LH's real value
91.4 near 'lh'   (stored lh:  [..., 91.4? no])    <- 91.4 is FSH's real value
22.0 near 'alt'  (stored alt: [10.0, 14.0, ...])  <- 22.0 is AST's real value
15.0 near 'ast'  (stored ast: [16.0, 18.0, ...])  <- 15.0 is ALT's real value
1.8  near 'crp'  (stored crp: [10.0])             <- 1.8 is hs-CRP's value
```

This is by this point the THIRD real diagnostic turn `check_composer_
numbers` has failed in production, and it has never once caught a genuine
fabrication — recorded here honestly, per this ADR's own standard for
tracking a safety-relevant guard's real-world hit rate rather than only its
test-suite score.

### Why: two distinct bugs in HOW a number was matched to an analyte

**1. Checked against every mention in the clause, not the one that governs
it.** The prior two passes fixed WHICH numbers count as candidate values
(`_quoted_number_is_value_evidence`); neither touched WHICH analyte a
qualifying number is compared against. The old code found every known
analyte label present anywhere in a clause, then nested-looped every
qualifying number against every one of them. "FSH was 91.4 and LH was
62.9" has two mentions and two numbers in one clause — 91.4 was correctly
matched against `fsh` (passing) AND incorrectly matched against `lh`
(failing, since 91.4 isn't LH's value), and the reverse for 62.9. Both
numbers were right; the pairing manufactured two mismatches out of nothing.

**2. Substring collision between a longer and a shorter label.** "hs-CRP"
matched the literal substring "crp" — a hyphen is not a `\w` character, so
`\bcrp\b` matches inside "hs-crp" — and 1.8 (hs-CRP's real value) was
checked against plain CRP's stored values (10.0), a completely different
assay with its own reference range.

### Decision — structural pairing, longest-match, fail safe on ambiguity

`reason/verify.py` gains `_mention_pattern` (one alternation regex over
every known analyte label for this patient, LONGEST-label-first at every
start position, mirroring the longest-alias-first precedence
`labs.validate.canonicalize`/`_SCORE_SUFFIX_TO_CANONICAL` already applies —
so "hs-crp" always wins over "crp" at the same text position) and
`_resolve_governing_mention`: each qualifying number is checked against the
ONE mention that structurally governs it — the nearest mention immediately
before it (`ALT 15`, `ALT was 15`, `ALT of 15 U/L`, tied by the existing
copula/unit evidence, now scoped to just the text between that mention and
the number so an unrelated earlier mention's copula word can never leak
in), or the nearest mention immediately after it (`15 U/L ALT`, tied by a
unit directly attached to the number belonging to that trailing mention).
Because both candidates are always the NEAREST mention on their side, a
number can never bind to a mention on the far side of another mention in
between — there is no "other" mention within reach to skip past.

**Fails safe on ambiguity, explicitly, per this checker's asymmetry.**
`check_composer_numbers` runs as a POSTCONDITION on the Composer stage —
after `apply_stage` has already committed the ledger (`build_diagnostic_
dag`'s node order: Ledger-Maintainer -> Challenger -> apply -> Composer).
A false positive here does not just block one claim; it discards an
already-correct, already-grounded reply that the citation checker and
`verify_claims` have already independently verified. A missed check, by
contrast, costs nothing beyond what those two earlier, independent checks
already cover. So when a number's evidence ties it to BOTH a different
preceding and a different following mention, `_resolve_governing_mention`
returns no governing mention at all rather than guessing — this asymmetry
is stated directly in the code comment above `check_composer_numbers`, per
this ADR's now-established practice of writing the reasoning down where a
future edit might otherwise "tighten" it back.

### Consequences (third revision)

- `evals/suites/hallucination.py`'s `composer_number_legitimate_phrasing_
  pass_rate` metric (pinned at 1.0) gains the multi-analyte pairing cases
  built directly from the production evidence: FSH/LH, ALT/AST, and a
  femoral-neck-T-score/lumbar-spine-BMD sentence, each with every number
  correctly quoted next to its own analyte, plus an hs-CRP-vs-CRP longest-
  match case. `_COMPOSER_NUMBER_FABRICATED_SENTENCES` gains the matching
  "still caught under correct pairing" cases — a wrong LH value alongside
  a correct FSH value, and an hs-CRP value that happens to equal plain
  CRP's real number — proving the fix does not trade false positives for
  false negatives.
- `tests/test_verify.py` pins the structural-pairing rule directly,
  including the ambiguous-pairing fail-safe case (a number tied by evidence
  to two different, disagreeing mentions must not be flagged).
- This guard's real-world record after three revisions: three production
  turns failed, zero genuine fabrications ever caught. The design is now
  conservative by construction (longest-match mention-spotting, single-
  governing-mention pairing, fail-safe-on-ambiguity) specifically because
  every failure to date has been a false positive, never a miss — the same
  proportionality principle the entailment verifier's "strip, don't reject"
  revision above already established for a different check. Loosening any
  of the three fail-safe properties above to make an unrelated change pass
  is exactly the kind of change CLAUDE.md rule 2 forbids doing silently.

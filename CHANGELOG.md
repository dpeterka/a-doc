# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.30.2] — 2026-09-04

### Fixed

- **The rule-out backfill prompt names the single-normal-result trap.**
  Measured on the real case file with v0.30.1's analyte fix in place, the
  backfill would retire **7** leads immediately (was 1 before the fix) — and
  several of those rule-outs are clinically wrong in one consistent way:

  | lead | proposed rule-out | why it is wrong |
  |---|---|---|
  | mast-cell activation syndrome | tryptase normal | MCAS wants an acute-episode rise; most patients have a normal baseline |
  | accelerated bone loss from estrogen deficiency | estradiol normal | the loss already happened; today's level does not undo it |
  | primary ovarian insufficiency | FSH normal | criteria want elevated FSH on two occasions ≥4 weeks apart |
  | perimenopause | FSH normal | FSH swings widely in perimenopause by definition |

  Of the seven, the cortisol rule-out (>18 µg/dL excluding adrenal
  insufficiency) and the platelet one are defensible. The rest are the
  plausible-sounding kind that would quietly end a live lead.

  **The same shape ADR 0042 found in the criteria scorers**: a normal draw is
  frequently an expected treatment effect or a between-episode value, not
  evidence the finding never happened. The prompt now names all three traps
  — episodic, historical/cumulative, repeat-testing — and instructs the model
  to return an empty string rather than substitute the convenient single
  value.

  **The backfill has still not been applied.** A dry run producing seven
  retirements of which several are wrong is the dry run doing its job.

## [0.30.1] — 2026-09-03

*Found by simulating what ADR 0047's backfill would actually retire — which
is the only reason it was found.*

### Fixed

- **A rule-out's analyte is matched on the normalized name.**
  `review.build_lab_lookup` keys the lookup on `_normalize_analyte(name)`,
  so `Vitamin B12` is stored under `vitaminb12`;
  `retirement.evaluate_rule_out` looked up `check.analyte` **raw**. Only a
  single lowercase word could ever match.

  Measured against the real case file: of **16** machine-checkable rule-outs
  proposed over **461** stored analytes, **15 answered "no result on file"
  and 1 matched** — the one whose analyte was `ferritin`. Every multi-word
  or capitalised name was unreachable.

  Same shape as `criteria._RA_RF` (`r"rheumatoid factor"`, a literal space
  matched against a space-stripped name) and the `LabFlag` set that missed
  `LL`: **a check that cannot fire looks exactly like a check that fires and
  finds nothing.** This one would have made ADR 0038's evaluator and ADR
  0047's writer each correct in isolation and jointly useless.

  Fixed at the evaluator rather than the writer, so every caller benefits —
  a check written by a review, by the web UI, or by hand resolves the same
  way. `normalize_analyte` is exported; the raw key is still tried as a
  fallback.

  The property that had to survive is pinned: **an analyte genuinely absent
  is still not met.** Absence of a test reads nothing like a negative result,
  and conflating them is the one failure this evaluator must not have.

- `RuleOutCheck.analyte`'s docstring claimed regex matching over the
  normalized name. It is a dict lookup and never was a regex. Corrected.

## [0.30.0] — 2026-09-03

*ADR 0047 — a lead states how it ends. CLN-01 reopened and addressed.*

### Context

The case file holds 54 hypotheses, 46 active, and **has never retired one**.
Measured in production 2026-09-02:

```
rule_out_check populated       0 / 54
rule_out prose populated       0 / 54
definitive-exclusion evidence  0
_outweighed fires for          1 / 46
   evidence for : against   618 : 43     median margin −8
age-based park                 0         threshold 30d+, oldest lead 7d
exempt from assessment        13
```

Every retirement path was inert, each for its own reason, and the root cause
is one thing: **nothing in the pipeline produces refutation at the rate
needed to end anything.** At 14:1, a rule that retires on "more against than
for" is unreachable by construction, not by tuning.

Underneath sat a plain defect. ADR 0035 required every new hypothesis to
state what would rule it out, and `casefile/rule_out.py` enforces it in code.
`reason/stages.py` calls it on the diagnostic chat path.
**`build_review_ledger_diff` never did** — and 43 of the 46 active
hypotheses were created there. ADR 0038 then built `RuleOutCheck` and a
retirement rule to evaluate the field: an evaluator with no writer, running
every review with nothing to read.

### Added

- **`adoc rule-out-backfill`** gives the leads already on the board a way to
  end. It proposes and does not invent: a lead the model declines, or answers
  with a vacuous phrase, is left alone and counted. `--dry-run` prints
  without writing. **A wrong rule-out is worse than none — a wrong one
  retires a live lead.** Lands through `apply_and_save` so the ledger
  invariants check it, and a failing batch costs only that batch.

  It proposes **both halves**: the prose a patient reads, and the
  `RuleOutCheck` a deterministic evaluator can answer.
  `retirement._rule_out_met` never reads the prose, so prose alone would
  satisfy ADR 0035 and still retire nothing. The check is **refused rather
  than approximated** — the analyte must appear in the labs actually on file,
  because `evaluate_rule_out` treats an unmeasured analyte as *not met* and a
  check naming an invented one can never fire. `checkable` is reported
  separately from `proposed`, since it is the number that decides whether
  anything can retire.

### Changed

- **The review path now enforces what the chat path always has.**
  `DivergenceDecisionPayload` gains `rule_out`;
  `prompts/divergence_adjudicator.md` (v2 → v3) asks for it on every
  accepted `panel_only` divergence, naming the vacuous forms so the model
  does not reach for them. An accepted lead with no usable rule-out is
  **dropped rather than added**, and the review's rationale says how many and
  why.

  **Reviews will add fewer hypotheses, and some weeks none. That is the
  point.**

### Fixed

- **A panel-proposed `definitive-exclusion` is no longer downgraded to
  `moderate`.** `_EVIDENCE_STRENGTHS` was a hardcoded
  `{"strong","moderate","weak"}` — a duplicate of the schema's
  `EvidenceStrength` that ADR 0038 extended and this copy never learned
  about. So a blind-panel member asserting the schema's own literal had it
  flattened onto the additive scale, which is exactly the summation ADR 0038
  exists to prevent. The synonym map made it worse: `definitive` → `strong`,
  but `definitive-exclusion` → `moderate`. Now derived from the schema with
  `get_args`. Observed in production; that path had never once run.

### Corrected

- **PLAN.md recorded ADR 0038 as closing CLN-01. It does not.** CLN-01 is
  reopened, and this release addresses it.

### Not fixed, and stated as such in the ADR

- `_outweighed` is decorative at 618:43 and is **not** the convergence
  mechanism. The mechanism is a *met rule-out*.
- The engines still cannot refute incumbents. The 2026-09-02 review produced
  **15 `opposes` verdicts and `nothing to apply`** — correctly, since
  `opposes` writes `evidence_against` only for `ledger_only` divergences and
  LIRICAL's 55 findings were mostly `engine_only`.
- The age rule cannot fire while the oldest lead is 7 days old on a v17
  ledger. **That is itself unexplained.**
- 13 leads remain exempt from assessment. Once a lead states how it ends,
  ADR 0038's rules already run *before* `is_protected`.

### Notes

- ADR 0044's question is answered: engine adjudication went from
  **66/66 neutral** to **{corroborates: 0, opposes: 15, neutral: 50}** on 65
  divergences. The engines have started disagreeing. They still cannot act
  on it.
- ADR 0043 measured in production: the blind panel ran 3 nodes concurrently,
  **183.2s wall clock against 458.1s sequential — 2.50×**.

## [0.29.1] — 2026-09-02

*Hotfix. A model-facing instruction was reaching the patient.*

### Fixed

- **A withheld informational answer showed the patient an instruction
  addressed to a model.** `_GATE_BLOCKED_MESSAGE` interpolated
  `GateResult.rewrite_instruction` — *"Rewrite this response to remove any
  specific drug name, dose, or instruction to start/stop/increase/decrease/
  taper a medication or supplement"* — directly into her transcript, in the
  middle of a conversation about her ears, with no way to tell what it meant
  or what to do about it.

  ADR 0040 asserted that string "is an instruction to the *model*, never
  shown to anyone". That was checked against `reason/stages.py`, where it is
  true, and not against `reason/tools.py`, where it was not.

  The replacement says three things she can act on: what was withheld, what
  usually works instead (ask again without the dose, or take it to the
  doctor as one of the review's questions), and that nothing was lost. The
  offending spans and the rewrite instruction stay model-facing — fed back
  into the rewrite loop and now logged by reason, never rendered.

### Not changed

The gate itself. The blocked answer was genuinely blocked; only the message
about it was wrong. Relaxing the informational path to the `recording_only`
scribe mode was measured and **rejected for now**: two red-team strings —
`"Take 5 mL twice daily."` and `"The recommended dose is 5 mL twice daily."`
— pass `recording_only`, because the imperative rule needs a drug-like token
and neither names a drug. Closing that hole is a prerequisite, not a
follow-on.

## [0.29.0] — 2026-09-02

*ADRs 0043–0046 — the last four findings of the adversarial-review adoption
track. Three of the four had a premise that did not survive checking, and
each correction is recorded rather than quietly worked around.*

### Added

- **The phenotype engines can see the serology** (ADR 0044). LIRICAL and
  sem-sim take HPO term ids and nothing else, and the terms reaching them
  came only from `case/phenotype.yaml` — matched from narrative text. So
  *arthralgia*, *fatigue* and *dry eyes* reached the engines and an ANA of
  1:640 did not. Measured consequence: `engine_adjudication` returned
  **66 of 66 neutral** verdicts and changed nothing, after LIRICAL had run
  for 76.9 seconds.

  New `knowledge/lab_phenotype.py`: 26 deterministic rules deriving HPO
  terms from stored labs. HPO already had the vocabulary — nothing needed
  inventing.

  Every rule names a published **label**, resolved through the real index at
  runtime, because a hardcoded id typed wrong is silently wrong forever.
  That mechanism found the gaps rather than guessing them: searching all
  19,119 terms showed **HPO has no anti-Smith antibody term at all**, so the
  SLE criteria's `anti-dsDNA or anti-Sm` item contributes only its dsDNA
  half. Recorded, not approximated with a neighbour.

  Nothing is derived from a normal result: LIRICAL treats negated phenotypes
  as evidence *against* a disease, and ADR 0042 established that a normal
  draw is frequently a treatment effect.

- **"Ask about this" links** on every review section and hypothesis card
  (ADR 0045), pre-filling the chat composer.

- **A stage ticker** for slow chat turns (ADR 0046) — `GET /chat/progress`,
  polled every 2s while a turn runs, showing which of the four stages is
  executing.

- **`HpoIndex.term_id_for`** — an exact label lookup. `find_terms` cannot
  resolve a known label: its word token must begin with a letter, so
  `Anti-beta-2-Glycoprotein I IgG antibody positivity` tokenises **without
  its `2`** and matches nothing.

### Changed

- **The declared DAG is now the real DAG** (ADR 0043). `depends_on` takes a
  list and every entry is checked; a new `after` carries orderings that are
  real but carry no data; execution order is derived by topological sort;
  and the blind panel and the two phenotype engines run as parallel batches.

  Eight of the review's twenty nodes read context they never declared. A
  test now parses the builder's own source with `ast` and asserts nothing is
  read undeclared.

  The derived order is verified **identical, node for node**, to the order
  that shipped. Stage order is a safety property (CLAUDE.md rule 3), so that
  equality is the only basis on which it could be handed to a sort.

- `criteria_scan` reaches `render_report` through the graph instead of the
  `results` sink, where it was a node the report depends on with no edge
  saying so.

- `LlmClient._audit` serialises its appends. An audit record exceeds the
  4096 bytes a write is atomic within, and the parallel batches above mean
  two calls can now reach it at once.

### Fixed

- **A cycle in a DAG is refused at construction**, and an unsatisfiable
  prerequisite fails before **any** node runs — previously a typo surfaced
  as a `KeyError` from whichever node reached it first, after the ones that
  write the ledger and cost frontier calls had already executed.

### Corrected

- **`docs/dag-topology.md` was wrong** and is fixed, diagram included. It
  called the sequencing-only edges "decorative" and proposed deleting them.
  `staleness_scan` reads `ledger-history.jsonl`, which `retirement_pass` and
  `apply_engine_diff` append to — deleting the edge would have moved the
  scan earlier and silently changed what it reads. The orderings were real
  constraints in the only vocabulary available; `after` is that vocabulary.

### Declined, with reasons recorded in the ADRs

- **Typed per-node inputs instead of the shared context** (part of DEV-01).
  `forbid_context_key` — the blind panel's anchoring defence — is a contract
  over the *whole run context*. Narrowing what a node sees narrows what its
  contracts can assert, and ADR 0002 designed it that way.
- **Auto-sending the "explain this" excerpt** (PAT-05 as written). A
  diagnostic turn commits its ledger diff before the composer speaks, so
  that click would be a mutation disguised as navigation. It pre-fills
  instead; a test pins that the click makes no model call.
- **Server-Sent Events for progress** (PAT-06 as written), and replacing the
  existing wait indicator. PAT-06 calls it "a static loading spinner"; it is
  a labelled `aria-live` line saying the wait takes minutes, written in
  response to this same complaint being made once already. The ticker is
  additive and a test asserts the wording stays.

### Notes

- **ADR 0044 is unmeasured.** Whether lab-derived terms turn `66/66 neutral`
  into anything is an empirical question. The number to check on the next
  real review is the count of non-neutral engine verdicts, and it is in
  PLAN.md's open issues. Shipping it and assuming it worked would repeat the
  LIRICAL mistake exactly.
- 56 new tests. 28 negative controls across the four ADRs, each verified to
  actually break its test — several only after a methodology fix: rapid
  write-run-restore cycles left stale `__pycache__` serving the previous
  module, which made two real controls look vacuous.

## [0.28.0] — 2026-09-02

*ADRs 0039–0042 — the rest of the adversarial-review adoption track. Four
ADRs, one release: they touch the same reader-facing surfaces and splitting
them would ship a half-renamed tier.*

### Added

- **Every review opens with `## The short version`** (ADR 0039) — what
  changed, what is being looked at now, what to raise first. The last real
  report was 52,969 characters with no summary at any point, for a reader
  with brain fog and fatigue.

  Derived from artifacts earlier DAG nodes already produced, so it cannot
  disagree with the sections beneath it. The review's proposed
  `patient_summary` LLM node was rejected: a fourth frontier call in a
  17-minute review, able to contradict the report it sits above.

- **A one-page printable appointment agenda** (ADR 0041) — new
  `casefile/export.py`, `GET /export/agenda`, and `/export/agenda.md`.
  Deterministic: every field is copied from the ledger, the labs database or
  `regimen.yaml`, and a test pins that the route makes zero model calls.

  One page is a bound, not an aspiration. The budget is derived from the
  print stylesheet — Letter at 0.5in margins, 9.5pt over 1.25 line-height =
  60 lines, less ~3 for table padding = 57 — and the worst case measures 54.
  It had to be a test: the first caps rendered 57 lines against a 46-line
  budget, and counting newlines instead of *wrapped* lines hid another 10.

  The medication table exists only because of `treatment_gate`'s
  `recording_only` scribe mode (ADR 0020): the full gate blocks every
  phrasing of a medication list, including a names-only one. `safety.py` is
  unmodified.

- **Each criteria set now shows its published citation** (ADR 0040).
  `CriteriaResult.citation` exists "so a doctor can look it up" per its own
  docstring and was rendered nowhere — 461 characters dead on the model.

### Changed

- **`cant-miss` renders as "Safety checklist"** (ADR 0039), with a note that
  being on the list is not a claim about the patient, plus a per-lead chip
  derived from ADR 0038's machinery: Ruled out / One test would settle this /
  Needs a specific finding / Being tracked.

  The schema value `cant-miss` is unchanged and no ledger on disk is touched.
  `prompts/composer.md` goes to version 3, because its own words for the tier
  were the last place the old label survived in patient-facing output.

- **Criteria lead with meaning, not arithmetic** (ADR 0039). Each set opens
  with a sentence saying in words that `points` is a floor; the point table
  moves inside a `<details>` block titled for a clinician. Collapsed, never
  removed. `LR` and `similarity` each gain a clause naming what kind of
  number they are — ADR 0036 forbids combining them in code, and a reader
  handed two bare numbers will combine them anyway.

- **The classification disclaimer is said once, in prose, before the first
  set** (ADR 0040) instead of once per set — 7 times, 17.7% of the section,
  in the position a reader has learned to skip.

- **Classification criteria read the whole record** (ADR 0042). `LabView`
  kept only the most recent row per analyte; `score_sle_2019`'s own docstring
  has said since it was written that the entry criterion is "ANA ≥1:80
  **ever**", and the 2019 EULAR/ACR criteria state that criteria need not
  occur simultaneously.

  Measured on one timeline — seropositive, complement-consumed and leukopenic
  in 2024, every value normal in 2026, which is what successful treatment
  looks like:

  ```
  before:  entry_met=False  0/10   "the 2019 criteria do not apply"
  after:   entry_met=True   13/10  threshold met
  ```

  Adding a later *normal* result erased the entire historical basis. The old
  behaviour scored successful suppression as evidence the disease had never
  been there. Complement now reads the lowest recorded value and
  counts-with-units read the peak; a criterion met historically carries both
  facts, cites both draws, and names any immunosuppressant in force when the
  later draw was taken.

  **Scores go up.** Measured in production on release day, over 2079 stored
  rows and 7 sets: **4 criterion items are now met by a historical value the
  latest draw does not meet**, where they previously read `not_met`. No set
  crosses its threshold on this record either way — the "a set now
  classifies" outcome is real on the test timeline and has not happened here.
  ADR 0040's sentence frames the case where it does: meeting a set says this
  case would count in a study of the condition, which is a different question
  from whether you have it.

  The regimen-suppression annotation is built and **unexercised** — none of
  the 7 current regimen entries is an immunosuppressant active on a superseded
  draw, which is the right answer for an untreated patient and means that half
  of CLN-03 has never actually run.

### Fixed

- **A critically low or high lab flag registered as neither** (ADR 0042).
  `LabFlag` has five members and both criteria predicates matched them by
  hand-written string sets covering `L` and `H` but not `LL`, `HH` or `A`.
  A critically low complement — the most clinically significant value the
  analyte can carry — read as normal in every criteria scorer, and so did
  every critically high value. Three of five members matched nothing.

  Now `labs.models.flag_is_low` / `flag_is_high`, derived from the enum and
  shared by all three call sites (`reported_corroborate.py` had the same
  holes plus two spellings the enum never produces). `A` is deliberately
  neither: it records that a value is out of range without saying which way,
  and guessing a direction would invent a finding.

  Same shape as the `_RA_RF` regex that could never match — a criterion
  silently unable to fire, indistinguishable from one that fires and finds
  nothing.

- **`UpdateHypothesis.rule_out` reached the ledger.** Shipped in 0.27.0
  (ADR 0038) and repeated here because the agenda now reads it.

- **A vacuous citation filter in the agenda's evidence selection.**
  `_support_lines` filtered on a truthy `Evidence.source`, which the schema
  validates non-empty — it read as a citation check and could never exclude
  anything. "Uncited" is an empty `evidence_for`, the definition
  `web.casefile_helpers` already uses.

### Rejected, and recorded so they are not re-proposed

- **PAT-01's `patient_summary` LLM node** — ADR 0039.
- **PAT-08's "Your Case Co-Pilot" persona** — ADR 0040. PLAN.md names
  over-trust and framing drift as risk 3. A co-pilot flies the same aircraft;
  the name claims shared authority over a decision this system must never
  appear to share. Warmth is already in the prompt; borrowed authority is not
  warmth.
- **PAT-07's patient-demographics header** — ADR 0041.
  `case/identifiers.yaml` exists to define what gets *scrubbed* (ADR 0017),
  and reading it to render PII would invert the one file whose purpose is
  removal.

Three of PAT-08's four claims were already false when measured: the composer
prompt already mandates "plain, compassionate language", `base.html` has
rendered a persistent footer disclaimer on every page since the web UI
shipped, and the report already had a "What to ask your doctor" section.

### Notes

- `case/regimen.yaml` gains a row in `docs/deployment-dependencies.md`. It is
  optional and absent by default, and the agenda now says so explicitly
  rather than rendering no medication table — which reads to a clinician as
  "takes nothing", not "unknown".
- `tests/test_criteria.py::test_the_most_recent_value_decides` is
  **deliberately replaced**: it pinned the property ADR 0042 reverses.
  CLAUDE.md rule 2 requires an ADR for exactly that, and the replacement
  carries the measurement in its docstring.
- 43 new tests, each negative-controlled.

## [0.27.0] — 2026-09-02

*ADR 0038 — how a hypothesis ends. Three gaps in ADR 0035's retirement pass,
which are one mechanism, so they ship together.*

### Added

- **A rule-out can now be evaluated, not only recorded.** `rule_out` was
  required at creation and never read again: 46 active hypotheses in
  production, 0 rule-outs ever evaluated. `RuleOutCheck` gives it a
  machine-checkable sibling — an analyte plus one of four operators
  (`negative`, `normal`, `below`, `above`) — and the retirement pass answers
  it against stored labs each review.

  Cannot-tell is **not** met. An analyte nobody measured never ends a
  hypothesis: absence of a test is the ordinary state of every differential.

- **`strength="definitive-exclusion"` ends a hypothesis without summation.**
  Clinical exclusion is not additive — a negative serum metanephrines
  excludes pheochromocytoma however many non-specific symptoms point at it,
  and `_outweighed`'s balance scale could not express that.

  Which sources may assert it is restricted in code, never left to a prompt:
  `labs:`, `doc:`, `encounter:` and `patient-report:` may; `pmid:` and
  `engine:` may not. Literature knows nothing about this patient, and a
  phenotype engine that never ranked something has not refuted it (ADR 0036).
  A refused claim is reported in the review rather than dropped.

- **`POST /ledger/hypotheses/{id}/retire`** — the first way a human can end a
  lead. `/ledger` was a single GET route and `is_protected` blocks every
  automatic rule, so a lead her doctor had definitively excluded stayed
  "worth discussing now" indefinitely: 10 can't-miss leads, none ever
  retired. Records reason and clinician, writes one `definitive-exclusion`
  evidence item plus a status change, through `apply_ledger_diff`. Reversible.

### Changed

- **Retirement protection is narrowed, not removed.** `cant-miss` and
  patient-origin hypotheses are still never retired by accumulated model
  opinion. The two new rules run before the protection check because both
  rest on something objective. Pheochromocytoma is a can't-miss lead *and*
  the textbook case of a diagnosis one negative test excludes.

- `ledger_maintainer` prompt v4 → v5: how to write a `rule_out_check`, and
  what may not be marked a definitive exclusion.

### Fixed

- **`UpdateHypothesis.rule_out` was declared and never applied.**
  `apply_diff` dropped it silently, so "settable after creation" (ADR 0035)
  was true of the schema and false of the behaviour. Every hypothesis on the
  ledger predates the field, so this was the only route by which any of them
  could acquire one.

## [0.26.1] — 2026-09-01

*Nine defects found by an adversarial code review of the codebase. Each was
verified against the real code before being fixed; five other items in that
review were investigated and deliberately not acted on (see the PR).*

### Fixed

- **The S3 backup source scan was unpaginated.** `list_objects_v2` returns at
  most 1,000 objects. Every source past the 1,000th read as "not present
  remotely", so each backup re-uploaded it. Now paginated, matching the
  restore path.

- **SQLite had no busy timeout.** The default is 5.0s. Separate ECS tasks are
  separate processes on one EFS file, and the in-process `RLock` does not
  cover them. A web request during a batch write raised
  `OperationalError: database is locked`. Now 30s.

- **The upload route caught only `VisionError`.** Any other failure returned
  HTTP 500 and left the file in `inbox/`, where every later sweep failed the
  same way. The scheduled path already had this guard; the interactive path
  did not.

- **`pdftoppm` had no subprocess timeout.** A corrupt PDF could hang an
  ingest task or a web worker with no limit. Now 150s.

- **A truncated `<think>` block defeated JSON extraction.** An unclosed tag
  does not match the strip pattern, so `find("{")` returned a brace inside
  the model's reasoning. `finish_reason == "length"` is now checked before
  any parse.

- **`EntailmentCache.save()` wrote in place.** Two processes could interleave
  and truncate the file. Now temp-file plus `os.replace`.

- **Document corroboration wrote a fabricated `#p1` page ref.** That check
  reads document dates only, never pages. The ref is now page-less.

- **Two apply-failure paths in the review DAG had no visible trace.** A failed
  `retirement_pass` or `apply_engine_diff` write read as an ordinary quiet
  week. Added `RetirementReport.error` and
  `EngineAdjudicationResult.apply_error`, both rendered.

- **`_RA_RF` contained a literal space.** Names match against
  `_normalize_slug` output, which strips spaces, so "Rheumatoid Factor"
  normalizes to `rheumatoidfactor` and the pattern never matched. RA 2010's
  rheumatoid-factor criterion was unsatisfiable from lab data since it
  shipped. Test coverage: 0.

## [0.26.0] — 2026-08-31

*Closes the loop ADR 0033 opened: a question answered in conversation now
stops being asked.*

### Added

- **The ordinary chat can close a next-appointment question.** The prompt
  instruction, the `answered_question_ids` field and the resolution call had
  all existed since ADR 0033 — inside `intake/agent.py`, and nowhere else.
  The diagnostic turn had no question handling at all, so answering one in
  normal conversation closed nothing.

  `resolve_answered` now lives in `casefile/questions.py` and both callers
  share it. The ledger-maintainer prompt (v3 → v4) is told to use the ids it
  can now see, and to ask one conversationally when it follows from what she
  just said — one at a time, the way intake does, never as a list.

  Resolution runs after the diff settles, so a citation retry cannot close a
  question on an attempt that was then discarded, and it is deliberately not
  gated on the ledger changing: "here is every supplement I take" updates the
  record without changing the differential.

### Fixed

- **The reasoner read a stale copy of the open questions.** `context.py` fed
  every stage `case/questions-open.md`, a rendering nothing regenerates,
  instead of `questions-open.yaml`, the store that holds the answered state.
  Measured in production: 43 questions, all `open`, none ever answered, while
  the answers sat in the record as facts — and 9 of 122 documents already
  mentioned the supplements she was being asked for again.

  ADR 0032's addendum in one line: a derived artifact is never the read path
  for data that has a source of truth.

- **An uncited can't-miss lead no longer leads the page** (ADR 0037). A
  can't-miss entry with no citations and low confidence was printed above
  leads her own labs point at. It stays in the leading group and is never
  hidden — the tier exists because missing one is catastrophic — but it now
  sorts last within it.

## [0.25.5] — 2026-08-31

### Fixed

- **The LIRICAL runner and its image disagreed on the data directory.** With
  the configuration wired in 0.25.4 the sidecar finally launched, and every
  task exited 1 on `Missing required file hp.json in /lirical-data`. The
  image downloads to `/opt/liricaldata`; the runner passed `/lirical-data`.

  The build-time smoke test could not catch this: it invokes `prioritize -d
  "$LIRICAL_DATA"` using the image's own env var, so it exercised the correct
  path while the only real caller passed a different one. A green build
  proved nothing about the call that matters — which is why the sidecar was
  described as "validated locally" and had still never worked.

  A test now parses `ENV LIRICAL_DATA` out of the Dockerfile and asserts it
  equals `LIRICAL_DATA_DIR`.

### Changed

- A release that lands on `main` without its back-merge to `develop` now
  fails CI immediately. It had been missed three times, and the symptom
  always arrived a release late: the next version bump found no version
  string to replace and produced an empty pull request.

## [0.25.4] — 2026-08-31

### Fixed

- **LIRICAL had never run — not once since ADR 0029.**
  `ADOC_LIRICAL_CLUSTER`, `ADOC_LIRICAL_SUBNETS` and
  `ADOC_LIRICAL_SECURITY_GROUPS` appeared nowhere in the repository, so
  `Settings` defaulted them to empty and `EcsLiricalRunner.run` returned
  "lirical task is not configured" in 0.0s on every review without launching
  anything.

  The image was built, the ECR repository created, `a-doc-lirical:1`
  registered and the narrow `ecs:RunTask` IAM granted — and nothing ever told
  the application where to run it. Measured in production: `cluster=''`,
  `subnets=[]`, `security_groups=[]`, against a healthy phenotype query of 8
  terms from 90 entries.

  Now wired into both task definitions, gated on the same condition as the
  sidecar. Verified with a CloudFormation change set rather than
  `validate-template`, which passed on the circular-dependency template in
  v0.22.0.

- **An engine that did not run left no trace in the report.**
  `render_review_markdown` reads the engines from the `results` sink, and
  every early return in both engine nodes skipped writing to it — only the
  success path recorded anything. So the "did not run this week" line, which
  exists precisely to make an absent engine visible, could never render.

  That is why a fault present since ADR 0029 never showed up in a single
  report. Both engine nodes now route every path through the sink; the
  identical defect in the similarity-engine node is fixed with it.

## [0.25.3] — 2026-08-31

*The release that lets the deep review run again. It had been failing before
doing any work, since the blind context pack outgrew a mis-declared window.*

### Fixed

- **DeepSeek's context window was declared 64,000; it is 131,072.** This is
  what took the review down: the blind context pack reached 31,261 tokens
  against a budget of 64,000 − 32,768 = 31,232 — over by 29 tokens, 0.09%.

  Verified against the live API rather than the docs. Featherless reports
  `context_length: 131072` for this exact model with `max_completion_tokens:
  32768` — precisely our completion reserve — and a 90,009-token prompt was
  accepted. Budget for that binding goes 31,232 → 98,304. Same model, same
  family, same behaviour; only a wrong number changes.

- **A call is now sized to the binding it actually goes to.** `complete()`
  resolves one binding by index and `blind_panel` calls it once per member,
  so no request is ever fanned out to all three at once. Sizing every call to
  the smallest window in the role failed a 200,000-token Opus call because a
  64,000-token DeepSeek shared the role.

- **Both engines proposing the same disease adopted it twice**, which the
  ledger invariants rejected — losing the entire engine diff, agreement
  evidence included. Both engines rank from the same phenotype, so this was
  the ordinary case, not an edge one.

- **An unusable disease name would have failed the whole review.**
  `build_engine_diff` sat outside its own try block, so a name that slugs to
  an invalid id raised out of a stage contracted never to fail a review.

- **`StatPearlsIndex` shared one sqlite connection across threadpool requests
  with no lock** — the pattern that already caused a production
  `sqlite3.InterfaceError` in `LabsDb`, which carries a long comment about it.

- **An optional sidecar could block the whole application deploy.** A
  transient failure in LIRICAL's build-time data download skipped "Deploy ECS
  stack", so v0.25.2 shipped nothing until the job was re-run. That build is
  now `continue-on-error`, and its change check — which compared against
  `HEAD~1` on a shallow clone, swallowed the error and therefore rebuilt on
  every deploy — works now the checkout fetches depth 2.

### Changed

- `docs/dag-topology.md`: mermaid diagrams of all three reasoning DAGs, and
  what drawing them revealed — eight of twenty review nodes read context they
  never declare, execution is sequential so the graph's one real branch is
  decorative, and several edges exist only to sequence.

## [0.25.2] — 2026-08-31

### Fixed

- **Both phenotype engines proposing the same disease adopted it twice.**
  `verdicts_to_ops` checked candidate ids against the ledger only, never
  against what the same pass had already adopted, so two `AddHypothesis` ops
  arrived with one id — the ledger invariants then rejected the *entire*
  engine diff, losing the agreement evidence riding along with it. Both
  engines rank from the same phenotype terms, so both surfacing the same
  missing candidate is the ordinary case rather than an edge one.

- **An unusable disease name would have failed the whole review.** A name
  that slugs to an empty id raises out of `Hypothesis` validation, and
  `build_engine_diff` was called outside the try in `_apply_engine_diff_fn`.
  Diff construction now sits inside the guard, and an unusable name is a
  reported note.

### Changed

- Documentation reviewed against the code. `CLAUDE.md` no longer enumerates
  CLI subcommands (the list had already gone stale, missing `genomics-panel`)
  and points at `adoc --help` instead. README's knowledge-layer section
  rewritten: it claimed one scorer where there are seven, that neither engine
  was wired into the review DAG, that the LIRICAL image was not deployable,
  and that no HPO phenotype profile existed — all four false. Phase 3 marked
  complete.

- `docs/dag-topology.md` — mermaid diagrams of all three reasoning DAGs, and
  what drawing them revealed: eight of twenty review nodes read context they
  do not declare, execution is sequential so the graph's one real branch (the
  blind panel) is decorative, and several edges exist only to sequence.

## [0.25.1] — 2026-08-31

### Fixed

- **The deep review could not run in production.** It failed at
  `blind_panel_0` with a context of ~31,261 tokens against a budget of
  31,232 — over by 29 tokens, 0.09%.

  `LlmClient.context_budget` subtracted a flat `CONTEXT_COMPLETION_RESERVE`
  (32,768) from the smallest bound window regardless of what the call would
  actually request. On DeepSeek-R1's 64,000-token window that reserves half
  the window for an output that is a short JSON list of hypotheses, leaving
  31,232 tokens of input — and the blind context pack had grown past it as
  phase 3 added sections to the pack.

  The reserve now describes the call: `context_budget` takes a
  `completion_reserve`, and `complete()` passes its own `max_tokens`. The
  blind panel asks for `BLIND_PANEL_MAX_TOKENS` (16,384), which still leaves
  a reasoning model room to think and raises the usable input budget from
  31,232 to 47,616 — 52% headroom over the pack that failed.

  If that ever proves too small the failure is loud rather than silent:
  `LlmClient` already raises on a truncated completion instead of returning a
  half-finished differential.

## [0.25.0] — 2026-08-31

*Closes every remaining PLAN.md phase 3 item. The knowledge layer stops being
a set of parallel opinions and starts changing the differential.*

### Added

- **Phenotype-engine divergence is adjudicated into the ledger** (ADR 0036).
  LIRICAL and the similarity index had run inside the review since ADR 0029
  and neither could change anything: both nodes sit after `apply_review_diff`,
  so by the time either spoke the ledger for that review was already written.
  The report got longer and the differential did not get sharper — exactly the
  failure `docs/research/scoring-across-engines.md` predicted.

  Two new nodes, `engine_adjudication` → `apply_engine_diff`. The model
  supplies a DIRECTION per divergence (corroborates / opposes / neutral) with
  a rationale; a postcondition contract enforces coverage, uniqueness, a
  substantive rationale each, and rejects one rationale reused everywhere.
  What a direction DOES to the ledger is plain code.

  Scores are never combined. A likelihood ratio and a Resnik similarity are
  not commensurable; both engines happen to store theirs in the same field, so
  the label is chosen by engine (`LR 12.4` vs `similarity 3.81`).

  `neutral` is a first-class outcome and the load-bearing decision. A
  phenotype-only engine that never ranked a hypothesis has not refuted it.
  Reading `ledger_only` as opposition would manufacture counter-evidence every
  week against precisely those hypotheses whose support lives in a modality
  the engine cannot see — and the retirement pass, which retires on
  accumulated counter-evidence, would start killing them.

  Guards: evidence-only ops (never a probability or tier re-grade), a
  mandatory rule-out before adopting anything, `expanded`/`low` entry tier, a
  cap of 3 adoptions per review, and `moderate` strength never `strong`.
  Engine agreement is recorded deterministically with no model call.

- **`engine:<lirical|semsim>:<YYYY-MM-DD>` citation scheme.** A hypothesis
  that exists BECAUSE an engine ranked it has to be able to say so. Unlike
  every other slug in the grammar this is a CLOSED set, so `engine:liricl:…`
  is a validation error rather than a citation resolving to nothing.

- **Three more classification scorers**, taking the registry from four to
  seven. EGPA 2022 and MPA 2022 complete the ANCA trio — published as a set of
  three, and encoding only GPA left the other two arms unmodelled. The payoff
  is their opposition: one eosinophil count now reads +5 for EGPA and −4 for
  both GPA and MPA, and MPO-ANCA alone classifies MPA while penalising GPA.

  Behçet ICBD 2014 is the first set reading NO labs — there is no serological
  marker, so a clinically diagnosed condition would otherwise be invisible to
  this layer however well the record described it. It reports
  `points_possible` against the threshold and never claims classification from
  text-matched findings.

  Every HPO id was verified against `hp.json` rather than recalled; two
  plausible guesses were wrong (`HP:0002383` is infectious encephalitis,
  `HP:0100648` a tongue neoplasm) and either would have produced a criterion
  that silently never matched.

- **Two eval suites, enabling gated model rotation.**
  `adoc eval --suite rare_disease_recall` builds 40 simulated patients from
  HPO annotations (4 of the disease's own terms plus 2 from an unrelated one),
  the way LIRICAL and Exomiser are themselves benchmarked. Measured on the
  2026-06-23 release: recall@1 0.225, recall@3 0.400, recall@10 0.525, median
  rank 2 when found. Gated at 0.40 — below the measured rate, so an ontology
  release that shifts a few cases does not cry wolf. A recall MISS does not
  fail a case: `adoc eval` ANDs every case into its verdict, so that would
  fail the suite permanently at any recall below 100%.

  `--suite self_case_replay` deliberately does not do what PLAN.md item (d)
  describes; that design needs a doctor-confirmed finding and there is none.
  It pins reproducibility and internal consistency of the deterministic layers
  over the real ledger instead. It skips visibly when no data repo is present
  AND when the ledger is empty — every check it makes is a bound or a floor,
  so its first run reported six green cases against an 83-byte ledger header.

- **Knowledge chat tools**: Mondo cross-references, Orphanet definitions and
  prevalence, StatPearls clinical-review lookup, and a disease-lookup tool.

- **The deterministic genomics panel** (ADR 0030): 5 markers, all called
  against the real array.

### Fixed

- **Reference-artifact paths no longer require a configured data repo.** The
  same bug had landed three times. `Settings` has no default for `data_dir`
  and raises without one, every ontology path on it is an absolute build
  artifact unrelated to patient data, and all the call sites sit inside broad
  `except` blocks. The exception was swallowed and each caller reported the
  wrong cause — most seriously, the review's LIRICAL node reported "the
  phenotype engine did not run" when the truth was an unset `ADOC_DATA_DIR`,
  silently disabling engine comparison in any environment without one. One
  `config.reference_path()` helper now serves all four call sites.

- **The review report showed a stale "after" ledger.** It read `ledger_after`
  from `apply_review_diff`, the PRE-retirement object, so since ADR 0035 a
  review that retired hypotheses reported a version it had already moved past
  and rendered a "what changed" ledger still containing every hypothesis it
  had just retired.

### Changed

- Dependency bumps: anthropic 1.2.0, openai 3.5.0, boto3 1.43.82,
  GitPython 3.1.61, actions/upload-artifact v7.

## [0.24.0] — 2026-08-30

*Tagged but deliberately NOT deployed. Both engines below are latent: ICAP
renders nothing until a positive ANA with a reported pattern arrives, and
the similarity index is only present in an image built from this tag.*

### Added

- **ICAP ANA-pattern mapping.** Maps an immunofluorescence pattern
  (AC-1 … AC-29) to the antibodies worth testing next and the conditions it
  is associated with. Pure code over a fixed reference table.

  Built knowing it renders nothing on the current case file, and shipped as
  latent capability. Measured first: all seven ANA screens from 2017 to 2025
  are negative — three by IFA, the method that produces patterns — every ENA
  antibody is negative, and no pattern word appears in the document corpus.
  A pattern is a property of a POSITIVE result. Verified against the real
  corpus: 2,079 rows scanned, 7 ANA results found, latest read as negative,
  zero patterns matched, zero lines rendered.

  Matching is longest-phrase-first because the substring traps change the
  clinical meaning: "homogeneous nucleolar" is AC-8 and points at systemic
  sclerosis while plain "homogeneous" is AC-1 and points at lupus, and
  "dense fine speckled" is AC-2, which argues AGAINST an ANA-associated
  rheumatic disease, while "fine speckled" is AC-4, which points at
  Sjogren's. Both pinned by test. Every rendering says a pattern is an
  association, not a diagnosis.

- **Phenotype semantic similarity**, a second independent engine beside
  LIRICAL. The two answer different questions — likelihood ratio against a
  curated model versus shared information content — so agreement is
  corroboration from genuinely different methods and disagreement is the
  finding. Not folded into a combined score: a similarity is not a
  probability.

  Resnik with symmetric best-match average, information content computed
  over disease frequency with annotations propagated to ancestors. A new
  build artifact from the public HPO release, baked into the image beside
  the existing HPO index: 19,835 parent edges and 11,645 diseases in 4.9MB.

  Validated against the real 2026-06-23 release. On the marfanoid triad
  LIRICAL's own smoke test uses, the top six are thoracic-aortic-aneurysm
  and Loeys-Dietz disorders with Marfan syndrome at 21 of 9,691, and
  information content is properly monotonic from 0.00 at the root to 5.20
  for "aortic root aneurysm".

  A known artefact is documented rather than tuned away: rare diseases with
  few specific annotations can outrank the obvious answer on a short query.
  That is a property of the measure and the concrete reason this engine
  reports divergence rather than an answer.

## [0.23.0] — 2026-08-30

### Added

- **`rule_out` is enforced in code, not only asked for in the prompt.** ADR
  0035 recorded the gap: a model that ignored the requirement produced an
  empty field and nothing rejected it. A new hypothesis without a usable
  falsification condition is now stripped from the diff before it reaches
  the ledger.

  A strip rather than a DAG contract, deliberately. A precondition runs
  before `apply_stage` and would raise on exactly the input the strip
  handles cleanly — failing a whole turn over one missing field, the defect
  fixed in v0.21.0. A postcondition cannot work either: the fifty
  hypotheses predating ADR 0035 all have an empty `rule_out`, so any check
  over the resulting ledger would fire on all of them forever.

  Stripping a hypothesis also removes every op that targets it. A verdict
  carries `add_evidence` and `record_challenge` ops pointing at what it
  adds, and leaving those behind produced a diff referencing an id nothing
  creates — which the ledger invariants reject outright, turning a
  contained strip back into the whole-payload failure it exists to avoid.

  Patient-origin and `cant-miss` hypotheses are excluded, exactly as they
  are from the retirement pass. The red-team suite caught this: a patient's
  own theory was being stripped before the quarantine could see it.

## [0.22.1] — 2026-08-30

### Fixed

- **The 0.22.0 deploy failed on a circular dependency and never shipped.**
  The LIRICAL runner's IAM policy is attached to `TaskRole` and granted
  `iam:PassRole` on `!GetAtt TaskRole.Arn` — a role referencing itself,
  which CloudFormation rejects when it builds the change set. Both roles
  declare an explicit `RoleName`, so the ARNs are now constructed from the
  account id and name instead.

  Production was never modified: the change set failed before any
  resource changed, so v0.21.0 stayed live throughout.

  `aws cloudformation validate-template` does not catch this — it passed
  on the broken template. Only creating a change set does, which is now
  how an IAM change to this stack gets verified before release.

## [0.22.0] — 2026-08-30

### Added

- **The ledger can now end a hypothesis** (ADR 0035). Measured at ledger
  version 12: 50 hypotheses, every one `active`, none ever retired across
  twelve versions, zero in the `most-likely` tier, 21 with no
  counter-evidence at all. `ruled-out` appeared in no prompt and no code —
  reachable in the type system, unreachable in practice. One stage added
  and no stage subtracted, so the ledger could only grow.

  Four changes: a deterministic retirement pass in the review; a required
  `rule_out` on every new hypothesis (the specific finding that would end
  it, never "further testing"); a displacement budget on the Challenger,
  which produced 47 of the 50; and a requirement that `most-likely` be
  populated or its emptiness explained.

  Two exclusions in the retirement pass are absolute: a `cant-miss`
  hypothesis is never auto-retired, because the cost of missing one is
  catastrophic and asymmetric, and a patient-origin hypothesis is never
  auto-retired, because her theory is hers to withdraw (ADR 0032). Nothing
  is deleted — retirement is a status change applied through a `LedgerDiff`
  so the existing invariants still check it, and reversible by any later
  review that finds support. Dry-run against the live ledger: 50 active
  → 42, with 11 protected and never assessed.

- **LIRICAL runs in the review and reports where it disagrees.** The
  engine is compared against the differential rather than folded into it:
  its composite likelihood ratio is the only genuine LR in the system,
  while criteria scorers produce points against a threshold and the panel
  produces uncalibrated buckets, and averaging those is the unit-blindness
  that has already produced three wrong clinical conclusions here. Three
  outcomes per item — the engine raises what the differential lacks, the
  differential holds what the engine cannot support (explicitly not a
  refutation, since LIRICAL sees only phenotype), and agreement, which is
  recorded rather than dropped.

  It runs on the phenotype QUERY, not the record (ADR 0034): eight terms
  rather than ninety, because the composite LR declines monotonically with
  profile size. Invoked as a sibling ECS task with narrowly scoped IAM —
  `RunTask` on one task-definition family in one cluster, `PassRole` for
  exactly the two roles that definition names and conditioned on
  `ecs-tasks`.

### Fixed

- **A Challenger rule that fired on nothing.** Its counter-arguments had to
  cover "every `most-likely` hypothesis", and `most-likely` was empty for
  twelve consecutive versions — so the requirement was satisfied
  vacuously every time. That is why 21 of 50 hypotheses carried no
  counter-evidence: not because they were unfalsifiable, but because
  nobody looked. Coverage now extends to the three highest-probability
  active hypotheses regardless of tier, and the stage is asked for cited
  `evidence_against` rather than prose alone, because a citation is what
  the retirement pass can act on later.

### Documentation

- `docs/research/scoring-across-engines.md` — why scores from different
  engines are not comparable, and why the ledger was not converging.
- ADR 0035, ADR 0034.

## [0.21.0] — 2026-08-29

### Fixed

- **One bad source ref could discard an entire Challenger verdict.** A live
  turn failed with two validation errors and lost every valid op alongside
  them. `Evidence.source` used a `field_validator`, which raises, and a
  raise inside a nested model fails everything containing it — so two
  unciteable evidence items in one hypothesis threw away the whole payload.
  ADR 0028 already required that no single field of one item may fail a
  payload; this is where that was not true. Unsalvageable evidence is now
  dropped from the hypothesis and logged loudly, while the hypothesis and
  the rest of the verdict survive.
- **A valid ref was rejected for carrying commentary.**
  `patient-report:2026-09-20 (as referenced in proposed diff...)` is a
  correct ref with an explanation appended — a model that has just written
  a ref tends to want to explain it. The trailing parenthetical is now
  stripped and the ref recovered. Salvage only: it strips a parenthetical
  and nothing else, so an invalid scheme stays invalid, and stripping runs
  only after a plain match has already failed, leaving a filename that
  legitimately contains parentheses untouched. The grammar is unchanged and
  constructing an `Evidence` directly with a bad ref is still an error.
- **A release could be tagged without its version bump.** `v0.20.0` was
  tagged and deployed from a branch whose `chore(release)` commit was never
  made — the bump and changelog edits were left unstaged — so the tag said
  0.20.0 while the package, and every provenance stamp it produced, said
  0.19.0. `tests/test_version.py` could not catch it: `pyproject.toml` and
  `adoc.__version__` agreed with each other and were merely stale relative
  to the tag. CI now refuses a tag that disagrees with the packaged
  version.

## [0.20.0] — 2026-08-29

*Released and deployed, but tagged without its version bump — the package
reported 0.19.0. Recorded here after the fact; see 0.21.0.*

### Fixed

- **A slow chat turn lost its reply.** Measured in production: an
  informational turn finished 63 seconds in with no POST completion logged,
  and a diagnostic turn was still running past seven minutes
  (`ledger_maintainer` 191s, `challenger` 218s) while the patient reloaded
  the page three times. The ALB's idle timeout was 60 seconds — the AWS
  default, never set in `alb.yaml` — so the load balancer cut the
  connection while the app was still working. The DAG completed and the
  ledger updated; the reply had nowhere to go.

  The timeout is now 1800s. Independently, the page no longer depends on
  that connection surviving: `chat_send` persists the assistant entry
  before rendering it, so `GET /chat/transcript` can return the reply and
  the page polls for it while a turn is in flight. A dropped connection
  becomes a short delay instead of a lost answer, which also covers a phone
  changing networks.

### Documentation

- ADR 0034 records the phenotype **record-versus-query** rule: the full
  profile is the record, what an engine receives is a query. LIRICAL scores
  +4.82 at eight terms and −25.97 at eighty-two.

## [0.19.0] — 2026-08-29

### Added

- **Open questions are a resolvable record** (`case/questions-open.yaml`,
  ADR 0033). A next-appointment question was a rendering rather than a
  thing: `questions-open.md` was rewritten wholesale by every review, so a
  question had no identity and nothing could record that it had been
  answered. The patient would answer in chat, the answer was captured
  correctly as a fact, and the next review regenerated the list from the
  ledger and asked again — the chooser has no memory of what she said
  between reviews and there was nowhere for that memory to live.

  Questions now carry a stable id derived from the panel text, so a review
  re-proposing the same panel inherits its answered state. A review merges
  rather than overwrites: wording and `last_asked_on` refresh, while
  `status`, `answered_on` and `answer_note` stay the store's. A question the
  chooser stops proposing is kept rather than deleted, since an item can
  drop out of one run and return in the next.

  `VisitCaptureResult` gains `answered_question_ids`. The model decides
  which queued question a message answers, because that needs judgement;
  deterministic code validates the ids, closes the questions and persists,
  and an id matching nothing is logged and dropped rather than failing the
  payload. The capture pass is shown the ids, since it cannot report an
  identifier it has never seen.

  Resolution runs before `run_visit_capture`'s `ops` early return, alongside
  the regimen changes that sit there for the same reason: "yes, biotin 10mg
  and vitamin D" may warrant no fact op when both are already on file and
  still definitively answers the question that asked for them.

## [0.18.0] — 2026-08-29

### Fixed

- **Model-authored markdown reached the patient as raw text.** The chat
  bubble macro interpolated a reply into a bare `<p>`, so headings arrived
  as `**...**` and every bulleted list collapsed into one unbroken block.
  The same file-sourced markdown (`case/questions-open.md`) was rendered
  through `markdown_lite` on the home page but raw on the intake record, so
  identical content was legible on one page and a wall of `#` and `**` on
  the other. Both now go through the filter, which escapes its input and
  emits only internal links.
- **`markdown_lite` was missing three constructs.** Asterisk italics
  (`*text*` — the form models actually emit; only `_text_` was handled),
  pipe tables (the criteria scorers render their per-item breakdown as one,
  and every row fell through to the paragraph branch to be joined with
  spaces), and code spans (`` `encounter:...` `` citation refs, whose
  backticks were literal). Verified against 74,232 characters of live
  case-file markdown: no unconverted construct remains.
- **The chat page grew without bound and put the newest reply furthest from
  the composer.** The composer is now at the top, the transcript reads
  newest-first beneath it, and history paginates at ten entries — five
  exchanges — per page. Older pages are read-only, since a composer there
  would prepend a reply to a page it does not belong to.
- **The assistant addressed the patient as though she were another system.**
  The patient-facing prompt was task and safety constraints with nothing
  about who is being addressed, so the model described the machinery to a
  peer — "the differential ledger holds 28 active entries", encounter IDs,
  internal status strings. It now names the reader, forbids the internal
  vocabulary, and asks for medical terms to be expanded on first use.
- **A summary placeholder was being reported as absent content.** `(pending
  review)` is `ingest.pipeline`'s "nobody has written a summary yet"; the
  context pack rendered it bare and a chat reply told the patient it had
  "no content from them yet". On the live case file 23 encounters carry the
  placeholder and 3 hold extracted text — two patient reports of 2,040 and
  38,965 characters a targeted question would have retrieved. The pack now
  distinguishes "unsummarised but searchable" from "nothing extracted".

### Changed

- Chat, review and ledger pages use a wider column; a reply or report
  carrying nested bullets and dated lab values is a document, not a quip.
  Wide tables scroll inside their own block rather than forcing the page
  sideways.

## [0.17.0] — 2026-08-28

### Fixed

- **Patient-reported history is read from the facts, not from artifacts
  derived from them.** The `intake_history` context section read four
  markdown files under `case/`. Those files are written only for topics the
  coverage state marks *covered*, so the read path was gated on something
  unrelated to whether the patient had answered. Measured on the live case
  file: 4 care-team facts sitting beside a 34-byte artifact holding only its
  heading, and one supplement fact with no artifact at all — the section
  reported care team as empty while four facts sat next to it. It now reads
  `IntakeFactsStore.active_facts()` directly, rendering each fact's
  statement verbatim so the patient's own wording survives. Scope is the
  three topics no other context path carries (family history, geography,
  care team); the rest already have one, and medications/supplements stay
  excluded because they converge on the regimen (ADR 0031).
- **`Provenance.app_version` had been wrong since 0.10.0.** `adoc.__version__`
  was a hand-maintained literal that drifted from the packaged version, so
  every artifact persisted across six releases was stamped `"0.10.0"`. The
  version is now single-sourced from installed package metadata, with tests
  pinning it to `pyproject.toml`. Artifacts produced before this release
  carry the stale stamp; it is not retroactively corrected.

### Documentation

- ADR 0032 addendum: a derived artifact is never the read path for data that
  has a source of truth.

## [0.16.0] — 2026-08-29

### Fixed

- **Patient history the intake captured is no longer invisible.** Intake wrote
  nine artifacts and the reasoner read three. On the live case file,
  `family-history.md` (644 bytes), `geography.md` (466) and `care-team.md`
  (34) were captured from the patient, written to disk, and read by nothing —
  as was the 141 KB `intake-facts.yaml`. The only intake-derived prose the
  context pack carried was a 698-byte case summary. Family history,
  geography, care team and undated events now form an `intake_history`
  section; a scaffolded-but-unpopulated file makes no section at all.
- **Intake medications converge on the regimen record** rather than a prose
  file nothing read. A list of names cannot answer whether she was taking
  something when a specimen was drawn; the regimen carries intervals and is
  already compared against lab collection dates. Intake's `still_taking`
  boolean converts honestly — taking becomes an open interval attested on the
  date she said it, not a start date, and stopped leaves both endpoints
  unset rather than claiming she stopped during the conversation.

## [0.15.0] — 2026-08-29

### Fixed

- **A failed intake turn no longer asks the patient to retype.** A turn ended
  with "Could you say it again, or put it a little differently?" after a
  6,775-character message. Nothing was wrong with what she wrote: the model
  returned `ops` as a JSON string containing a valid list, and the repair for
  that shape already exists. The message now says the failure is ours, that
  her words are saved, and that there is no need to retype.
- **Validation errors name the real fault.** `_validate_with_repairs`
  propagated the LAST candidate's error — the most heavily rewritten and least
  informative — so a repair that had already fixed the field was masked by a
  later candidate re-raising the shallow error. It now reports the candidate
  that got furthest, and a payload defeating every repair has its shape
  logged (keys and types, never values).

### Added

- **A single message is capped at 2,000 characters**, enforced by the
  textarea and again by the route, because `maxlength` is a convenience and
  not a control. Refused before the transcript is appended or a model is
  called, so an oversized message costs nothing and leaves no half-recorded
  turn.
- **Classification criteria are scored in every review and rendered
  itemized** — each criterion with its weight and what the record says. A
  phenotype match raises an item to `possible`, never `met`: the published
  criteria count an item only when no more likely explanation exists, and two
  of this patient's matched terms (a bupropion-induced seizure, and an
  arthritis mention from a list of conditions being considered) would have
  scored 11 points against a threshold of 10.
- **Three more criteria sets** — Sjögren 2016, RA 2010, GPA 2022 — chosen
  because the stored labs can actually feed them. Myositis and APS would have
  scored nothing but blanks and are deliberately unwritten.
- Numeric criteria thresholds carry a **unit** and convert; a row whose unit
  cannot be converted is excluded rather than compared. The GPA eosinophil
  penalty had matched both `4.5 %` and `320 cells/uL` against a 1×10⁹/L
  threshold.
- The phenotype profile is **narrowed before reaching an engine** (8 terms,
  from a measured sweep) and "myxedema coma" maps to Hypothyroidism rather
  than matching `Coma` alone.

## [0.14.1] — 2026-08-28

### Fixed

- **The 0.14.0 deploy failed and never shipped.** Adding the LIRICAL sidecar
  gave the CI stack a second ECR repository to manage, and the deploy role had
  never needed `ecr:CreateRepository` — the first repository came from the
  one-time manual bootstrap. CloudFormation rolled back cleanly (the existing
  repository, its images and the running service were untouched), but
  production stayed on 0.13.1. The role now has the permission, scoped to the
  two `a-doc` repositories, and the new repository `DependsOn` the role so the
  policy update lands first rather than racing it.

## [0.14.0] — 2026-08-28

### Added

- **Phenotype profile** (`case/phenotype.yaml`, `adoc phenotype-backfill`).
  HPO terms matched deterministically from encounter bodies — no model, since
  the ontology ships 26,237 synonyms so "joint pain" resolves to
  `HP:0002829 Arthralgia` on its own. Restricted to descendants of
  `HP:0000118`, because indexing every branch matched "Severe" and
  "Frequency" as though they were symptoms. Negation is detected before and
  after the phrase ("Coma: no") and cannot cross a sentence boundary — that
  leak recorded "Headache: yes" as absent — and a *following* "denies"
  negates the next finding rather than the previous one, which had been
  marking "night sweats" excluded in "joint pain and night sweats, denies
  fever". Built only from observed sources:
  scanning the case summary added one term and broke the independence that
  justifies an engine reading it.
- **LIRICAL is deployable**: its own ECR repository, a CI build that runs only
  when `deploy/lirical/` changes, and an ECS task definition behind a
  Condition so a stack deployed before the first image build never references
  a missing tag.
- **Encounter bodies are searchable** (ADR 0015 extended). Every
  patient-report encounter from chat previously contributed a title and
  nothing else once it left the recent window.
- **`case/reported-results.yaml`**: results the patient remembers but has no
  document for. Held apart from the measured series permanently — a remembered
  value must never sit in the series the citation checker guards — and checked
  against measured rows within 45 days, where a contradiction is as valuable
  an outcome as a match.
- **`case/disputes.yaml`**: the patient can say the record is wrong. A dispute
  never deletes; it marks the conflict wherever the item appears, stops it
  being read as established, and waits for a human. Only a human resolves one
  (ADR 0032).

### Fixed

- **The dates patients actually say now parse.** "two months ago", "last
  month" and "a few weeks ago" all returned nothing, so a regimen statement
  reached the record undated — losing exactly what decides whether she was
  taking something when a specimen was drawn. Worse, "June 2026" parsed as
  2026-01-01: five months off and stated as a date rather than the month it
  names.
- Three citation-extraction defects that were dropping real citations: a year
  qualifying a noun ("the 2025 panel"), a threshold inside a ratio claim
  ("FSH/LH ratio > 1.0"), and a bare `1` leaking out of "HbA1c".

## [0.13.1] — 2026-08-28

### Added

- **The regimen record is kept current from conversation.** It was a snapshot
  of whatever the backfill found; the list changes as the patient talks. The
  post-turn visit-capture pass now carries regimen statements alongside its
  fact ops, so this costs no extra model call. Two deterministic guards: a
  proposed substance name must actually appear in the patient's own message
  (a model that invents one writes a fiction into a medical record), and
  timing is parsed here from her own words rather than computed by the model,
  so "last month" keeps its precision. A start with no stated date attests
  today rather than claiming she began it during the conversation.

## [0.13.0] — 2026-08-28

### Added

- **`case/regimen.yaml`: what the patient takes, and when** (ADR 0031).
  Medications and supplements were modelled as `still_taking: bool`, and that
  boolean cannot answer the question this case turns on — was she taking
  biotin when the 2026-07-15 assay ran? High-dose biotin distorts many hormone
  and antibody immunoassays, so whether a result is real depends on an
  interval overlapping a specimen date. Entries now carry `started`/`stopped`
  with per-endpoint precision, `attested_on` dates, attribution, `reported_on`
  and source refs. A restart is a new interval, never a widened one; an
  undated entry reports `unknown`, never absent.
- A `regimen` section in the fixed context order, so the chat turn and the
  deep review read the same record. It aligns the regimen to lab collection
  dates and flags possible assay interference.
- `adoc regimen-backfill` seeds the record from regimen encounters already on
  disk — deterministic and offline, so the supplement list never leaves the
  machine. Run against the real case file it produced 23 entries, taking the
  regimen context a reasoner sees from 107 characters to 1,817.
- **Combination products are flagged when their name hides their contents.**
  Biotin appears zero times in the patient's regimen document while her biotin
  measured high — it is inside a B complex. The pack now says so, and names
  the label that would settle it, instead of asking her to bring every bottle
  she owns.

### Fixed

- The review report's "What to ask your doctor" section rendered one empty
  bullet per item — a row of bare dashes above the metrics appendix — because
  it still read the free-text field the item schema had moved away from. Both
  surfaces now share one renderer.

## [0.12.1] — 2026-08-27

### Fixed

- **The context pack now states whether a hypothesis has a plain-language
  gloss.** The first forced review on 0.12.0 produced zero glosses across 28
  hypotheses: the challenge-sweep prompt says to write one "only when the
  context pack shows it does not already have one", and the ledger section
  never rendered that field. The model was asked to check a state it was
  never shown. ADR 0028's rule one step out — if a model must act on a state,
  show it the state.
- **A long next-appointment list is prioritised, not truncated.** The same
  review produced 14 doctor items. None were junk, so dropping them would
  discard real clinical content while leaving the page looking complete; the
  first six of each group now lead and the remainder sit under "Also worth
  raising, lower priority (N)". `test_chooser.md` v3 asks for items ordered
  by yield, since that ordering decides what one appointment covers.

## [0.12.0] — 2026-08-27

### Added

- **Deterministic classification-criteria scorers** (`knowledge/criteria.py`),
  with SLE 2019 EULAR/ACR as the first of ~10. Every item is `met`,
  `not_met`, or `not_assessed`, and totals are an explicit **floor**: most
  items in these sets are clinical and no lab row can answer them, so scoring
  an unseen item as `not_met` would report a confident low total that is an
  artifact of missing input. Domain maxima are respected (additive across
  domains, single highest item within one) and every met item carries the
  `labs:<slug>:<date>` ref it was decided from.
- **LIRICAL v2.4.1 phenotype-only sidecar** (`deploy/lirical/`,
  `knowledge/lirical.py`, ADR 0029) — a non-LLM differential engine, run as
  its own container rather than reimplemented in Python. Validated locally;
  **not yet in CI/CFN and not yet wired into the review DAG**, because its
  input is a list of HPO terms and no phenotype profile exists.
- `Hypothesis.plain_language`: one or two sentences saying what a condition
  IS. A name alone is not communication — "Primary ovarian insufficiency /
  menopausal-range hypogonadism" is precise and tells the person whose case
  file it is nothing. Backfilled by the challenge sweep, which visits every
  active hypothesis on every review.
- ADR 0030 records which archived genomic data is admissible: the raw
  23andMe array export only. The 25 imputed BCFs carry `HDS` dosages with no
  per-variant quality metric, so a confidently imputed variant cannot be
  distinguished from a coin flip; the two "phased" exports are labelled by
  the vendor as not for medical use.

### Changed

- **The next-appointment page is assembled by code, not narrated by a model.**
  It had become 22 dense paragraphs — unusable for the patient and for any
  doctor in an appointment. `TestChooserItem` now carries short named parts
  (`panel`, `ask`, `why`) instead of one unbounded text field, so length is
  bounded by design. Each panel lists every hypothesis it bears on, one
  linked line each.
- **Items are split by who can answer them.** The list was telling the
  patient to ask her doctor what her own ingested imaging reports say, which
  supplements she takes, and whether she has bloating. Those are not
  appointment items: the system either holds the document or can simply ask
  her. `TestChooserItem.audience` separates them, patient-answerable first.
- `markdown_lite` gains italics, internal-only links, and continuation lines
  that stay inside their list item — without which the reformatted page
  rendered as literal underscores and detached paragraphs.
- An empty "Evidence for" section now says why it is empty instead of
  vanishing; silence reads as "no evidence was looked for".
- `test_chooser.md` v2 bans self-referential ranking and cost editorialising;
  `challenge_sweep.md` v3 defines the plain-language gloss task.

### Fixed

- A gloss-only hypothesis update no longer counts as a "change" on the home
  page, which would otherwise have announced all 26 hypotheses as changed on
  the first review after `plain_language` shipped.

## [0.11.9] — 2026-08-27

### Fixed

- **Encounters carry their citable ref in the context pack.** Encounter files
  are named `YYYY-MM-DD--<slug>.md` but the pack showed only the date, so a
  panel wrote `encounter:2026-08-04` and the citations were dropped. Fourth
  instance of one defect — a model asked to reproduce an identifier it was
  never shown — now stated as a standing rule in ADR 0028.
- **A decline stated in words matches a value stored with a minus.** Three
  real DXA citations were dropped for claiming "a decline of 8%" against a
  stored `-8.0`. Magnitude matches only when the row is negative and the
  claim states direction lexically; a claimed rise still fails against a
  stored fall.

## [0.11.8] — 2026-08-27

### Fixed

- **No single field of one evidence item can fail a review any more.** A
  panel member wrote `strength: "supporting"`, the `Literal` refused it, and
  all 14 nodes and 13 minutes went with it — the same death as the invalid
  source ref two releases ago, one field over. `strength` and
  `probability_bucket` now map obvious synonyms and degrade unknown values
  with a warning rather than raising.
- **Hypothesis cards are readable.** `challenger_notes` accumulates one entry
  per review and the card dumped the whole accumulation into a single
  paragraph, so three challenges from three different weeks arrived as one
  unbroken block, each opening with the same 60-character stem. Entries are
  now split and labelled, all but the newest folded. Source refs render in
  words rather than slug syntax, with the exact ref kept in the link title.
  Chips carry field labels instead of reading as a run-on.

### Changed

- `challenge_sweep.md` v2 and `divergence_adjudicator.md` v2 cap a note at
  three sentences and a rationale at two or three. These strings are written
  verbatim onto the hypothesis and stack up on the patient's case page; a
  150-word argued paragraph is the same challenge as a 40-word one, only
  harder to act on.

## [0.11.7] — 2026-08-27

### Fixed

- **An analyte name's digits are no longer read as a quoted value.** The
  citation checker extracted `-125` from the name "CA-125" (the optional sign
  in its number pattern read the hyphen as a minus) and `2024` from "percent
  change vs 2024", then dropped two well-formed citations of real rows for
  disagreeing with the stored values. The digits-then-word direction was
  already handled ("25-hydroxy", "10-year"); this adds the mirror, which
  covers most of immunology (CA-125, HLA-B27, IL-6, CD4, C3, T4, B12, A1C).
  Years are stripped only behind a temporal preposition, because real
  analytes live in that range — vitamin B12 in the 2000s pg/mL still reads
  as a value.

## [0.11.6] — 2026-08-27

### Fixed

- **Panel citations now survive agreement, not only disagreement.** A
  divergence exists only where the blind panel and the ledger disagree, so
  citations reached the ledger exclusively on disagreement: where the panel
  *agreed* with a hypothesis its refs were dropped on the floor, and a
  probability mismatch kept only the disagreeing members' refs. The result,
  measured in production after the previous release fixed the crash: 25
  hypotheses, 1 with any evidence. The best-supported hypotheses — endorsed
  by both the ledger and an independent panel — were exactly the ones
  rendering as uncited. Citations are now collected for every hypothesis the
  panel named and attached with `AddEvidence`, deduped so a weekly review
  does not re-add the same citation forever (ADR 0028 addendum).

## [0.11.5] — 2026-08-27

### Fixed

- **A bad panel citation no longer destroys the review.** The blind panel
  began citing densely and immediately killed a 14-node run: it emitted
  `other:monospot_(heterophile)_screen:2026-03-17` — a real analyte on a
  real date with an invented prefix — and `BlindEvidenceItem.source`
  validated its ref in a field validator, which raises. Four bad refs failed
  the payload, the panel member, and the whole review. Filtering now happens
  after the payload parses, where the drop-and-log filter was always meant
  to run (ADR 0028).
- **The context pack now shows each lab row's citable ref.** Document
  excerpts always carried `doc:<file>#p<page>`; lab rows carried nothing, so
  a model had to construct `labs:<slug>:<date>` by guessing the slug — and
  guessed the prefix from a section heading. Refs are slugified rather than
  interpolated raw, because 1178 of 2079 stored names contain the whitespace
  or colons the grammar forbids. Measured after: 568 refs, 0 invalid, 0
  unresolvable.
- `blind_reviewer.md` v4: copy refs verbatim, never construct one; a panel
  heading is not a ref prefix.

## [Unreleased]

## [0.11.4] - 2026-08-27

### Added
- **Unit normalization.** 26 of this patient's 461 analytes were stored under more than one unit, in two categories. **Cosmetic** (21 of them) is the same quantity spelled differently by different labs — `IU/L`≡`U/L`≡`unit/L`, `mcg/dL`≡`ug/dL`, `uIU/mL`≡`mIU/L`, `Thousand/uL`≡`x10E3/uL`≡`x10(9)/L` — now grouped as synonyms, which changes no stored value. **Magnitude** is exactly the five CBC differential absolutes, which report `x10E3/uL` at some points and `cells/uL` at others, a factor of 1000; these get explicit conversion factors, and an unknown conversion returns nothing rather than a guess. Stored values are deliberately *not* rewritten — `citation_check` compares a claim's number against the stored value, so converting in place would break every existing citation and make the store disagree with its source document. Conversion happens at comparison time, where the ambiguity actually hurts.
- The trajectory section now converts rather than scoping to the latest unit, recovering history it had been discarding: 23 comparable eosinophil readings instead of 17, and the 2017 value converts correctly for a real 220% rise across the decade.

## [0.11.3] - 2026-08-27

Readability and temporal fidelity, all found by reading the real case file rather than fixtures.

### Added
- **The context pack shows trajectories, not just a snapshot** (ADR 0027). Its labs sections were "abnormal, most recent per analyte" and "latest panel" — a stage could see *movement* only by calling a tool, or by reading a document that narrated its own comparison (which is how the blind panel knew about the DEXA decline: the report did the arithmetic). The snapshot was hiding the ovarian-failure signature on the real corpus — LH rising 1330%, FSH rising 1119%, AMH falling 96% — none of it visible in any single row. Only readings sharing a unit are compared: a naive version reported `eosinophils rising 319,900%`, which was a unit change mid-history (×1000), not a clinical event.
- **The blind panel must cite, so hypotheses arrive with evidence.** The ledger held 24 hypotheses and *zero* evidence items, so every card rendered an empty evidence section. The prompt had always asked for source refs; the schema gave it nowhere to put them, and 0 of 24 hypotheses carried a ref even in prose — the panel cited *values* densely and never the row they came from. An unresolvable ref is dropped and logged rather than failing a 12-minute review, because the review path has no citation-check contract of its own.

### Fixed
- **The differential has a spine.** 24 hypotheses rendered flat in file order, equal weight, no ranking — for a patient reading her own case file that reads as "you might have 24 things". Now ordered strongest-first and split into what to discuss now versus a folded tail. Nothing is dropped, and a can't-miss lead is never folded however unlikely.
- **"What's new" no longer dumps model prose.** It rendered the full adjudication rationale verbatim — thousands of words in one paragraph. The diff's ops are typed, so the change set is now derived: "2 new leads, 1 updated, 2 re-challenged", with the reasoning behind a disclosure.
- **Encounters no longer assert fabricated date precision.** `"2021"` parsed to `2021-01-01` and `"spring 2022"` to `2022-01-01` — the wrong season, stated to the day, indistinguishable downstream from a real January 1st. `date_precision` and `reported_on` are recorded and rendered (`2021`, `~2022`, `(reported …)`), both defaulting so existing encounter files round-trip unchanged.

## [0.11.2] - 2026-08-26

A scheduled review failed in production; investigating it found two Phase-2 completeness gaps behind it.

### Fixed
- **Truncation is detected on every call, not only structured ones.** Both providers guarded with `if request.schema is not None`, so a free-text completion that hit the token budget was returned as if it had finished — and `run_informational_turn` passes no schema, meaning a patient-facing answer that stopped mid-sentence reached the patient undetected. The check also lived inside each provider's transport, which the injection seam bypasses, so no test could reach it. The transport now *reports* (`TransportResponse.truncated`) and `LlmClient.complete` *judges*: one provider-agnostic rule, audited as an error.
- **A divergence no longer has to be echoed character-for-character.** The adjudication contract compared ids by exact string equality against a generated slug — for the review that failed, a 62-character unbroken run. The model *had* adjudicated the divergence and written a substantive rationale; the run died on spelling. Matching is now exact-first, then id and human-readable name with case and punctuation stripped, and an ambiguous key resolves to nothing rather than guessing — attaching a rationale to the wrong hypothesis is worse than failing the contract.

### Added
- **Context is sized to the weakest bound model** (`ModelBinding.context_window`, `LlmClient.context_budget`). Nothing previously modelled a context window at all. It matters because `blind_panel` renders one context pack and hands the identical payload to three model families: a pack sized to the largest window is only sometimes valid. Windows are *declared* in `models.yaml` rather than inferred from a model id, because the real limit depends on the model **and its host**; an undeclared window disables the check rather than inventing a number. Measured against the real store: the blind pack is ~9,580 tokens against a 31,232 budget.

## [0.11.1] - 2026-08-26

Found by rebuilding the real corpus from source. Every one of these cost data or visibility in a live 121-document run.

### Fixed
- **An empty row is not a result, and one bad row no longer kills a run.** A re-ingest died on document 100 of ~97 with an unhandled `ValidationError` — `LabResult requires at least one of value/value_text` — taking every remaining document with it. Two defects: the ADR 0025 gate classified a row with *neither* a number nor result text as `qualitative` and kept it (a label the extractor transcribed with nothing beside it is not a result), and the failure escaped to the top of `adoc backfill`, so the per-file `error` outcome that exists precisely for this never fired. A document that cannot be processed is now an error *for that document*; the run continues and reports it.
- **Tool output nested under a placeholder envelope is rescued.** Six lab reports were lost to `{"parameter_name": "DocumentExtraction", "parameter_value": {...}}` — the model echoing the tool's scaffolding instead of filling it in. `_unwrap_tool_input` only unwraps a single-key dict, so a two-key envelope went straight to a hard failure: 5% of the corpus, silently, as six error lines nobody reads. Repaired in the same flat-first chain as the other known malformations; a legitimate payload containing a nested object is never unwrapped.
- **A long command's progress is visible while it runs.** A healthy backfill went 25 minutes with two lines of output while archiving 300+ files, because Python block-buffers stdout when it isn't a TTY and the genomics phase makes no model calls at all (so the per-call logging that would otherwise show life has nothing to say). A healthy run looked exactly like a hung one. stdout is now line-buffered at every entrypoint.
- Office lock files (`~$name.docx`) are skipped. They appear whenever a document is open in Word, are not documents, and one reached a real backfill and reported as an error.

## [0.11.0] - 2026-08-26

### Added
- **An intake conversation can be replayed instead of typed** (`scripts/intake-replay`). A full initial visit took about an hour by hand, so nothing about the intake agent was ever tested twice the same way. It now takes ~9 minutes: 33 turns, driven through the real `POST /chat/send`, reporting per-turn timing, withheld replies, and which message intake completed on. `--then` sends one message afterwards, which is how the slow post-intake diagnostic turn gets exercised.
- **A simulated patient, because a fixed script cannot test a conversational agent** (`--persona`, new test-only `patient_simulator` model role). The intake agent rewords its questions and invents new ones — it started asking for a preferred name — and a canned list of answers then drifts one question behind and silently stops testing anything. The simulator reads case notes and answers what was actually asked, says "I don't remember" when the notes don't cover it, and never volunteers what it wasn't asked. That last part matters: vagueness is what exercises the probe and clarification paths at all. Its first run found three defects an hour of manual typing had not.
- **A bounded result is stored as a number** (ADR 0025). 183 rows held their value as the string `<20` or `>150`, where nothing numeric could reach it — `<20` on an RNA Polymerase III antibody is a negative result, and a move from `<20` to `45` is clinically meaningful; both were invisible. `LabResult.comparator` records that a value is a *bound* rather than a point measurement. Titers are excluded deliberately: `<1:256` is not the number 1, and 41 rows are that shape. Note the obligation this creates — a consumer reading `value` alone now treats `<20` as a measurement of exactly 20.
- **Human review decisions are source data** (ADR 0026), in `case/review-decisions.jsonl`. `labs.sqlite` is explicitly a *derived* artifact, yet the outcomes of a person reviewing the confirm queue lived only inside it, entangled with the extractor's output — which made "rebuild from sources" destructive rather than routine. 587 of 2033 rows carried human review (396 corrected, 162 confirmed, 29 rejected). Also documents what was never built: `adoc backfill --re-extract`, promised by `PLAN.md` and `labs/db.py`'s docstring, does not exist.
- **Logging that says what a long turn is doing** (`adoc.logging_setup`). Per-DAG-node start and elapsed time, per-model-call role/model/timing/tokens, and which contract stopped a run (the contract name only — a violation message can quote patient-facing text). Configured for every entrypoint, not just `serve`, since the experiment scripts run out-of-process and were silent.

### Fixed
- **A chat turn no longer looks like a dead button.** Reported as "the Send button stopped working after intake finished" — it hadn't. The first post-intake turn routes to the full DAG and runs for minutes, and nothing on the page said so: no indicator, no disabled button, and htmx queues a repeat submit behind the in-flight one, so pressing Send again also did nothing. The wait is inherent, so the fix is to stop hiding it.
- **A threshold is no longer mistaken for a claimed lab value** (ADR 0023). A diagnostic turn died after 604 seconds on six `composer_number_check` mismatches — an assay floor, a deficiency cutoff, a decision limit, two IgE class boundaries. The worst case flagged was "Vitamin D insufficiency is defined as below 30 ng/mL", which asserts nothing about this patient, and it withheld her entire answer. It had survived three previous narrowings because ADR 0016 treats an attached unit as proof a number is a value, and threshold phrasing almost always attaches a real unit; nothing examined the word *governing* the number. ADR 0023 also records the trigger for demoting this check to a warning: it has now been narrowed four times without ever catching a real fabrication.
- **Intake records medications rather than prescribing them.** A reply was withheld from a patient who had just said she could not remember her medication, because it named a drug and a dose *while asking about it* — which is intake's whole job. The gate now has a recording mode that keeps the imperative-instruction rule and drops the bare-dosage rule; instruction-shaped output still blocks. A withheld intake reply also used to log nothing, so the only way to notice was reading the conversation.
- **A gerund opening a clause is no longer read as an instruction.** "…Taking for those (iron, selenium)?" was withheld: the advice detector looks back a few tokens for a subject, and a clause-initial verb has nothing to look back at. English has no "-ing" imperative — *take iron* instructs, *taking iron* cannot. "Consider taking" and "I recommend tapering" still block.
- **A list field arriving as a JSON string no longer kills a turn.** Tool-use output sometimes serializes `ops` into a string; pydantic rejected it, the retry reproduced the same shape, and the turn died — 4 of 33 turns in one run. Repairs are now tried plainest-reading-first, so a correct payload is never reinterpreted and an unrescuable one still raises a real error.
- **The patient is no longer shown a pydantic traceback.** One turn put `Input should be a valid list` and an `errors.pydantic.dev` URL in the chat window. The detail goes to the log, where it is actionable.
- Local dev scripts: `restart-local` came back on the default port instead of the one it had just stopped, found it occupied, and left nothing running. `--force` destroyed the web login, because `work/` is gitignored and so never in a clone — and `user-create-local` needs a TTY, so a clean slate could not be scripted at all; it is now preserved, with `--reset-users` to opt out. A working dir with no login now says so instead of surfacing later as an unexplained 401.

## [0.10.0] - 2026-08-25

### Removed
- **The red-flag emergency screen is gone** (ADR 0021, supersedes 0014). In live use it produced only false positives and never caught a real emergency — most memorably flagging a patient's plumbing, "our home has a septic system and a well", as sepsis. Intake is historical narrative by construction, so it matched constantly. Removed rather than tuned: it went block → warn → proposed-warn-on-question in a single day, which indicates the mechanism doesn't fit the product. **There is now no automated emergency detection anywhere in the system**, on the owner's reasoning that a patient having a genuine emergency does not type it into this app.

### Fixed
- The treatment/dosing gate no longer mistakes a measurement for a dose (ADR 0020). A real diagnostic reply was withheld — after the ledger had already committed — because it reported an ultrasound volume of `106.0 mL`. `mg`/`mcg`/`IU` still fire on their own; `g`/`mL`/`units` now require dosing context (an imperative verb or a frequency), because a dose is part of an instruction while a measurement is part of a finding. All blocked fixture cases still block, including a new liquid-dose case so the change can't become a hole.
- A malformed fact operation no longer costs an entire intake turn. The model emitted an `add_fact` op with its fields flat beside `op` rather than nested under `fact`; that failed structured-output validation, which fails the whole turn *before* the per-op tolerance added earlier can see it — the patient lost a full message of family history. The flat shape is now lifted into place, and a parse failure retries once with the validation error fed back.
- The composer's number check no longer mis-pairs values across analytes. When one sentence mentioned two analytes it cross-checked them — FSH's real value judged against LH's stored values, ALT's against AST's, a BMD against a T-score — and `hs-CRP` matched the substring `crp`. Numbers now bind to a governing mention, longest label wins, and genuine ambiguity fails safe.

### Changed
- **The deep review is event-triggered rather than weekly** (ADR 0019). New documents or a chat turn that actually changed the ledger set a marker; a 30-minute tick runs the cheap deterministic parts always, and a full review when the marker is set and a 6-hour cooldown has passed — so a multi-file drop coalesces into one review. A 7-day floor preserves the old weekly guarantee, because the blind panel's value is highest precisely when nothing new arrived.
- **The ledger and review pages are now one screen.** Once reviews fire on evidence rather than a calendar, "the latest review" and "the current picture" are the same thing. `/ledger` shows the live differential, the latest review inline with what triggered it, and prior reviews as history; `/reviews` redirects.

## [0.9.0] - 2026-08-25

### Added
- **The initial visit follows a clinician's progression** (ADR 0018, refining 0012 — no visible sections, still). It opens by asking age and sex at birth rather than "what's been bothering you", then moves through family history, geography, a review of the record already on file, and only then what's recent. The order is internal steering read off the coverage map, not a stepper: anything volunteered out of order is still captured immediately.
- **Geography is a new intake topic** — residences, travel, and environmental/occupational exposure. It was missing, and it matters here: the record includes tick-borne and regional infectious panels.
- **The record-review stage uses the document corpus for its intended purpose** — walking the history already on file and asking the patient to confirm or correct it, citing what it references, instead of asking her to retype what her own documents say.
- **Visits now have continuity.** An explicitly persisted follow-up marker (set by the model through typed ops, never inferred) plus a code-authored greeting that opens a new visit with when you last spoke, what is unresolved, and what was flagged to revisit. The same three things appear on the Intake record page.

### Changed
- `entailment_verifier` rebound from DeepSeek R1 to DeepSeek V3 on live evidence (ADR 0016): over 20 labelled pairs, R1 took 78s at 85% agreement while V3 took 20s at 95%. The reasoning model was both slower and less accurate, and it skewed toward rejection — so the earlier over-blocking incident was not purely a prompt problem as first concluded. V3 keeps the verifier on a third family, distinct from the proposer (Anthropic) and the challenger (OpenAI).

### Fixed
- **A diagnostic turn no longer takes 23 minutes.** Measured at 1410s with the entailment verifier consuming 66% of it across four calls to strip a single claim. The now-pointless retry is gone (the consequence is stripping that claim, not losing the turn), verdicts are cached across a turn's contract re-checks, and only most-likely-tier evidence is verified synchronously — the rest is queued and swept by the weekly review, which provably picks it up. Verifier calls per turn: four to one, pinned by a CI guard on the model-call count.
- The composer's number check no longer reads percentages or years as lab values ("Ferritin dropped by 40% since 2024" flagged both 40 and 2024). It now requires positive evidence that a number *is* a value, while still checking percent-suffixed numbers when the analyte's own unit is a percent — a blanket percent exclusion would have opened a fabrication hole.
- **Deploys take about a minute instead of six.** The ALB target group's deregistration delay was never set, so it defaulted to 300s: the load balancer drained the old task for five minutes before ECS could start its replacement, and because the service runs single-writer (max 100% / min 0%) nothing could overlap. Now 30s, with the health-check interval dropped to 10s.

## [0.8.1] - 2026-08-25

A code review of the whole codebase produced these; each fix has a test verified to fail without it.

### Fixed
- **Direct identifiers were being sent to external model providers.** The web app built its LLM client with no scrubber and the client's default was a no-op, so every chat and intake turn transmitted unscrubbed text to Anthropic, OpenAI, and Featherless. Separately, the identifiers file the scrubber reads for name/DOB/address was never scaffolded, so even the CLI path only ever applied shape-based SSN/phone/email/MRN patterns. The real scrubber is now the default (a no-op must be explicit), `adoc init` scaffolds `case/identifiers.yaml`, `adoc identifiers show|add|remove` manages it, and a missing or empty file produces a loud warning instead of silent degradation (ADR 0017). Page images sent for vision extraction remain unscrubbable and are documented as an accepted limitation.
- **Two paths let model text reach the patient without passing the treatment/dosing gate.** Informational chat replies were never gated at all — deprecating `guarded_turn` in 0.7.1 had silently orphaned the only gated entry point — and the ledger page and weekly review reports rendered evidence claims, challenger notes, and review markdown raw. The gate now lives inside the function every informational caller uses, and both surfaces redact only the offending passage rather than blanking the page.
- **The Phase 2 guards blocked so aggressively that a real diagnostic turn could not complete** (measured: 29 of 41 claims rejected, still 14 after the retry). `not_entailed` now means factual conflict with the source rather than "adds clinical interpretation", and a failing claim is stripped from the diff instead of destroying the turn — stronger grounding, since unverified evidence never reaches the ledger. The composer's number check no longer reads "elevated across 3 separate panels" as a lab value. The eval suite gained the missing false-positive direction: it previously only tested that fabrications are caught, never that legitimate claims pass (ADR 0016, revised).
- **A shared YAML parser on the auth path was crashing requests and returning silently wrong results** (31–173 failures per 320 concurrent calls) — the third instance of the shared-mutable-state-across-threads bug class, and the true cause of a test that had been dismissed as flaky, now deterministic. The login rate limiter carried the same false single-threading assumption and could under-count concurrent failed attempts.
- **Zip ingestion could lose a document silently**: a failed member left the archive marked successful, so inbox hygiene deleted it with no failure record anywhere. An unexpected exception also abandoned every remaining file in an ingest batch.
- LLM-decided twin rejections now carry model id, prompt version, and timestamp, as the provenance rule requires.

## [0.8.0] - 2026-08-25

### Added
- **Document text corpus** (ADR 0015). Every non-genomic ingested document's full text is extracted (`pdftotext` for PDFs, python-docx for `.docx`, verbatim for text — no LLM calls), stored as a committed `doc-text/<sha256>.txt`, and indexed in SQLite FTS5. This makes the narrative content of the record usable for the first time: the patient's own written history, plus the interpretive comments on lab reports and the impressions in imaging and consult reports, none of which ever became lab rows. Retrieval is capped, ranked, verbatim snippets with source refs — fed to diagnostic turns, intake turns, and a new `search_documents` tool — so the intake conversation can reference what she already wrote instead of asking her to retype it. Genomic files are structurally excluded: the extraction dispatcher's type has no genomic member. New `adoc backfill-doc-text` covers already-ingested documents; Documents → Consumed gains a read-only text view.
- **Phase 2 complete** (ADR 0016). A cross-family entailment verifier (a third model family, distinct from both the ledger-maintainer's and the challenger's) judges each evidence claim against its resolved source text; `doc:` refs resolve against the document corpus, page-scoped where the citation names a page. Non-entailed claims bounce back with the verifier's objection, one retry, then a DAG contract rejects the diff. Composer output gets a deterministic (no-model) check that every number near an analyte name matches a value actually stored in the lab database. Abstention is a first-class typed signal with a contract preventing an unsupported hypothesis from reaching the most-likely tier. A hallucination eval suite runs in CI: planted-fact containment and fabricated-citation detection both at 1.0, entailment precision/recall measured on hand-labeled pairs, plus an abstention-rate probe.

## [0.7.2] - 2026-08-25

### Fixed
- Concurrent requests could crash the labs page and, worse, return torn data. `LabsDb` shared one SQLite connection across FastAPI's sync-route threadpool on the (false) assumption that this app never serves two requests at once; a browser issuing parallel requests produced `sqlite3.InterfaceError: bad parameter or other API misuse` in production, and — with the fix removed to check — also a torn row surfacing as a `None` primary key inside a validated model. All 43 connection-touching methods now hold a re-entrant lock spanning execute through fetch.
- The same shared-singleton-across-threads shape applied to `DataRepo`'s git writes. `commit()` is a read-modify-write over `.git/index` and `HEAD`, and every intake turn commits, so a concurrent caller could collide on git's index lock or silently lose a commit — that is, lose patient-reported facts. `commit()` and `tag()` are now serialized.
- Both regression tests were verified to fail with their lock removed (SQLite: `InterfaceError` and a torn-row validation error; git: a half-written `COMMIT_EDITMSG`), so they genuinely exercise the race rather than passing either way.

## [0.7.1] - 2026-08-25

### Changed
- Red-flag screening now **warns rather than blocks** (ADR 0014). The screen itself is unchanged — same rules, same terms, same matching, still running before any model call — but a match prepends a fixed, code-inserted warning naming the matched category and the conversation continues, instead of the warning replacing the turn. As a hard block it made intake unusable: recounting history is the whole point of an initial visit, and the screen deliberately does no negation or tense detection, so it fired on ordinary historical accounts and discarded the patient's entire message. The pinned red-team contract was replaced accordingly — from "zero API calls on a flagged turn" to "the warning always reaches the patient and the model cannot suppress it."
- The initial-visit opener no longer carries an emergency disclaimer, and points at records already on file rather than asking for documents the patient has already provided.

## [0.7.0] - 2026-08-25

### Fixed
- An invalid fact operation no longer destroys an intake turn. A single malformed field (live: a `kind` value placed in the `section` field) previously aborted the whole batch, losing the patient's message entirely — no reply, no facts, nothing persisted. `section` is now a closed enumeration rejected at structured-output validation, invalid operations are skipped and reported rather than aborting their siblings, and one feedback-guided retry names the failures so they can be re-emitted. The patient always gets a reply.
- The Dropbox puller ingested only PDFs, so `.docx`/`.txt`/`.zip`/genomic files placed in the inbox were silently never pulled despite full pipeline support. Now filters on every type `detect_intake_kind` classifies, derived from that function rather than hand-listed.
- The red-flag screen's message no longer dead-ends an intake conversation: it now states the next step for both cases (happening now → seek care; describing past events → continue one at a time, or add the written history as a document). The screen itself — its rules, terms, matching, and the zero-API-call invariant — is unchanged.
- Six per-item query patterns that were invisible on local disk but costly over the deployed EFS/NFS mount: the labs index issued one query per analyte (452 queries, ~11 s in production), the weekly review scanned trends per analyte (~450/run), ingestion computed the same trend deviation twice per candidate row, and the twins sweep, confirm queue, and ledger page each re-queried per item. All now bulk-fetch, with regression tests asserting the paths stay O(1) in queries.

### Added
- The initial-visit opener now drives the conversation: a short greeting, one focused question, a note that documents can stand in for retyping, and emergency guidance up front. Very long messages get acknowledged and worked through one thing at a time rather than processed as a wall.
- Real empty states across the UI: Home is a dashboard in both the pre- and post-onboarding cases (including a "what's already on file" summary of ingested documents, labs, and encounters), the ledger explains itself when no leads exist instead of claiming to be a complete record, and Weekly Reviews describes the blind re-differential panel and when it runs.
- Phase 2 begins: a deterministic citation checker resolves every evidence source ref before a ledger diff can apply — lab refs must match a real row (with any quoted value matching exactly), document refs a real file and page, PMIDs verified against NCBI with caching. Failures get one objection-guided retry, then reject the diff. Network failure never rejects.

## [0.6.0] - 2026-08-24

### Added
- Conversational agentic onboarding: the chat conducts a clinician-style "initial visit" — no wizard, no visible sections. The agent probes vague answers once, always establishes timing (accepting "asked but unknown"), classifies doctor-diagnosed vs. patient-assumed conditions (capturing the patient's reasoning), and cross-references what it hears against already-ingested documents. Deterministic per-fact gates make skipping these structurally impossible (ADR 0011, ADR 0012).
- Intake record page: every patient-reported fact with attribution, precision, and corroboration badges, full revision history, and correct-anytime flow (during and after onboarding).
- Fact corroboration against the ingested record (deterministic, no LLM): event date-window matching scaled by precision, period corroboration for diagnoses, lab-series matching for symptoms; contradictions surface conversationally once (ADR 0013). New `adoc intake-corroborate` sweep command.
- Longitudinal visit capture: after onboarding, each successful chat turn silently files genuinely new patient-reported facts (`reported_on`-dated); the intake record shows a "since last visit" strip.
- Lab panel taxonomy hardening: distinct canonical specs for race-stratified eGFR variants, plasma vs. RBC manganese, blood vs. serum copper/selenium, left/right hip BMD, urine vs. serum creatinine.

### Fixed
- Canonicalization no longer over-merges clinically distinct analytes: stored names are only ever renamed on an exact human-reviewed alias match — suffix/score-heuristic matches keep their distinct stored names (site-prefixed DEXA scores, specimen-suffixed analytes) while retaining read-time panel/validation/trend benefits. Applies to the recanonicalize sweep, ingestion, and the Use-Reading-A/B resolution flow.
- `adoc labs-recanonicalize` is crash-proof: plan-then-execute grouping makes UNIQUE collisions structurally impossible, rejected-row tombstones are respected as key occupants, legacy permissive-derived stored names are restored, and dry-run/live parity is exact by construction.
- The Composer gets one gate-guided rewrite when its draft trips the treatment gate (e.g. restating a supplement dose from the case file) instead of the turn dying; the deterministic gate remains the final, unbypassable authority as a DAG postcondition.

## [0.5.1] - 2026-08-24

### Fixed
- Onboarding no longer crashes on undated medical events — recorded in case/undated-events.md with vague timing preserved; extraction prompt instructs null over placeholder strings; confirm route hardened.
- Labs detail 404 for slash-bearing analyte names (uvicorn decodes %2F pre-routing) — analyte routes use URL-safe ids with legacy redirects.

### Added
- Documents nav group (Add / Review / Consumed / Failed) with the new Consumed page (filename, consumed date, type, accepted/awaiting counts, archived-original links).
- Labs detail page: shaded reference band, per-specimen readings table with source links, calculated-score labeling, rich hover.


## [0.5.0] - 2026-08-24

### Security & Safety (code-review remediation)
- Treatment gate rewritten: clause-window verb-to-drug matching blocks natural phrasings ("stop taking your prednisone"); doctor-referral clauses allowlisted; red-team fixture expanded.
- Sessions bound to user identity + password fingerprint (removal/rotation revokes immediately); upload size cap; X-Forwarded-Proto trust gated.
- Chat surfaces safety-check withholding instead of a 500; challenge notes need substance and recency; review postconditions reject trivial/copy-stamped notes; blind-panel blindness is a content-aware DAG contract.

### Fixed
- Re-extraction after rejection revives to the queue instead of silently dropping; specimen inference skips serum-panel analytes and mixed panels; twin-sweep rule path exact-match only; zero-overlap rescue pairs need eyes; SDK transports own retries/timeouts; vision cost auditing real; rclone source path corrected (dropbox:a-doc/a-doc-inbox); docs/ADR sync (ADRs 0009/0010).


## [0.4.4] - 2026-08-24

### Fixed
- ECS health-check grace period raised 120s → 900s so a first-boot
  `adoc bootstrap-data` restore (git clone + `sources/`/JSONL sync +
  `labs.sqlite` rebuild) has time to finish before the ALB starts
  health-checking the task.

## [0.4.3] - 2026-08-24

### Fixed
- `adoc restore`: boto3 `list_objects_v2` pagination (previously only the
  first page of `sources/` objects was considered), atomic staging (the
  full restore is assembled in a sibling `.restore-staging` directory and
  only moved into place once every step succeeds), and a fail-fast
  `docker-entrypoint.sh` (a real restore error now fails the container
  instead of silently falling through to `adoc init`).

## [0.4.2] - 2026-08-24

### Added
- Document-drop intake section auto-completes when `sources/` is already
  non-empty (a seeded/curated deployment) instead of prompting the patient
  to upload documents that are already on file.

## [0.4.1] - 2026-08-24

### Changed
- Onboarding basics-section intro copy: dropped the exposures example
  (user request).

## [0.4.0] - 2026-08-24

### Added
- Restore/seed path: `adoc restore` + `adoc bootstrap-data` (container entrypoint restores from S3 when EFS is empty); docx/text/zip/genomic intake with 431MB genotype archive + inventory; recursive inbox scanning; inbox hygiene (failed-folder + UI); confirm-queue triage redesign (buckets, bulk confirm, Use-reading-A/B, lightbox, source refs); specimen dimension; twin sweep (two-phase); semantic range/unit/flag comparators + `labs-reclassify`; score-kind analytes; implausible-date gate.

### Fixed
- Review-session findings: LabCorp unit spellings and footnote letters, range labels/pointers/single-source/multi-tier sets, Claude tool-input nesting, gpt-5.x vision protocol parity, extraction truncation detection, Use-reading convergence crash, sqlite sidecar tracking.


## [0.3.0] - 2026-08-23

### Changed
- Patient access: public ALB at https://adoc.petabloc.io with username/password auth (scrypt user store, lockouts) — Tailscale removed (ADR 0007).
- Deployment: ECS Fargate + EFS with CI-built container images — EC2/install.sh/systemd removed (ADR 0006); SQLite journal TRUNCATE on EFS; new `adoc backup` and `adoc user` commands.
- Repository made public: history rewritten to remove personal problem-statement wording; GitHub deploy token eliminated; branch protection enabled.


## [0.2.0] - 2026-08-23

### Added
- Phase 1 MVP: differential ledger with five code-enforced anti-anchoring invariants; labs SQLite store with FTS5, confirm queue, and lossless JSONL rebuild; PHI scrubber; provider-agnostic LLM client (Anthropic/OpenAI/Featherless) with audit logging; typed DAG runner with contract enforcement; deterministic red-flag screen and treatment gate; versioned prompts; diagnostic-turn DAG with mandatory cross-family Challenger; cross-model double-pass document ingestion with 9 AUTO gates; 10-section resumable onboarding wizard; patient-facing web UI (dashboard, chat, upload, confirm queue, Plotly trends); weekly review DAG with 3-family blind panel and divergence adjudication; offline eval harness (extraction + red-team suites); unattended instance bootstrap (SSM secrets, backups, runbook).

### Fixed
- Live-verified model bindings and provider protocol quirks across all three families.


## [0.1.0] - 2026-08-23

### Added

- Phase 0 project scaffold: `pyproject.toml` (uv-managed), ruff/mypy/pytest
  configuration, pre-commit hooks (ruff, gitleaks), `models.yaml` role
  bindings, `src/adoc` package skeleton (`config.py`, `cli.py`, empty
  subpackages), initial test suite, GitHub Actions workflows (`ci`,
  `deploy`, `eval`), CloudFormation stack skeletons in `deploy/cfn/`
  (`network`, `backup`, `instance`, `ci`), systemd units/timers, an install
  script, and the first five architecture decision records.

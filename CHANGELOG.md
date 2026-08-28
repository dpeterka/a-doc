# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

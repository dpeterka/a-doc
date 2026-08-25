# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

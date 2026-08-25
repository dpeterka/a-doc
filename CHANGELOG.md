# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

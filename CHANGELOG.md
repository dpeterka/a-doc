# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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


### Added

- Phase 0 project scaffold: `pyproject.toml` (uv-managed), ruff/mypy/pytest
  configuration, pre-commit hooks (ruff, gitleaks), `models.yaml` role
  bindings, `src/adoc` package skeleton (`config.py`, `cli.py`, empty
  subpackages), initial test suite, GitHub Actions workflows (`ci`,
  `deploy`, `eval`), CloudFormation stack skeletons in `deploy/cfn/`
  (`network`, `backup`, `instance`, `ci`), systemd units/timers, an install
  script, and the first five architecture decision records.

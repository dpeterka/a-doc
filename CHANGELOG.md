# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 0 project scaffold: `pyproject.toml` (uv-managed), ruff/mypy/pytest
  configuration, pre-commit hooks (ruff, gitleaks), `models.yaml` role
  bindings, `src/adoc` package skeleton (`config.py`, `cli.py`, empty
  subpackages), initial test suite, GitHub Actions workflows (`ci`,
  `deploy`, `eval`), CloudFormation stack skeletons in `deploy/cfn/`
  (`network`, `backup`, `instance`, `ci`), systemd units/timers, an install
  script, and the first five architecture decision records.

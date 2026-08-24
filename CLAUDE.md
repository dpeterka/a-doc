# CLAUDE.md — agent instructions for a-doc

a-doc is a single-patient, longitudinal medical-diagnostic assistant (Python). Read `PLAN.md` before any non-trivial work — it is the source of truth for architecture, phasing, and rationale. Keep `PLAN.md` current as phases complete.

## Commands

- Install/sync deps: `uv sync --all-extras`
- Test: `uv run pytest` (coverage gate enforced in CI)
- Lint/format: `uv run ruff check --fix . && uv run ruff format .`
- Types: `uv run mypy src`
- App CLI: `uv run adoc <init|onboard|ingest|review|serve|backfill|eval|user|backup|restore|bootstrap-data|labs-infer-specimen|labs-dedupe-twins|labs-reclassify|labs-recanonicalize>` (15 subcommands; `user` has `add`/`list`/`remove`)

## Hard rules

1. **PHI boundary**: patient data lives ONLY in the separate data repo (`ADOC_DATA_DIR`, no git remote). Never read real patient data into context, never commit it here, never add it to fixtures. Tests and CI use synthetic fixtures under `tests/fixtures/` only.
2. **Safety behavior is pinned by tests**: the red-team transcript, ledger invariants, and DAG node-contract tests are required CI checks. Never weaken, skip, or delete these tests to make a change pass. Prompt template edits (`src/adoc/reason/prompts/`) are code — they require the safety suite to pass.
3. **Stage order is enforced by code, not prompts**: diagnostic outputs must flow through the DAG (Ledger-Maintainer → Challenger → apply → Composer). Never add a code path that lets model output reach the ledger or the patient UI without its contract checks.
4. **Model bindings live in `models.yaml`**, never hardcoded. Changing a binding requires an `adoc eval` comparison report and a PR.
5. **No treatment/dosing advice paths.** The output gate in `reason/safety.py` is deterministic; anything patient-facing goes through it.
6. **Architecture changes need an ADR** in `docs/adr/` (short, numbered, status header).

## Git workflow (GitFlow)

- `main` = released/deployed; every merge is a semver tag. `develop` = integration.
- Work on `feature/<slug>` branched from `develop`; PR back to `develop` with CI green. `release/*` stabilizes; `hotfix/*` branches from `main`.
- Conventional commits (`feat:`, `fix:`, `docs:`, `ci:`, `refactor:`, `test:`, `chore:`).
- Never push directly to `main`. Update `CHANGELOG.md` in release branches.

## Infrastructure

- All AWS resources are CloudFormation in `deploy/cfn/` (network, backup, alb, ecs, ci). No console-created resources; changes go through PRs and change sets. Deploys run from GitHub Actions via the OIDC role, which also builds/pushes the application image to ECR.
- The app runs as ECS Fargate tasks (`deploy/cfn/ecs.yaml`) on a shared EFS filesystem — not EC2 (see ADR 0006; this superseded the original EC2 + `install.sh` + systemd design). No direct public ingress: the service security group admits only the ALB's security group, on the app port. A shell is reachable via `aws ecs execute-command` only (`EnableExecuteCommand: true`). The app is reached through a public ALB (`deploy/cfn/alb.yaml`) at `https://adoc.petabloc.io`, with username/password auth + in-app rate limiting in `src/adoc/web` (explicit user decision, superseding the earlier Tailscale-only design — see PLAN.md).
- `labs.sqlite`'s journal mode is `TRUNCATE` in the deployed environment (`ADOC_SQLITE_JOURNAL_MODE`, `config.Settings.sqlite_journal_mode`) because WAL is unsafe on EFS/NFS — see `labs/db.py`'s `LabsDb.__init__` docstring. The web service only ever runs one task at a time (`DeploymentConfiguration` max 100%/min 0%) — SQLite + git still want a single writer.

## Code conventions

- Python ≥3.12, `src/` layout, Pydantic v2 models for every cross-boundary payload (extraction schemas, ledger diffs, DAG artifacts).
- Deterministic logic (validation, ledger invariants, citation checking, safety gates) is plain code with unit tests — never delegated to a model.
- Every persisted LLM-derived artifact carries provenance: `{app_version, prompt_template_version, model_id, dag_node, timestamp}`.

### Prompt versioning

Two versioning conventions coexist, both required to keep provenance stamps accurate — bump the version on every semantic edit (wording that changes model behavior), not on typo-only fixes:

- **DAG-stage system prompts** (`src/adoc/reason/prompts/*.md`): each file starts with a mandatory `<!-- version: N -->` header, parsed and hashed by `reason/prompts.py`'s `load_prompt`. `reason/stages.py` reads the parsed version to stamp `Provenance.prompt_template_version` on every artifact that stage produces.
- **Inline prompts elsewhere** (e.g. `labs/twins.py`'s `TWIN_CLASSIFY_PROMPT_VERSION`, `ingest/pipeline.py`'s `CLASSIFY_PROMPT_VERSION`/`DOCX_CLASSIFY_PROMPT_VERSION`): a module-level `*_PROMPT_VERSION` string constant embedded directly into the prompt text it labels, read the same way at the call site to stamp provenance.

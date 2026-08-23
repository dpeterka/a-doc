# a-doc

**a-doc is a decision-support tool, not a medical device.** It is being built
for one specific patient to hold the whole picture across specialists, labs,
and time, and to produce evidence-linked *leads* — things to bring up with a
doctor — never diagnoses or treatment instructions. Every patient-facing
output is framed that way, and a deterministic red-flag screen and output
gate enforce it in code, not just in prompts.

## Architecture, in brief

a-doc runs a single frontier reasoner through **functional stages, not
specialty personas** — a Ledger-Maintainer, a mandatory cross-family
Challenger, a Test-Chooser, and a Composer — as an explicit, code-defined DAG
(`src/adoc/reason/dag.py`) where every node has pre/postcondition contracts
enforced by code (e.g. the Challenger must produce a substantive
counter-argument or the run fails). This targets anchoring, the single
biggest measured failure mode for this use case, structurally rather than by
hoping a prompt holds.

State is split across **two git repositories**: this code repo, and a
separate PHI-only data repo (`ADOC_DATA_DIR`) with no remote, holding
markdown case files, a `differential-ledger.yaml`, immutable source
documents, and a SQLite labs database that is rebuildable from a committed
JSONL export. Every mutation is a commit, so the case file's history is
auditable and revertible without a database migration story.

The UI is **FastAPI + Jinja2 + HTMX + SSE + Plotly.js**, not a chat
framework — most of the surface area (confirm queue, ledger dashboard, trend
charts) is page CRUD, and only part of it is chat.

See `PLAN.md` for the full research-backed rationale, phasing, and schemas,
and `CLAUDE.md` for agent/contributor rules.

## Setup

```bash
uv sync --all-extras
cp .env.example .env   # fill in API keys and ADOC_DATA_DIR
uv run adoc init       # validates Settings + models.yaml load cleanly
```

## Dev workflow

- GitFlow: branch `feature/<slug>` from `develop`, PR back to `develop` with
  CI green; `release/*` stabilizes; `hotfix/*` branches from `main`. Never
  push directly to `main`.
- Install pre-commit hooks: `uv run pre-commit install` (ruff + gitleaks run
  on every commit).
- CI gates on every PR: `ruff check`, `ruff format --check`, `mypy src`,
  `pytest` with a coverage gate. The red-team transcript, ledger-invariant,
  and DAG-contract tests are required checks — see `CLAUDE.md`.

## Deploy overview

All AWS resources are CloudFormation stacks in `deploy/cfn/`, deployed in
this order: `ci` (once, manually — see note below) → `network` → `backup` →
`instance`. Deploys after the initial bootstrap run from GitHub Actions via
an OIDC-assumed role (`deploy/cfn/ci.yaml`) — no long-lived AWS credentials
are stored in the repo.

`ci.yaml` creates the very IAM role that GitHub Actions needs in order to
deploy anything, including `ci.yaml` itself — that first deployment is a
manual, one-time bootstrap (e.g. `aws cloudformation deploy` from a local
admin session), after which its `DeployRoleArn` output is copied into the
`AWS_DEPLOY_ROLE_ARN` repository variable so future deploys are automated.

The EC2 instance has no SSH keys and no public ingress; it is reachable via
AWS SSM and Tailscale only, and the app binds to the tailnet, never the
public internet.

## Phase status

| Phase | Description | Status |
|---|---|---|
| 0 | Project scaffold | complete |
| 1 | MVP (onboarding, ingestion, DAG reasoning, web UI, AWS deploy) | not started |
| 2 | Grounding & anti-hallucination hardening | not started |
| 3 | Knowledge layer (HPO/LIRICAL/Monarch, ACR/EULAR criteria) + full eval | not started |
| 4 | Extras (Apple Health import, specialist finder, notifications) | not started |

# Deployment dependencies

Everything the running application needs that is **not** in the Python
package, and how to check each one is actually there.

This file exists because of a specific failure. LIRICAL was containerised,
pushed to ECR, given a task definition and IAM — and never ran once, for the
entire life of ADR 0029, because nobody wired `ADOC_LIRICAL_CLUSTER` into a
task definition. Every layer looked done in isolation. Nothing checked that
they were connected.

**A capability is not deployed until something outside its own code has
proven it runs in production.** Adding a dependency means adding a row here
and a way to verify it.

## The check

`scripts/check_deploy_deps.py` verifies the live deployment against this
table. Run it after any deploy that adds a dependency:

```
uv run python scripts/check_deploy_deps.py            # against AWS
uv run python scripts/check_deploy_deps.py --in-task  # inside a task, checks files too
```

## 1. Environment, per task definition (`deploy/cfn/ecs.yaml`)

Both `a-doc-web` and `a-doc-jobs` get the same set. A missing one is silent:
`Settings` has a default for every field except `data_dir`.

| Variable | Value | Missing ⇒ |
|---|---|---|
| `ADOC_DATA_DIR` | `/data/a-doc-data` | `Settings()` **raises**; the only loud one |
| `ADOC_SQLITE_JOURNAL_MODE` | `TRUNCATE` | WAL on EFS — unsafe, see `labs/db.py` |
| `ADOC_BACKUP_BUCKET` | backup stack export | backups silently no-op |
| `ADOC_TRUST_FORWARDED_FOR` | `true` | client IPs wrong in rate limiting |
| `ADOC_LIRICAL_CLUSTER` | `a-doc` | **engine never runs** ("not configured", 0.0s) |
| `ADOC_LIRICAL_TASK_DEFINITION` | `a-doc-lirical:N` | as above |
| `ADOC_LIRICAL_SUBNETS` | both public subnets | `run_task` places nothing |
| `ADOC_LIRICAL_SECURITY_GROUPS` | service SG | `run_task` places nothing |

The four LIRICAL rows are gated on the `HasLiricalImage` condition, so the
app is never pointed at a task definition that does not exist.

## 2. Secrets — SSM SecureString, injected as `Secrets:`

`/a-doc/anthropic-api-key`, `/a-doc/openai-api-key`,
`/a-doc/featherless-api-key`, `/a-doc/rclone-conf`.

A missing key fails at the first call to that provider, not at startup.
`rclone-conf` absent means ingest degrades quietly — the entrypoint tolerates
it by design.

## 3. Reference data baked into the application image (`Dockerfile`)

Built at image-build time from public ontologies, then the sources are
deleted in the same layer (Docker layers are additive — deleting in a later
`RUN` saves nothing).

| Path | ~Size | Absent ⇒ |
|---|---|---|
| `/opt/hpo-index.json` | 2 MB | phenotype matching off |
| `/opt/semsim-index.json` | 8 MB | similarity engine skips |
| `/opt/mondo-index.json` | 7 MB | vocabulary mismatch read as disagreement |
| `/opt/orphadata-index.json` | 4 MB | no definitions/prevalence |
| `/opt/statpearls.sqlite` | 40 MB | no clinical review text |

All five degrade **silently and by design** — a missing index is the ordinary
state of a local checkout. That is why they need an explicit check in
production rather than an absence of errors.

## 4. The LIRICAL sidecar (ADR 0029)

Separate image, separate ECR repository, separate task definition.

- **`ENV LIRICAL_DATA` in `deploy/lirical/Dockerfile` must equal
  `LIRICAL_DATA_DIR` in `knowledge/lirical_runner.py`.** Two artifacts with no
  compiler between them; they disagreed (`/opt/liricaldata` vs
  `/lirical-data`) and every launched task exited 1. Pinned by
  `tests/test_lirical_runner.py`.
- The image's build-time smoke test uses the image's own `$LIRICAL_DATA`, so
  **a green sidecar build says nothing about whether the app can call it.**
- IAM: `ecs:RunTask` on the family, `iam:PassRole` for the two roles it names
  (conditioned on `ecs-tasks`), `DescribeTasks`/`StopTask`. In
  `deploy/cfn/ecs.yaml` on `TaskRole`, with the ARN built by `!Sub` — a
  `!GetAtt` here is a circular dependency that `validate-template` does not
  catch. Only a change set does.

## 5. CloudFormation stacks, in order

`ci` → `network` → `backup` → `alb` → `ecs`. All deployed by
`.github/workflows/deploy.yml` on push to `main`.

The sidecar build step is `continue-on-error`: it downloads LIRICAL's data
from external hosts and fails for reasons unrelated to this repository. It
did, and skipped the ECS deploy, so a release shipped nothing.

## The recurring failure mode

Every entry above fails **silently** except `ADOC_DATA_DIR`. That is
deliberate — a review must not die because a reference index is missing — but
it means *absence looks exactly like working*. Two rules follow:

1. **Every degraded path must say so where a human will see it**, not only in
   a log. The LIRICAL failure was invisible for months because the engine
   nodes only wrote to the report's `results` sink on success.
2. **Verify a new dependency in production once, by measurement**, before
   calling it done. Not "the deploy went green" — a probe that shows the thing
   working. See `verify-by-measurement-not-silence` in the project memory.

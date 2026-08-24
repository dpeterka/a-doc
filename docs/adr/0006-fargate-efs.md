# 0006. ECS Fargate + EFS over EC2 + install.sh

Status: Accepted

## Context

ADR 0004 chose a single always-on EC2 instance because SQLite + git-as-
database want one persistent writer, not a fleet. That design's boot-time
provisioning (`deploy/cfn/instance.yaml`'s `UserData` → `deploy/install.sh`
→ systemd units/timers) reassembles the entire runtime from scratch on
every instance replacement: install packages, install `uv`, clone the
repo, write `/etc/adoc/env` from SSM, write the rclone config, install
systemd units, and either restore from S3 or run `adoc init`. That
sequence is long, order-sensitive, and only ever exercised for real at
replacement time (rare, by design) or in the documented restore drill —
exactly the shape of bug class that hides until the day it matters
(a missing package, a systemd unit drifted from what's on disk, a step
that silently no-ops). It is also slow to deploy: a code change requires
either a full instance replacement or an in-place
`aws ssm send-command ... install.sh` re-run, neither of which is a fast,
routine "ship this build" action.

The operator (this project's user) made an explicit decision to move to a
container-image-based deployment instead: build the exact runtime once as
a Docker image, push it to ECR, and run it as ECS Fargate tasks. This
trades the boot-time-assembly bug class for an image-immutability
guarantee — what was tested locally (`docker build .`) is bit-for-bit what
runs in production — at the cost of needing a shared, network-attached
filesystem for the SQLite/git data directory, since Fargate tasks have no
persistent local disk of their own across replacements.

## Decision

Replace the EC2 instance stack with:

- **`Dockerfile`** (repo root): the application image — `python:3.12-slim`
  + `poppler-utils`/`git`/`rclone`/`curl`, dependencies installed via
  `uv sync --frozen --no-dev` (the official `uv` binary, not the installer
  script), a non-root `adoc` user fixed at uid/gid 1000 (matching the EFS
  AccessPoint's `PosixUser` so no mount-time chown is needed), and
  `docker-entrypoint.sh` handling the two things that still vary
  per-container-start (writing the rclone config from an env var, and
  `adoc init` on a first-ever-empty data directory).
- **`deploy/cfn/ecs.yaml`**: an EFS filesystem (encrypted, IA lifecycle,
  one access point rooted at `/data`) replacing the EC2 instance's
  attached EBS data volume; a web `AWS::ECS::Service` (desired count 1)
  behind the ALB's now-`TargetType: ip` target group (`deploy/cfn/alb.yaml`);
  and `AWS::Events::Rule` schedules replacing
  `deploy/systemd/adoc-{ingest,review,backup}.timer`, each `RunTask`-ing a
  shared "jobs" task definition with a `containerOverrides` command
  (`run-ingest.sh` / `adoc review` / `adoc backup`).
- **`deploy/cfn/ci.yaml`**: gains the `a-doc` ECR repository (scan-on-push,
  keep-last-10 lifecycle) and the deploy role's ECR/ECS/EFS/Events/scoped-
  logs permissions needed to manage all of the above.
- **`deploy/cfn/alb.yaml`**: target group becomes `TargetType: ip`
  (registered/deregistered dynamically by the ECS service); the
  `InstanceStackName` parameter and static `Targets` list are gone.
- **Deleted**: `deploy/cfn/instance.yaml`, `deploy/install.sh`,
  `deploy/backup.sh`, `deploy/systemd/*`. (The already-deployed
  `a-doc-instance` stack itself is not touched by this change — see
  README.md "Cutover from the EC2 instance.")
- **`adoc backup`** (new CLI command, `src/adoc/backup.py`): the same
  git-bundle + `sources/` + `labs-export.jsonl` → S3 logic
  `deploy/backup.sh` used to run as a shell script, now a tested Python
  path invoked by the scheduled backup task instead of a script baked into
  the instance image.

### SQLite journal mode on EFS

WAL relies on a shared-memory index file (`-wal`/`-shm`) coordinated
between writers via `mmap` and POSIX advisory (`fcntl`) locks. NFS-family
filesystems — EFS included — have historically unreliable/non-atomic
advisory-lock and mmap write-back semantics across clients; SQLite's own
documentation warns WAL must not be used on a network filesystem, where it
can silently corrupt the database. `labs.db.LabsDb.__init__` therefore
takes `journal_mode` as a parameter (default `"WAL"`, fine for local/dev/
test on a normal filesystem); `config.Settings.sqlite_journal_mode`
defaults to `"WAL"` but `deploy/cfn/ecs.yaml`'s task definitions set
`ADOC_SQLITE_JOURNAL_MODE=TRUNCATE`, so the deployed app never runs WAL
against EFS.

### Single-writer discipline

TRUNCATE mode keeps ordinary rollback-journal semantics but still assumes
exactly one writer at a time — the same requirement ADR 0004 already had
for git. The web service's `DeploymentConfiguration` (`MaximumPercent:
100`, `MinimumHealthyPercent: 0`) enforces this at the ECS level: the old
task is always stopped before its replacement starts, so two tasks never
run concurrently against the same EFS-mounted data directory. This costs a
brief availability gap on every deploy in exchange for never risking two
writers — an accepted tradeoff for a single-patient app. The scheduled
ingest/review/backup jobs are not mutually excluded from each other or
from the web task by any distributed lock; this is a known, documented gap
rather than a solved problem, judged low-risk at this system's cadence and
scale.

## Consequences

- Deploys become "build an image, push it, update the service" instead of
  "replace an instance and re-run a multi-step boot script" — faster, and
  the exact artifact that ran in CI/local testing is the exact artifact
  that runs in production.
- EFS introduces a new operational dependency (mount targets, an access
  point, NFS-specific failure modes) in exchange for removing the boot-time
  assembly dependency; the SQLite journal-mode caveat above is the direct
  cost of that trade and is now enforced by a typed `Settings` field and
  tested in `tests/test_labs_db.py`, not just documented.
- User provisioning and one-off shells move from SSM Session Manager (EC2)
  to ECS Exec (`aws ecs execute-command`), which needs `EnableExecuteCommand:
  true` on the service and matching `ssmmessages:*` permissions on the task
  role — functionally equivalent, different plumbing.
- The restore-from-backup drill (ADR 0004, PLAN.md Phase-1 acceptance) is
  reframed: EFS is not destroyed by a routine task replacement the way the
  EC2 instance's attached EBS volume was, so the drill now specifically
  exercises restoring onto a freshly created filesystem rather than every
  routine instance rebuild.
- The already-deployed EC2 stack is left in place until the operator
  confirms the ECS service is healthy and cuts over by hand (README.md) —
  infrastructure-as-code changes still never delete a running stack from
  application code.

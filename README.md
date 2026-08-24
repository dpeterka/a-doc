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

## Deploy runbook

**Image-based deploys.** All AWS resources are CloudFormation stacks in
`deploy/cfn/`; the application ships as a container image (root
`Dockerfile`) built and pushed to ECR by `.github/workflows/deploy.yml`, and
run on **ECS Fargate** tasks with data on **EFS** — see ADR 0006 for why
this replaced the original EC2 + `install.sh` + systemd-timers design.
Stack deploy order: `ci` (once, manually — see note below) → build/push the
image → `network` → `backup` → `alb` → `ecs`. Deploys after the initial
bootstrap run from GitHub Actions via an OIDC-assumed role
(`deploy/cfn/ci.yaml`) — no long-lived AWS credentials are stored in the
repo.

`ci.yaml` creates the very IAM role that GitHub Actions needs in order to
deploy anything, including `ci.yaml` itself (and the `a-doc` ECR repository
the image is pushed to) — that first deployment is a manual, one-time
bootstrap (e.g. `aws cloudformation deploy` from a local admin session),
after which its `DeployRoleArn` output is copied into the
`AWS_DEPLOY_ROLE_ARN` repository variable so future deploys are automated.

**Patient access is via a public ALB** at `https://adoc.petabloc.io`
(`deploy/cfn/alb.yaml`) — an explicit user decision that replaced the
original Tailscale-only design, unchanged by the Fargate migration. The
app itself still has no direct public ingress: `deploy/cfn/ecs.yaml`'s
`ServiceSecurityGroup` admits inbound port 8080 from the ALB's security
group only. In-app authentication (username/password, scrypt-hashed, with
in-app rate limiting) is the only auth layer in front of the app — see
"User provisioning" and "How the patient reaches the UI" below. There is
deliberately no WAF and no TOTP in this design.

### One-time SSM parameters

The ECS task definitions (`deploy/cfn/ecs.yaml`) inject a fixed set of
`SecureString` parameters under the `/a-doc/` path as container `secrets`,
resolved by the task execution role at task-launch time. They are created
once, by hand, from a local admin AWS session — they are secrets, so they
are deliberately *not* CloudFormation resources (a template's parameters
would land in the stack's event history and drift-detection output in
plaintext). All of them use the AWS-managed default key (`alias/aws/ssm`) —
omit `--key-id` when creating them, since `a-doc-task-execution-role`'s
`kms:Decrypt` grant in `deploy/cfn/ecs.yaml` is scoped to that default key
only. If a customer-managed KMS key is ever used for one of these instead,
that key's own key policy must separately grant `kms:Decrypt` to
`a-doc-task-execution-role` (the default key's policy already permits any
IAM-permitted principal in the account, which the execution role's policy
statement grants; a custom key's policy does not, by default).

```bash
# rclone config defining the "dropbox" remote the ingest task's
# run-ingest.sh uses (run `rclone config` locally against the Dropbox
# app-folder backend, complete the OAuth flow in a browser, then paste the
# resulting file). Injected into every task as the RCLONE_CONF env var;
# docker-entrypoint.sh writes it to ~/.config/rclone/rclone.conf.
aws ssm put-parameter --name /a-doc/rclone-conf \
  --type SecureString --value "$(cat ~/.config/rclone/rclone.conf)"

# LLM provider API keys
aws ssm put-parameter --name /a-doc/anthropic-api-key \
  --type SecureString --value "sk-ant-xxxxxxxxxxxxxxxx"
aws ssm put-parameter --name /a-doc/openai-api-key \
  --type SecureString --value "sk-xxxxxxxxxxxxxxxx"
# optional — only needed if the Featherless blind-panel role is enabled
aws ssm put-parameter --name /a-doc/featherless-api-key \
  --type SecureString --value "xxxxxxxxxxxxxxxx"
```

There is no SSM parameter for web login credentials — those are created
via an ECS Exec one-off (see "User provisioning" below), not threaded
through SSM/task secrets. There is no `github-token` parameter (deploys
never needed one) and the old `/a-doc/session-passphrase` parameter is
gone from the required set entirely — it was legacy even under the EC2
design and nothing in the ECS task definitions reads it.

To rotate any of these later, add `--overwrite` and re-run the same
command, then force a new deployment of the web service (`aws ecs
update-service --cluster a-doc --service a-doc-web --force-new-deployment`)
and re-run (or wait for) the scheduled job tasks — values are only read at
task-launch time, not live-reloaded by a running task.

**These parameters must exist *before* the ecs stack is deployed for the
first time.** A task that fails to resolve a secret fails to start
entirely (no partial provisioning state to recover, unlike the old
UserData/install.sh boot sequence) — fix the parameter and either
`update-service --force-new-deployment` (web) or wait for/manually invoke
the next scheduled rule (jobs).

### User provisioning

Web login is username/password, one entry per person who needs access
(scrypt-hashed, stored at `<data_dir>/work/users.yaml` on the EFS-mounted
data directory — gitignored, never in the data repo's git history). There
is no SSM parameter and no CloudFormation resource for this; it is managed
via an ECS Exec one-off shell into the running web task (`EnableExecuteCommand:
true` on the service in `deploy/cfn/ecs.yaml`):

```bash
TASK_ID=$(aws ecs list-tasks --cluster a-doc --service-name a-doc-web \
  --query "taskArns[0]" --output text)
aws ecs execute-command --cluster a-doc --task "$TASK_ID" \
  --container adoc --interactive --command "adoc user add <username>"
aws ecs execute-command --cluster a-doc --task "$TASK_ID" \
  --container adoc --interactive --command "adoc user list"
aws ecs execute-command --cluster a-doc --task "$TASK_ID" \
  --container adoc --interactive --command "adoc user remove <username>"
```

`adoc user add` on an existing username replaces that user's password
(useful for rotation). Login also has in-app rate limiting: 5 consecutive
failures for a username, or 20 for a client IP, within a 15-minute sliding
window locks further attempts (HTTP 429) until the window clears; counters
are in-memory only, so a service restart resets them (an accepted
tradeoff — see `src/adoc/web/security.py`).

### Stack deploy order

`ci` (once, manually) → build/push the image → `network` → `backup` →
`alb` → `ecs`, matching `.github/workflows/deploy.yml`. `ecs.yaml` must
come after `network`/`backup`/`alb` because it imports all three (VPC and
subnet/security-group ids; the backup bucket name and KMS key; the ALB's
target group ARN) via `Fn::ImportValue`. There is no `instance` stack any
more.

### Single-writer discipline (SQLite + git on EFS)

SQLite + git-as-database want exactly one writer, same as the EC2 design.
Two mechanisms enforce that on Fargate:

- **Deployment configuration**: the web service's
  `DeploymentConfiguration` is `MaximumPercent: 100` /
  `MinimumHealthyPercent: 0` — CloudFormation/ECS always stops the old
  task before starting its replacement, so two web tasks never run
  concurrently against the same EFS-mounted data directory (at the cost of
  a brief availability gap on each deploy — an accepted tradeoff for a
  single-patient app, in exchange for never risking two writers).
- **SQLite journal mode**: `labs.sqlite`'s journal mode is `TRUNCATE` in
  the deployed environment (`ADOC_SQLITE_JOURNAL_MODE=TRUNCATE` in
  `deploy/cfn/ecs.yaml`'s task definitions), not the local/dev default
  `WAL`. WAL relies on a shared-memory index file coordinated via `mmap` +
  POSIX advisory locks, which is unsafe on NFS-family filesystems like
  EFS and can silently corrupt the database; TRUNCATE is a plain
  rollback-journal mode with no such requirement. See
  `src/adoc/labs/db.py`'s `LabsDb.__init__` docstring and ADR 0006.

The scheduled ingest/review/backup jobs are **not** mutually excluded from
each other or from the web task by any lock — this is an accepted gap
(cadence and single-patient scale make a collision low-probability, not
impossible), not a solved problem.

### Restore-from-backup drill (release gate)

PLAN.md's Phase-1 acceptance criteria and `CLAUDE.md`/ADR 0004 call a
tested restore a release gate — do this before considering a deploy
"done," not just once at setup. Unlike the EC2 design, EFS is not
destroyed by a routine task/service replacement, so this drill now
specifically exercises restoring onto a **freshly created** filesystem
(e.g. after deleting and redeploying the `ecs` stack, or standing up a new
environment):

1. Confirm a real backup exists: `aws s3 ls
   s3://<backup-bucket>/latest/a-doc-data.bundle` (bucket name is the
   `BucketName` output of the `a-doc-backup` stack).
2. Get a shell on a running task via ECS Exec (see "User provisioning")
   and, from it, restore manually:
   ```bash
   aws s3 cp s3://<backup-bucket>/latest/a-doc-data.bundle /tmp/a-doc-data.bundle
   git clone /tmp/a-doc-data.bundle /data/a-doc-data.restored
   aws s3 sync s3://<backup-bucket>/latest/sources/ /data/a-doc-data.restored/sources/
   aws s3 cp s3://<backup-bucket>/latest/labs-export.jsonl \
     /data/a-doc-data.restored/labs-export.jsonl
   ```
3. Verify the restored repo: `git -C /data/a-doc-data.restored log
   --oneline -5` shows real history, not an empty init commit; `adoc init`
   (with `ADOC_DATA_DIR` pointed at the restored path) reports "already
   initialized" rather than creating a new empty case file.
4. Once satisfied, swap it into place (`mv /data/a-doc-data
   /data/a-doc-data.bak && mv /data/a-doc-data.restored /data/a-doc-data`)
   and force a new deployment of the web service so it picks up the
   restored data.
5. Confirm `https://adoc.petabloc.io/healthz` returns `ok` and the ALB
   target group shows the task healthy (below).

### How the patient reaches the UI

The web task's `adoc serve` binds `0.0.0.0:8080` — safe to bind widely
because `deploy/cfn/ecs.yaml`'s `ServiceSecurityGroup` admits inbound 8080
from the ALB's security group only, so nothing else can reach it.
`deploy/cfn/alb.yaml` puts a public, internet-facing Application Load
Balancer in front of it: an ACM certificate for `adoc.petabloc.io`
(DNS-validated against the `petabloc.io` Route53 hosted zone,
`Z009458513KFY2WNUS7C0` — CloudFormation creates the validation record and
waits for issuance automatically, no manual step), an HTTPS:443 listener
forwarding to a `TargetType: ip` target group (registered/deregistered
dynamically by the ECS service — no static instance target any more), an
HTTP:80 listener that redirects to HTTPS, and a Route53 alias A record
pointing `adoc.petabloc.io` at the ALB. The target group's health check
hits the unauthenticated `/healthz` route.

For the patient: open `https://adoc.petabloc.io/` in any browser and sign
in with a username/password provisioned via `adoc user add` (see "User
provisioning" above). There is no VPN/tailnet step any more — the
in-app login and its rate limiting are the only gate, by explicit user
decision (no WAF, no TOTP in this design).

### Cutover from the EC2 instance

The prior EC2 deployment (`a-doc-instance` stack, `deploy/install.sh`,
`deploy/systemd/*`) is superseded by this ECS/EFS design but is **not**
deleted by any of this work — CloudFormation stacks are never torn down
from application code changes. Once the `a-doc-ecs` service is deployed
and confirmed healthy behind the ALB (target group shows the Fargate task
healthy, `/healthz` returns `ok`, a manual smoke test of chat/ingest
succeeds), the operator deletes the old instance stack by hand:
`aws cloudformation delete-stack --stack-name a-doc-instance`. Do this only
after the data on EFS has been confirmed current (e.g. via a manual
`adoc backup` on the old instance and a restore drill onto EFS, or a
direct one-time copy) — the EC2 instance's `/data` EBS volume and the new
EFS filesystem start out as two independent, unsynchronized copies of the
data repo.

## Phase status

| Phase | Description | Status |
|---|---|---|
| 0 | Project scaffold | complete |
| 1 | MVP (onboarding, ingestion, DAG reasoning, web UI, AWS deploy) | code complete — ECS Fargate + EFS deploy verification pending (see ADR 0006) |
| 2 | Grounding & anti-hallucination hardening | not started |
| 3 | Knowledge layer (HPO/LIRICAL/Monarch, ACR/EULAR criteria) + full eval | not started |
| 4 | Extras (Apple Health import, specialist finder, notifications) | not started |

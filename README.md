# a-doc

**a-doc is a decision-support tool, not a medical device.** It is being built
for one specific patient to hold the whole picture across specialists, labs,
and time, and to produce evidence-linked *leads* — things to bring up with a
doctor — never diagnoses or treatment instructions. Every patient-facing
output is framed that way, and a deterministic output gate enforces it in
code, not just in prompts.

## Architecture, in brief

a-doc runs a single frontier reasoner through **functional stages, not
specialty personas** — a Ledger-Maintainer, a mandatory cross-family
Challenger, a Test-Chooser, and a Composer — as an explicit, code-defined DAG
(`src/adoc/reason/dag.py`) where every node has pre/postcondition contracts
enforced by code (e.g. the Challenger must produce a substantive
counter-argument or the run fails). This targets anchoring, the primary
failure mode for this use case, structurally rather than by hoping a prompt
holds.

State is split across **two git repositories**: this code repo, and a
separate PHI-only data repo (`ADOC_DATA_DIR`) with no remote, holding
markdown case files, a `differential-ledger.yaml`, immutable source
documents, and a SQLite labs database that is rebuildable from a committed
JSONL export. Every mutation is a commit, so the case file's history is
auditable and revertible without a database migration story.

The UI is **FastAPI + Jinja2 + HTMX + SSE + Plotly.js**, not a chat
framework — most of the surface area (confirm queue, ledger dashboard, trend
charts) is page CRUD, and only part of it is chat.

See `PLAN.md` for the full architecture, phasing, and schemas, and
`CLAUDE.md` for agent/contributor rules.

## Supported input types

The Dropbox inbox / manual upload accept document kinds detected by content
(never by filename extension alone — see `ingest/filetypes.py`):

- **PDF** (`%PDF-` magic): archived immutably, page images rendered via
  `pdftoppm`, and read by the vision double-pass extractor (a PDF-native
  pass + a rendered-page-image pass, cross-model).
- **`.docx`** (a real OOXML zip package): archived immutably like a PDF but
  with **no page rendering** — read directly as TEXT with `python-docx` (no
  LibreOffice/PDF conversion step). A lab-classified `.docx` goes through
  the same cross-model double-pass and reconcile/confirm-queue gates as a
  PDF lab report (page numbers default to 1 — a `.docx` has no page
  structure); a narrative `.docx` becomes a full-text
  `patient-report`/`imaging` encounter carrying the complete extracted
  text.
- **`.txt`/`.md`**: read verbatim.
- **`.zip`**: expanded, each member re-classified.
- **Genomic files** (23andMe raw export, `.vcf`/`.bcf`/BAM/FASTQ): archived
  byte-for-byte, never sent to any LLM — see "Genomic data" below.

Anything else is rejected with a clear error asking for a supported type.
Because a `.docx` has no page image, its confirm-queue rows show a text
fallback panel instead of a source-page image.

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
runs on **ECS Fargate** tasks with data on **EFS** (ADR 0006). Stack deploy
order: `ci` (once, manually — see note below) → build/push the image →
`network` → `backup` → `alb` → `ecs`. Deploys after the initial bootstrap
run from GitHub Actions via an OIDC-assumed role (`deploy/cfn/ci.yaml`) — no
long-lived AWS credentials are stored in the repo.

`ci.yaml` creates the IAM role GitHub Actions needs to deploy anything,
including `ci.yaml` itself (and the ECR repository the image is pushed to)
— that first deployment is a manual, one-time bootstrap (e.g.
`aws cloudformation deploy` from a local admin session), after which its
`DeployRoleArn` output is copied into the `AWS_DEPLOY_ROLE_ARN` repository
variable so future deploys are automated.

**Patient access is via a public ALB** at `https://adoc.petabloc.io`
(`deploy/cfn/alb.yaml`, ADR 0007). The app itself has no direct public
ingress: `deploy/cfn/ecs.yaml`'s `ServiceSecurityGroup` admits inbound port
8080 from the ALB's security group only. In-app authentication
(username/password, scrypt-hashed, with in-app rate limiting) is the only
auth layer in front of the app — see "User provisioning" and "How the
patient reaches the UI" below. There is deliberately no WAF and no TOTP in
this design.

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
`a-doc-task-execution-role`.

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

There is no SSM parameter for web login credentials — those are created via
an ECS Exec one-off (see "User provisioning" below), not threaded through
SSM/task secrets.

To rotate any of these later, add `--overwrite` and re-run the same
command, then force a new deployment of the web service (`aws ecs
update-service --cluster a-doc --service a-doc-web --force-new-deployment`)
and re-run (or wait for) the scheduled job tasks — values are only read at
task-launch time, not live-reloaded by a running task.

**These parameters must exist *before* the ecs stack is deployed for the
first time.** A task that fails to resolve a secret fails to start
entirely — fix the parameter and either `update-service
--force-new-deployment` (web) or wait for/manually invoke the next
scheduled rule (jobs).

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
target group ARN) via `Fn::ImportValue`.

### Single-writer discipline (SQLite + git on EFS)

SQLite and git-as-database want exactly one writer. Two mechanisms enforce
that on Fargate:

- **Deployment configuration**: the web service's
  `DeploymentConfiguration` is `MaximumPercent: 100` /
  `MinimumHealthyPercent: 0` — ECS always stops the old task before
  starting its replacement, so two web tasks never run concurrently against
  the same EFS-mounted data directory (at the cost of a brief availability
  gap on each deploy).
- **SQLite journal mode**: `labs.sqlite`'s journal mode is `TRUNCATE` in
  the deployed environment (`ADOC_SQLITE_JOURNAL_MODE=TRUNCATE` in
  `deploy/cfn/ecs.yaml`'s task definitions), not the local/dev default
  `WAL`. WAL relies on a shared-memory index file coordinated via `mmap` +
  POSIX advisory locks, which is unsafe on NFS-family filesystems like EFS
  and can silently corrupt the database; TRUNCATE is a plain
  rollback-journal mode with no such requirement. See
  `src/adoc/labs/db.py`'s `LabsDb.__init__` docstring and ADR 0006.

The scheduled ingest/review/backup jobs are **not** mutually excluded from
each other or from the web task by any lock — an accepted gap at
single-patient scale, not a solved problem.

**Expect a brief 503 window on every web-service deploy.** Because
`MaximumPercent: 100`/`MinimumHealthyPercent: 0` always stops the old task
before starting its replacement, the ALB has zero healthy targets for the
new task's cold-start + health-check time. `HealthCheckGracePeriodSeconds`
is `900` (`deploy/cfn/ecs.yaml`'s `WebService`), sized generously so a
first-boot `adoc bootstrap-data` restore (git clone + `sources/`/JSONL sync
+ `labs.sqlite` rebuild from S3) has time to finish before ECS starts
health-checking; a plain code deploy against an already-seeded EFS finishes
well under that. Plan deploys for a moment when a brief outage is
acceptable.

### Seeding a deployment from local onboarding

The approved onboarding flow is: run `adoc onboard` (and any initial
`adoc backfill`/`adoc ingest`) **locally first**, curate the case file
until it's in good shape, then hand it to the deployed remote — not the
other way around.

1. Locally, with `ADOC_DATA_DIR` pointed at your curated data repo:
   `ADOC_BACKUP_BUCKET=<backup-bucket> uv run adoc backup`. This is the
   exact same `adoc backup` the nightly ECS scheduled task runs — it
   git-bundles the full history + `labs-export.jsonl` + `sources/` to
   `s3://<backup-bucket>/latest/`.
2. On the remote side, nothing further is required: the next task to
   start against an **empty EFS** filesystem runs `adoc bootstrap-data`
   (via `docker-entrypoint.sh`), which sees `ADOC_BACKUP_BUCKET` set,
   finds the backup, and restores it automatically — `git clone` of the
   bundle (full history, checked out, remote stripped), `sources/`,
   `labs-export.jsonl`, and a rebuilt `labs.sqlite`.
3. Once seeded, ongoing Dropbox ingestion on the remote is safe to layer
   on top: `sources/` documents are sha256-addressed and `labs` rows are
   deduped on `(date, name, source_doc)` (see `labs/db.py`), so
   re-ingesting anything already present in the restored history is a
   no-op rather than a duplicate.

### Replacing a deployed store from a local rebuild

When extraction improves enough to warrant rebuilding the corpus, rebuild
**locally**, review it there, then replace the deployed store — rather than
re-ingesting remotely and reviewing twice. Done for real on 2026-08-26.

```
# 1. snapshot the deployed backup — `adoc backup` overwrites latest/
aws s3 cp s3://$BUCKET/latest/ s3://$BUCKET/prod-prewipe-$(date +%F)/ --recursive

# 2. publish the local store as the new latest/
ADOC_DATA_DIR=~/a-doc-data-local ADOC_BACKUP_BUCKET=$BUCKET adoc backup

# 3. drain the service (single-writer discipline)
aws ecs update-service --cluster a-doc --service a-doc-web --desired-count 0

# 4. wipe + restore as a ONE-OFF TASK, then scale back to 1
aws ecs run-task --cluster a-doc --task-definition a-doc-web --launch-type FARGATE \
  --network-configuration '<same as the service>' --overrides file://overrides.json
```

Three things that will bite you, all learned the hard way:

- **`work/` is gitignored, so the login store is not in the backup.** Restoring
  replaces the data dir and leaves the deployment with no way to log in. Copy
  `work/users.yaml` somewhere outside the data dir first and put it back after.
  `case/identifiers.yaml` *is* tracked and does travel in the bundle — check
  the local copy is populated before backing up, or you will restore an empty
  scrubber over a working one.
- **Do the destructive work as a one-off ECS task, not via `execute-command`.**
  A backgrounded process started in an exec session dies when the session
  closes. It fails *silently*: an empty log, an untouched store, and no error.
  A task has its own lifecycle, CloudWatch logs, and an exit code you can read.
- **Verify by row count, not by directory size.** A half-restored store and a
  healthy one are both ~1.1G. Compare `wc -l labs-export.jsonl` against the
  local original.

### Restore-from-backup drill (release gate)

`PLAN.md`/`CLAUDE.md`/ADR 0004 call a tested restore a release gate — do
this before considering a deploy "done," not just once at setup. Restore is
one command, `adoc restore` (`src/adoc/backup.py`'s `restore_from_bucket`,
the tested inverse of `run_backup` — see `tests/test_backup.py`), which
refuses to run over an already-initialized data repo (no `--force`) and
fails clearly if the bucket has no backup. This drill specifically
exercises restoring onto a **freshly created** filesystem (e.g. after
deleting and redeploying the `ecs` stack, or standing up a new
environment):

1. Confirm a real backup exists: `aws s3 ls
   s3://<backup-bucket>/latest/a-doc-data.bundle` (bucket name is the
   `BucketName` output of the `a-doc-backup` stack).
2. Clear EFS and let the task re-seed itself automatically: stop the
   running web task (or scale the ECS service to 0 and back to 1) after
   emptying `<data_dir>` on EFS — the next task to start runs
   `adoc bootstrap-data`, which restores from the bucket since it's set
   and has a backup.
   - To restore by hand instead (e.g. to inspect the result before
     swapping it in), get a shell via ECS Exec (see "User provisioning")
     and run `adoc restore --bucket <backup-bucket>` with `ADOC_DATA_DIR`
     pointed at a fresh path, or invoke `restore_from_bucket` directly.
3. Verify the restored repo: `git -C <data_dir> log --oneline -5` shows
   real history, not just an empty init commit, and `adoc init` (pointed
   at the same `ADOC_DATA_DIR`) reports "already initialized" rather than
   creating a new empty case file.
4. Confirm `https://adoc.petabloc.io/healthz` returns `ok` and the ALB
   target group shows the task healthy.
5. **Re-provision logins.** `<data_dir>/work/users.yaml` lives under the
   gitignored `work/` directory, which `adoc backup`/`restore` never ships
   to or from S3 (ADR 0009). A restore therefore always comes back with
   **zero** web logins, even though every other piece of state (case file,
   ledger, labs, sources) is fully restored. Every restore drill must
   re-run the ECS Exec `adoc user add <username>` step before declaring
   the drill complete.

### How the patient reaches the UI

The web task's `adoc serve` binds `0.0.0.0:8080` — safe to bind widely
because `deploy/cfn/ecs.yaml`'s `ServiceSecurityGroup` admits inbound 8080
from the ALB's security group only. `deploy/cfn/alb.yaml` puts a public,
internet-facing Application Load Balancer in front of it: an ACM
certificate for `adoc.petabloc.io` (DNS-validated against the
`petabloc.io` Route53 hosted zone, `Z009458513KFY2WNUS7C0` —
CloudFormation creates the validation record and waits for issuance
automatically), an HTTPS:443 listener forwarding to a `TargetType: ip`
target group (registered/deregistered dynamically by the ECS service), an
HTTP:80 listener that redirects to HTTPS, and a Route53 alias A record
pointing `adoc.petabloc.io` at the ALB. The target group's health check
hits the unauthenticated `/healthz` route.

For the patient: open `https://adoc.petabloc.io/` in any browser and sign
in with a username/password provisioned via `adoc user add` (see "User
provisioning" above). The in-app login and its rate limiting are the only
gate, by design (no VPN/tailnet, no WAF, no TOTP).

## Lab maintenance commands

Three `no-new-LLM-call` maintenance sweeps clean up rows already in
`labs.sqlite` under the *current* validation/reconcile logic — run them
after a reconcile-comparator or validation change to retroactively fix up
rows that queued under older, stricter rules, or periodically as ordinary
housekeeping:

- **`adoc labs-reclassify [--dry-run]`** (`labs/reclassify.py`) — recomputes
  every still-`PENDING` row's disagreement reasons under the current
  semantic ref-range/unit/flag comparators (`ingest/reconcile.py`) and the
  current trend history. A row whose only "disagreement" was a comparator
  false positive (a cosmetic unit spelling, a unicode dash, `None` vs.
  `""`) flips to `auto`; a row with a real disagreement stays queued but
  its stored reasons are refreshed. Use this after any change to
  `reconcile.py`'s comparators or `labs/validate.py`'s analyte specs, to
  drain the confirm queue of stale false positives without re-extracting
  anything.
- **`adoc labs-dedupe-twins [--dry-run]`** (`labs/twins.py`) — sweeps
  legacy single-pass `PENDING` rows for a duplicate already-resolved row in
  the same document (deterministic candidate gate on value/page/unit/
  specimen, then a rule-based name match, falling back to exactly one
  `classifier`-role LLM call only when the rule can't decide) and
  auto-rejects the duplicate half.
- **`adoc labs-infer-specimen`** (`labs/specimen.py`) — deterministically
  back-fills `specimen` (serum/plasma/urine/etc.) for existing rows still
  carrying the `unknown` default, from their source document's
  filename/`doc_type` keywords only, so older rows join the correct
  per-specimen trend series instead of being excluded from trend
  comparison.

All three are read-modify-write passes over already-extracted data — none
of them makes a new extraction or classification call beyond the single
narrow LLM fallback in `labs-dedupe-twins`, and `--dry-run` on the latter
two reports what would change without mutating anything.

## Genomic data

Raw genotype files (a 23andMe-style raw text export, per-chromosome
imputed `.bcf`/`.vcf` files, BAM/FASTQ) are a supported intake kind
(`ingest/filetypes.py` sniffs content, not filename) but are handled
completely differently from documents: they are archived byte-for-byte
under `sources/genomics/` (gitignored — a patient's raw genotype files
never bloat the git bundle) and folded into one regenerated
`case/genomics-inventory.md` summary table, never a per-file encounter.
**A genomic file's content never reaches any model** — no vision call, no
text-extraction call is ever made against it; this is a CRITICAL DESIGN
RULE (`ingest/genomics.py`), not just a default. `sources/genomics/` is
still backed up: `adoc backup` syncs the whole `sources/` tree on disk to
S3 regardless of what git tracks, so the bytes are safe even though
they're not in git history. See ADR 0010.

## Currently deployed

The deployed version is always the latest `v*` git tag on `main` — check
`git tag -l --sort=-v:refname | head -1` (or the ECS task definition's
image tag) rather than assuming any version mentioned elsewhere in this
document is still current.

## Phase status

| Phase | Description | Status |
|---|---|---|
| 0 | Project scaffold | complete |
| 1 | MVP (onboarding, ingestion, DAG reasoning, web UI, AWS deploy) | complete |
| 2 | Grounding & anti-hallucination hardening | complete |
| 3 | Knowledge layer (HPO/LIRICAL/Monarch, ACR/EULAR criteria) + full eval | **next** |
| 4 | Extras (Apple Health import, specialist finder, notifications) | not started |

See `PLAN.md` for phase acceptance criteria.

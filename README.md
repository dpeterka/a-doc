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

All AWS resources are CloudFormation stacks in `deploy/cfn/`, deployed in
this order: `ci` (once, manually — see note below) → `network` → `backup` →
`instance` → `alb`. Deploys after the initial bootstrap run from GitHub
Actions via an OIDC-assumed role (`deploy/cfn/ci.yaml`) — no long-lived AWS
credentials are stored in the repo.

`ci.yaml` creates the very IAM role that GitHub Actions needs in order to
deploy anything, including `ci.yaml` itself — that first deployment is a
manual, one-time bootstrap (e.g. `aws cloudformation deploy` from a local
admin session), after which its `DeployRoleArn` output is copied into the
`AWS_DEPLOY_ROLE_ARN` repository variable so future deploys are automated.

**Patient access is via a public ALB** at `https://adoc.petabloc.io`
(`deploy/cfn/alb.yaml`) — an explicit user decision that replaced the
original Tailscale-only design. The EC2 instance itself still has no SSH
keys and no direct public ingress: `deploy/cfn/network.yaml`'s
`InstanceSecurityGroup` admits inbound port 8080 from the ALB's security
group only, so the app process is unreachable except through the ALB. A
shell on the instance remains **SSM Session Manager only**, exactly as
before — the ALB changes how *patients* reach the app, not how operators
reach the box. In-app authentication (username/password, scrypt-hashed,
with in-app rate limiting) is the only auth layer in front of the app now
that there is no tailnet gate — see "User provisioning" and "How the
patient reaches the UI" below. There is deliberately no WAF and no TOTP in
this design.

### One-time SSM parameters

The instance's `UserData` and `deploy/install.sh` read a fixed set of
`SecureString` parameters under the `/a-doc/` path at boot. They are created
once, by hand, from a local admin AWS session — they are secrets, so they
are deliberately *not* CloudFormation resources (a template's parameters
would land in the stack's event history and drift-detection output in
plaintext). All of them use the AWS-managed default key (`alias/aws/ssm`) —
omit `--key-id` when creating them, since `a-doc-instance-role`'s
`kms:Decrypt` grant in `deploy/cfn/instance.yaml` is scoped to that default
key only. If a customer-managed KMS key is ever used for one of these
instead, that key's own key policy must separately grant `kms:Decrypt` to
`a-doc-instance-role` (the default key's policy already permits any
IAM-permitted principal in the account, which the instance role's policy
statement grants; a custom key's policy does not, by default).

```bash
# GitHub fine-grained deploy token, read-only on this repo (mint below)
aws ssm put-parameter --name /a-doc/github-deploy-token \
  --type SecureString --value "github_pat_xxxxxxxxxxxxxxxx"

# rclone config defining the "dropbox" remote used by adoc-ingest.service
# (run `rclone config` locally against the Dropbox app-folder backend,
# complete the OAuth flow in a browser, then paste the resulting file)
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
directly on the instance with `adoc user add` (see "User provisioning"
below), not threaded through SSM/UserData. (The old
`/a-doc/session-passphrase` parameter and its `ADOC_SESSION_PASSPHRASE`
env var are gone from the required set; `install.sh` still reads that
parameter if it happens to exist, purely for backward compatibility, but
nothing requires it any more.)

To rotate any of these later, add `--overwrite` and re-run the same command,
then either wait for the next `install.sh` run (e.g. a code deploy) or
trigger one via `aws ssm send-command` — values are only read at
`install.sh` time, not live-reloaded by the running services.

**These parameters must exist *before* the instance stack is deployed for
the first time.** `UserData` runs exactly once, at first boot, with no
automatic retry; if a parameter is missing, the boot script fails partway
and the instance is left half-provisioned. Recovery is either (a) fix the
parameter and let CloudFormation replace the instance (e.g. a harmless
`UpdateReplacePolicy`-neutral property nudge, or delete+redeploy the
instance stack), or (b) if the failure happened after the initial package
installs, re-run the same logic in place via
`aws ssm send-command --document-name AWS-RunShellScript --targets ...
--parameters commands="bash /opt/a-doc/deploy/install.sh"` once `/opt/a-doc`
exists.

#### Minting the GitHub fine-grained deploy token

The instance clones this repo over HTTPS using an `x-access-token`, so the
token only ever needs read access to code, never more:

1. github.com → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token.
2. Resource owner: `dpeterka`. Repository access: **Only select
   repositories** → `a-doc`.
3. Permissions → Repository permissions → **Contents: Read-only**. Leave
   every other permission at "No access."
4. Set an expiration (fine-grained tokens cap out at 1 year) and set a
   calendar reminder to rotate it before then — regenerate, then
   `aws ssm put-parameter --name /a-doc/github-deploy-token --type
   SecureString --overwrite --value "<new token>"`, then re-run
   `install.sh` on the instance (or just wait for the next boot).

### User provisioning

Web login is username/password, one entry per person who needs access
(scrypt-hashed, stored at `<data_dir>/work/users.yaml` on the instance —
gitignored, never in the data repo's git history). There is no SSM
parameter and no CloudFormation resource for this; it is managed directly
on the instance, over an SSM Session Manager shell (the only shell access
this instance ever has):

```bash
aws ssm start-session --target <instance-id>
sudo -u adoc /opt/a-doc/.venv/bin/adoc user add <username>    # prompts twice for a password
sudo -u adoc /opt/a-doc/.venv/bin/adoc user list
sudo -u adoc /opt/a-doc/.venv/bin/adoc user remove <username>
```

`adoc user add` on an existing username replaces that user's password
(useful for rotation). Login also has in-app rate limiting: 5 consecutive
failures for a username, or 20 for a client IP, within a 15-minute sliding
window locks further attempts (HTTP 429) until the window clears; counters
are in-memory only, so a service restart resets them (an accepted
tradeoff — see `src/adoc/web/security.py`).

### Stack deploy order

`ci` (once, manually) → `network` → `backup` → `instance` → `alb`, matching
`.github/workflows/deploy.yml`. Backup must exist before instance because
`instance.yaml` imports the backup bucket/KMS key via `Fn::ImportValue`, and
`install.sh` separately resolves the bucket name at boot via
`aws cloudformation list-exports` (see the `BackupBucketExportLookup`
statement in `instance.yaml`) rather than a CFN parameter — this keeps the
bucket name out of `deploy.yml`'s `--parameter-overrides` entirely, so no
change to that workflow was needed for this slice. Once resolved, the
bucket name is written into `/etc/adoc/env` as `ADOC_BACKUP_BUCKET` for
`adoc-backup.service` to read. `alb.yaml` must come after `instance.yaml`
because it imports the instance's `InstanceId` export to register it as
the ALB target group's target.

### Instance replacement behavior

The EC2 instance's data volume (`/dev/xvdb` → `/data`) is declared inline in
`instance.yaml`'s `BlockDeviceMappings`, not as a separate persistent
`AWS::EC2::Volume`. That is deliberate simplicity, not an oversight — it
means the data volume's lifecycle is tied to the instance's: a **stack
update that replaces the instance** (e.g. changing `AmiId` or an
immutable-once-set property) or a **stack delete** destroys that volume
along with it. There is no snapshot/persistent-volume trick keeping data
alive across a replacement.

This is intentional: PLAN.md and `CLAUDE.md` treat "a rebuilt instance
restores to a working system from S3" as the actual reliability mechanism,
not EBS persistence — so instance replacement is expected to be routine and
recoverable, exercised by the drill below, rather than something to avoid.
A plain **reboot** or **stop/start** of the same instance does *not* lose
`/data` (EBS volumes attached to a still-existing instance persist across
those).

### Restore-from-backup drill (release gate)

PLAN.md's Phase-1 acceptance criteria and `CLAUDE.md`/ADR 0004 call a tested
restore a release gate — do this before considering a deploy "done," not
just once at setup:

1. Confirm a real backup exists: `aws s3 ls
   s3://<backup-bucket>/latest/a-doc-data.bundle` (bucket name is the
   `BackupBucketName` output of the `a-doc-backup` stack).
2. Force a rebuild of the instance. There is no auto-scaling group behind
   it, so this means deleting the `a-doc-instance` stack and redeploying it
   — either by re-running the `deploy` GitHub Actions workflow, or locally:
   `aws cloudformation delete-stack --stack-name a-doc-instance`, wait for
   deletion to finish, then `aws cloudformation deploy --stack-name
   a-doc-instance --template-file deploy/cfn/instance.yaml
   --parameter-overrides NetworkStackName=a-doc-network
   BackupStackName=a-doc-backup --capabilities CAPABILITY_NAMED_IAM`.
3. Watch `/var/log/a-doc-userdata.log` (via SSM Session Manager or
   `aws ssm send-command`) for the UserData run, then `install.sh`'s own
   output — `$DATA_DIR is empty; restoring from s3://.../latest/` confirms
   the restore path (rather than the fresh-`adoc init` fallback) ran.
4. Verify the restored repo: `sudo -u adoc git -C /data/a-doc-data log
   --oneline -5` shows real history, not an empty init commit;
   `sudo -u adoc /opt/a-doc/.venv/bin/adoc init` reports "already
   initialized" rather than creating a new empty case file; and (once labs
   data exists) the labs SQLite DB rebuilds cleanly from the restored
   `labs-export.jsonl`.
5. Confirm the web UI and timers come back: `systemctl status
   adoc-web.service adoc-ingest.timer adoc-review.timer
   adoc-backup.timer`, and that `https://adoc.petabloc.io/healthz` returns
   `ok` and the ALB target group shows the instance healthy (below).

### How the patient reaches the UI

`adoc-web.service` runs uvicorn bound to `0.0.0.0:8080` — safe to bind
widely because `deploy/cfn/network.yaml`'s `InstanceSecurityGroup` admits
inbound 8080 from the ALB's security group only, so nothing else can reach
it. `deploy/cfn/alb.yaml` puts a public, internet-facing Application Load
Balancer in front of it: an ACM certificate for `adoc.petabloc.io`
(DNS-validated against the `petabloc.io` Route53 hosted zone,
`Z009458513KFY2WNUS7C0` — CloudFormation creates the validation record and
waits for issuance automatically, no manual step), an HTTPS:443 listener
forwarding to the instance, an HTTP:80 listener that redirects to HTTPS,
and a Route53 alias A record pointing `adoc.petabloc.io` at the ALB. The
target group's health check hits the unauthenticated `/healthz` route.

For the patient: open `https://adoc.petabloc.io/` in any browser and sign
in with a username/password provisioned via `adoc user add` (see "User
provisioning" above). There is no VPN/tailnet step any more — the
in-app login and its rate limiting are the only gate, by explicit user
decision (no WAF, no TOTP in this design).

## Phase status

| Phase | Description | Status |
|---|---|---|
| 0 | Project scaffold | complete |
| 1 | MVP (onboarding, ingestion, DAG reasoning, web UI, AWS deploy) | code complete — instance deploy verification pending |
| 2 | Grounding & anti-hallucination hardening | not started |
| 3 | Knowledge layer (HPO/LIRICAL/Monarch, ACR/EULAR criteria) + full eval | not started |
| 4 | Extras (Apple Health import, specialist finder, notifications) | not started |

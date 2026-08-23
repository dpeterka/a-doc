#!/usr/bin/env bash
# a-doc instance bootstrap / update script.
#
# Idempotent: safe to re-run on an already-provisioned instance (e.g. after
# a code deploy, or when re-run by EC2 UserData on a fresh boot, or manually
# via `aws ssm send-command` to pick up a code change without a full
# instance replacement). See README.md "Deploy runbook" for the one-time SSM
# parameters this depends on and the restore-from-backup drill.
#
# Standalone-safe: this script does not assume UserData ran first (region
# resolution and the AWS CLI presence check are repeated here), so it can be
# re-run on its own against an already-booted instance.

set -euo pipefail

# SSM RunCommand executes without HOME set; uv and rclone config paths need
# one, and this script runs as root in every supported invocation path.
export HOME="${HOME:-/root}"

REPO_URL_BASE="github.com/dpeterka/a-doc.git"
REPO_BRANCH="main"
APP_DIR="/opt/a-doc"
DATA_DIR="/data/a-doc-data"
CONFIG_DIR="/etc/adoc"
APP_USER="adoc"
BACKUP_EXPORT_NAME="a-doc-backup-BucketName"

echo "==> Resolving region (IMDSv2)"
: "${AWS_DEFAULT_REGION:=}"
if [ -z "$AWS_DEFAULT_REGION" ]; then
  IMDS_TOKEN="$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")"
  AWS_DEFAULT_REGION="$(curl -fsS \
    -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/region)"
fi
export AWS_DEFAULT_REGION

echo "==> Ensuring the AWS CLI is present"
if ! command -v aws &>/dev/null; then
  dnf install -y unzip
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install --update
  rm -rf /tmp/awscliv2.zip /tmp/aws
fi

ssm_get() {
  aws ssm get-parameter --name "$1" --with-decryption --query Parameter.Value --output text
}

echo "==> Ensuring system user and directories exist"
id "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONFIG_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"

echo "==> Installing system packages (git, poppler-utils, rclone; uv+python separately below)"
dnf install -y git poppler-utils
# python3.12 may not exist in every AL2023 repo snapshot; uv provisions its
# own interpreter below regardless, so this is best-effort.
dnf install -y python3.12 || echo "install.sh: python3.12 dnf package unavailable; relying on uv-managed Python"
if ! command -v rclone &>/dev/null; then
  dnf install -y rclone || curl -fsSL https://rclone.org/install.sh | bash
fi

echo "==> Ensuring uv + Python 3.12 are available"
if ! command -v uv &>/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.12 || true

echo "==> Fetching/updating application code"
# The repository is public: clone anonymously over HTTPS.
REPO_URL="https://${REPO_URL_BASE}"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" remote set-url origin "$REPO_URL"
  git -C "$APP_DIR" fetch origin "$REPO_BRANCH"
  git -C "$APP_DIR" checkout "$REPO_BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$REPO_BRANCH"
else
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Installing dependencies (frozen lockfile, no dev group)"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && uv sync --frozen --no-dev"

echo "==> Resolving backup bucket name (CloudFormation export ${BACKUP_EXPORT_NAME})"
BACKUP_BUCKET="$(aws cloudformation list-exports \
  --query "Exports[?Name=='${BACKUP_EXPORT_NAME}'].Value | [0]" --output text 2>/dev/null || true)"
if [ -z "$BACKUP_BUCKET" ] || [ "$BACKUP_BUCKET" = "None" ]; then
  echo "install.sh: WARNING: could not resolve ${BACKUP_EXPORT_NAME} export;" \
    "nightly backups (adoc-backup.timer) will fail until the backup stack is deployed" >&2
  BACKUP_BUCKET=""
fi

echo "==> Writing /etc/adoc/env from SSM parameters"
{
  echo "ADOC_DATA_DIR=$DATA_DIR"
  echo "ANTHROPIC_API_KEY=$(ssm_get /a-doc/anthropic-api-key)"
  echo "OPENAI_API_KEY=$(ssm_get /a-doc/openai-api-key)"
  echo "FEATHERLESS_API_KEY=$(ssm_get /a-doc/featherless-api-key 2>/dev/null || true)"
  # Web login is now username/password (`adoc user add/list/remove`, run
  # over an SSM Session Manager shell), not this passphrase — the
  # ADOC_SESSION_PASSPHRASE parameter is optional/legacy and only fetched
  # if it still exists.
  echo "ADOC_SESSION_PASSPHRASE=$(ssm_get /a-doc/session-passphrase 2>/dev/null || true)"
  echo "ADOC_BACKUP_BUCKET=$BACKUP_BUCKET"
  # Trusting the last hop of X-Forwarded-For for rate-limiting/logging is
  # safe here specifically because network.yaml's InstanceSecurityGroup
  # only admits inbound 8080 from the ALB's security group - no other path
  # can reach this port, so the header can only have been set by the ALB.
  echo "ADOC_TRUST_FORWARDED_FOR=true"
} > "$CONFIG_DIR/env"
chmod 600 "$CONFIG_DIR/env"
chown "$APP_USER:$APP_USER" "$CONFIG_DIR/env"

echo "==> Writing rclone config from SSM"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.config/rclone"
ssm_get /a-doc/rclone-conf > "/home/$APP_USER/.config/rclone/rclone.conf"
chmod 600 "/home/$APP_USER/.config/rclone/rclone.conf"
chown "$APP_USER:$APP_USER" "/home/$APP_USER/.config/rclone/rclone.conf"

echo "==> Installing systemd units and timers"
cp "$APP_DIR"/deploy/systemd/*.service "$APP_DIR"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
for unit in adoc-web.service adoc-ingest.timer adoc-review.timer adoc-backup.timer; do
  systemctl enable --now "$unit"
done

# ---------------------------------------------------------------------------
# Data repo bring-up: if /data/a-doc-data is empty, prefer restoring from the
# backup bucket (this is the release-gate "tested restore" from PLAN.md's
# Phase-1 acceptance criteria and CLAUDE.md); fall back to a fresh `adoc
# init` only if no backup exists yet (first-ever install). See README.md
# "Restore-from-backup drill" for how this is exercised as a release gate.
# ---------------------------------------------------------------------------
if [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  RESTORED=false
  if [ -n "$BACKUP_BUCKET" ] && aws s3api head-object \
      --bucket "$BACKUP_BUCKET" --key "latest/a-doc-data.bundle" &>/dev/null; then
    echo "==> $DATA_DIR is empty; restoring from s3://$BACKUP_BUCKET/latest/"
    RESTORE_TMP="$(mktemp -d)"
    aws s3 cp "s3://$BACKUP_BUCKET/latest/a-doc-data.bundle" "$RESTORE_TMP/a-doc-data.bundle"
    git clone "$RESTORE_TMP/a-doc-data.bundle" "$DATA_DIR"
    aws s3 sync "s3://$BACKUP_BUCKET/latest/sources/" "$DATA_DIR/sources/"
    aws s3 cp "s3://$BACKUP_BUCKET/latest/labs-export.jsonl" "$DATA_DIR/labs-export.jsonl" \
      2>/dev/null || true
    rm -rf "$RESTORE_TMP"
    chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
    RESTORED=true
  fi
  if [ "$RESTORED" = false ]; then
    echo "==> $DATA_DIR is empty and no backup bundle was found; initializing a fresh data repo"
    sudo -u "$APP_USER" "$APP_DIR/.venv/bin/adoc" init
  fi
fi

echo "==> install.sh complete"

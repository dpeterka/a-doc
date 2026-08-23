#!/usr/bin/env bash
# a-doc instance bootstrap / update script.
#
# Idempotent: safe to re-run on an already-provisioned instance (e.g. after
# a code deploy, or when re-run by EC2 UserData on a fresh boot). Restoring
# /data/a-doc-data from S3 on a rebuilt instance is a release gate per
# PLAN.md's Phase-1 acceptance criteria ("tested restore is a release
# gate") — see the clearly-delimited restore block near the end, which is
# left as a TODO until Phase 1 finalizes the backup layout.

set -euo pipefail

REPO_URL="https://github.com/dpeterka/a-doc.git"
REPO_BRANCH="main"
APP_DIR="/opt/a-doc"
DATA_DIR="/data/a-doc-data"
CONFIG_DIR="/etc/adoc"
APP_USER="adoc"

echo "==> Ensuring system user and directories exist"
id "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONFIG_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"

echo "==> Ensuring uv + Python 3.12 are available"
if ! command -v uv &>/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.12 || true

echo "==> Fetching/updating application code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$REPO_BRANCH"
  git -C "$APP_DIR" checkout "$REPO_BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$REPO_BRANCH"
else
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Installing dependencies (frozen lockfile)"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && uv sync --all-extras --frozen"

echo "==> Installing systemd units and timers"
cp "$APP_DIR"/deploy/systemd/*.service "$APP_DIR"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
for unit in adoc-web.service adoc-ingest.timer adoc-review.timer; do
  systemctl enable --now "$unit"
done

# ---------------------------------------------------------------------------
# TODO(phase 1): restore /data/a-doc-data from the backup bucket on a fresh
# instance. Exact layout (bundle naming, retention key structure) isn't
# finalized until Phase 1 implements the backup job itself; this block is
# intentionally isolated so it's easy to fill in without touching the rest
# of this script.
# ---------------------------------------------------------------------------
if [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  echo "==> $DATA_DIR is empty — restore-from-backup not yet implemented (TODO phase 1)"
  # aws s3 sync "s3://<backup-bucket>/latest/" /tmp/adoc-restore/
  # git clone /tmp/adoc-restore/data.bundle "$DATA_DIR"
  # (then apply labs-export.jsonl / sources/ as needed)
fi

echo "==> install.sh complete"

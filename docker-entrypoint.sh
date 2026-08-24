#!/usr/bin/env bash
# a-doc container entrypoint: per-task bootstrap that used to live in
# deploy/install.sh, now scoped to what actually differs container-to-
# container (rclone config, first-run data-repo init) rather than a whole
# instance's package/service provisioning - ECS/Fargate + the image build
# own everything else now.
#
# Runs as the non-root `adoc` user (uid 1000, set by the Dockerfile) in
# every task: web (`adoc serve`), and the scheduled ingest/review/backup
# jobs (see deploy/cfn/ecs.yaml).

set -euo pipefail

DATA_DIR="${ADOC_DATA_DIR:-/data/a-doc-data}"

# --- rclone config, if provided (ECS task secrets inject it as the
# RCLONE_CONF env var from the /a-doc/rclone-conf SSM SecureString; local
# `docker run` / dev just won't set it, and the ingest wrapper below
# degrades gracefully in that case). ---
if [ -n "${RCLONE_CONF:-}" ]; then
  install -d -m 700 "$HOME/.config/rclone"
  printf '%s' "$RCLONE_CONF" > "$HOME/.config/rclone/rclone.conf"
  chmod 600 "$HOME/.config/rclone/rclone.conf"
fi

# --- First-run data-repo bring-up. EFS (or a local bind mount for dev) is
# expected to already exist and be writable by uid 1000 (the EFS
# AccessPoint's PosixUser in deploy/cfn/ecs.yaml). If it's missing/empty,
# delegate the decide-and-run logic to `adoc bootstrap-data` (kept in
# Python, not shell, so it's unit-tested in tests/test_cli.py): it
# restores from ADOC_BACKUP_BUCKET when one is set and actually has a
# backup, falling back to `adoc init` only when the bucket genuinely has
# nothing to restore yet; a real restore error (bad creds, network,
# corrupt bundle) fails the container start loudly rather than silently
# initializing an empty case file over it. This is how a freshly created
# EFS filesystem gets seeded automatically now (see README.md's restore
# drill / seed-from-local sections) - no manual restore step needed for
# the common case. ---
if [ ! -d "$DATA_DIR" ] || [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  echo "docker-entrypoint: $DATA_DIR is missing or empty; running 'adoc bootstrap-data'"
  adoc bootstrap-data
fi

exec "$@"

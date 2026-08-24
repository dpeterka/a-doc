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
# AccessPoint's PosixUser in deploy/cfn/ecs.yaml); if it's empty, `adoc
# init` creates the case-file layout so the very first task to start
# (typically the web service) doesn't crash looking for files that don't
# exist yet. This is NOT a restore path - restoring from the S3 backup
# bucket onto a freshly created EFS filesystem is a manual, documented
# step (see README.md), not something this entrypoint guesses at doing
# automatically on every container start. ---
if [ ! -d "$DATA_DIR" ] || [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  echo "docker-entrypoint: $DATA_DIR is missing or empty; running 'adoc init'"
  adoc init
fi

exec "$@"

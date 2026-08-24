#!/usr/bin/env bash
# Command for the scheduled "ingest" ECS task (deploy/cfn/ecs.yaml,
# rate(10 minutes)): pull new PDFs from the Dropbox app-folder rclone
# remote into the inbox, then run the real ingestion pipeline.
#
# Shipped as a script (rather than inlined into the EventBridge rule's
# container override, or folded into docker-entrypoint.sh, which every
# task - including the always-on web service - runs) so the two steps are
# one command for the scheduler to invoke and are independently testable/
# runnable by hand (`docker run ... run-ingest.sh`) without needing a full
# task-definition override to reproduce.
#
# `--min-age 1m` avoids racing a file mid-upload from the patient's
# Dropbox client. If RCLONE_CONF wasn't provided (e.g. a local/dev run,
# or the rclone-conf SSM parameter momentarily missing), rclone has no
# configured remote and this step fails; that failure is logged and
# swallowed here so a missing/rotating rclone config degrades to
# "ingest whatever is already in the inbox" rather than blocking ingest
# entirely.

set -euo pipefail

DATA_DIR="${ADOC_DATA_DIR:-/data/a-doc-data}"

echo "run-ingest: pulling new PDFs from dropbox:a-doc-inbox"
if ! rclone move dropbox:a-doc-inbox "$DATA_DIR/inbox" --include "*.pdf" --min-age 1m; then
  echo "run-ingest: rclone move failed or has no configured remote - continuing with" \
    "adoc ingest anyway" >&2
fi

echo "run-ingest: running adoc ingest"
exec adoc ingest

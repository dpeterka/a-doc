#!/usr/bin/env bash
# Command for the scheduled "ingest" ECS task (deploy/cfn/ecs.yaml,
# rate(10 minutes)): pull new intake-eligible files from the Dropbox
# app-folder rclone remote into the inbox, then run the real ingestion
# pipeline.
#
# Shipped as a script (rather than inlined into the EventBridge rule's
# container override, or folded into docker-entrypoint.sh, which every
# task - including the always-on web service - runs) so the two steps are
# one command for the scheduler to invoke and are independently testable/
# runnable by hand (`docker run ... run-ingest.sh`) without needing a full
# task-definition override to reproduce.
#
# Defect fix (live blocker): this used to filter on `*.pdf` only, so a
# patient's `.docx` narrative dropped in `a-doc-inbox/NarrativeSummary/`
# was NEVER pulled even though the pipeline fully supports it (ADR 0008;
# `ingest.filetypes.detect_intake_kind` classifies pdf/docx/text/zip/
# genomic). The `--include` patterns below are exactly the extensions that
# function can classify - keep them in sync if its suffix list ever
# changes. None of these patterns has a leading "/", so rclone's filtering
# matches them at ANY depth in the remote tree, not just the top level
# (rclone docs: an unanchored pattern - no leading "/" - matches at any
# level) - required here since the narrative lives one level deeper, in
# `a-doc-inbox/NarrativeSummary/*.docx`.
#
# `--min-age 1m` avoids racing a file mid-upload from the patient's
# Dropbox client. If RCLONE_CONF wasn't provided (e.g. a local/dev run,
# or the rclone-conf SSM parameter momentarily missing), rclone has no
# configured remote and this step fails; that failure is logged and
# swallowed here so a missing/rotating rclone config degrades to
# "ingest whatever is already in the inbox" rather than blocking ingest
# entirely.
#
# `ADOC_RCLONE_SOURCE` is the rclone remote:path to pull from - env-
# configurable because the real Dropbox app-folder backend nests the
# inbox one level deeper (`a-doc/a-doc-inbox`) than the remote name alone
# (`dropbox:a-doc-inbox`) would suggest; the default below matches the
# real, live-verified app-folder layout (`deploy/cfn/ecs.yaml` sets the
# same value explicitly for the jobs task definition).

set -euo pipefail

DATA_DIR="${ADOC_DATA_DIR:-/data/a-doc-data}"
RCLONE_SOURCE="${ADOC_RCLONE_SOURCE:-dropbox:a-doc/a-doc-inbox}"

echo "run-ingest: pulling new intake-eligible files from $RCLONE_SOURCE"
if ! rclone move "$RCLONE_SOURCE" "$DATA_DIR/inbox" \
  --include "*.pdf" \
  --include "*.docx" \
  --include "*.txt" \
  --include "*.md" \
  --include "*.zip" \
  --include "*.vcf" \
  --include "*.vcf.gz" \
  --include "*.bcf" \
  --include "*.bam" \
  --include "*.fastq" \
  --include "*.fastq.gz" \
  --include "*.fq" \
  --include "*.fq.gz" \
  --min-age 1m; then
  echo "run-ingest: rclone move failed or has no configured remote - continuing with" \
    "adoc ingest anyway" >&2
fi

echo "run-ingest: running adoc ingest"
exec adoc ingest

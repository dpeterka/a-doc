#!/usr/bin/env bash
# a-doc nightly backup: git-bundles the PHI data repo and ships it, plus
# sources/ and the labs JSONL export, to the versioned + SSE-KMS backup
# bucket. Invoked by adoc-backup.service (see deploy/systemd/), which runs
# as the `adoc` user with /etc/adoc/env loaded (written by install.sh).
#
# Always writes to fixed "latest/" keys rather than dated prefixes — the
# backup bucket has S3 versioning enabled with a 365-day noncurrent-version
# expiration (deploy/cfn/backup.yaml), so history is retained via object
# versions without any date-prefix bookkeeping here.
#
# Restore procedure: README.md "Restore-from-backup drill"; the same logic
# also runs automatically in deploy/install.sh when /data/a-doc-data is
# empty on a freshly (re)provisioned instance.

set -euo pipefail

DATA_DIR="${ADOC_DATA_DIR:-/data/a-doc-data}"
BUCKET="${ADOC_BACKUP_BUCKET:-}"
BUNDLE_PATH="/tmp/a-doc-data.bundle"

if [ -z "$BUCKET" ]; then
  echo "adoc-backup: ADOC_BACKUP_BUCKET is not set in /etc/adoc/env - skipping backup" >&2
  exit 1
fi

cleanup() {
  rm -f "$BUNDLE_PATH"
}
trap cleanup EXIT

echo "==> Bundling $DATA_DIR"
git -C "$DATA_DIR" bundle create "$BUNDLE_PATH" --all

echo "==> Uploading bundle to s3://$BUCKET/latest/a-doc-data.bundle"
aws s3 cp "$BUNDLE_PATH" "s3://$BUCKET/latest/a-doc-data.bundle"

echo "==> Syncing sources/ to s3://$BUCKET/latest/sources/"
aws s3 sync "$DATA_DIR/sources/" "s3://$BUCKET/latest/sources/" --delete

if [ -f "$DATA_DIR/labs-export.jsonl" ]; then
  echo "==> Uploading labs-export.jsonl to s3://$BUCKET/latest/labs-export.jsonl"
  aws s3 cp "$DATA_DIR/labs-export.jsonl" "s3://$BUCKET/latest/labs-export.jsonl"
else
  echo "adoc-backup: $DATA_DIR/labs-export.jsonl does not exist yet - skipping" >&2
fi

echo "==> Backup complete"

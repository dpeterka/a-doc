"""`adoc backup`: git-bundle the PHI data repo and ship it, plus
`labs-export.jsonl` and `sources/`, to `s3://$ADOC_BACKUP_BUCKET/latest/`.

This replaces the old EC2 `deploy/backup.sh` + `adoc-backup.timer` (see
deploy/cfn/ecs.yaml's `backup` scheduled task, which runs `adoc backup` on
a cron schedule) with a testable Python path: the same logic that runs in
production is exercised in `tests/test_backup.py` against a fake S3 client,
rather than only ever being exercised by a real `aws s3` invocation.

Upload is a plain, sequential loop (per the task spec: "skip unchanged by
size/mtime is fine, simple loop") — this is a single-patient system with a
handful of source documents, not a fleet-scale sync job. `boto3` is an
injection seam (`S3Client` protocol) so tests never construct a real client
or need `moto`; real wiring (`build_s3_client` in this module) is the only
place `boto3` is imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from git import Repo

BUNDLE_KEY = "latest/a-doc-data.bundle"
JSONL_KEY = "latest/labs-export.jsonl"
SOURCES_PREFIX = "latest/sources/"


class BackupError(Exception):
    """Raised when `run_backup` cannot proceed (e.g. no bucket configured)."""


class S3Client(Protocol):
    """The subset of boto3's S3 client surface `run_backup` needs.

    Matches boto3's real `S3.Client` method signatures/keyword args exactly,
    so a real `boto3.client("s3")` satisfies this protocol structurally with
    no adapter, and tests can substitute a small fake that also matches it.
    """

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None: ...

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BackupReport:
    bucket: str
    bundle_uploaded: bool
    jsonl_uploaded: bool
    sources_uploaded: int
    sources_skipped: int


def build_s3_client() -> S3Client:
    """Real wiring: a real boto3 S3 client. Overridden by tests/CLI seams
    so a test run never talks to AWS."""
    import boto3

    client: S3Client = boto3.client("s3")
    return client


def _bundle_data_repo(data_dir: Path, bundle_path: Path) -> None:
    """Write a full `git bundle --all` of the data repo to `bundle_path`."""
    repo = Repo(data_dir)
    repo.git.bundle("create", str(bundle_path), "--all")


def _remote_source_sizes(s3: S3Client, bucket: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    response = s3.list_objects_v2(Bucket=bucket, Prefix=SOURCES_PREFIX)
    for obj in response.get("Contents", []):
        sizes[obj["Key"]] = obj["Size"]
    return sizes


def _sync_sources(s3: S3Client, bucket: str, sources_dir: Path) -> tuple[int, int]:
    """Upload every file under `sources_dir`, skipping ones whose size
    already matches the remote object (cheap, sufficient dedupe for a
    single-patient corpus of immutable source documents that are never
    edited in place after ingest).
    """
    if not sources_dir.is_dir():
        return 0, 0

    remote_sizes = _remote_source_sizes(s3, bucket)
    uploaded = 0
    skipped = 0
    for path in sorted(sources_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(sources_dir).as_posix()
        key = f"{SOURCES_PREFIX}{rel}"
        if remote_sizes.get(key) == path.stat().st_size:
            skipped += 1
            continue
        s3.upload_file(str(path), bucket, key)
        uploaded += 1
    return uploaded, skipped


def run_backup(data_dir: Path, bucket: str, s3: S3Client) -> BackupReport:
    """Bundle+upload the data repo to `s3://{bucket}/latest/`.

    Always writes to fixed `latest/` keys (matching the old
    `deploy/backup.sh` convention) — the backup bucket has S3 versioning
    enabled with a 365-day noncurrent-version expiration
    (`deploy/cfn/backup.yaml`), so history is retained via object versions
    without any date-prefix bookkeeping here.
    """
    if not bucket:
        raise BackupError("ADOC_BACKUP_BUCKET is not set")

    bundle_path = data_dir / "work" / "a-doc-data.bundle"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _bundle_data_repo(data_dir, bundle_path)
        s3.upload_file(str(bundle_path), bucket, BUNDLE_KEY)
        bundle_uploaded = True
    finally:
        bundle_path.unlink(missing_ok=True)

    sources_uploaded, sources_skipped = _sync_sources(s3, bucket, data_dir / "sources")

    jsonl_path = data_dir / "labs-export.jsonl"
    jsonl_uploaded = False
    if jsonl_path.is_file():
        s3.upload_file(str(jsonl_path), bucket, JSONL_KEY)
        jsonl_uploaded = True

    return BackupReport(
        bucket=bucket,
        bundle_uploaded=bundle_uploaded,
        jsonl_uploaded=jsonl_uploaded,
        sources_uploaded=sources_uploaded,
        sources_skipped=sources_skipped,
    )

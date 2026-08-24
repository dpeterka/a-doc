"""`adoc backup`: git-bundle the PHI data repo and ship it, plus
`labs-export.jsonl` and `sources/`, to `s3://$ADOC_BACKUP_BUCKET/latest/`.
`restore_from_bucket` is the inverse: it seeds a fresh, uninitialized
`data_dir` from those same `latest/` keys (approved design: local curated
onboarding -> `adoc backup` -> remote adopts via restore; see PLAN.md
"State" and the restore-drill section of README.md).

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

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from git import Repo

from adoc.casefile.repo import DataRepo
from adoc.labs.db import LabsDb

BUNDLE_KEY = "latest/a-doc-data.bundle"
JSONL_KEY = "latest/labs-export.jsonl"
SOURCES_PREFIX = "latest/sources/"


class BackupError(Exception):
    """Raised when `run_backup` cannot proceed (e.g. no bucket configured)."""


class RestoreError(Exception):
    """Raised when `restore_from_bucket` cannot proceed: `data_dir` already
    holds an initialized data repo (never clobbered — no `--force` is
    offered), the bucket isn't configured, or (see `NoBackupError`) the
    bucket has no backup to restore from."""


class NoBackupError(RestoreError):
    """The bucket is reachable but has no backup at `BUNDLE_KEY`.

    A distinct subclass (rather than a plain `RestoreError`) so a caller
    like `adoc bootstrap-data` can tell "nothing to restore yet - fall
    back to `adoc init`" apart from every other `RestoreError`/exception,
    which must fail loudly instead (see that command's docstring)."""


class S3Client(Protocol):
    """The subset of boto3's S3 client surface `run_backup`/
    `restore_from_bucket` need.

    Matches boto3's real `S3.Client` method signatures/keyword args exactly,
    so a real `boto3.client("s3")` satisfies this protocol structurally with
    no adapter, and tests can substitute a small fake that also matches it.
    """

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None: ...

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None: ...

    def list_objects_v2(
        self, Bucket: str, Prefix: str, ContinuationToken: str | None = None
    ) -> dict[str, Any]: ...


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


# --------------------------------------------------------------------------
# Restore (the inverse of run_backup — seeds an uninitialized data_dir)
# --------------------------------------------------------------------------

# Operational dirs `casefile.repo.DataRepo` gitignores (never present in the
# bundle's git history) but that the app expects to exist.
_OPERATIONAL_DIRS = ("inbox", "work", "logs")


@dataclass(frozen=True)
class RestoreReport:
    bucket: str
    data_dir: Path
    bundle_commit_sha: str
    sources_restored: int
    lab_rows_rebuilt: int
    warnings: list[str]


def _object_exists(s3: S3Client, bucket: str, key: str) -> bool:
    response = s3.list_objects_v2(Bucket=bucket, Prefix=key)
    return any(obj["Key"] == key for obj in response.get("Contents", []))


def _list_source_keys(s3: S3Client, bucket: str) -> list[str]:
    """Every object key under `SOURCES_PREFIX`, following
    `NextContinuationToken` until `IsTruncated` is false — unlike
    `run_backup`'s `_remote_source_sizes` (a single, unpaginated call,
    fine for this single-patient corpus today), restore is the one path
    the task spec asks to paginate explicitly, so a corpus that outgrows
    one `list_objects_v2` page still restores completely.
    """
    keys: list[str] = []
    token: str | None = None
    while True:
        # boto3 rejects ContinuationToken=None (the parameter must be
        # OMITTED on the first call) - found by the first real restore;
        # the test fake must mirror this rejection.
        kwargs: dict[str, str] = {"Bucket": bucket, "Prefix": SOURCES_PREFIX}
        if token is not None:
            kwargs["ContinuationToken"] = token
        response = s3.list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in response.get("Contents", []))
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if token is None:
            return keys


def _clone_bundle(bundle_path: Path, data_dir: Path) -> Repo:
    """Clone `bundle_path` into `data_dir` on the bundle's own HEAD branch,
    then strip the `origin` remote the clone wires up (pointing at the
    now-deleted local bundle file) — the restored repo must satisfy the
    same no-remote invariant as any other data repo (CLAUDE.md rule 1,
    `casefile.repo.DataRepo`'s module docstring).
    """
    repo = Repo.clone_from(str(bundle_path), str(data_dir))
    if repo.head.is_detached:
        # Defensive fallback: `git bundle create --all` records HEAD, so
        # `clone_from` normally checks out a real branch already (verified
        # against a real bundle) — this only guards a bundle that somehow
        # didn't carry a HEAD symref.
        if not repo.heads:
            raise RestoreError("restored bundle has no branches to check out")
        repo.heads[0].checkout()
    for remote in list(repo.remotes):
        repo.delete_remote(remote)
    return repo


def _restore_sources(s3: S3Client, bucket: str, data_dir: Path) -> int:
    """Download every object under `SOURCES_PREFIX` into `<data_dir>/sources/`.

    `sources/` is also tracked in the data repo's git history (not
    gitignored), so the clone already populated it from the bundle; this
    re-downloads from S3 anyway to restore the third `latest/` leg
    `run_backup` writes, exactly mirroring its layout. Both copies are the
    same immutable, sha256-addressed originals, so re-writing them is
    idempotent — never a destructive overwrite of different content.
    """
    sources_dir = data_dir / "sources"
    restored = 0
    for key in _list_source_keys(s3, bucket):
        rel = key[len(SOURCES_PREFIX) :]
        if not rel:
            continue
        dest = sources_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(Bucket=bucket, Key=key, Filename=str(dest))
        restored += 1
    return restored


def _restore_jsonl(
    s3: S3Client, bucket: str, data_dir: Path, scratch_dir: Path, warnings: list[str]
) -> Path | None:
    """Reconcile `<data_dir>/labs-export.jsonl` (already present from the
    clone — it's a committed file, per `labs.db`'s module docstring)
    against the S3 copy `run_backup` also uploads.

    The cloned copy is preferred: git history is this app's source of
    truth. The S3 copy is only a consistency cross-check — a byte mismatch
    is appended to `warnings` (e.g. the S3 copy could legitimately lag by
    one backup cycle) rather than raised. Returns the path to rebuild
    `labs.sqlite` from, or `None` if neither copy exists (e.g. restoring a
    case file that was backed up before any document was ever ingested).
    """
    jsonl_path = data_dir / "labs-export.jsonl"
    if not _object_exists(s3, bucket, JSONL_KEY):
        return jsonl_path if jsonl_path.is_file() else None

    remote_copy = scratch_dir / "labs-export.jsonl.s3-check"
    s3.download_file(Bucket=bucket, Key=JSONL_KEY, Filename=str(remote_copy))

    if not jsonl_path.is_file():
        # Shouldn't normally happen (the bundle should carry it), but
        # don't lose lab data just because the git copy is missing.
        remote_copy.replace(jsonl_path)
        return jsonl_path

    if jsonl_path.read_bytes() != remote_copy.read_bytes():
        warnings.append(
            "labs-export.jsonl in the git bundle differs from the S3 copy at "
            f"s3://{bucket}/{JSONL_KEY} - using the git bundle's committed copy "
            "(git history is the source of truth)."
        )
    return jsonl_path


def _count_lab_rows(jsonl_path: Path) -> int:
    count = 0
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and json.loads(line)["table"] == "lab":
                count += 1
    return count


def restore_from_bucket(
    bucket: str,
    data_dir: Path,
    *,
    s3_client: S3Client | None = None,
    sqlite_journal_mode: str = "WAL",
) -> RestoreReport:
    """Seed `data_dir` from `s3://{bucket}/latest/` — the inverse of
    `run_backup`, and the remote-adoption half of the approved
    local-curated-onboarding -> backup -> restore design (PLAN.md "State").

    Preconditions (both enforced before anything is written):
    - `data_dir` must NOT already hold an initialized data repo
      (`DataRepo.is_initialized`) — this never clobbers existing data, and
      deliberately offers no `--force` escape hatch.
    - the bucket must actually contain a bundle at `BUNDLE_KEY`.

    Steps: download+clone the git bundle (full history, checked out on its
    own HEAD branch, remote stripped); restore `sources/`; reconcile
    `labs-export.jsonl` (git copy preferred, S3 copy cross-checked);
    rebuild `labs.sqlite` from that jsonl (`LabsDb.rebuild_from_jsonl`);
    recreate the gitignored operational dirs (`inbox/`, `work/`, `logs/`).
    """
    if not bucket:
        raise RestoreError("ADOC_BACKUP_BUCKET is not set")
    if DataRepo(data_dir).is_initialized:
        raise RestoreError(
            f"{data_dir} already contains an initialized data repo - "
            "restore_from_bucket refuses to run over existing data (no "
            "--force is offered; move or remove it by hand first if you "
            "really intend to replace it)."
        )

    s3 = s3_client if s3_client is not None else build_s3_client()

    if not _object_exists(s3, bucket, BUNDLE_KEY):
        raise NoBackupError(f"no backup found at s3://{bucket}/{BUNDLE_KEY} - nothing to restore")

    warnings: list[str] = []
    # Stage the entire restore in a sibling directory and move it into
    # place only once EVERYTHING succeeded - a partial restore must never
    # occupy data_dir, or the next boot's is-empty check would skip
    # bootstrap and serve a half-restored case file (found by the first
    # real restore, which failed mid-way and left a bare clone behind).
    staging_dir = data_dir.parent / (data_dir.name + ".restore-staging")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    try:
        with tempfile.TemporaryDirectory(prefix="adoc-restore-") as scratch:
            scratch_dir = Path(scratch)
            bundle_path = scratch_dir / "a-doc-data.bundle"
            s3.download_file(Bucket=bucket, Key=BUNDLE_KEY, Filename=str(bundle_path))

            repo = _clone_bundle(bundle_path, staging_dir)
            bundle_commit_sha = repo.head.commit.hexsha

            sources_restored = _restore_sources(s3, bucket, staging_dir)
            jsonl_path = _restore_jsonl(s3, bucket, staging_dir, scratch_dir, warnings)

        for relpath in _OPERATIONAL_DIRS:
            (staging_dir / relpath).mkdir(parents=True, exist_ok=True)

        lab_rows_rebuilt = 0
        if jsonl_path is not None:
            with LabsDb(staging_dir / "labs.sqlite", journal_mode=sqlite_journal_mode) as db:
                db.rebuild_from_jsonl(jsonl_path)
                lab_rows_rebuilt = _count_lab_rows(jsonl_path)
        else:
            warnings.append(
                "no labs-export.jsonl found in the bundle or the S3 backup; "
                "labs.sqlite was not rebuilt"
            )

        if data_dir.exists() and not any(data_dir.iterdir()):
            data_dir.rmdir()  # an empty mount-point dir; os.rename needs it gone
        os.rename(staging_dir, data_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return RestoreReport(
        bucket=bucket,
        data_dir=data_dir,
        bundle_commit_sha=bundle_commit_sha,
        sources_restored=sources_restored,
        lab_rows_rebuilt=lab_rows_rebuilt,
        warnings=warnings,
    )

"""Tests for adoc.backup: git-bundle + sources/ + labs-export.jsonl -> S3.

Uses a small in-memory fake S3 client (matching the `S3Client` protocol)
instead of `moto` or a real `boto3` client, per the injection-seam
preference documented in `adoc.backup`'s module docstring.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from adoc.backup import (
    SOURCES_PREFIX,
    BackupError,
    NoBackupError,
    RestoreError,
    restore_from_bucket,
    run_backup,
)
from adoc.casefile.repo import DataRepo
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult

_OMITTED: Any = object()  # sentinel: parameter not passed at all


class FakeS3Client:
    """Records uploads/downloads; `list_objects_v2` reflects what has been
    uploaded so far, matching real S3 list-after-upload semantics closely
    enough for the size-based skip-unchanged check in `_sync_sources` and
    for `restore_from_bucket`'s existence/pagination checks.

    Object *bytes* (not just sizes) are kept so `download_file` can hand
    real content back — `run_backup` only ever needed sizes, but
    `restore_from_bucket` needs the actual bytes to round-trip.
    """

    def __init__(self, *, page_size: int | None = None) -> None:
        self.uploads: list[tuple[str, str, str]] = []  # (local_path, bucket, key)
        self.downloads: list[tuple[str, str, str]] = []  # (bucket, key, local_path)
        self._contents: dict[str, bytes] = {}
        # When set, `list_objects_v2` paginates at this many keys per call
        # (a small number in tests) to exercise restore's pagination loop
        # instead of the single "not paginated" call `run_backup` makes.
        self._page_size = page_size

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
        self.uploads.append((Filename, Bucket, Key))
        self._contents[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        self.downloads.append((Bucket, Key, Filename))
        Path(Filename).write_bytes(self._contents[Key])

    def list_objects_v2(
        self, Bucket: str, Prefix: str, ContinuationToken: str | None = _OMITTED
    ) -> dict[str, Any]:
        # Mirror real boto3: passing ContinuationToken=None explicitly is a
        # ParamValidationError - callers must OMIT it on the first page
        # (found by the first real restore against live S3).
        if ContinuationToken is None:
            raise ValueError(
                "Parameter validation failed: Invalid type for parameter "
                "ContinuationToken, value: None"
            )
        if ContinuationToken is _OMITTED:
            ContinuationToken = None
        matching = sorted(key for key in self._contents if key.startswith(Prefix))
        if self._page_size is None:
            page = matching
            rest: list[str] = []
        else:
            start = int(ContinuationToken) if ContinuationToken else 0
            page = matching[start : start + self._page_size]
            rest = matching[start + self._page_size :]
        contents = [{"Key": key, "Size": len(self._contents[key])} for key in page]
        result: dict[str, Any] = {"Contents": contents}
        if rest:
            result["IsTruncated"] = True
            result["NextContinuationToken"] = str(
                (int(ContinuationToken) if ContinuationToken else 0) + len(page)
            )
        return result


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "a-doc-data"
    DataRepo.init_at(root)
    return root


def test_run_backup_requires_a_bucket(data_dir: Path) -> None:
    with pytest.raises(BackupError):
        run_backup(data_dir, "", FakeS3Client())


def test_run_backup_uploads_bundle_and_jsonl(data_dir: Path) -> None:
    (data_dir / "labs-export.jsonl").write_text('{"table": "document"}\n', encoding="utf-8")
    s3 = FakeS3Client()

    report = run_backup(data_dir, "a-doc-backup-bucket", s3)

    assert report.bundle_uploaded is True
    assert report.jsonl_uploaded is True
    keys = {key for _, _, key in s3.uploads}
    assert "latest/a-doc-data.bundle" in keys
    assert "latest/labs-export.jsonl" in keys
    # the temp bundle file is cleaned up after upload
    assert not (data_dir / "work" / "a-doc-data.bundle").exists()


def test_run_backup_skips_jsonl_when_absent(data_dir: Path) -> None:
    report = run_backup(data_dir, "a-doc-backup-bucket", FakeS3Client())
    assert report.jsonl_uploaded is False


def test_run_backup_uploads_new_sources_and_skips_unchanged(data_dir: Path) -> None:
    # DataRepo.init_at already seeds sources/.gitkeep - account for it below.
    sources = data_dir / "sources"
    (sources / "doc-a.pdf").write_bytes(b"pdf-bytes-aaaa")
    (sources / "sub").mkdir()
    (sources / "sub" / "doc-b.pdf").write_bytes(b"pdf-bytes-b")
    s3 = FakeS3Client()

    first = run_backup(data_dir, "bucket", s3)
    assert first.sources_uploaded == 3
    assert first.sources_skipped == 0

    # Re-running with unchanged file sizes should skip everything.
    second = run_backup(data_dir, "bucket", s3)
    assert second.sources_uploaded == 0
    assert second.sources_skipped == 3

    # Changing one file's size should re-upload only that one.
    (sources / "doc-a.pdf").write_bytes(b"a-longer-pdf-payload-now")
    third = run_backup(data_dir, "bucket", s3)
    assert third.sources_uploaded == 1
    assert third.sources_skipped == 2


def test_run_backup_handles_missing_sources_dir(tmp_path: Path) -> None:
    # A data dir without a sources/ directory at all (shouldn't happen via
    # DataRepo.init_at, but run_backup should not crash if it's absent).
    root = tmp_path / "bare-data-dir"
    DataRepo.init_at(root)
    import shutil

    shutil.rmtree(root / "sources")

    report = run_backup(root, "bucket", FakeS3Client())
    assert report.sources_uploaded == 0
    assert report.sources_skipped == 0


# --------------------------------------------------------------------------
# restore_from_bucket (the inverse of run_backup)
# --------------------------------------------------------------------------


def _seed_with_labs(root: Path) -> Path:
    """Populate `data_dir` (already `DataRepo.init_at`'d by the caller)
    with a couple of source files and lab rows, committing both plus the
    exported `labs-export.jsonl` - enough content for `run_backup` +
    `restore_from_bucket` to have real history/sources/labs to round-trip.
    """
    sha = "b" * 64
    (root / "sources" / "doc-a.pdf").write_bytes(b"pdf-bytes-aaaa")
    (root / "sources" / "sub").mkdir()
    (root / "sources" / "sub" / "doc-b.pdf").write_bytes(b"pdf-bytes-b")

    db_path = root / "labs.sqlite"
    with LabsDb(db_path) as db:
        db.upsert_document(
            LabDocument(
                sha256=sha,
                filename="doc-a.pdf",
                doc_type="lab-result",
                doc_date=date(2026, 5, 2),
                page_count=1,
                ingested_at=datetime(2026, 5, 3),
                status="complete",
            )
        )
        db.insert_results(
            [
                LabResult(
                    date=date(2026, 5, 2),
                    name="crp",
                    name_raw="CRP",
                    value=1.2,
                    ucum_unit="mg/L",
                    source_doc=sha,
                    raw_json="{}",
                ),
                LabResult(
                    date=date(2026, 7, 10),
                    name="crp",
                    name_raw="CRP",
                    value=0.9,
                    ucum_unit="mg/L",
                    source_doc=sha,
                    raw_json="{}",
                ),
            ]
        )
        jsonl_path = root / "labs-export.jsonl"
        db.export_jsonl(jsonl_path)

    repo = DataRepo(root)
    repo.commit("test: seed sources + labs export", paths=["sources", "labs-export.jsonl"])
    return jsonl_path


def test_restore_round_trip_reproduces_git_log_sources_and_labs(tmp_path: Path) -> None:
    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    _seed_with_labs(src)

    s3 = FakeS3Client()
    run_backup(src, "bucket", s3)

    dst = tmp_path / "restored-data"
    report = restore_from_bucket("bucket", dst, s3_client=s3)

    assert report.warnings == []
    assert report.sources_restored == 3  # doc-a.pdf, sub/doc-b.pdf, sources/.gitkeep
    assert report.lab_rows_rebuilt == 2

    src_log = [c.hexsha for c in Repo(src).iter_commits()]
    dst_repo = Repo(dst)
    dst_log = [c.hexsha for c in dst_repo.iter_commits()]
    assert src_log == dst_log
    assert report.bundle_commit_sha == dst_log[0]

    assert (dst / "sources" / "doc-a.pdf").read_bytes() == b"pdf-bytes-aaaa"
    assert (dst / "sources" / "sub" / "doc-b.pdf").read_bytes() == b"pdf-bytes-b"

    src_jsonl = (src / "labs-export.jsonl").read_text(encoding="utf-8")
    dst_jsonl = (dst / "labs-export.jsonl").read_text(encoding="utf-8")
    assert src_jsonl == dst_jsonl

    for relpath in ("inbox", "work", "logs"):
        assert (dst / relpath).is_dir()

    # the restored repo has no remote - CLAUDE.md's PHI-boundary invariant
    assert list(dst_repo.remotes) == []

    with LabsDb(dst / "labs.sqlite") as db:
        series = db.series("crp")
        assert [r.value for r in series] == [1.2, 0.9]


def test_restore_requires_a_bucket(tmp_path: Path) -> None:
    with pytest.raises(RestoreError):
        restore_from_bucket("", tmp_path / "dst", s3_client=FakeS3Client())


def test_restore_refuses_to_run_over_an_initialized_repo(tmp_path: Path) -> None:
    dst = tmp_path / "dst-data"
    DataRepo.init_at(dst)

    with pytest.raises(RestoreError, match="already contains an initialized data repo"):
        restore_from_bucket("bucket", dst, s3_client=FakeS3Client())


def test_restore_missing_bundle_raises_no_backup_error(tmp_path: Path) -> None:
    with pytest.raises(NoBackupError):
        restore_from_bucket("bucket", tmp_path / "dst-data", s3_client=FakeS3Client())


def test_restore_warns_on_jsonl_mismatch_but_prefers_the_git_copy(tmp_path: Path) -> None:
    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    _seed_with_labs(src)

    s3 = FakeS3Client()
    run_backup(src, "bucket", s3)

    # Simulate a stale S3 copy (e.g. the bucket lagging one backup cycle
    # behind the git history) by corrupting the uploaded jsonl bytes.
    from adoc.backup import JSONL_KEY

    s3._contents[JSONL_KEY] = b'{"table": "document", "row": {}}\n'

    dst = tmp_path / "restored-data"
    report = restore_from_bucket("bucket", dst, s3_client=s3)

    assert len(report.warnings) == 1
    assert "labs-export.jsonl" in report.warnings[0]
    # the committed (git) copy won, not the corrupted S3 one
    assert report.lab_rows_rebuilt == 2


def test_restore_paginates_the_sources_listing(tmp_path: Path) -> None:
    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    _seed_with_labs(src)

    s3 = FakeS3Client(page_size=1)
    run_backup(src, "bucket", s3)

    dst = tmp_path / "restored-data"
    report = restore_from_bucket("bucket", dst, s3_client=s3)

    assert report.sources_restored == 3
    assert (dst / "sources" / "doc-a.pdf").read_bytes() == b"pdf-bytes-aaaa"
    assert (dst / "sources" / "sub" / "doc-b.pdf").read_bytes() == b"pdf-bytes-b"


def test_restore_without_any_jsonl_skips_rebuild_with_a_warning(tmp_path: Path) -> None:
    # A data repo backed up before any document was ever ingested - no
    # labs-export.jsonl in the bundle's history or on S3.
    src = tmp_path / "src-data"
    DataRepo.init_at(src)

    s3 = FakeS3Client()
    run_backup(src, "bucket", s3)

    dst = tmp_path / "restored-data"
    report = restore_from_bucket("bucket", dst, s3_client=s3)

    assert report.lab_rows_rebuilt == 0
    assert any("labs-export.jsonl" in w for w in report.warnings)
    assert not (dst / "labs.sqlite").exists()


def test_restore_falls_back_to_the_s3_jsonl_when_the_clone_lacks_it(tmp_path: Path) -> None:
    # labs-export.jsonl written to disk but never committed (e.g. a crash
    # between `export_jsonl` and the pipeline's commit) - `run_backup`
    # uploads whatever is on disk regardless of git status, so the S3 copy
    # can legitimately be the only copy that exists.
    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    (src / "labs-export.jsonl").write_text("", encoding="utf-8")

    s3 = FakeS3Client()
    run_backup(src, "bucket", s3)

    dst = tmp_path / "restored-data"
    report = restore_from_bucket("bucket", dst, s3_client=s3)

    assert report.warnings == []
    assert (dst / "labs-export.jsonl").is_file()
    assert report.lab_rows_rebuilt == 0


def test_restore_skips_an_s3_directory_marker_key(tmp_path: Path) -> None:
    # Some tools (e.g. the S3 console) create a zero-byte object exactly at
    # the prefix itself ("latest/sources/") as a folder placeholder; it
    # must not be treated as a file to restore.
    src = tmp_path / "src-data"
    DataRepo.init_at(src)
    _seed_with_labs(src)

    s3 = FakeS3Client()
    run_backup(src, "bucket", s3)
    s3._contents[SOURCES_PREFIX] = b""

    dst = tmp_path / "restored-data"
    report = restore_from_bucket("bucket", dst, s3_client=s3)

    assert report.sources_restored == 3  # the placeholder key itself is not counted


def test_failed_restore_leaves_data_dir_absent(tmp_path: Path) -> None:
    """A partial restore must never occupy data_dir (real incident: a
    mid-restore failure left a bare clone that the next boot's is-empty
    check then skipped, serving a half-restored case file)."""
    src_dir = tmp_path / "source-repo"
    DataRepo.init_at(src_dir)
    _seed_with_labs(src_dir)
    s3 = FakeS3Client()
    run_backup(src_dir, "bkt", s3)

    real_download = s3.download_file

    def _boom(Bucket: str, Key: str, Filename: str) -> None:
        if "sources/" in Key:
            raise RuntimeError("network died mid-sources")
        return real_download(Bucket=Bucket, Key=Key, Filename=Filename)

    s3.download_file = _boom  # type: ignore[method-assign]
    dest = tmp_path / "restored"

    with pytest.raises(RuntimeError, match="mid-sources"):
        restore_from_bucket("bkt", dest, s3_client=s3)

    assert not dest.exists()
    assert not (tmp_path / "restored.restore-staging").exists()

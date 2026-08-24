"""Tests for adoc.backup: git-bundle + sources/ + labs-export.jsonl -> S3.

Uses a small in-memory fake S3 client (matching the `S3Client` protocol)
instead of `moto` or a real `boto3` client, per the injection-seam
preference documented in `adoc.backup`'s module docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adoc.backup import BackupError, run_backup
from adoc.casefile.repo import DataRepo


class FakeS3Client:
    """Records uploads; `list_objects_v2` reflects what has been uploaded
    so far, matching real S3 list-after-upload semantics closely enough
    for the size-based skip-unchanged check in `_sync_sources`."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str]] = []  # (local_path, bucket, key)
        self._sizes: dict[str, int] = {}

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
        self.uploads.append((Filename, Bucket, Key))
        self._sizes[Key] = Path(Filename).stat().st_size

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, Any]:
        contents = [
            {"Key": key, "Size": size}
            for key, size in self._sizes.items()
            if key.startswith(Prefix)
        ]
        return {"Contents": contents}


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

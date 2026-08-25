"""Tests for `deploy/container/run-ingest.sh` (Defect fix, live blocker):

The Dropbox puller used to filter on `*.pdf` only, so a patient's `.docx`
narrative dropped in `a-doc-inbox/NarrativeSummary/` was never pulled even
though the ingestion pipeline fully supports it
(`adoc.ingest.filetypes.detect_intake_kind`). These are plain text/grep
checks of the shipped script - no subprocess execution, no real rclone.
"""

from __future__ import annotations

from pathlib import Path

from adoc.ingest.filetypes import (
    _GENOMIC_GZ_SUFFIXES,
    _GENOMIC_PLAIN_FASTQ_SUFFIXES,
)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "deploy" / "container" / "run-ingest.sh"


def _script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_min_age_guard_is_preserved() -> None:
    assert "--min-age 1m" in _script_text()


def test_includes_every_extension_detect_intake_kind_can_classify() -> None:
    """Derived from `detect_intake_kind`'s own suffix lists (never
    hand-guessed) so this can't silently drift from what the pipeline
    actually supports: pdf, docx, text (.txt/.md), zip, plain vcf, and
    every genomic gz/fastq suffix."""
    text = _script_text()
    expected_patterns = {
        "*.pdf",
        "*.docx",
        "*.txt",
        "*.md",
        "*.zip",
        "*.vcf",
        *(f"*{suffix}" for suffix in _GENOMIC_GZ_SUFFIXES),
        *(f"*{suffix}" for suffix in _GENOMIC_PLAIN_FASTQ_SUFFIXES),
    }
    for pattern in sorted(expected_patterns):
        assert f'--include "{pattern}"' in text, f"missing rclone --include for {pattern!r}"


def test_docx_narrative_pattern_present_for_the_live_incident() -> None:
    # The specific file class that was silently dropped: a-doc-inbox/
    # NarrativeSummary/*.docx (one level deep -- see the script's own
    # comment on why an unanchored rclone --include pattern still matches).
    assert '--include "*.docx"' in _script_text()


def test_comments_and_echo_no_longer_claim_pdf_only() -> None:
    text = _script_text()
    assert "PDFs" not in text

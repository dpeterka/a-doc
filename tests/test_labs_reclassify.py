"""Tests for adoc.labs.reclassify: retro-reclassification of already-PENDING
rows under the current semantic reconcile comparators (`adoc
labs-reclassify`, feature/semantic-compare).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.labs.reclassify import reclassify_pending

SHA = "c" * 64


def _extracted(
    *,
    name_raw: str = "Potassium",
    value: float | None = 4.1,
    value_text: str | None = None,
    unit_raw: str | None = "mmol/L",
    ref_range_raw: str | None = "3.5-5.1",
    flag_raw: str | None = None,
    specimen: str = "unknown",
    page: int = 1,
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "name_raw": name_raw,
        "value": value,
        "value_text": value_text,
        "unit_raw": unit_raw,
        "ref_range_raw": ref_range_raw,
        "flag_raw": flag_raw,
        "specimen": specimen,
        "page": page,
        "confidence": confidence,
    }


def _pending_row(
    *,
    pass_a: dict[str, Any] | None,
    pass_b: dict[str, Any] | None,
    reasons: list[str],
    lab_date: date = date(2026, 5, 2),
    name_raw: str = "Potassium",
    value: float | None = 4.1,
) -> LabResult:
    payload = {"pass_a": pass_a, "pass_b": pass_b, "reasons": reasons}
    return LabResult(
        date=lab_date,
        name=name_raw.lower(),
        name_raw=name_raw,
        value=value,
        source_doc=SHA,
        extraction_status=ExtractionStatus.PENDING,
        raw_json=json.dumps(payload),
    )


def _seed_document(db: LabsDb) -> None:
    db.upsert_document(
        LabDocument(
            sha256=SHA,
            filename="doc.pdf",
            doc_type="lab_report",
            page_count=1,
            status=DocumentStatus.COMPLETE,
        )
    )


# --------------------------------------------------------------------------
# Comparator false-positives: recompute to zero reasons -> auto-flip.
# --------------------------------------------------------------------------


def test_flips_false_ref_range_mismatch_to_auto(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    pass_a = _extracted(ref_range_raw="<20")
    pass_b = _extracted(ref_range_raw="<20 Units")
    (row_id,) = db.insert_results(
        [
            _pending_row(
                pass_a=pass_a,
                pass_b=pass_b,
                reasons=["ref_range_mismatch: '<20' vs '<20 Units'"],
            )
        ]
    )
    assert row_id is not None

    report = reclassify_pending(db)

    assert report.checked == 1
    assert report.auto_flipped == 1
    assert report.rewritten == 1
    assert report.auto_flipped_ids == [row_id]

    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.AUTO
    payload = row.raw_payload()
    assert payload["reasons"] == []
    assert payload["previous_reasons"] == ["ref_range_mismatch: '<20' vs '<20 Units'"]
    assert "reclassified_at" in payload
    # original extraction payloads preserved verbatim
    assert payload["pass_a"]["ref_range_raw"] == "<20"
    assert payload["pass_b"]["ref_range_raw"] == "<20 Units"


def test_flips_false_unit_mismatch_to_auto(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    pass_a = _extracted(name_raw="RBC", value=4.5, unit_raw="M/uL", ref_range_raw="3.80-5.10")
    pass_b = _extracted(
        name_raw="RBC", value=4.5, unit_raw="Million/uL", ref_range_raw="3.80 - 5.10 Million/uL"
    )
    (row_id,) = db.insert_results(
        [
            _pending_row(
                name_raw="RBC",
                value=4.5,
                pass_a=pass_a,
                pass_b=pass_b,
                reasons=[
                    "unit_mismatch: 'M/uL' vs 'Million/uL'",
                    "ref_range_mismatch: '3.80-5.10' vs '3.80 - 5.10 Million/uL'",
                ],
            )
        ]
    )
    assert row_id is not None

    report = reclassify_pending(db)

    assert report.auto_flipped == 1
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.AUTO


def test_flips_false_flag_mismatch_to_auto(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    pass_a = _extracted(flag_raw=None)
    pass_b = _extracted(flag_raw="")
    (row_id,) = db.insert_results(
        [
            _pending_row(
                pass_a=pass_a,
                pass_b=pass_b,
                reasons=["flag_mismatch: None vs ''"],
            )
        ]
    )
    assert row_id is not None

    report = reclassify_pending(db)

    assert report.auto_flipped == 1
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.AUTO


# --------------------------------------------------------------------------
# Genuine disagreements stay pending, untouched.
# --------------------------------------------------------------------------


def test_leaves_genuine_value_mismatch_pending_and_untouched(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    pass_a = _extracted(value=8.0)
    pass_b = _extracted(value=9.0)
    original_reasons = ["value_mismatch: 8.0 vs 9.0"]
    (row_id,) = db.insert_results(
        [_pending_row(pass_a=pass_a, pass_b=pass_b, reasons=original_reasons, value=8.0)]
    )
    assert row_id is not None
    before = db.get_row(row_id)
    assert before is not None

    report = reclassify_pending(db)

    assert report.checked == 1
    assert report.auto_flipped == 0
    assert report.still_disagreed == 1
    assert report.rewritten == 0  # recomputed reasons are identical - untouched

    after = db.get_row(row_id)
    assert after is not None
    assert after.extraction_status == ExtractionStatus.PENDING
    assert after.raw_json == before.raw_json
    assert "reclassified_at" not in after.raw_payload()


def test_a_real_unit_mismatch_is_not_flipped(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    # An unmapped analyte name (no `ANALYTE_SPECS` entry) so this test
    # isolates the unit comparator alone - no unrelated unit-whitelist
    # `validate_row` issues muddy the recomputed reason list.
    pass_a = _extracted(name_raw="some unmapped analyte", unit_raw="mg/dL")
    pass_b = _extracted(name_raw="some unmapped analyte", unit_raw="g/dL")
    (row_id,) = db.insert_results(
        [
            _pending_row(
                name_raw="some unmapped analyte",
                pass_a=pass_a,
                pass_b=pass_b,
                reasons=["unit_mismatch: 'mg/dL' vs 'g/dL'"],
            )
        ]
    )
    assert row_id is not None

    report = reclassify_pending(db)

    assert report.still_disagreed == 1
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING


# --------------------------------------------------------------------------
# Rows untouched entirely: single_pass / name_variant / missing payloads.
# --------------------------------------------------------------------------


def test_single_pass_rows_are_never_touched(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    (row_id,) = db.insert_results(
        [
            _pending_row(
                pass_a=_extracted(),
                pass_b=None,
                reasons=["single_pass"],
            )
        ]
    )
    assert row_id is not None

    report = reclassify_pending(db)

    assert report.checked == 0
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING
    assert row.raw_payload()["reasons"] == ["single_pass"]


def test_name_variant_rescued_pairs_are_never_touched(tmp_path: Path) -> None:
    """Rescued pairs (`ingest.reconcile._reconcile_rescued_pair`) carry BOTH
    payloads but deliberately skip the field-comparison gates - recomputing
    them here would manufacture disagreement reasons a human never needed
    to see."""
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    pass_a = _extracted(name_raw="FRAX 10-year probability of hip fracture", unit_raw=None)
    pass_b = _extracted(name_raw="10-year probability of hip fracture", unit_raw=None)
    (row_id,) = db.insert_results(
        [
            _pending_row(
                pass_a=pass_a,
                pass_b=pass_b,
                reasons=["name_variant"],
            )
        ]
    )
    assert row_id is not None

    report = reclassify_pending(db)

    assert report.checked == 0
    row = db.get_row(row_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING
    assert row.raw_payload()["reasons"] == ["name_variant"]


# --------------------------------------------------------------------------
# --dry-run / idempotency
# --------------------------------------------------------------------------


def test_dry_run_mutates_nothing(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    pass_a = _extracted(ref_range_raw="<20")
    pass_b = _extracted(ref_range_raw="<20 Units")
    (row_id,) = db.insert_results(
        [
            _pending_row(
                pass_a=pass_a,
                pass_b=pass_b,
                reasons=["ref_range_mismatch: '<20' vs '<20 Units'"],
            )
        ]
    )
    assert row_id is not None
    before = db.get_row(row_id)
    assert before is not None

    report = reclassify_pending(db, dry_run=True)

    assert report.auto_flipped == 1
    after = db.get_row(row_id)
    assert after is not None
    assert after.extraction_status == ExtractionStatus.PENDING  # unmutated
    assert after.raw_json == before.raw_json


def test_reclassify_is_idempotent(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    _seed_document(db)
    pass_a = _extracted(ref_range_raw="<20")
    pass_b = _extracted(ref_range_raw="<20 Units")
    db.insert_results(
        [
            _pending_row(
                pass_a=pass_a,
                pass_b=pass_b,
                reasons=["ref_range_mismatch: '<20' vs '<20 Units'"],
            )
        ]
    )
    real_pass_a = _extracted(name_raw="Potassium (repeat)", value=8.0)
    real_pass_b = _extracted(name_raw="Potassium (repeat)", value=9.0)
    db.insert_results(
        [
            _pending_row(
                name_raw="Potassium (repeat)",
                pass_a=real_pass_a,
                pass_b=real_pass_b,
                reasons=["value_mismatch: 8.0 vs 9.0"],
                value=8.0,
            )
        ]
    )

    first = reclassify_pending(db)
    second = reclassify_pending(db)

    assert first.auto_flipped == 1
    assert first.still_disagreed == 1
    # the auto-flipped row is no longer PENDING at all on the second pass
    assert second.auto_flipped == 0
    assert second.checked == 1
    assert second.still_disagreed == 1
    assert second.rewritten == 0

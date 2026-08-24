"""Tests for adoc.labs.twins: the legacy single-pass PENDING row twin
sweep (queue-ergonomics slice item 4, `adoc labs-dedupe-twins`).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from adoc.config import ModelBinding
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult
from adoc.labs.twins import (
    find_candidate,
    names_equivalent_by_rule,
    read_last_sweep_summary,
    sweep_twins,
    write_sweep_summary,
)
from adoc.reason.client import (
    AnthropicProvider,
    LlmClient,
    TransportRequest,
    TransportResponse,
)

SHA = "a" * 64
SHA_OTHER = "b" * 64


def _doc(sha256: str = SHA) -> LabDocument:
    return LabDocument(
        sha256=sha256,
        filename="doc.pdf",
        doc_type="lab_report",
        page_count=6,
        status=DocumentStatus.COMPLETE,
    )


def _row(
    name_raw: str,
    *,
    value: float | None = None,
    value_text: str | None = None,
    unit_raw: str | None = None,
    specimen: str = "unknown",
    page: int = 4,
    source_doc: str = SHA,
    status: ExtractionStatus = ExtractionStatus.AUTO,
    reasons: list[str] | None = None,
) -> LabResult:
    raw_json = json.dumps({"reasons": reasons or []})
    return LabResult(
        date=date(2026, 5, 2),
        name=name_raw.lower(),
        name_raw=name_raw,
        value=value,
        value_text=value_text,
        ucum_unit=unit_raw,
        specimen=specimen,  # type: ignore[arg-type]
        source_doc=source_doc,
        source_page=page,
        extraction_status=status,
        raw_json=raw_json,
    )


def _client(same_measurement: bool | None) -> LlmClient:
    """A fake `LlmClient` whose `classifier` role responds with
    `same_measurement`. `None` makes the transport explode - proof the
    LLM must never be called for a case the rule path or the candidate
    gate already decided."""

    def _transport(request: TransportRequest) -> TransportResponse:
        if same_measurement is None:
            raise AssertionError("the LLM transport must not be called in this test")
        return TransportResponse(
            text="",
            tool_input={"same_measurement": same_measurement},
            input_tokens=5,
            output_tokens=5,
        )

    bindings: dict[str, list[ModelBinding]] = {
        "classifier": [ModelBinding(provider="anthropic", model="fake-haiku")]
    }
    providers = {"anthropic": AnthropicProvider(api_key=None, transport=_transport)}
    return LlmClient(bindings, providers)


# --------------------------------------------------------------------------
# names_equivalent_by_rule / find_candidate: pure deterministic functions
# --------------------------------------------------------------------------


def test_names_equivalent_by_rule_matches_on_token_subset() -> None:
    assert names_equivalent_by_rule("T-Score", "LEFT HIP femoral neck T-Score") is True


def test_names_equivalent_by_rule_matches_after_casefold_and_cleaning() -> None:
    assert (
        names_equivalent_by_rule(
            "frax 10-year probability of hip fracture is",
            "FRAX 10-year probability of hip fracture",
        )
        is True
    )


def test_names_equivalent_by_rule_rejects_unrelated_names() -> None:
    assert names_equivalent_by_rule("Potassium", "Sodium") is False


def test_find_candidate_returns_none_when_value_differs(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results([_row("Potassium", value=4.1, status=ExtractionStatus.AUTO)])
    (pending_id,) = db.insert_results(
        [_row("K+", value=41.0, status=ExtractionStatus.PENDING, reasons=["single_pass"])]
    )
    pending = db.get_row(pending_id)  # type: ignore[arg-type]
    assert pending is not None
    assert find_candidate(db, pending) is None


# --------------------------------------------------------------------------
# sweep_twins: rule path, LLM path, value-mismatch never-sweeps, dry-run,
# idempotency.
# --------------------------------------------------------------------------


def test_sweep_rejects_a_rule_path_twin(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results(
        [
            _row(
                "LEFT HIP femoral neck T-Score",
                value=-1.2,
                page=5,
                status=ExtractionStatus.AUTO,
            )
        ]
    )
    (pending_id,) = db.insert_results(
        [
            _row(
                "T-Score",
                value=-1.2,
                page=5,
                status=ExtractionStatus.PENDING,
                reasons=["single_pass"],
            )
        ]
    )
    assert pending_id is not None

    report = sweep_twins(db, _client(None))  # LLM must never be called: rule path decides it

    assert report.checked == 1
    assert report.rejected == 1
    assert report.rejected_rule == 1
    assert report.rejected_llm == 0
    assert report.rejected_ids == [pending_id]
    row = db.get_row(pending_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.REJECTED
    payload = row.raw_payload()
    assert payload["method"] == "rule"
    assert "auto_rejected_twin_of" in payload


def test_sweep_rejects_an_llm_path_twin(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    (resolved_id,) = db.insert_results(
        [
            _row(
                "FRAX 10-year probability of hip fracture",
                value=None,
                value_text="12%",
                page=4,
                status=ExtractionStatus.AUTO,
            )
        ]
    )
    (pending_id,) = db.insert_results(
        [
            _row(
                "annual fracture risk estimate",  # no token overlap - rule path can't decide
                value=None,
                value_text="12%",
                page=4,
                status=ExtractionStatus.PENDING,
                reasons=["single_pass"],
            )
        ]
    )
    assert resolved_id is not None and pending_id is not None

    report = sweep_twins(db, _client(True))

    assert report.rejected == 1
    assert report.rejected_rule == 0
    assert report.rejected_llm == 1
    row = db.get_row(pending_id)
    assert row is not None
    assert row.extraction_status == ExtractionStatus.REJECTED
    assert row.raw_payload()["method"] == "llm"
    assert row.raw_payload()["auto_rejected_twin_of"] == resolved_id


def test_sweep_leaves_row_untouched_when_llm_says_different_measurement(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results(
        [
            _row(
                "annual fracture risk estimate B",
                value_text="12%",
                page=4,
                status=ExtractionStatus.AUTO,
            )
        ]
    )
    (pending_id,) = db.insert_results(
        [
            _row(
                "unrelated other reading",
                value_text="12%",
                page=4,
                status=ExtractionStatus.PENDING,
                reasons=["single_pass"],
            )
        ]
    )

    report = sweep_twins(db, _client(False))

    assert report.rejected == 0
    row = db.get_row(pending_id)  # type: ignore[arg-type]
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING


def test_sweep_never_touches_a_row_whose_value_genuinely_differs(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results([_row("Potassium", value=4.1, page=1, status=ExtractionStatus.AUTO)])
    # A different name_raw (so it doesn't collide with the row above on the
    # UNIQUE(date, name, specimen, source_doc) constraint) - realistic
    # anyway, since a genuine value mismatch would come from two
    # differently-worded extraction rows, not a literal re-insert.
    (pending_id,) = db.insert_results(
        [
            _row(
                "Potassium (repeat)",
                value=41.0,
                page=1,
                status=ExtractionStatus.PENDING,
                reasons=["single_pass"],
            )
        ]
    )

    # LLM transport explodes if called at all - a value mismatch must never
    # even reach the name-equivalence step.
    report = sweep_twins(db, _client(None))

    assert report.checked == 1
    assert report.rejected == 0
    row = db.get_row(pending_id)  # type: ignore[arg-type]
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING


def test_sweep_ignores_pending_rows_without_single_pass_reason(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results([_row("Potassium", value=4.1, page=1, status=ExtractionStatus.AUTO)])
    (pending_id,) = db.insert_results(
        [
            _row(
                "Potassium Level",
                value=4.1,
                page=1,
                status=ExtractionStatus.PENDING,
                reasons=["missing_date"],
            )
        ]
    )

    report = sweep_twins(db, _client(None))

    assert report.checked == 0
    assert report.rejected == 0
    row = db.get_row(pending_id)  # type: ignore[arg-type]
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING


def test_sweep_dry_run_mutates_nothing(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results(
        [_row("LEFT HIP femoral neck T-Score", value=-1.2, page=5, status=ExtractionStatus.AUTO)]
    )
    (pending_id,) = db.insert_results(
        [
            _row(
                "T-Score",
                value=-1.2,
                page=5,
                status=ExtractionStatus.PENDING,
                reasons=["single_pass"],
            )
        ]
    )

    report = sweep_twins(db, _client(None), dry_run=True)

    assert report.rejected == 1
    assert report.rejected_ids == [pending_id]
    row = db.get_row(pending_id)  # type: ignore[arg-type]
    assert row is not None
    assert row.extraction_status == ExtractionStatus.PENDING  # unmutated


def test_sweep_is_idempotent(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results(
        [_row("LEFT HIP femoral neck T-Score", value=-1.2, page=5, status=ExtractionStatus.AUTO)]
    )
    db.insert_results(
        [
            _row(
                "T-Score",
                value=-1.2,
                page=5,
                status=ExtractionStatus.PENDING,
                reasons=["single_pass"],
            )
        ]
    )

    first = sweep_twins(db, _client(None))
    second = sweep_twins(db, _client(None))

    assert first.rejected == 1
    assert second.rejected == 0
    assert second.checked == 0  # the row is no longer PENDING at all


# --------------------------------------------------------------------------
# write_sweep_summary / read_last_sweep_summary
# --------------------------------------------------------------------------


def test_sweep_summary_round_trips(tmp_path: Path) -> None:
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(_doc())
    db.insert_results(
        [_row("LEFT HIP femoral neck T-Score", value=-1.2, page=5, status=ExtractionStatus.AUTO)]
    )
    db.insert_results(
        [
            _row(
                "T-Score",
                value=-1.2,
                page=5,
                status=ExtractionStatus.PENDING,
                reasons=["single_pass"],
            )
        ]
    )
    report = sweep_twins(db, _client(None))

    write_sweep_summary(tmp_path, report, at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC))

    summary = read_last_sweep_summary(tmp_path)
    assert summary is not None
    assert summary["rejected"] == 1
    assert summary["checked"] == 1


def test_read_last_sweep_summary_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_last_sweep_summary(tmp_path) is None

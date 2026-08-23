"""Extraction eval suite (PLAN.md "Self-evaluation": "golden extraction
fixtures ... field-level F1").

Replays every `tests/fixtures/extractions/*.json` fixture (an
`{"pass_a": DocumentExtraction, "pass_b": DocumentExtraction}` pair — the
same fixtures `tests/test_ingest_pipeline.py` uses) through the real,
deterministic `ingest.reconcile.reconcile`. No model call, no network:
reconciliation is pure code. Scored against `manifest.json` (declared
next to the fixtures): canonical-name accuracy, and an AUTO/PENDING
confusion matrix (precision/recall on `"auto"` as the positive class).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from adoc.evals.runner import ClientFactory, SuiteCaseResult, SuiteMetric, SuiteResult
from adoc.ingest.reconcile import reconcile
from adoc.ingest.schema import DocumentExtraction
from adoc.labs.db import LabsDb

FIXTURES_DIR = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "extractions"
MANIFEST_NAME = "manifest.json"

ReconcileStatus = Literal["auto", "pending"]


class ExpectedRow(BaseModel):
    """One manifest-declared expected outcome for a fixture's reconciled row."""

    name_raw: str
    canonical_name: str | None
    status: ReconcileStatus


def _load_manifest(fixtures_dir: Path) -> dict[str, list[ExpectedRow]]:
    manifest_path = fixtures_dir / MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        fixture_name: [ExpectedRow.model_validate(row) for row in rows]
        for fixture_name, rows in payload.items()
    }


def _load_fixture(path: Path) -> tuple[DocumentExtraction, DocumentExtraction]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return (
        DocumentExtraction.model_validate(payload["pass_a"]),
        DocumentExtraction.model_validate(payload["pass_b"]),
    )


def run(
    *,
    client_factory: ClientFactory,
    candidate: str | None = None,
    fixtures_dir: Path | None = None,
) -> SuiteResult:
    """Replay `fixtures_dir` (default `tests/fixtures/extractions/`)
    through `reconcile()` and score against `manifest.json`.

    `client_factory` is accepted for dispatch-signature uniformity with
    `evals.runner.Suite` (see that module's docstring) but never called —
    reconciliation never invokes a model. `candidate` is recorded on the
    returned `SuiteResult.binding_label` only; it cannot change this
    suite's outcome since there is no model call to override.
    """
    del client_factory
    directory = fixtures_dir if fixtures_dir is not None else FIXTURES_DIR
    manifest = _load_manifest(directory)

    cases: list[SuiteCaseResult] = []
    name_total = 0
    name_correct = 0
    true_positive = 0  # expected auto, actual auto
    false_positive = 0  # expected pending, actual auto
    false_negative = 0  # expected auto, actual pending
    true_negative = 0  # expected pending, actual pending

    db = LabsDb(":memory:")
    for fixture_name, expected_rows in sorted(manifest.items()):
        pass_a, pass_b = _load_fixture(directory / fixture_name)
        actual_rows = reconcile(pass_a, pass_b, db)
        actual_by_name = {row.name_raw: row for row in actual_rows}

        case_ok = True
        detail_parts: list[str] = []
        for expected in expected_rows:
            actual = actual_by_name.get(expected.name_raw)
            if actual is None:
                case_ok = False
                detail_parts.append(f"{expected.name_raw}: expected a row, got none")
                continue

            name_total += 1
            if actual.canonical_name == expected.canonical_name:
                name_correct += 1
            else:
                case_ok = False
                detail_parts.append(
                    f"{expected.name_raw}: canonical_name expected="
                    f"{expected.canonical_name!r} actual={actual.canonical_name!r}"
                )

            expected_auto = expected.status == "auto"
            actual_auto = actual.status == "auto"
            if expected_auto and actual_auto:
                true_positive += 1
            elif expected_auto and not actual_auto:
                false_negative += 1
                case_ok = False
                detail_parts.append(f"{expected.name_raw}: expected auto, got pending")
            elif actual_auto:
                false_positive += 1
                case_ok = False
                detail_parts.append(f"{expected.name_raw}: expected pending, got auto")
            else:
                true_negative += 1

        if len(actual_rows) != len(expected_rows):
            case_ok = False
            detail_parts.append(
                f"row count mismatch: expected {len(expected_rows)}, got {len(actual_rows)}"
            )

        cases.append(
            SuiteCaseResult(case_id=fixture_name, passed=case_ok, detail="; ".join(detail_parts))
        )

    auto_calls = true_positive + false_positive
    auto_actual = true_positive + false_negative
    precision = true_positive / auto_calls if auto_calls else 1.0
    recall = true_positive / auto_actual if auto_actual else 1.0
    canonical_accuracy = name_correct / name_total if name_total else 1.0

    metrics = [
        SuiteMetric(name="canonical_name_accuracy", value=canonical_accuracy),
        SuiteMetric(name="auto_precision", value=precision),
        SuiteMetric(name="auto_recall", value=recall),
        SuiteMetric(name="auto_true_positive", value=float(true_positive)),
        SuiteMetric(name="auto_false_positive", value=float(false_positive)),
        SuiteMetric(name="auto_false_negative", value=float(false_negative)),
        SuiteMetric(name="pending_true_negative", value=float(true_negative)),
    ]

    binding_label = candidate or "n/a (deterministic replay, no model call)"
    return SuiteResult(
        suite="extraction", binding_label=binding_label, cases=cases, metrics=metrics
    )


__all__ = ["ExpectedRow", "run"]

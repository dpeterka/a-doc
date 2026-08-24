"""Confirm queue surface (PLAN.md "Ingestion"): pending rows triaged into
a "models agreed" bucket (needs a quick OK) and a "models disagreed"
bucket (needs your eyes), grouped by source document, each row shown
beside its source page image with Confirm / Correct / Reject actions
wired to `LabsDb`. Every action — including the two bulk-confirm ones —
re-exports `labs-export.jsonl` and makes exactly one data-repo commit,
per PLAN.md "State" (sqlite is derived; the JSONL export + git history is
the record of truth).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any, Literal

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.ingest.reconcile import row_is_agreed
from adoc.labs.db import LabsDb, PendingRow, ResolutionConvergedError
from adoc.labs.models import LabFlag, LabResult
from adoc.labs.twins import read_last_sweep_summary
from adoc.labs.validate import ANALYTE_SPECS, canonicalize
from adoc.web.casefile_helpers import page_image_url
from adoc.web.deps import get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/confirm")

LABS_EXPORT_RELPATH = "labs-export.jsonl"

_NOT_READ = "not read on this pass"

# One-line, patient-facing gloss for the "agreed" bucket's single-source
# reasons. `reconcile.py` appends the literal string `"missing_date"`,
# but `labs.validate.ValidationIssue.message` is free text keyed on the
# analyte's canonical name (e.g. "CRP: unit 'mg/dL' not in whitelist
# ('mg/L',)") rather than its `IssueCode`, so these match on a
# characteristic substring of each `validate_row`/`trend_outlier`
# message shape instead of a prefix.
_FRIENDLY_REASON_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("not in whitelist", "the unit isn't one we recognize yet for this test"),
    ("outside plausible bounds", "the value is outside what's typical for this test"),
    ("does not match titer format", "the titer wasn't written in the usual format"),
    (
        "ref_range_single_source",
        "only one reading captured the reference range - we kept the one provided",
    ),
    (
        "away from the median",
        "this value is far outside the pattern of your earlier readings",
    ),
    ("but value", "the H/L flag doesn't quite match the value"),
    (
        "name_variant_unverified",
        "two readings matched by value but their names differ a lot - check they are the same test",
    ),
)

# Reason prefixes (see `ingest.reconcile`) that name which field of a
# matched pair disagreed — used to highlight that field in the
# side-by-side pass comparison.
_REASON_TO_DIFF_FIELD: dict[str, str] = {
    "value_mismatch": "value",
    "value_text_mismatch": "value_text",
    "unit_mismatch": "unit_raw",
    "ref_range_mismatch": "ref_range_raw",
    "flag_mismatch": "flag_raw",
    "specimen_mismatch": "specimen",
}

_COMPARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("value", "Value"),
    ("unit_raw", "Unit"),
    ("ref_range_raw", "Reference range"),
    ("flag_raw", "Flag"),
    ("specimen", "Specimen"),
)


@dataclass
class DocumentGroup:
    """One document's PENDING rows within a confirm-queue bucket."""

    sha: str
    filename: str
    doc_date: date_cls | None
    doc_type: str
    page_count: int
    pending_count: int
    done_count: int
    rows: list[dict[str, Any]] = field(default_factory=list)


def _friendly_reason(reason: str) -> str:
    if reason == "missing_date":
        return "we couldn't find a date on this document"
    for substring, friendly in _FRIENDLY_REASON_SUBSTRINGS:
        if substring in reason:
            return friendly
    return "a routine check flagged this for a second look"


def _diff_fields_from_reasons(reasons: list[str]) -> set[str]:
    diffs: set[str] = set()
    for reason in reasons:
        prefix = reason.split(":", 1)[0]
        mapped = _REASON_TO_DIFF_FIELD.get(prefix)
        if mapped:
            diffs.add(mapped)
    return diffs


def _pass_field(pass_data: dict[str, Any] | None, field_name: str) -> Any:
    if pass_data is None:
        return _NOT_READ
    return pass_data.get(field_name) or _NOT_READ


def _pass_value(pass_data: dict[str, Any] | None) -> Any:
    if pass_data is None:
        return _NOT_READ
    if pass_data.get("value") is not None:
        return pass_data["value"]
    return pass_data.get("value_text") or _NOT_READ


def _compare_rows(
    pass_a: dict[str, Any] | None, pass_b: dict[str, Any] | None, diff_fields: set[str]
) -> list[dict[str, Any]]:
    """Side-by-side pass-A/pass-B display rows for a disagreement row,
    each flagged `diff` if that field is one of the mismatch reasons."""
    rows: list[dict[str, Any]] = []
    for field_name, label in _COMPARE_FIELDS:
        if field_name == "value":
            a_display, b_display = _pass_value(pass_a), _pass_value(pass_b)
            is_diff = "value" in diff_fields or "value_text" in diff_fields
        else:
            a_display, b_display = _pass_field(pass_a, field_name), _pass_field(pass_b, field_name)
            is_diff = field_name in diff_fields
        rows.append({"label": label, "a": a_display, "b": b_display, "diff": is_diff})
    return rows


def _row_reasons(pr: PendingRow) -> list[str]:
    reasons: list[str] = pr.row.raw_payload().get("reasons", [])
    return reasons


def _is_score_row(row: LabResult) -> bool:
    """True for a FRAX/T-score/Z-score-shaped row (queue-ergonomics slice
    item 2): either its canonical spec is `kind="score"`, or - for an
    analyte not (yet) in `ANALYTE_SPECS` at all - it simply has a value
    but no unit and no reference range, the same shape. Drives the
    confirm-row template's "Calculated score — no reference range
    applies" note in place of blank unit/range lines."""
    canonical = canonicalize(row.name) or row.name
    spec = ANALYTE_SPECS.get(canonical)
    if spec is not None:
        return spec.kind == "score"
    return row.value is not None and row.ucum_unit is None and row.ref_text is None


def _row_view(repo: DataRepo, pr: PendingRow, *, agreed: bool) -> dict[str, Any]:
    payload = pr.row.raw_payload()
    reasons: list[str] = payload.get("reasons", [])
    pass_a: dict[str, Any] | None = payload.get("pass_a")
    pass_b: dict[str, Any] | None = payload.get("pass_b")
    diff_fields = _diff_fields_from_reasons(reasons)
    return {
        "row": pr.row,
        "image_url": page_image_url(repo, pr.row.source_doc, pr.row.source_page),
        "agreed": agreed,
        "friendly_reason": _friendly_reason(reasons[0]) if reasons else None,
        "compare_rows": _compare_rows(pass_a, pass_b, diff_fields),
        "single_pass": pass_a is None or pass_b is None,
        # Which pass(es) actually have a reading to apply - drives whether
        # the disagreement bucket's "Use reading A"/"Use reading B"
        # buttons render at all (queue-ergonomics slice item 1): a
        # single_pass row only ever has one of the two.
        "has_pass_a": pass_a is not None,
        "has_pass_b": pass_b is not None,
        "is_score": _is_score_row(pr.row),
    }


def _build_groups(
    repo: DataRepo,
    items: list[PendingRow],
    *,
    agreed: bool,
    doc_totals: dict[str, int],
    pending_totals: dict[str, int],
) -> list[DocumentGroup]:
    """Group `items` (already-classified rows, one bucket) by document,
    preserving `items`' own ordering across documents (callers rely on
    `LabsDb.pending_grouped()`'s document-date-descending order for the
    "disagreed" bucket)."""
    order: list[str] = []
    by_doc: dict[str, list[PendingRow]] = defaultdict(list)
    for pr in items:
        sha = pr.row.source_doc
        if sha not in by_doc:
            order.append(sha)
        by_doc[sha].append(pr)

    groups: list[DocumentGroup] = []
    for sha in order:
        prs = by_doc[sha]
        first = prs[0]
        total = doc_totals.get(sha, len(prs))
        total_pending = pending_totals.get(sha, len(prs))
        groups.append(
            DocumentGroup(
                sha=sha,
                filename=first.doc_filename,
                doc_date=first.doc_date,
                doc_type=first.doc_type,
                page_count=first.doc_page_count,
                pending_count=len(prs),
                done_count=max(total - total_pending, 0),
                rows=[_row_view(repo, pr, agreed=agreed) for pr in prs],
            )
        )
    return groups


def _twin_sweep_note(repo: DataRepo) -> str | None:
    """The confirm queue's dismissible "N duplicate readings were
    auto-resolved" note (queue-ergonomics slice item 4) - present only
    when the last `adoc labs-dedupe-twins` sweep actually rejected at
    least one row (`labs/twins.py`'s persisted `work/twin-sweep.json`)."""
    summary = read_last_sweep_summary(repo.root)
    if not summary:
        return None
    rejected = summary.get("rejected", 0)
    if not rejected:
        return None
    noun = "duplicate reading" if rejected == 1 else "duplicate readings"
    verb = "was" if rejected == 1 else "were"
    return f"{rejected} {noun} {verb} auto-resolved"


def _pending_context(repo: DataRepo, db: LabsDb, *, error: str | None = None) -> dict[str, Any]:
    items = db.pending_grouped()
    doc_totals = db.lab_counts_by_document()
    pending_totals: dict[str, int] = defaultdict(int)
    for pr in items:
        pending_totals[pr.row.source_doc] += 1

    agreed_items: list[PendingRow] = []
    disagreement_items: list[PendingRow] = []
    for pr in items:
        (agreed_items if row_is_agreed(_row_reasons(pr)) else disagreement_items).append(pr)

    return {
        "error": error,
        "twin_sweep_note": _twin_sweep_note(repo),
        "agreed_count": len(agreed_items),
        "agreed_groups": _build_groups(
            repo, agreed_items, agreed=True, doc_totals=doc_totals, pending_totals=pending_totals
        ),
        "disagreement_count": len(disagreement_items),
        "disagreement_groups": _build_groups(
            repo,
            disagreement_items,
            agreed=False,
            doc_totals=doc_totals,
            pending_totals=pending_totals,
        ),
    }


def _agreed_ids(items: list[PendingRow]) -> list[int]:
    return [pr.row.id for pr in items if pr.row.id is not None and row_is_agreed(_row_reasons(pr))]


def _export_and_commit(repo: DataRepo, db: LabsDb, message: str) -> None:
    db.export_jsonl(repo.root / LABS_EXPORT_RELPATH)
    repo.commit(message, paths=[LABS_EXPORT_RELPATH])


@router.get("")
def confirm_queue(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(request, "confirm.html", _pending_context(repo, db))


@router.post("/bulk-confirm-agreed")
def bulk_confirm_agreed(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    """Confirm every currently-agreed row across every document, in one
    commit — the queue's global "Confirm all agreed (N)" action."""
    ids = _agreed_ids(db.pending_grouped())
    confirmed = db.bulk_confirm(ids)
    if confirmed:
        _export_and_commit(repo, db, f"confirm: bulk-confirmed {confirmed} agreed rows")
    return templates.TemplateResponse(request, "_confirm_queue.html", _pending_context(repo, db))


@router.post("/documents/{sha}/bulk-confirm-agreed")
def bulk_confirm_agreed_for_document(
    request: Request,
    sha: str,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    """Confirm every currently-agreed row for one document, in one
    commit — the per-document "Confirm all agreed rows in this document"
    action."""
    ids = _agreed_ids([pr for pr in db.pending_grouped() if pr.row.source_doc == sha])
    confirmed = db.bulk_confirm(ids)
    if confirmed:
        _export_and_commit(repo, db, f"confirm: bulk-confirmed {confirmed} agreed rows")
    return templates.TemplateResponse(request, "_confirm_queue.html", _pending_context(repo, db))


@router.post("/{row_id}/confirm")
def confirm_row(
    request: Request,
    row_id: int,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    db.confirm_row(row_id)
    _export_and_commit(repo, db, f"confirm: row {row_id} confirmed")
    return templates.TemplateResponse(request, "_confirm_queue.html", _pending_context(repo, db))


@router.post("/{row_id}/resolve-pass/{which}")
def resolve_with_pass(
    request: Request,
    row_id: int,
    which: Literal["a", "b"],
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    """A disagreement row's "Use reading A"/"Use reading B" action
    (queue-ergonomics slice item 1): apply that pass's fields wholesale
    (`LabsDb.resolve_with_pass`) instead of silently keeping pass A's
    placeholder reading, as a bare Confirm used to."""
    try:
        db.resolve_with_pass(row_id, which)
        message = None
    except ResolutionConvergedError as exc:
        # The chosen reading already exists as another row of this document
        # (its unpaired twin) - the queue item was rejected as a duplicate.
        message = (
            "That reading already exists in this document's records - "
            "this queue item was marked as its duplicate."
        )
        _ = exc
    _export_and_commit(repo, db, f"confirm: row {row_id} resolved with pass {which.upper()}")
    context = _pending_context(repo, db)
    if message:
        context["notice"] = message
    return templates.TemplateResponse(request, "_confirm_queue.html", context)


@router.post("/{row_id}/reject")
def reject_row(
    request: Request,
    row_id: int,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    db.reject_row(row_id)
    _export_and_commit(repo, db, f"confirm: row {row_id} rejected")
    return templates.TemplateResponse(request, "_confirm_queue.html", _pending_context(repo, db))


def _parse_correction_fields(
    *,
    date: str | None,
    name: str | None,
    value: str | None,
    value_text: str | None,
    ucum_unit: str | None,
    ref_low: str | None,
    ref_high: str | None,
    flag: str | None,
    specimen: str | None,
) -> dict[str, Any]:
    """Build the `LabsDb.correct_row(**fields)` kwargs from the confirm form's
    (all-optional) inputs, skipping any left blank."""
    fields: dict[str, Any] = {}
    if date:
        fields["date"] = date_cls.fromisoformat(date)
    if name:
        fields["name"] = name
    if value:
        fields["value"] = float(value)
    if value_text:
        fields["value_text"] = value_text
    if ucum_unit:
        fields["ucum_unit"] = ucum_unit
    if ref_low:
        fields["ref_low"] = float(ref_low)
    if ref_high:
        fields["ref_high"] = float(ref_high)
    if flag:
        fields["flag"] = LabFlag(flag)
    if specimen:
        fields["specimen"] = specimen
    return fields


@router.post("/{row_id}/correct")
def correct_row(
    request: Request,
    row_id: int,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
    date: str = Form(""),
    name: str = Form(""),
    value: str = Form(""),
    value_text: str = Form(""),
    ucum_unit: str = Form(""),
    ref_low: str = Form(""),
    ref_high: str = Form(""),
    flag: str = Form(""),
    specimen: str = Form(""),
) -> Response:
    error: str | None = None
    try:
        fields = _parse_correction_fields(
            date=date or None,
            name=name or None,
            value=value or None,
            value_text=value_text or None,
            ucum_unit=ucum_unit or None,
            ref_low=ref_low or None,
            ref_high=ref_high or None,
            flag=flag or None,
            specimen=specimen or None,
        )
        if not fields:
            error = "Change at least one field before saving a correction."
        else:
            db.correct_row(row_id, **fields)
            _export_and_commit(repo, db, f"confirm: row {row_id} corrected")
    except ValueError as exc:
        error = f"Could not save that correction: {exc}"

    return templates.TemplateResponse(
        request, "_confirm_queue.html", _pending_context(repo, db, error=error)
    )

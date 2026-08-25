"""LLM-assisted twin sweep for legacy single-pass PENDING rows
(queue-ergonomics slice item 4).

Before `ingest/reconcile.py`'s RESCUE pass existed (item 3b of the same
slice), a document whose two extraction passes named the same measurement
differently (the real FRAX case: "FRAX 10-year probability of hip
fracture" vs. a sentence-fragment "10-year probability of hip fracture
is") could leave BOTH readings stranded as separate `single_pass` PENDING
rows - twins of each other that reconcile.py never had a chance to pair.
`adoc labs-dedupe-twins` (`sweep_twins` below) is the one-time/periodic
maintenance pass that finds and auto-rejects the duplicate half of such a
pair among rows already ingested before the RESCUE pass existed.

For each still-PENDING row whose `reasons` include `single_pass`:

  1. **Deterministic candidate gate** (`find_candidate`, NO LLM): a
     resolved (`auto`/`confirmed`/`corrected`) row in the SAME document,
     page within +/- `PAGE_TOLERANCE`, an identical value (or identical,
     normalized value_text), a compatible unit, and the same
     specimen-or-unknown. No candidate found -> the row is left untouched
     and NO LLM call is ever made - a genuinely different value never
     reaches step 2.
  2. **Name-equivalence check**, only once a candidate exists:
     a. deterministic first (`names_equivalent_by_rule`, NO LLM): after
        `ingest.reconcile.clean_result_name` + casefold, the two names
        are equal, or one's token set is a subset of the other's (e.g.
        "T-Score" subset-matches "LEFT HIP femoral neck T-Score").
     b. otherwise exactly ONE `LlmClient.complete` call, role
        `"classifier"`, asking only whether the two names denote the same
        measurement, given their shared value/page context.
  3. a twin (rule- or LLM-confirmed) is rejected via
     `LabsDb.reject_row_as_twin`, with an audit note recording which
     method decided it. A non-twin, or a row with no candidate at all, is
     left completely untouched.

`write_sweep_summary`/`read_last_sweep_summary` persist the outcome of the
most recent REAL (non-dry-run) sweep to `work/twin-sweep.json` under the
data repo root - `work/` is gitignored (`casefile.repo._GITIGNORE`), so
this is a local-only breadcrumb, never committed. The confirm-queue web
page (`web.routes.confirm`) reads it back to show a dismissible "N
duplicate readings were auto-resolved" note whenever the last sweep
rejected at least one row.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adoc.ingest.reconcile import clean_result_name
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.reason.client import LlmClient, LlmError, Message

PAGE_TOLERANCE = 1

TWIN_SWEEP_SUMMARY_RELPATH = "work/twin-sweep.json"

TWIN_CLASSIFY_PROMPT_VERSION = "twin-classifier-v1"
TWIN_CLASSIFY_PROMPT = f"""[{TWIN_CLASSIFY_PROMPT_VERSION}]
You are deciding whether two differently-worded result names, both
reporting the SAME value on the same (or an adjacent) page of the same
document, actually name the SAME underlying measurement - just transcribed
differently by two independent readings of the document - or whether they
are two genuinely different measurements that happen to share a value by
coincidence.

Answer same_measurement=true only if a clinician would agree the two names
refer to one and the same test/result. When genuinely unsure, answer
false: a false negative just leaves a row in the human review queue for a
person to resolve; a false positive would silently discard a real,
distinct result.
"""


class _SameMeasurement(BaseModel):
    same_measurement: bool


@dataclass
class TwinSweepReport:
    """Outcome of one `sweep_twins` run."""

    checked: int = 0
    rejected: int = 0
    rejected_rule: int = 0
    rejected_llm: int = 0
    rejected_ids: list[int] = field(default_factory=list)
    paired: int = 0  # pending<->pending retro-pairs (survivor upgraded to name_variant)


def _normalize_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    normalized = re.sub(r"\s+", " ", unit.strip().lower())
    return normalized or None


def _units_compatible(a: str | None, b: str | None) -> bool:
    na, nb = _normalize_unit(a), _normalize_unit(b)
    return na is None or nb is None or na == nb


def _specimen_compatible(a: str, b: str) -> bool:
    return a == b or a == "unknown" or b == "unknown"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _values_match(pending: LabResult, candidate: LabResult) -> bool:
    if pending.value is not None and candidate.value is not None:
        return pending.value == candidate.value
    if pending.value_text is not None and candidate.value_text is not None:
        return _normalize_text(pending.value_text) == _normalize_text(candidate.value_text)
    return False


def find_candidate(
    db: LabsDb, pending: LabResult, *, resolved_cache: dict[str, list[LabResult]] | None = None
) -> LabResult | None:
    """The deterministic candidate gate (module docstring, step 1): the
    first already-resolved row in the same document that could plausibly
    be `pending`'s other-pass twin. Returns `None` - never triggering an
    LLM call - when no such row exists, in particular whenever every
    same-document resolved row's value genuinely differs.

    `resolved_cache`, when given, memoizes `db.resolved_rows_for_document`
    per `source_doc` across repeated calls - `sweep_twins`'s loop over
    every PENDING `single_pass` row commonly calls this several times for
    rows sharing one document, and each call would otherwise re-run the
    identical `labs.sqlite` query for that document's resolved rows.
    Rejecting a PENDING row (this sweep's only mutation) never changes the
    resolved set, so the cache never goes stale across one sweep.
    """
    if resolved_cache is not None:
        if pending.source_doc not in resolved_cache:
            resolved_cache[pending.source_doc] = db.resolved_rows_for_document(pending.source_doc)
        candidates = resolved_cache[pending.source_doc]
    else:
        candidates = db.resolved_rows_for_document(pending.source_doc)
    for candidate in candidates:
        if candidate.id == pending.id:
            continue
        if pending.source_page is None or candidate.source_page is None:
            continue
        if abs(pending.source_page - candidate.source_page) > PAGE_TOLERANCE:
            continue
        if not _values_match(pending, candidate):
            continue
        if not _units_compatible(pending.ucum_unit, candidate.ucum_unit):
            continue
        if not _specimen_compatible(pending.specimen, candidate.specimen):
            continue
        return candidate
    return None


def names_equivalent_by_rule(name_a: str, name_b: str) -> bool:
    """Step 2a (module docstring): deterministic name-equivalence, NO LLM.

    Exact equality after `clean_result_name` + casefold ONLY (D3). A
    token-SUBSET match (e.g. "Iron" subset-matching "Iron Binding
    Capacity", "T4" subset-matching "Free T4", "Calcium" subset-matching
    "Ionized Calcium") used to auto-decide these as twins here - but those
    are clinically DISTINCT paired tests, not the same measurement
    transcribed two ways. A token-subset case is no longer decided by rule
    at all: it falls through to the LLM path (`_names_equivalent_by_llm`),
    which judges genuine suffix/subset variants (e.g. "T-Score" vs "LEFT
    HIP femoral neck T-Score") correctly while still rejecting the
    clinically-distinct-pair cases above.
    """
    a = clean_result_name(name_a).casefold()
    b = clean_result_name(name_b).casefold()
    return a == b


def _names_equivalent_by_llm(
    client: LlmClient, name_a: str, name_b: str, *, value: str, page: int
) -> bool:
    """Step 2b (module docstring): ONE `LlmClient.complete` call, only
    reached when the rule-based check above didn't already decide it."""
    prompt = (
        f"Name 1: {name_a!r}\nName 2: {name_b!r}\n"
        f"Both were read as value {value!r} on/near page {page} of the same document."
    )
    try:
        result = client.complete(
            "classifier",
            system=TWIN_CLASSIFY_PROMPT,
            messages=[Message(role="user", content=prompt)],
            schema=_SameMeasurement,
        )
    except LlmError:
        # A false negative here just leaves the row in the human queue -
        # never treat an LLM failure as a twin.
        return False
    parsed = result.parsed
    assert isinstance(parsed, _SameMeasurement)  # schema= guarantees this
    return parsed.same_measurement


def sweep_twins(db: LabsDb, client: LlmClient, *, dry_run: bool = False) -> TwinSweepReport:
    """Run the twin sweep (module docstring) over every currently-PENDING
    `single_pass` row.

    `dry_run=True` computes and reports exactly what a real run would do
    (including counts and which rows) without calling
    `LabsDb.reject_row_as_twin` at all - `db` is left completely
    unmutated. Idempotent: a row this sweep rejects is no longer PENDING,
    so a second run's `db.pending()` simply won't see it again.
    """
    report = TwinSweepReport()
    # Shared across every PENDING row this sweep checks - multiple
    # single_pass rows commonly come from the same document, and without
    # this `find_candidate` would re-run the identical
    # `resolved_rows_for_document` query once per row instead of once per
    # document (see `find_candidate`'s docstring).
    resolved_cache: dict[str, list[LabResult]] = {}
    for row in db.pending():
        reasons = row.raw_payload().get("reasons", [])
        if "single_pass" not in reasons:
            continue
        report.checked += 1

        candidate = find_candidate(db, row, resolved_cache=resolved_cache)
        if candidate is None:
            continue

        if names_equivalent_by_rule(row.name_raw, candidate.name_raw):
            method: str = "rule"
        elif _names_equivalent_by_llm(
            client,
            row.name_raw,
            candidate.name_raw,
            value=str(row.value if row.value is not None else row.value_text),
            page=row.source_page or 0,
        ):
            method = "llm"
        else:
            continue

        assert row.id is not None  # rows read back from the db always have one
        assert candidate.id is not None
        report.rejected += 1
        report.rejected_ids.append(row.id)
        if method == "rule":
            report.rejected_rule += 1
        else:
            report.rejected_llm += 1
        if not dry_run:
            db.reject_row_as_twin(row.id, twin_of=candidate.id, method=method)  # type: ignore[arg-type]

    _retro_pair_pending_twins(db, client, report, dry_run=dry_run)
    return report


def _pass_side(row: LabResult) -> str | None:
    """Which extraction pass produced this single-pass row: "a", "b", or
    None when it isn't a one-sided row at all."""
    payload = row.raw_payload()
    has_a = payload.get("pass_a") is not None
    has_b = payload.get("pass_b") is not None
    if has_a and not has_b:
        return "a"
    if has_b and not has_a:
        return "b"
    return None


def _retro_pair_pending_twins(
    db: LabsDb, client: LlmClient, report: TwinSweepReport, *, dry_run: bool
) -> None:
    """Phase 2: pending<->pending twins. Before reconcile's RESCUE pass
    existed, BOTH halves of a differently-named pair could be stranded
    PENDING - neither ever resolves, so phase 1's resolved-candidate gate
    never fires. Pair them here: same document, page within tolerance,
    identical value(-text), compatible unit, same specimen-or-unknown, and
    - critically - originating from OPPOSITE passes (two same-value rows
    from the SAME pass are genuinely different measurements, never paired).
    The longer-named row survives (upgraded to the agreed `name_variant`
    bucket, still awaiting one human OK); the other is rejected as its
    twin."""
    remaining = [
        row
        for row in db.pending()
        if row.id not in set(report.rejected_ids)
        and "single_pass" in row.raw_payload().get("reasons", [])
    ]
    by_doc: dict[str, list[LabResult]] = {}
    for row in remaining:
        by_doc.setdefault(row.source_doc, []).append(row)

    used: set[int] = set()
    for doc_rows in by_doc.values():
        for i, row_a in enumerate(doc_rows):
            if row_a.id in used:
                continue
            side_a = _pass_side(row_a)
            if side_a is None:
                continue
            for row_b in doc_rows[i + 1 :]:
                if row_b.id in used:
                    continue
                side_b = _pass_side(row_b)
                if side_b is None or side_b == side_a:
                    continue
                if row_a.source_page is None or row_b.source_page is None:
                    continue
                if abs(row_a.source_page - row_b.source_page) > PAGE_TOLERANCE:
                    continue
                if not _values_match(row_a, row_b):
                    continue
                if not _units_compatible(row_a.ucum_unit, row_b.ucum_unit):
                    continue
                if not _specimen_compatible(row_a.specimen, row_b.specimen):
                    continue

                if names_equivalent_by_rule(row_a.name_raw, row_b.name_raw):
                    method: str = "rule"
                elif _names_equivalent_by_llm(
                    client,
                    row_a.name_raw,
                    row_b.name_raw,
                    value=str(row_a.value if row_a.value is not None else row_a.value_text),
                    page=row_a.source_page,
                ):
                    method = "llm"
                else:
                    continue

                longer, shorter = (
                    (row_a, row_b)
                    if len(clean_result_name(row_a.name_raw))
                    >= len(clean_result_name(row_b.name_raw))
                    else (row_b, row_a)
                )
                assert longer.id is not None and shorter.id is not None
                report.checked += 1
                report.paired += 1
                report.rejected += 1
                report.rejected_ids.append(shorter.id)
                if method == "rule":
                    report.rejected_rule += 1
                else:
                    report.rejected_llm += 1
                if not dry_run:
                    db.reject_row_as_twin(shorter.id, twin_of=longer.id, method=method)  # type: ignore[arg-type]
                    db.mark_single_pass_as_name_variant(longer.id, other_name=shorter.name_raw)
                used.add(shorter.id)
                used.add(longer.id)
                break

    return


def write_sweep_summary(repo_root: Path, report: TwinSweepReport, *, at: datetime) -> None:
    """Persist `report` as the "last sweep" summary (module docstring) -
    called only after a REAL (non-dry-run) sweep."""
    path = repo_root / TWIN_SWEEP_SUMMARY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "checked": report.checked,
                "rejected": report.rejected,
                "rejected_rule": report.rejected_rule,
                "rejected_llm": report.rejected_llm,
                "at": at.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def read_last_sweep_summary(repo_root: Path) -> dict[str, Any] | None:
    """The last-persisted sweep summary (module docstring), or `None` if
    no sweep has ever run (or the file is unreadable/corrupt - never let
    a bad local breadcrumb break the confirm queue page)."""
    path = repo_root / TWIN_SWEEP_SUMMARY_RELPATH
    if not path.is_file():
        return None
    try:
        summary: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return summary

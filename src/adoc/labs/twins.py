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


def find_candidate(db: LabsDb, pending: LabResult) -> LabResult | None:
    """The deterministic candidate gate (module docstring, step 1): the
    first already-resolved row in the same document that could plausibly
    be `pending`'s other-pass twin. Returns `None` - never triggering an
    LLM call - when no such row exists, in particular whenever every
    same-document resolved row's value genuinely differs."""
    for candidate in db.resolved_rows_for_document(pending.source_doc):
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


def _name_tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.casefold()))


def names_equivalent_by_rule(name_a: str, name_b: str) -> bool:
    """Step 2a (module docstring): deterministic name-equivalence, NO LLM.
    Equal after `_clean_result_name` + casefold, or one's token set a
    subset of the other's."""
    a = clean_result_name(name_a).casefold()
    b = clean_result_name(name_b).casefold()
    if a == b:
        return True
    tokens_a, tokens_b = _name_tokens(a), _name_tokens(b)
    if not tokens_a or not tokens_b:
        return False
    return tokens_a <= tokens_b or tokens_b <= tokens_a


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
    for row in db.pending():
        reasons = row.raw_payload().get("reasons", [])
        if "single_pass" not in reasons:
            continue
        report.checked += 1

        candidate = find_candidate(db, row)
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

    return report


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

"""Claim-level entailment verifier + Composer quantitative check (PLAN.md
Phase 2, "Grounding & anti-hallucination hardening" — second bullet).

The deterministic citation checker (`reason.citations`) proves a claim's
source *ref* resolves to real, matching data. It cannot prove the claim's
*prose* is actually supported by that source — a claim could cite a real,
matching lab row and still misstate what the row means. `verify_claims`
closes that gap: a cross-family model (role `entailment_verifier`, bound to
a DIFFERENT model family than `primary_reasoner` in `models.yaml`, mirroring
ADR 0005's Challenger cross-family rule so the verifier does not share the
Ledger-Maintainer's blind spots) judges `entailed` | `not_entailed` for each
`(claim, resolved source text)` pair. `insufficient_source` (the source ref
resolves per the citation checker, but no TEXT for it is available yet) is
never sent to the model at all and is deliberately NOT a rejection — same
principle as the citation checker's `unverifiable`: a turn must never be
hostage to missing infrastructure. Only `not_entailed` blocks.

Source-text resolution is injectable (`SourceTextResolver`) so this module
never hard-depends on the parallel document-text-corpus workstream:
`DefaultSourceTextResolver` builds `labs:` source text deterministically
from the stored row, and returns `encounter:` file text where an encounter
file already exists — everything else (`doc:`, `pmid:`, `patient-report:`)
resolves to `None` today. The moment a document-text corpus lands, a richer
resolver can be injected without this module changing.

`check_composer_numbers` is a separate, purely deterministic check (no LLM):
every number in the Composer's patient-facing text that sits near a known
analyte name must match a value actually stored for that analyte in
`labs.sqlite` — arithmetic/number-matching is never delegated to a model
(CLAUDE.md: "deterministic logic ... is plain code with unit tests").
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import AddEvidence, AddHypothesis, LedgerOp
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.labs.validate import canonicalize
from adoc.reason.client import LlmClient, Message
from adoc.reason.context import ENCOUNTERS_RELDIR
from adoc.reason.prompts import load_prompt

EntailmentJudgment = Literal["entailed", "not_entailed", "insufficient_source"]

VERIFICATION_LOG_RELPATH = "logs/entailment-checks.jsonl"

# What the model itself is ever allowed to say — `insufficient_source` is
# decided by source-text resolution, before the model is ever called, never
# by the model's own judgment (it never sees a pair it has no text for).
_ModelEntailmentJudgment = Literal["entailed", "not_entailed"]


# --------------------------------------------------------------------------
# Claims: pulling (claim, source ref) pairs out of LedgerOps
# --------------------------------------------------------------------------


class Claim(BaseModel):
    """One evidence claim to verify, with enough identity to trace it back
    to the hypothesis it supports/opposes."""

    hypothesis_id: str
    for_or_against: Literal["for", "against"]
    claim: str
    source: str


def claims_from_ops(ops: Sequence[LedgerOp]) -> list[Claim]:
    """Every evidence claim carried by `ops` (from
    `AddHypothesis.hypothesis.evidence_for`/`evidence_against` and
    `AddEvidence.evidence`) — the same op-shapes `reason.citations.
    check_ops_citations` walks, so a caller checking one `LedgerDiff`'s ops
    (or a Challenger's `additional_ops`) gets an identical claim set for
    both checks."""
    claims: list[Claim] = []
    for op in ops:
        if isinstance(op, AddHypothesis):
            for ev in op.hypothesis.evidence_for:
                claims.append(
                    Claim(
                        hypothesis_id=op.hypothesis.id,
                        for_or_against="for",
                        claim=ev.claim,
                        source=ev.source,
                    )
                )
            for ev in op.hypothesis.evidence_against:
                claims.append(
                    Claim(
                        hypothesis_id=op.hypothesis.id,
                        for_or_against="against",
                        claim=ev.claim,
                        source=ev.source,
                    )
                )
        elif isinstance(op, AddEvidence):
            claims.append(
                Claim(
                    hypothesis_id=op.id,
                    for_or_against=op.for_or_against,
                    claim=op.evidence.claim,
                    source=op.evidence.source,
                )
            )
    return claims


# --------------------------------------------------------------------------
# Source-text resolution (the seam for the incoming document-text corpus)
# --------------------------------------------------------------------------


class SourceTextResolver(Protocol):
    """Resolves a `Evidence.source` ref to the text an entailment judge
    should read, or `None` if no text is available yet. Deliberately the
    SAME grammar `reason.citations` resolves refs against, but a different
    question: citation checking asks "does this ref point at real data?";
    this asks "what does that data actually SAY?"."""

    def resolve(self, source: str) -> str | None: ...


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_slug(text: str) -> str:
    """Case/punctuation-insensitive key for slug matching — deliberately a
    local, separate implementation from `labs.validate`'s private
    `_normalize` and from `reason.citations`'s own copy of the same idea
    (see that module's docstring-comment on why this is duplicated rather
    than imported: each module owns its own normalization helper)."""
    return _NON_ALNUM_RE.sub("", text.lower())


def _row_slug_candidates(row: LabResult) -> set[str]:
    candidates = {row.name, row.name_raw}
    for candidate in (row.name, row.name_raw):
        mapped = canonicalize(candidate)
        if mapped is not None:
            candidates.add(mapped)
    return {_normalize_slug(c) for c in candidates}


def _render_lab_row_source_text(row: LabResult) -> str:
    """Deterministic rendering of a stored `LabResult` row as "source text"
    for entailment judging (PLAN.md: "for labs: refs, the stored row
    (values, units, ref ranges, flags) is the source text")."""
    value = row.value_text if row.value is None else str(row.value)
    parts = [f"{row.name} on {row.date.isoformat()}: {value}"]
    if row.ucum_unit:
        parts.append(f"unit {row.ucum_unit}")
    if row.ref_text:
        parts.append(f"reference range {row.ref_text}")
    elif row.ref_low is not None or row.ref_high is not None:
        parts.append(f"reference range {row.ref_low}-{row.ref_high}")
    if row.flag:
        parts.append(f"flag {row.flag}")
    if row.specimen != "unknown":
        parts.append(f"specimen {row.specimen}")
    return "; ".join(parts)


class DefaultSourceTextResolver:
    """Default `SourceTextResolver`.

    - `labs:<slug>:<date>` — deterministic rendering of every matching
      stored row (`_render_lab_row_source_text`), joined; always resolvable
      once the row exists (no external dependency).
    - `encounter:<file>` — the encounter file's own text (frontmatter-free:
      summary + new findings + plan + any full extracted text), when that
      file already exists on disk. This is the seam for the parallel
      document-text-corpus workstream: encounter files already carry real
      narrative text today (PLAN.md docx ingestion), so this resolver
      starts working the moment more encounters exist, without this module
      changing.
    - `doc:<file>#p<page>` — the cited document's extracted text (ADR 0015's
      document-text corpus, which has now landed): the page's own text when
      the ref names a page and that page was stored separately, otherwise
      the whole document's text. `None` when nothing was ever extracted for
      it (e.g. a scan with no text layer, or a genomic file, which by
      construction never has text) — which yields `insufficient_source`,
      never a rejection.
    - `pmid:<id>` / `patient-report:<date>` — `None`. A PMID's abstract is
      not stored locally (the citation checker verifies only that the id
      exists), and a patient-report ref cites the patient's own statement,
      which has no external source text to entail against.
    """

    def __init__(self, db: LabsDb, repo: DataRepo) -> None:
        self._db = db
        self._repo = repo

    def resolve(self, source: str) -> str | None:
        if source.startswith("labs:"):
            return self._resolve_labs(source)
        if source.startswith("encounter:"):
            return self._resolve_encounter(source)
        if source.startswith("doc:"):
            return self._resolve_doc(source)
        return None

    def _resolve_doc(self, source: str) -> str | None:
        """`doc:<filename>#p<page>` -> that document's extracted text.

        Page-scoped when the corpus stored per-page text and the ref names a
        page; whole-document otherwise. A document with no stored text at all
        returns `None` (-> `insufficient_source`), which is the honest answer
        for an image-only scan and the only possible answer for a genomic
        file, whose text is never extracted by construction (ADR 0015).
        """
        ref = source[len("doc:") :]
        filename, _, page_part = ref.partition("#p")
        document = next(
            (doc for doc in self._db.list_documents() if doc.filename == filename), None
        )
        if document is None:
            return None
        if page_part:
            try:
                page = int(page_part)
            except ValueError:
                page = 0
            if page > 0:
                page_text = self._db.get_document_page_text(document.sha256, page)
                if page_text is not None:
                    return page_text
        return self._db.get_document_text(document.sha256)

    def _resolve_labs(self, source: str) -> str | None:
        _, slug, date_str = source.split(":", 2)
        try:
            ref_date = date.fromisoformat(date_str)
        except ValueError:
            return None
        slug_norm = _normalize_slug(slug)
        matches = [
            row
            for row in self._db.all_non_rejected_rows()
            if row.date == ref_date and slug_norm in _row_slug_candidates(row)
        ]
        if not matches:
            return None
        return "\n".join(_render_lab_row_source_text(row) for row in matches)

    def _resolve_encounter(self, source: str) -> str | None:
        filename = source[len("encounter:") :]
        path = self._repo.root / ENCOUNTERS_RELDIR / filename
        if not path.is_file():
            return None
        encounter = read_encounter(path)
        parts = [encounter.summary, encounter.new_findings, encounter.plan]
        if encounter.extracted_text.strip():
            parts.append(encounter.extracted_text)
        text = "\n\n".join(p.strip() for p in parts if p.strip())
        return text or None


# --------------------------------------------------------------------------
# Verification report
# --------------------------------------------------------------------------


class ClaimVerification(BaseModel):
    """The result of judging one `Claim`."""

    source: str
    claim: str
    judgment: EntailmentJudgment
    rationale: str = ""


class VerificationReport(BaseModel):
    checks: list[ClaimVerification] = Field(default_factory=list)

    @property
    def not_entailed(self) -> list[ClaimVerification]:
        return [c for c in self.checks if c.judgment == "not_entailed"]

    @property
    def insufficient_source(self) -> list[ClaimVerification]:
        return [c for c in self.checks if c.judgment == "insufficient_source"]

    @property
    def failing(self) -> list[ClaimVerification]:
        """Only `not_entailed` blocks a diff — `insufficient_source` is
        deliberately excluded, same principle as the citation checker's
        `unverifiable` (module docstring)."""
        return [c for c in self.checks if c.judgment == "not_entailed"]

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"entailed": 0, "not_entailed": 0, "insufficient_source": 0}
        for check in self.checks:
            counts[check.judgment] += 1
        return counts


# --------------------------------------------------------------------------
# The model call
# --------------------------------------------------------------------------


class _EntailmentJudgmentOut(BaseModel):
    claim_index: int
    judgment: _ModelEntailmentJudgment
    rationale: str = ""


class _EntailmentPayload(BaseModel):
    judgments: list[_EntailmentJudgmentOut] = Field(default_factory=list)


def verify_claims(
    client: LlmClient,
    claims: Sequence[Claim],
    *,
    db: LabsDb,
    repo: DataRepo,
    resolver: SourceTextResolver | None = None,
) -> VerificationReport:
    """Judge every claim in `claims`, role `entailment_verifier`.

    Source text is resolved first, per claim, via `resolver` (default
    `DefaultSourceTextResolver(db, repo)`). A claim whose source text cannot
    be resolved is `insufficient_source` and is never sent to the model at
    all. Claims WITH resolved text are sent to the model in a single call
    (one completion per `verify_claims` invocation, not one per claim) so a
    turn carrying several evidence claims costs one verifier call, not N.

    If the model's response omits a judgment for a claim it was sent (a
    schema-valid but incomplete response), that claim is scored
    `not_entailed` — fail closed: a claim the verifier did not actually
    judge can never be assumed to have passed."""
    checks: list[ClaimVerification] = []
    resolved_resolver = resolver or DefaultSourceTextResolver(db, repo)

    pairs: list[tuple[int, Claim, str]] = []
    for claim in claims:
        source_text = resolved_resolver.resolve(claim.source)
        if source_text is None:
            checks.append(
                ClaimVerification(
                    source=claim.source,
                    claim=claim.claim,
                    judgment="insufficient_source",
                    rationale="no source text available to verify this ref yet",
                )
            )
            continue
        pairs.append((len(pairs), claim, source_text))

    if not pairs:
        return VerificationReport(checks=checks)

    prompt = load_prompt("entailment_verifier")
    payload_in = [
        {
            "claim_index": idx,
            "claim": claim.claim,
            "source_ref": claim.source,
            "source_text": source_text,
        }
        for idx, claim, source_text in pairs
    ]
    user_content = (
        "Judge entailment for each claim/source pair below. Return one judgment per "
        "claim_index.\n\n" + json.dumps(payload_in, indent=2)
    )
    result = client.complete(
        "entailment_verifier",
        system=prompt.text,
        messages=[Message(role="user", content=user_content)],
        schema=_EntailmentPayload,
    )
    parsed = result.parsed
    assert isinstance(parsed, _EntailmentPayload)
    judgment_by_index = {j.claim_index: j for j in parsed.judgments}

    for idx, claim, _source_text in pairs:
        judged = judgment_by_index.get(idx)
        if judged is None:
            checks.append(
                ClaimVerification(
                    source=claim.source,
                    claim=claim.claim,
                    judgment="not_entailed",
                    rationale="entailment verifier returned no judgment for this claim",
                )
            )
        else:
            checks.append(
                ClaimVerification(
                    source=claim.source,
                    claim=claim.claim,
                    judgment=judged.judgment,
                    rationale=judged.rationale,
                )
            )
    return VerificationReport(checks=checks)


def build_entailment_retry_feedback(report: VerificationReport) -> str:
    """Render `report.failing` (the `not_entailed` claims) as feedback text
    for a same-generation retry, mirroring `reason.citations.
    build_retry_feedback`'s shape."""
    lines = ["The following evidence claim(s) were judged NOT entailed by their cited source:"]
    for check in report.failing:
        lines.append(f"- {check.source} ({check.claim!r}): {check.rationale}")
    lines.append(
        "Fix this by rewriting the claim so it accurately reflects what the cited source "
        "actually says, citing a source that truly supports it, or dropping the claim "
        "entirely if no genuine support exists. Return the complete corrected result in "
        "the same schema."
    )
    return "\n".join(lines)


def log_verification_report(repo: DataRepo, report: VerificationReport, *, dag_node: str) -> None:
    """Append one JSON line recording `report`'s outcome counts (and full
    detail on any `not_entailed` claim) to `logs/entailment-checks.jsonl`,
    following the same append-only JSONL pattern as
    `reason.citations.log_citation_report`."""
    path = repo.root / VERIFICATION_LOG_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "dag_node": dag_node,
        "counts": report.counts,
        "failing": [c.model_dump(mode="json") for c in report.failing],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------
# Composer quantitative (number-grounding) check — pure code, no LLM
# --------------------------------------------------------------------------

_DATE_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TITER_RE = re.compile(r"\d+\s*:\s*\d+")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?;\n]+")
_DECIMAL_POINT_RE = re.compile(r"(?<=\d)\.(?=\d)")
_DECIMAL_PLACEHOLDER = "\x00"


def _split_clauses(text: str) -> list[str]:
    """Split `text` into clauses on sentence boundaries — a local,
    single-purpose copy of the same idea `reason.safety`'s
    `_split_clauses` uses, kept private to each module by this codebase's
    convention (see `reason.citations`'s normalization-helper docstring).

    A decimal point ("8.5") is NOT a clause boundary — it is protected
    before splitting (and restored after) so "Your CRP was 8.5 mg/L." does
    not split into "Your CRP was 8" / "5 mg/L", which would silently lose
    the number this check exists to verify."""
    protected = _DECIMAL_POINT_RE.sub(_DECIMAL_PLACEHOLDER, text)
    return [
        c.replace(_DECIMAL_PLACEHOLDER, ".")
        for c in _CLAUSE_BOUNDARY_RE.split(protected)
        if c.strip()
    ]


def _analyte_value_index(db: LabsDb) -> dict[str, set[float]]:
    """Every stored NUMERIC value, indexed by the lowercased literal analyte
    label (both `name` and `name_raw`) it was recorded under — the labels a
    Composer's patient-facing prose would plausibly use, since it renders
    from the same rows (`reason.context._labs_section`)."""
    index: dict[str, set[float]] = {}
    for row in db.all_non_rejected_rows():
        if row.value is None:
            continue
        for label in {row.name, row.name_raw}:
            key = label.strip().lower()
            if not key:
                continue
            index.setdefault(key, set()).add(row.value)
    return index


class ComposerNumberMismatch(BaseModel):
    """One patient-facing number that sits next to a known analyte name but
    does not match ANY stored value recorded for that analyte."""

    clause: str
    analyte_label: str
    quoted_number: float
    stored_values: list[float]


class ComposerNumberCheck(BaseModel):
    mismatches: list[ComposerNumberMismatch] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.mismatches


def check_composer_numbers(text: str, db: LabsDb) -> ComposerNumberCheck:
    """Deterministically check every number the Composer's `tiers_rendered`
    text quotes near a known analyte name against `labs.sqlite` (PLAN.md
    Phase 2: "every number in patient-facing output that is attributable to
    a lab value must match labs.sqlite exactly"). No LLM call — numeral
    extraction and comparison are plain code (CLAUDE.md code conventions).

    Conservative by construction: a number only counts as "attributable to
    a lab value" when it shares a CLAUSE with a literal, word-bounded
    mention of an analyte name actually recorded in `labs.sqlite` for this
    patient — an unrelated number (a page count, a date fragment, a test
    kit catalog number) never trips this check because it never sits next
    to a real analyte name. Dates and titer ratios (`1:640`) are stripped
    before number extraction so their digits never masquerade as a quoted
    result value (mirrors `reason.citations._extract_quoted_numbers`)."""
    index = _analyte_value_index(db)
    if not index:
        return ComposerNumberCheck()

    mismatches: list[ComposerNumberMismatch] = []
    for clause in _split_clauses(text):
        lowered = clause.lower()
        matched_labels = [
            label for label in index if re.search(rf"\b{re.escape(label)}\b", lowered)
        ]
        if not matched_labels:
            continue

        cleaned = _DATE_IN_TEXT_RE.sub(" ", clause)
        cleaned = _TITER_RE.sub(" ", cleaned)
        numbers = [float(m) for m in _NUMBER_RE.findall(cleaned)]
        if not numbers:
            continue

        for label in matched_labels:
            stored = index[label]
            for number in numbers:
                if not any(abs(number - v) <= 1e-9 for v in stored):
                    mismatches.append(
                        ComposerNumberMismatch(
                            clause=clause.strip(),
                            analyte_label=label,
                            quoted_number=number,
                            stored_values=sorted(stored),
                        )
                    )
    return ComposerNumberCheck(mismatches=mismatches)


COMPOSER_NUMBER_REWRITE_INSTRUCTION = (
    "Rewrite this response so every number attributed to a lab result exactly matches the "
    "value actually stored for that analyte. Do not restate, round, or recompute a lab "
    "value from memory — quote it exactly as recorded, or omit the number if you are not "
    "citing a specific stored value."
)


def build_composer_number_retry_feedback(check: ComposerNumberCheck) -> str:
    lines = [f"{COMPOSER_NUMBER_REWRITE_INSTRUCTION} The following number(s) did not match:"]
    for m in check.mismatches:
        lines.append(
            f"- {m.quoted_number!r} near {m.analyte_label!r} (stored value(s): "
            f"{m.stored_values}) in: {m.clause!r}"
        )
    return "\n".join(lines)


__all__ = [
    "Claim",
    "ClaimVerification",
    "ComposerNumberCheck",
    "ComposerNumberMismatch",
    "DefaultSourceTextResolver",
    "EntailmentJudgment",
    "SourceTextResolver",
    "VerificationReport",
    "build_composer_number_retry_feedback",
    "build_entailment_retry_feedback",
    "check_composer_numbers",
    "claims_from_ops",
    "log_verification_report",
    "verify_claims",
]

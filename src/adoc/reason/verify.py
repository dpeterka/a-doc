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

Source-text resolution is injectable (`SourceTextResolver`).
`DefaultSourceTextResolver` builds `labs:` source text deterministically
from the stored row, returns `encounter:` file text where an encounter file
already exists, and resolves `doc:` refs against the document-text corpus
(ADR 0015); `pmid:` and `patient-report:` refs resolve to `None` (see
`DefaultSourceTextResolver`'s docstring for why).

`check_composer_numbers` is a separate, purely deterministic check (no LLM):
every number in the Composer's patient-facing text that sits near a known
analyte name must match a value actually stored for that analyte in
`labs.sqlite` — arithmetic/number-matching is never delegated to a model
(CLAUDE.md: "deterministic logic ... is plain code with unit tests").

ADR 0016 revised (2026-08-25, "strip, don't reject"): a `not_entailed`
claim no longer fails a diff outright. `strip_not_entailed_ops` removes
just the offending evidence item(s) from a diff/verdict's ops (never
`insufficient_source` ones — unresolvable is not the same as wrong), so
the turn proceeds on the remaining, verified evidence instead of the whole
turn being lost. The one entailment outcome still treated as a hard
failure is `VerificationReport.all_not_entailed`: every claim in the
checked ops judged `not_entailed`, with nothing — not even an
`insufficient_source` claim — surviving. That is evidence the pipeline
itself produced garbage, not merely an imprecise claim, so
`reason.stages.entailment_check_contract` still raises a
`ContractViolation` in that one case. See the ADR for the measured
over-block rate this fixes and why the threshold is "all", not "any".

Latency (2026-08-25, "diagnostic-turn-latency"): a real, full-case-file
diagnostic turn measured 930s (66% of total model time) in the
`entailment_verifier` role alone, from a reasoning model (DeepSeek R1)
burning ~13k output tokens per call on a comparatively easy judgment, times
up to 4 calls/turn (a same-generation retry that is now redundant under
"strip, don't reject" — a `not_entailed` claim is stripped either way, so
paying for a second completion to maybe rescue one evidence item is not
worth ~300s). Two changes address this without weakening what
`not_entailed` catches:
- `reason.stages` no longer retries the model on an entailment failure —
  `verify_claims` is called exactly once per stage call ("verify once;
  strip what fails").
- `EntailmentCache` (below) caches a verdict by a hash of `(claim,
  resolved source text)`, mirroring `reason.citations.EutilsPmidVerifier`'s
  on-disk cache pattern (`work/`-scoped, gitignored, rebuildable). Unlike
  the PMID cache's TTL for `not_found`, a judgment for an EXACT (claim,
  source text) pair is cached forever once made — the pair is bytewise
  identical, so the judgment can never go stale for THAT pair; a changed
  claim or a changed source text (e.g. a corrected lab value) simply
  hashes to a different key and misses, invalidating naturally. This also
  means the DAG's independent postcondition/precondition re-checks
  (`reason.stages.entailment_check_contract`, run moments after the stage
  call on the same or a merged claim set) are cache hits, not a second
  round of model calls — they remain genuine independent re-checks (same
  code path, same judging logic), they are just free when the exact pair
  was already judged this run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import AddEvidence, AddHypothesis, Evidence, LedgerOp
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
# Entailment verdict cache (latency: "diagnostic-turn-latency" — see module
# docstring for the measured problem this addresses)
# --------------------------------------------------------------------------

ENTAILMENT_CACHE_RELPATH = "work/entailment-cache.json"


class EntailmentCache:
    """On-disk cache of `(claim, source_text) -> judgment`, mirroring
    `reason.citations.EutilsPmidVerifier`'s on-disk cache pattern
    (`work/`-scoped, so it is gitignored and rebuildable — losing this file
    only means re-paying for calls that were previously free, never a
    correctness change).

    Keyed on a hash of the claim text plus the RESOLVED source text (not
    just the source ref), so a changed source — a corrected lab value, an
    edited encounter file — invalidates naturally: same ref, different
    resolved text, different key, a clean miss. No TTL: unlike the PMID
    cache's `not_found` (which can flip true if NCBI indexes a PMID later),
    an entailment judgment for a byte-identical `(claim, source_text)` pair
    cannot become stale — the pair itself never changes underneath it.

    Loads/saves the whole file per call, matching `EutilsPmidVerifier`'s
    own simplicity; callers that need to check many claims in one pass
    (`verify_claims`) load once and save once, not once per claim, to avoid
    N redundant JSON round-trips.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def key(claim: str, source_text: str) -> str:
        digest = hashlib.sha256()
        digest.update(claim.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(source_text.encode("utf-8"))
        return digest.hexdigest()

    def load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, entries: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")


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
      file already exists on disk.
    - `doc:<file>#p<page>` — the cited document's extracted text (ADR 0015's
      document-text corpus): the page's own text when the ref names a page
      and that page was stored separately, otherwise the whole document's
      text. `None` when nothing was ever extracted for it (e.g. a scan with
      no text layer, or a genomic file, which by construction never has
      text) — which yields `insufficient_source`, never a rejection.
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
    hypothesis_id: str = ""


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
        """Every `not_entailed` claim — the candidates `strip_not_entailed_
        ops` will drop from a diff/verdict's ops, UNLESS `all_not_entailed`
        (see that property) makes this a hard failure instead.
        `insufficient_source` is deliberately excluded, same principle as
        the citation checker's `unverifiable` (module docstring)."""
        return [c for c in self.checks if c.judgment == "not_entailed"]

    @property
    def all_not_entailed(self) -> bool:
        """True when EVERY claim checked was judged `not_entailed` — nothing
        survives, not even an `insufficient_source` claim. This is the one
        entailment outcome `reason.stages.entailment_check_contract` still
        treats as a hard failure (ADR 0016 revised, "strip, don't reject"):
        a diff whose entire evidence set is not_entailed is evidence the
        pipeline itself is broken, not merely imprecise — stripping it down
        to nothing would silently hide that rather than surface it. `False`
        when `checks` is empty (nothing to fail on, same as `failing`
        being empty)."""
        return bool(self.checks) and len(self.not_entailed) == len(self.checks)

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
    cache: EntailmentCache | None = None,
) -> VerificationReport:
    """Judge every claim in `claims`, role `entailment_verifier`.

    Source text is resolved first, per claim, via `resolver` (default
    `DefaultSourceTextResolver(db, repo)`). A claim whose source text cannot
    be resolved is `insufficient_source` and is never sent to the model at
    all.

    When `cache` is given, a claim whose `(claim, resolved source text)`
    pair was already judged (by ANY prior call sharing this cache, this run
    or a previous one) is scored from the cache and never sent to the model
    either — see `EntailmentCache`. Only claims that are neither
    `insufficient_source` NOR a cache hit are sent to the model, in a
    single call (one completion per `verify_claims` invocation, not one per
    claim) so a turn carrying several evidence claims costs at most one
    verifier call, not N.

    If the model's response omits a judgment for a claim it was sent (a
    schema-valid but incomplete response), that claim is scored
    `not_entailed` — fail closed: a claim the verifier did not actually
    judge can never be assumed to have passed. A judgment scored this way
    (no real verdict from the model) is deliberately NOT written to the
    cache — it is a response-shape artifact, not a genuine judgment about
    the pair, and caching it could wrongly pin a transient glitch forever.
    """
    checks: list[ClaimVerification] = []
    resolved_resolver = resolver or DefaultSourceTextResolver(db, repo)
    cache_entries = cache.load() if cache is not None else {}
    cache_dirty = False

    pairs: list[tuple[int, Claim, str, str | None]] = []
    for claim in claims:
        source_text = resolved_resolver.resolve(claim.source)
        if source_text is None:
            checks.append(
                ClaimVerification(
                    source=claim.source,
                    claim=claim.claim,
                    judgment="insufficient_source",
                    rationale="no source text available to verify this ref yet",
                    hypothesis_id=claim.hypothesis_id,
                )
            )
            continue

        cache_key = EntailmentCache.key(claim.claim, source_text) if cache is not None else None
        cached_entry = cache_entries.get(cache_key) if cache_key is not None else None
        if cached_entry is not None and cached_entry.get("judgment") in (
            "entailed",
            "not_entailed",
        ):
            checks.append(
                ClaimVerification(
                    source=claim.source,
                    claim=claim.claim,
                    judgment=cached_entry["judgment"],
                    rationale=cached_entry.get("rationale", ""),
                    hypothesis_id=claim.hypothesis_id,
                )
            )
            continue

        pairs.append((len(pairs), claim, source_text, cache_key))

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
        for idx, claim, source_text, _cache_key in pairs
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

    for idx, claim, _source_text, cache_key in pairs:
        judged = judgment_by_index.get(idx)
        if judged is None:
            checks.append(
                ClaimVerification(
                    source=claim.source,
                    claim=claim.claim,
                    judgment="not_entailed",
                    rationale="entailment verifier returned no judgment for this claim",
                    hypothesis_id=claim.hypothesis_id,
                )
            )
        else:
            if cache_key is not None:
                cache_entries[cache_key] = {
                    "judgment": judged.judgment,
                    "rationale": judged.rationale,
                }
                cache_dirty = True
            checks.append(
                ClaimVerification(
                    source=claim.source,
                    claim=claim.claim,
                    judgment=judged.judgment,
                    rationale=judged.rationale,
                    hypothesis_id=claim.hypothesis_id,
                )
            )
    if cache is not None and cache_dirty:
        cache.save(cache_entries)
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
# Deferred verification queue (latency: "diagnostic-turn-latency",
# 2026-08-25) — claims on `expanded`/`cant-miss` hypotheses are not
# verified synchronously in a diagnostic turn (only `most-likely` evidence
# drives this turn's patient-facing reply within the latency budget); this
# is where they wait until the weekly review sweeps them
# (`reason.review.sweep_deferred_entailment_claims`), so nothing is
# silently skipped forever.
# --------------------------------------------------------------------------

DEFERRED_CLAIMS_RELPATH = "work/entailment-deferred.json"


class DeferredClaim(BaseModel):
    """One evidence claim whose entailment check was deferred out of a
    diagnostic turn's synchronous path — enough identity to rebuild a
    `Claim` and re-resolve its source text later."""

    hypothesis_id: str
    for_or_against: Literal["for", "against"]
    claim: str
    source: str
    dag_node: str
    deferred_at: datetime


def _load_deferred_claims(path: Path) -> list[DeferredClaim]:
    if not path.exists():
        return []
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    items: list[DeferredClaim] = []
    for entry in raw:
        try:
            items.append(DeferredClaim.model_validate(entry))
        except Exception:  # noqa: BLE001 - a malformed entry is dropped, never crashes the sweep
            continue
    return items


def _save_deferred_claims(path: Path, items: Sequence[DeferredClaim]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([item.model_dump(mode="json") for item in items], sort_keys=True),
        encoding="utf-8",
    )


def queue_deferred_claims(
    repo: DataRepo, claims: Sequence[Claim], *, dag_node: str, at: datetime | None = None
) -> None:
    """Append `claims` to the deferred-verification queue
    (`work/entailment-deferred.json`, gitignored — losing this file loses
    the deferral, not the underlying evidence, which stays exactly as
    entailed/unverified as it always was; the weekly review simply has
    nothing to sweep that run). A no-op when `claims` is empty."""
    if not claims:
        return
    path = repo.root / DEFERRED_CLAIMS_RELPATH
    existing = _load_deferred_claims(path)
    deferred_at = at or datetime.now(UTC)
    existing.extend(
        DeferredClaim(
            hypothesis_id=c.hypothesis_id,
            for_or_against=c.for_or_against,
            claim=c.claim,
            source=c.source,
            dag_node=dag_node,
            deferred_at=deferred_at,
        )
        for c in claims
    )
    _save_deferred_claims(path, existing)


def pop_deferred_claims(repo: DataRepo) -> list[DeferredClaim]:
    """Read and CLEAR the deferred-verification queue in one step — the
    weekly review sweep calls this exactly once per run so each deferred
    claim is picked up exactly once (barring a review that fails before
    committing, which is no worse than a diagnostic turn's own claims: the
    underlying evidence is unchanged either way, only WHEN it gets
    verified is affected)."""
    path = repo.root / DEFERRED_CLAIMS_RELPATH
    items = _load_deferred_claims(path)
    if items:
        _save_deferred_claims(path, [])
    return items


# --------------------------------------------------------------------------
# Strip-don't-reject (ADR 0016 revised, 2026-08-25)
# --------------------------------------------------------------------------

STRIPPED_CLAIMS_LOG_RELPATH = "logs/entailment-stripped.jsonl"


def strip_not_entailed_ops(
    ops: Sequence[LedgerOp], report: VerificationReport
) -> tuple[list[LedgerOp], list[ClaimVerification]]:
    """Remove every evidence item `report` judged `not_entailed` from `ops`
    (ADR 0016 revised, "strip, don't reject"): dropped from an
    `AddHypothesis.hypothesis.evidence_for`/`evidence_against` list in
    place, and an `AddEvidence` op whose own (single) evidence item was
    `not_entailed` is dropped entirely. `entailed` and `insufficient_source`
    items are kept unchanged — unresolvable is not the same as wrong, same
    principle as the citation checker's `unverifiable`.

    Callers decide WHETHER to call this at all: `report.all_not_entailed`
    (nothing survives) is the one case this function should never be
    applied to — see `reason.stages.ledger_maintainer_stage`/
    `challenger_stage`, which leave `ops` untouched in that case so the
    `entailment_check_*` DAG contract can raise instead.

    Matches evidence to a judgment by `(source, claim)` text — the only
    identity `VerificationReport` carries back from `verify_claims`. Two
    evidence items with byte-identical claim text and source ref get the
    same judgment applied to both, which is a reasonable de-facto behavior
    given no finer-grained identity exists on either side.

    Returns `(stripped_ops, removed)` — the corrected ops, plus exactly the
    `ClaimVerification`s that were actually dropped, for the caller to pass
    to `log_stripped_claims`."""
    not_entailed_keys = {(c.source, c.claim) for c in report.not_entailed}
    if not not_entailed_keys:
        return list(ops), []

    def _is_stripped(ev: Evidence) -> bool:
        return (ev.source, ev.claim) in not_entailed_keys

    stripped_ops: list[LedgerOp] = []
    any_removed = False
    for op in ops:
        if isinstance(op, AddHypothesis):
            hyp = op.hypothesis
            kept_for = [e for e in hyp.evidence_for if not _is_stripped(e)]
            kept_against = [e for e in hyp.evidence_against if not _is_stripped(e)]
            if len(kept_for) != len(hyp.evidence_for) or len(kept_against) != len(
                hyp.evidence_against
            ):
                any_removed = True
                hyp = hyp.model_copy(
                    update={"evidence_for": kept_for, "evidence_against": kept_against}
                )
                op = op.model_copy(update={"hypothesis": hyp})
            stripped_ops.append(op)
        elif isinstance(op, AddEvidence):
            if _is_stripped(op.evidence):
                any_removed = True
                continue
            stripped_ops.append(op)
        else:
            stripped_ops.append(op)

    removed = list(report.not_entailed) if any_removed else []
    return stripped_ops, removed


def log_stripped_claims(
    repo: DataRepo, stripped: Sequence[ClaimVerification], *, dag_node: str
) -> None:
    """Append one JSON line recording every evidence claim actually DROPPED
    from a diff/verdict's ops by `strip_not_entailed_ops` (ADR 0016
    revised, "strip, don't reject") to `logs/entailment-stripped.jsonl`.

    Distinct from `log_verification_report`'s per-attempt raw judgment
    counts (which include mid-retry failures the model then self-corrected
    and which are never actually dropped): this file is the direct signal
    for "how much evidence is being silently dropped from patient-facing
    reasoning", so over-stripping is measurable without reconstructing it
    from retry-attempt noise. A no-op when `stripped` is empty."""
    if not stripped:
        return
    path = repo.root / STRIPPED_CLAIMS_LOG_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "dag_node": dag_node,
        "count": len(stripped),
        "stripped": [c.model_dump(mode="json") for c in stripped],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------
# Composer quantitative (number-grounding) check — pure code, no LLM
# --------------------------------------------------------------------------

_DATE_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TITER_RE = re.compile(r"\d+\s*:\s*\d+")
# Same pattern as `reason.citations._RANGE_RE` (reused rather than
# reinvented, per the codebase's per-module-ownership convention for these
# clause-cleaning regexes — see `_split_clauses`'s docstring): a range-shaped
# mention ("reference range 0.0-5.0") is stripped before number extraction
# so its two boundary numbers never masquerade as a quoted result value.
_RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?;\n]+")
_DECIMAL_POINT_RE = re.compile(r"(?<=\d)\.(?=\d)")
_DECIMAL_PLACEHOLDER = "\x00"

# A number sitting next to a known analyte name is treated as that
# analyte's VALUE by default (conservative-by-construction: this is what
# lets the check catch a fabricated value with no unit at all). The one
# thing that overrides that default is explicit positive evidence the
# number is a COUNT of something instead — a frequency, a duration, a
# number of occasions — never a result. `_COUNT_CONTEXT_FILLER_WORDS` are
# short qualifiers skipped over when looking for that evidence ("3
# SEPARATE panels").
_VALUE_TOKEN_RE = re.compile(r"[A-Za-z]+")
_COUNT_CONTEXT_WORDS = {
    "panel",
    "panels",
    "occasion",
    "occasions",
    "time",
    "times",
    "week",
    "weeks",
    "month",
    "months",
    "year",
    "years",
    "day",
    "days",
    "visit",
    "visits",
    "test",
    "tests",
    "sample",
    "samples",
    "draw",
    "draws",
    "reading",
    "readings",
    "result",
    "results",
    "attempt",
    "attempts",
    "appointment",
    "appointments",
}
_COUNT_CONTEXT_FILLER_WORDS = {
    "separate",
    "different",
    "additional",
    "more",
    "other",
    "various",
    "total",
    "multiple",
    "prior",
    "previous",
    "recent",
    "further",
}


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


def _analyte_unit_index(db: LabsDb) -> dict[str, set[str]]:
    """Every distinct `ucum_unit` recorded for each analyte label (both
    `name` and `name_raw`, lowercased) — PER-ANALYTE, unlike a single global
    unit vocabulary. This is what lets `_quoted_number_is_value_evidence`
    decide the "is this analyte itself a percent-unit analyte?" question
    (`% SATURATION`, a differential count, ...) from this patient's own
    stored data rather than a hardcoded analyte-name list."""
    index: dict[str, set[str]] = {}
    for row in db.all_non_rejected_rows():
        if not row.ucum_unit:
            continue
        for label in {row.name, row.name_raw}:
            key = label.strip().lower()
            if not key:
                continue
            index.setdefault(key, set()).add(row.ucum_unit.strip().lower())
    return index


# Positive-evidence design (2026-08-25, second pass). The first pass
# (above: `_COUNT_CONTEXT_WORDS`/`_COUNT_CONTEXT_FILLER_WORDS`) defaulted
# every number sitting near a known analyte name to "is a value" and tried
# to enumerate what to exclude (a count, a frequency, a duration). That
# design kept losing: a live diagnostic run was lost when "Ferritin dropped
# by 40% since 2024" flagged BOTH "40" (a percent CHANGE, not a value) and
# "2024" (a YEAR, not a value) as claimed Ferritin readings — ordinary
# clinical prose is full of numbers near an analyte name that are not its
# value (percentages, years, dates, durations, counts, ratios), and
# enumerating every such shape is an unbounded, always-losing game.
#
# Inverted here: a number counts as a candidate VALUE only when something
# in the text actually TIES it to the analyte —
#   - it is immediately followed by a unit THIS analyte is actually
#     recorded under (per-analyte, via `_analyte_unit_index` — never a
#     unit that merely exists somewhere else in the patient's data), or
#   - it is immediately preceded (skipping a short filler word such as
#     "back"/"in"/"up"/"down"/"out") by a copula/preposition that ties a
#     number to a measurement ("was 15 ng/mL", "at 22", "reading of 8.5").
# A number with neither kind of positive evidence is left unflagged by
# construction — no exclusion list required.
#
# Even a number WITH positive evidence is still vetoed when:
#   - it is immediately followed by "%" and this analyte's own recorded
#     unit is not itself "%" — a percentage attached to an analyte whose
#     real unit is something else (ng/mL, mg/dL, ...) is a percent CHANGE
#     or a percentage OF something, not the analyte's value. When the
#     analyte genuinely IS a percent-unit analyte ("% SATURATION", a
#     differential count), a %-suffixed number is checked exactly like any
#     other value — decided from `_analyte_unit_index`, never a hardcoded
#     analyte name;
#   - it is a bare 4-digit integer in a plausible calendar-year range
#     ("since 2024" is a date, not a measurement) AND its only positive
#     evidence was a copula, not a unit — a unit directly attached (e.g.
#     "B12 was 2024 pg/mL") is unambiguous regardless of magnitude, so the
#     year veto only guards the copula-only case, where common English date
#     phrasing ("...was 2024") can otherwise borrow a copula word. Accepted,
#     documented tradeoff: a genuine unit-less 4-digit reading that happens
#     to fall in this numeric range is a rare false negative traded off
#     against the much more common false positive this fixes;
#   - it is immediately followed (skipping a short qualifier) by an
#     explicit count/frequency/duration word from `_COUNT_CONTEXT_WORDS`
#     ("at 6 weeks" is a duration, not a value, even though "at" is a
#     copula) — kept from the first pass.
_VALUE_COPULA_WORDS = {
    "was",
    "is",
    "were",
    "are",
    "at",
    "of",
    "reading",
    "read",
    "measured",
    "resulted",
    "reached",
    "recorded",
    "value",
    "came",
}
_COPULA_SKIPPABLE_FILLERS = {"back", "in", "up", "down", "out"}
_HEAD_WORD_RE = re.compile(r"[A-Za-z]+")

_YEAR_MIN = 1900
_YEAR_MAX = 2099


def _has_preceding_copula(head: str) -> bool:
    """True iff the closest real word before the number (skipping a short
    filler word like "back"/"in"/"up"/"down"/"out") is a copula/preposition
    from `_VALUE_COPULA_WORDS`. Only the CLOSEST non-filler word counts —
    "CRP was elevated on 2 occasions" must not fire just because "was"
    appears earlier in the clause; the word immediately governing "2" is
    "on", which is neither a filler nor a copula, so this returns `False`
    there (the count-word veto is a second, independent guard for exactly
    that shape, not the only one)."""
    words = [w.lower() for w in _HEAD_WORD_RE.findall(head)]
    for word in reversed(words[-4:]):
        if word in _COPULA_SKIPPABLE_FILLERS:
            continue
        return word in _VALUE_COPULA_WORDS
    return False


_COMPARATOR_WORDS = {
    "below",
    "above",
    "under",
    "over",
    "beneath",
    "beyond",
    "exceeds",
    "exceed",
    "exceeding",
    "less",
    "fewer",
    "greater",
    "least",
    "most",
    "minimum",
    "maximum",
    "threshold",
    "cutoff",
    "than",
}
# "<", ">", "<=", ">=", "≤", "≥" immediately before the number.
_COMPARATOR_SYMBOL_RE = re.compile(r"(?:[<>]=?|[≤≥])\s*$")


def _has_preceding_comparator(head: str) -> bool:
    """True iff the number is governed by a COMPARATOR rather than asserted
    as a value — "below 30 ng/mL", "above 1.5", "< 0.08".

    Only the closest non-filler word counts, matching
    `_has_preceding_copula`'s rule, so "was 24.1, less than the 30
    threshold" still treats 24.1 as a claimed value.
    """
    if _COMPARATOR_SYMBOL_RE.search(head):
        return True
    words = [w.lower() for w in _HEAD_WORD_RE.findall(head)]
    for word in reversed(words[-3:]):
        if word in _COPULA_SKIPPABLE_FILLERS:
            continue
        return word in _COMPARATOR_WORDS
    return False


def _looks_like_bare_year(number_text: str) -> bool:
    """True iff `number_text` is a plain integer (no decimal point) whose
    value falls in a plausible calendar-year range — "since 2024" is a
    date, not a measurement. Only consulted for copula-only evidence (see
    the module-level design comment above)."""
    if "." in number_text:
        return False
    try:
        value = int(number_text)
    except ValueError:
        return False
    return _YEAR_MIN <= value <= _YEAR_MAX


def _quoted_number_is_value_evidence(
    number_text: str, head: str, tail: str, analyte_units: set[str]
) -> bool:
    """Positive-evidence check for whether a quoted number sitting near a
    known analyte name is plausibly THAT analyte's value (see the
    module-level design comment above for the full rationale). `head`/
    `tail` are the (date/titer/range-stripped) clause text immediately
    before/after the quoted number; `analyte_units` are the units THIS
    analyte is actually recorded under."""
    stripped_tail = tail.lstrip()
    lowered_tail = stripped_tail.lower()
    lowered_units = {u.lower() for u in analyte_units if u}

    # A comparator-governed number is a THRESHOLD, not a claimed result, and
    # this veto runs before the positive-evidence check because the usual
    # phrasing attaches a real unit ("below 30 ng/mL") and so would sail
    # through it. See the `_COMPARATOR_WORDS` rationale and ADR 0023: a live
    # diagnostic turn was lost to six of these at once, including "Vitamin D
    # insufficiency is defined as below 30 ng/mL", which asserts nothing
    # about this patient at all.
    if _has_preceding_comparator(head):
        return False

    has_unit_evidence = any(lowered_tail.startswith(unit) for unit in lowered_units)
    has_copula_evidence = _has_preceding_copula(head)
    if not (has_unit_evidence or has_copula_evidence):
        return False

    if lowered_tail.startswith("%") and "%" not in lowered_units:
        return False
    if not has_unit_evidence and _looks_like_bare_year(number_text):
        return False

    for word in _VALUE_TOKEN_RE.findall(stripped_tail)[:2]:
        word_l = word.lower()
        if word_l in _COUNT_CONTEXT_FILLER_WORDS:
            continue
        singular = word_l[:-1] if word_l.endswith("s") else word_l
        return word_l not in _COUNT_CONTEXT_WORDS and singular not in _COUNT_CONTEXT_WORDS
    return True


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


# --------------------------------------------------------------------------
# Structural pairing (2026-08-25, third pass). The first two passes fixed
# WHICH numbers count as candidate values (`_quoted_number_is_value_evidence`
# above); this pass fixes something different and, in production, more
# damaging: WHICH analyte a candidate number is checked against.
#
# The old code found every known analyte label that appeared ANYWHERE in a
# clause, then checked every qualifying number against EVERY one of those
# labels — not just the one it actually refers to. A live production run
# ("FSH was 91.4 and LH 62.9…", "ALT 15, AST 22…") failed with 24
# mismatches that turned out to be entirely this: 91.4 IS a real, correctly-
# quoted FSH value, but it was also checked against LH's stored values (no
# match, since it's FSH's number) and reported as a fabrication. The
# Composer was quoting real values correctly; the checker was pairing them
# with the wrong analyte. A second, distinct flavor of the same root cause:
# "hs-CRP 1.8" matched the literal substring "crp" (word-bounded — a hyphen
# is a non-word character, so `\bcrp\b` matches inside "hs-crp") and got
# checked against plain CRP's stored values, a completely different assay.
#
# This guard had, by this point, failed three real diagnostic turns and
# caught zero genuine fabrications. Given `check_composer_numbers` runs
# AFTER the ledger is already committed (`composer_stage`/
# `composer_number_check_contract` in `reason.stages`), a false positive
# here does not just block a claim — it discards an already-correct,
# already-grounded reply. That asymmetry is why the fix below is
# deliberately conservative: a genuinely ambiguous pairing is left
# unflagged (a missed check costs nothing — the citation checker and
# `verify_claims` already ground the underlying evidence independently),
# never guessed at. See `_resolve_governing_mention`'s docstring for the
# structural pairing rule itself.
# --------------------------------------------------------------------------


def _mention_pattern(labels: Sequence[str]) -> re.Pattern[str] | None:
    """One combined alternation regex matching any of `labels` at a
    `\\b`-bounded position, tried LONGEST-label-first at every start
    position so a shorter label can never win a match a longer, more
    specific label also covers at the same spot — e.g. "hs-crp" must win
    over "crp" when both are known analyte labels for this patient and the
    text reads "hs-CRP"; otherwise "crp" matches the tail substring of
    "hs-crp" (a hyphen is not a word character, so `\\bcrp\\b` matches
    inside it) and a number gets judged against the WRONG analyte's stored
    values. Mirrors the longest-alias-first precedence `labs.validate`
    already applies for its own alias/suffix resolution
    (`_SCORE_SUFFIX_TO_CANONICAL`'s length-sorted table, consulted by
    `canonicalize`) — same idea, applied here to spotting mentions in free
    text rather than resolving a canonical name. `None` when `labels` is
    empty."""
    if not labels:
        return None
    ordered = sorted(set(labels), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(label) for label in ordered) + r")\b")


def _resolve_governing_mention(
    number_match: re.Match[str],
    mentions: Sequence[re.Match[str]],
    text: str,
    unit_index: dict[str, set[str]],
) -> re.Match[str] | None:
    """Which single analyte mention in `mentions` (if any) governs the
    quoted number at `number_match` — the structural-pairing fix (module
    design note above `check_composer_numbers`): a number is checked
    against the ONE analyte mention that actually refers to it, never
    against every analyte name that happens to share its clause.

    A number is governed by the NEAREST mention immediately before it —
    `ALT 15`, `ALT was 15`, `ALT of 15 U/L` — tied to it via
    `_quoted_number_is_value_evidence`'s existing copula/unit evidence,
    scoped to just the text between THAT mention and the number (so a
    copula word belonging to some earlier, unrelated mention can never
    leak in and manufacture false evidence) — or by the nearest mention
    immediately AFTER it — `15 U/L ALT` — tied to it by a unit directly
    attached to the number that belongs to that trailing mention. Because
    both candidates are always the NEAREST mention on their respective
    side, a number can never bind to a mention on the far side of another
    mention sitting in between: there is no "other" mention within reach to
    even consider skipping past.

    Fails safe on ambiguity, per this module's asymmetry (a missed check
    costs nothing; a false positive discards an already-committed, already-
    correct reply): if a number has positive evidence tying it to BOTH a
    different preceding and a different following mention, that pairing is
    genuinely unclear and this returns `None` — no flag — rather than
    guessing which one it meant. `None` too when neither side ties the
    number to any mention at all."""
    num_start, num_end = number_match.start(), number_match.end()
    preceding: re.Match[str] | None = None
    following: re.Match[str] | None = None
    for mention in mentions:
        if mention.end() <= num_start:
            preceding = mention
        elif mention.start() >= num_end and following is None:
            following = mention

    preceding_ok = False
    if preceding is not None:
        local_head = text[preceding.end() : num_start]
        tail = text[num_end:]
        preceding_units = unit_index.get(preceding.group(0), set())
        preceding_ok = _quoted_number_is_value_evidence(
            number_match.group(), local_head, tail, preceding_units
        )

    following_ok = False
    if following is not None:
        gap = text[num_end : following.start()].strip(" ,;:")
        following_units = {u.lower() for u in unit_index.get(following.group(0), set())}
        following_ok = bool(gap) and gap.lower() in following_units

    if preceding_ok and following_ok:
        # Both sides claim this number and disagree on who it belongs to —
        # genuinely ambiguous unless they happen to name the same mention.
        assert preceding is not None and following is not None  # narrows for mypy
        return preceding if preceding.group(0) == following.group(0) else None
    if preceding_ok:
        return preceding
    if following_ok:
        return following
    return None


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
    to a real analyte name. Dates, titer ratios (`1:640`), and range-shaped
    mentions (`0.0-5.0`, e.g. a reference range restated in the reply) are
    stripped before number extraction so their digits never masquerade as a
    quoted result value (mirrors `reason.citations._extract_quoted_numbers`
    exactly, including reusing its `_RANGE_RE`).

    ADR 0016 revised (2026-08-25, second pass): sharing a clause with an
    analyte name is NOT enough on its own — a number is also required to
    carry POSITIVE evidence it is that analyte's value (a directly-attached
    unit, or a copula/preposition tying it to the analyte), via
    `_quoted_number_is_value_evidence` (see that function's module-level
    design comment for the full rationale and the false positives it
    fixes: a percent CHANGE and a bare YEAR both wrongly flagged as a
    claimed analyte value under the first-pass exclusion-list design).

    ADR 0016 revised (2026-08-25, third pass): mentions are found via
    LONGEST-label-first matching (`_mention_pattern`, so "hs-CRP" never
    resolves as plain "CRP"), and each qualifying number is checked against
    the ONE analyte mention that structurally governs it
    (`_resolve_governing_mention`), never against every analyte name that
    happens to share the clause — see the design note above
    `ComposerNumberMismatch` for the production failure this fixes. A
    number that clears both gates and still fails to match any stored
    value for the analyte that actually governs it is exactly the
    fabrication this check exists to catch."""
    index = _analyte_value_index(db)
    if not index:
        return ComposerNumberCheck()
    unit_index = _analyte_unit_index(db)
    mention_pattern = _mention_pattern(list(index.keys()))

    mismatches: list[ComposerNumberMismatch] = []
    for clause in _split_clauses(text):
        cleaned = _DATE_IN_TEXT_RE.sub(" ", clause)
        cleaned = _TITER_RE.sub(" ", cleaned)
        cleaned = _RANGE_RE.sub(" ", cleaned)
        cleaned_lower = cleaned.lower()

        mentions = list(mention_pattern.finditer(cleaned_lower)) if mention_pattern else []
        if not mentions:
            continue
        number_matches = list(_NUMBER_RE.finditer(cleaned_lower))
        if not number_matches:
            continue

        for number_match in number_matches:
            governing = _resolve_governing_mention(
                number_match, mentions, cleaned_lower, unit_index
            )
            if governing is None:
                continue
            label = governing.group(0)
            stored = index[label]
            number = float(number_match.group())
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
    "DEFERRED_CLAIMS_RELPATH",
    "DefaultSourceTextResolver",
    "DeferredClaim",
    "ENTAILMENT_CACHE_RELPATH",
    "EntailmentCache",
    "EntailmentJudgment",
    "SourceTextResolver",
    "VerificationReport",
    "build_composer_number_retry_feedback",
    "build_entailment_retry_feedback",
    "check_composer_numbers",
    "claims_from_ops",
    "log_stripped_claims",
    "log_verification_report",
    "pop_deferred_claims",
    "queue_deferred_claims",
    "strip_not_entailed_ops",
    "verify_claims",
]

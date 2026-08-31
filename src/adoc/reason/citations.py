"""Deterministic citation checker (PLAN.md Phase 2, "Grounding &
anti-hallucination hardening" — first bullet).

`check_diff_citations`/`check_ops_citations` are plain code, NO LLM
(CLAUDE.md: deterministic logic is never delegated to a model): every
`Evidence.source` ref (grammar in `casefile.schema`) carried by a proposed
`LedgerDiff` — or by a raw list of `LedgerOp`s, so callers can check a
Challenger's `additional_ops` before they are ever merged into an applied
diff — is resolved against the actual data (labs db, ingested documents,
encounter files, PMID existence) before it is allowed to reach the ledger.

Four outcomes per ref (`CitationOutcome`):
  - `resolved`: the ref points at real, matching data.
  - `unresolved`: the ref points at nothing (no such analyte/date row, no
    such document/page, no such encounter file, a PMID that does not
    exist).
  - `mismatched`: the ref resolves, but a number the claim quotes disagrees
    with the stored value.
  - `unverifiable`: verification could not be completed (PMID checks only —
    NCBI E-utilities unreachable/timed out). This is deliberately NOT a
    rejection: a DAG turn must never be hostage to NCBI uptime. It still
    passes the citation gate, but is recorded so the gap is visible.

`reason.stages` wires `check_ops_citations` in at two points: as a
DAG-contract gate (nothing unresolved/mismatched may reach `apply`) and as
a same-generation retry loop inside `ledger_maintainer_stage`/
`challenger_stage` (mirrors the composer's gate-guided rewrite loop, PR
#94) so a model gets one chance to fix a bad ref before the deterministic
gate becomes the final word.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    LedgerDiff,
    LedgerOp,
)
from adoc.genomics.panel import GENOMIC_PANEL_RELPATH
from adoc.knowledge.pubmed import fetch_with_retry
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.labs.validate import canonicalize
from adoc.reason.context import ENCOUNTERS_RELDIR

CitationOutcome = Literal["resolved", "unresolved", "mismatched", "unverifiable"]

CITATION_LOG_RELPATH = "logs/citation-checks.jsonl"


class CitationCheck(BaseModel):
    """The result of resolving one `Evidence.source` ref."""

    source: str
    outcome: CitationOutcome
    reason: str
    claim: str = ""


class CitationReport(BaseModel):
    """Every `CitationCheck` produced for one diff/op-list."""

    checks: list[CitationCheck] = Field(default_factory=list)

    @property
    def unresolved(self) -> list[CitationCheck]:
        return [c for c in self.checks if c.outcome == "unresolved"]

    @property
    def mismatched(self) -> list[CitationCheck]:
        return [c for c in self.checks if c.outcome == "mismatched"]

    @property
    def unverifiable(self) -> list[CitationCheck]:
        return [c for c in self.checks if c.outcome == "unverifiable"]

    @property
    def failing(self) -> list[CitationCheck]:
        """Refs that must block the diff: `unresolved` or `mismatched`.
        `unverifiable` is deliberately excluded — see module docstring."""
        return [c for c in self.checks if c.outcome in ("unresolved", "mismatched")]

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "resolved": 0,
            "unresolved": 0,
            "mismatched": 0,
            "unverifiable": 0,
        }
        for check in self.checks:
            counts[check.outcome] += 1
        return counts


# --------------------------------------------------------------------------
# PMID verification
# --------------------------------------------------------------------------


class PmidVerifier(Protocol):
    def verify(self, pmid: str) -> Literal["found", "not_found", "error"]: ...


PmidTransport = Callable[[str], bytes]

_EUTILS_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_PMID_NOT_FOUND_TTL_DAYS = 30
_PMID_VERIFIER_TIMEOUT_SECONDS = 5.0
PMID_CACHE_RELPATH = "work/pmid-cache.json"


class EutilsPmidVerifier:
    """Real `PmidVerifier`: NCBI E-utilities `esummary`, stdlib `urllib` only
    (CLAUDE.md: no new runtime deps).

    Caches at `cache_path` (data-repo `work/pmid-cache.json`): a PMID found
    once is cached `"found"` forever (a real PMID never stops existing); a
    definitive `"not_found"` is cached for `_PMID_NOT_FOUND_TTL_DAYS` days
    only, since a brand-new PMID can be indexed later. A transport failure
    (timeout, connection error, malformed response) is NEVER cached and
    always reported as `"error"` — a transient NCBI outage must never
    freeze a not-found verdict in place, and must never itself become a
    cached rejection.
    """

    def __init__(
        self,
        cache_path: Path,
        *,
        transport: PmidTransport | None = None,
        timeout: float = _PMID_VERIFIER_TIMEOUT_SECONDS,
    ) -> None:
        self._cache_path = cache_path
        self._timeout = timeout
        self._transport = transport or self._default_transport

    def _default_transport(self, url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=self._timeout) as response:  # noqa: S310
            result: bytes = response.read()
            return result

    def _load_cache(self) -> dict[str, Any]:
        if not self._cache_path.exists():
            return {}
        try:
            loaded: Any = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")

    def verify(self, pmid: str) -> Literal["found", "not_found", "error"]:
        cache = self._load_cache()
        entry = cache.get(pmid)
        if isinstance(entry, dict):
            if entry.get("status") == "found":
                return "found"
            if entry.get("status") == "not_found":
                try:
                    checked_at = datetime.fromisoformat(entry["checked_at"])
                except (KeyError, ValueError):
                    checked_at = None
                if checked_at is not None and (
                    datetime.now(UTC) - checked_at < timedelta(days=_PMID_NOT_FOUND_TTL_DAYS)
                ):
                    return "not_found"

        url = f"{_EUTILS_ESUMMARY_URL}?db=pubmed&id={pmid}&retmode=json"
        try:
            # Share the throttle with PubMed search. Both hit E-utilities, and
            # two independent limiters produce twice the published rate between
            # them -- which is how a real, findable PMID ends up recorded
            # `unverifiable` for no better reason than our own traffic. Observed
            # live: one of two freshly-searched PMIDs came back "error" purely
            # because the search that produced it had just used the budget.
            raw = fetch_with_retry(self._transport, url)
            data = json.loads(raw)
            result = data.get("result", {})
            uids = result.get("uids", [])
            record = result.get(pmid, {})
            found = pmid in uids and "error" not in record
        except Exception:  # noqa: BLE001 - any transport/parse failure is "error", never a crash
            return "error"

        status: Literal["found", "not_found"] = "found" if found else "not_found"
        cache[pmid] = {"status": status, "checked_at": datetime.now(UTC).isoformat()}
        self._save_cache(cache)
        return status


# --------------------------------------------------------------------------
# Numeric-claim extraction (labs: value check)
# --------------------------------------------------------------------------

_DATE_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TITER_RE = re.compile(r"\d+\s*:\s*\d+")
_RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Words that put the direction of a change in the prose rather than on the
# number. A percent-change row stores -8.0; the panel writes "a decline of
# 8%". Both say the same thing.
_DECLINE_WORD_RE = re.compile(
    r"\b(?:declin\w*|decreas\w*|drop\w*|fell|fall\w*|loss|lost|lower\w*|"
    r"reduc\w*|down|negative)\b",
    re.IGNORECASE,
)


# A number hyphenated to a word is a compound MODIFIER, not a value:
# "10-year probability", "25-hydroxy vitamin D", "6-minute walk". Stripped
# alongside dates and titers for exactly the same reason — the digits are
# part of a name, and comparing them to a stored result is meaningless. The
# hyphen is what distinguishes this from a real reading, which is never
# glued to a following word.
_COMPOUND_MODIFIER_RE = re.compile(r"\b\d+(?:\.\d+)?-[A-Za-z]\w*")


# The MIRROR of `_COMPOUND_MODIFIER_RE`: digits glued to the END of a name,
# not the start. Analytes are full of them — CA-125, HLA-B27, IL-6, CD4, C3,
# T4, B12, A1C — and `_NUMBER_RE`'s optional sign turns the hyphen into a
# minus, so "CA-125 was normal" quoted `-125.0`. A live review dropped a
# perfectly good citation of a real CA-125 row because -125.0 did not equal
# the stored 27.7.
#
# The digits must be GLUED to the letters (optionally through one hyphen) for
# this to fire, which is what separates a name from a reading: prose writes a
# value with a space — "CRP 8.5", "FSH 91.4" — and those are untouched.
# The trailing `[A-Za-z]*` matters: "HbA1c" has a letter AFTER its digit, so
# a pattern anchored on a word boundary right after the digits misses it
# and leaks a bare 1 into the quoted numbers. That cost a real HbA1c
# citation on a live review.
_ANALYTE_NAME_DIGITS_RE = re.compile(r"\b[A-Za-z]+-?\d+(?:\.\d+)?[A-Za-z]*\b")


# A bare four-digit year introduced by a temporal preposition is a date, not
# a result: "percent change vs 2024" quoted `2024.0`. Deliberately narrow —
# an unqualified 4-digit number is stripped only with that lead-in, because
# real analytes do live in this range (vitamin B12 in the 2000s pg/mL, a
# platelet count, a ferritin), and silently discarding those would trade one
# false positive for a worse false negative.
_YEAR_IN_CONTEXT_RE = re.compile(
    r"\b(?:since|vs\.?|versus|from|during|in|by|after|before)\s+(?:19|20)\d{2}\b"
    # A month name in front of a year makes it a date, whatever follows.
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:19|20)\d{2}\b"
    # "the 2025 panel", "your 2024 results" — a determiner in front of a year
    # is a date qualifying a noun, never a measurement. This is the case that
    # survived the first pass: a live review dropped a real TSAT citation for
    # "quoting" 2025.
    r"|\b(?:the|that|your|her|his|our)\s+(?:19|20)\d{2}\b"
    # A parenthesised year is a citation aside, e.g. "(2024)".
    r"|\((?:19|20)\d{2}\)",
    re.IGNORECASE,
)

# A threshold inside a RATIO claim. "FSH/LH ratio > 1.0" quotes 1.0 as the
# cut-off being compared against, not as a value of either analyte — and a
# live review dropped real FSH and HbA1c citations for exactly that.
#
# Scoped to claims containing "ratio" on purpose. Stripping every number after
# a comparator would break the opposite case: labs genuinely report results as
# "<0.08", and that number IS the value (`LabResult.comparator` exists for
# it). Narrowing to ratios keeps both behaviours correct.
_RATIO_THRESHOLD_RE = re.compile(
    r"(?:[<>]=?|≥|≤|greater than|less than|above|below|over|under|at least|of)\s*"
    r"-?\d+(?:\.\d+)?",
    re.IGNORECASE,
)


def _extract_quoted_numbers(claim: str) -> list[float]:
    """Decimal literals a claim quotes, per PLAN.md's Phase-2 spec: dates
    (`YYYY-MM-DD`), titer ratios (`1:640`), and range-shaped mentions
    (`3.5-5.1`, e.g. a reference range restated in the claim) are stripped
    first so their digits never masquerade as a quoted result value — as are
    digits belonging to an analyte's NAME (`CA-125`) and years introduced by
    a temporal preposition (`vs 2024`), each of which cost a real citation on
    a live review."""
    cleaned = _DATE_IN_TEXT_RE.sub(" ", claim)
    cleaned = _TITER_RE.sub(" ", cleaned)
    cleaned = _RANGE_RE.sub(" ", cleaned)
    cleaned = _COMPOUND_MODIFIER_RE.sub(" ", cleaned)
    cleaned = _ANALYTE_NAME_DIGITS_RE.sub(" ", cleaned)
    cleaned = _YEAR_IN_CONTEXT_RE.sub(" ", cleaned)
    if "ratio" in cleaned.lower():
        cleaned = _RATIO_THRESHOLD_RE.sub(" ", cleaned)
    return [float(m) for m in _NUMBER_RE.findall(cleaned)]


# --------------------------------------------------------------------------
# labs: ref resolution
# --------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_slug(text: str) -> str:
    """Case/punctuation-insensitive key for slug matching — deliberately a
    local, separate implementation from `labs.validate`'s private
    `_normalize` (same idea: strip everything but letters/digits, lowercase)
    so this module never reaches into another module's private helper."""
    return _NON_ALNUM_RE.sub("", text.lower())


# Narrative reports (a DEXA/FRAX summary, an imaging impression) yield rows
# whose "analyte name" is really a prose lead-in with the value at the end of
# the sentence: "10-year probability of hip fracture IS", "Left total hip: a
# statistically significant decrease OF". A model citing that row writes the
# sensible slug and drops the dangling connective, and the citation then
# failed to resolve — costing a real diagnostic turn 203 seconds at the
# ledger-maintainer's citation check, for a row that was present and correctly
# cited. Only these trailing connectives are shed, and only from the END of a
# stored name: the remainder must still match in full, so no cited slug can
# resolve onto a DIFFERENT analyte.
_TRAILING_CONNECTIVES = ("is", "of", "was", "are", "were", "shows", "at", "to")


def _shed_trailing_connectives(name: str) -> str:
    """Drop trailing connective words from a stored analyte name."""
    words = name.split()
    while words and words[-1].lower().strip(":,;") in _TRAILING_CONNECTIVES:
        words.pop()
    return " ".join(words)


def _row_slug_candidates(row: LabResult) -> set[str]:
    """Every normalized slug a stored row could plausibly be cited under:
    its own stored `name`/`name_raw`, whatever `labs.validate.canonicalize`
    maps either of those onto (PLAN.md: "also accept via
    labs.validate.canonicalize so a spec-canonical slug matches") — this
    lets a model cite an `ANALYTE_SPECS` canonical name even when the
    stored row's own `name`/`name_raw` spelling differs slightly — and the
    same names with any trailing connective shed (see above)."""
    candidates = {row.name, row.name_raw}
    for candidate in (row.name, row.name_raw):
        mapped = canonicalize(candidate)
        if mapped is not None:
            candidates.add(mapped)
    candidates |= {_shed_trailing_connectives(c) for c in list(candidates) if c}
    return {_normalize_slug(c) for c in candidates if c}


def _check_labs_ref(source: str, claim: str, db: LabsDb) -> CitationCheck:
    _, slug, date_str = source.split(":", 2)
    try:
        ref_date = date.fromisoformat(date_str)
    except ValueError:
        return CitationCheck(
            source=source, outcome="unresolved", reason=f"invalid date {date_str!r}", claim=claim
        )

    slug_norm = _normalize_slug(slug)
    matches = [
        row
        for row in db.all_non_rejected_rows()
        if row.date == ref_date and slug_norm in _row_slug_candidates(row)
    ]
    if not matches:
        return CitationCheck(
            source=source,
            outcome="unresolved",
            reason=f"no lab row found for analyte slug {slug!r} on {date_str}",
            claim=claim,
        )

    quoted_numbers = _extract_quoted_numbers(claim)
    if not quoted_numbers:
        return CitationCheck(
            source=source,
            outcome="resolved",
            reason="row exists; claim quotes no number",
            claim=claim,
        )

    numeric_rows = [row for row in matches if row.value is not None]
    if not numeric_rows:
        return CitationCheck(
            source=source,
            outcome="resolved",
            reason="row exists (non-numeric result); nothing to compare a quoted number against",
            claim=claim,
        )

    states_decline = bool(_DECLINE_WORD_RE.search(claim))
    for row in numeric_rows:
        assert row.value is not None
        for number in quoted_numbers:
            if abs(row.value - number) <= 1e-9:
                return CitationCheck(
                    source=source,
                    outcome="resolved",
                    reason=f"claimed value {number} matches stored value {row.value}",
                    claim=claim,
                )
            # A claim can carry the sign in WORDS while the row carries it as
            # a minus: "a decline of 8%" against a stored -8.0. Three real
            # citations of three real DXA rows were dropped for quoting 8.0,
            # 6.7 and 7.0 against -8.0, -6.7 and -7.0. Magnitude is accepted
            # only when the row is negative AND the claim says so lexically,
            # so a claim that a value ROSE still fails against a fall.
            if states_decline and row.value < 0 and abs(abs(row.value) - abs(number)) <= 1e-9:
                return CitationCheck(
                    source=source,
                    outcome="resolved",
                    reason=(
                        f"claimed decline of {number} matches stored value {row.value} "
                        "(direction stated in words, sign stored on the value)"
                    ),
                    claim=claim,
                )

    stored = ", ".join(str(row.value) for row in numeric_rows)
    return CitationCheck(
        source=source,
        outcome="mismatched",
        reason=(
            f"claim quotes {quoted_numbers} but the stored value(s) for {slug!r} on "
            f"{date_str} are [{stored}]"
        ),
        claim=claim,
    )


# --------------------------------------------------------------------------
# doc:/encounter:/pmid:/patient-report: ref resolution
# --------------------------------------------------------------------------


def _check_doc_ref(source: str, claim: str, db: LabsDb) -> CitationCheck:
    rest = source[len("doc:") :]
    filename, sep, page_str = rest.rpartition("#p")
    if not sep:
        # Page-less ref: "this document", valid since the document-text
        # corpus made unpaginated records (.docx, .txt) citable. Resolve
        # the document itself; there is no page to bounds-check.
        filename = rest
        doc = next((d for d in db.list_documents() if d.filename == filename), None)
        if doc is None:
            return CitationCheck(
                source=source,
                outcome="unresolved",
                reason=f"no ingested document named {filename!r}",
                claim=claim,
            )
        return CitationCheck(source=source, outcome="resolved", reason="", claim=claim)
    try:
        page = int(page_str)
    except ValueError:
        return CitationCheck(
            source=source,
            outcome="unresolved",
            reason=f"invalid page number {page_str!r}",
            claim=claim,
        )

    doc = next((d for d in db.list_documents() if d.filename == filename), None)
    if doc is None:
        return CitationCheck(
            source=source,
            outcome="unresolved",
            reason=f"no ingested document named {filename!r}",
            claim=claim,
        )
    if page < 1 or page > doc.page_count:
        return CitationCheck(
            source=source,
            outcome="unresolved",
            reason=f"document {filename!r} has {doc.page_count} page(s); ref cites page {page}",
            claim=claim,
        )
    return CitationCheck(
        source=source, outcome="resolved", reason="document and page exist", claim=claim
    )


def _check_encounter_ref(source: str, claim: str, repo: DataRepo) -> CitationCheck:
    filename = source[len("encounter:") :]
    path = repo.root / ENCOUNTERS_RELDIR / filename
    if path.is_file():
        return CitationCheck(
            source=source, outcome="resolved", reason="encounter file exists", claim=claim
        )
    return CitationCheck(
        source=source,
        outcome="unresolved",
        reason=f"no encounter file named {filename!r}",
        claim=claim,
    )


def _check_pmid_ref(source: str, claim: str, pmid_verifier: PmidVerifier | None) -> CitationCheck:
    pmid = source[len("pmid:") :]
    if pmid_verifier is None:
        return CitationCheck(
            source=source,
            outcome="unverifiable",
            reason="no PMID verifier configured",
            claim=claim,
        )
    status = pmid_verifier.verify(pmid)
    if status == "found":
        return CitationCheck(
            source=source,
            outcome="resolved",
            reason=f"PMID {pmid} found via NCBI E-utilities",
            claim=claim,
        )
    if status == "not_found":
        return CitationCheck(
            source=source,
            outcome="unresolved",
            reason=f"PMID {pmid} not found via NCBI E-utilities",
            claim=claim,
        )
    return CitationCheck(
        source=source,
        outcome="unverifiable",
        reason=f"PMID {pmid} verification unavailable (network failure/timeout)",
        claim=claim,
    )


def _check_genomic_ref(source: str, claim: str, repo: DataRepo) -> CitationCheck:
    """Resolve `genomic:<gene>:<rsid>` against the built panel (ADR 0030).

    Checks the gene as well as the marker. A ref naming a real rsid under the
    wrong gene is as wrong as an invented one, and letting it pass would mean
    a claim could cite a marker that says nothing about the gene it names.

    A marker present on the array but NOT CALLED resolves as `unverifiable`
    rather than `unresolved`: the ref is legitimate and the panel does carry
    the marker, but there is no genotype behind it. Recorded so the gap is
    visible, and deliberately not a rejection — the same treatment an
    unreachable PMID gets.
    """
    _, _, rest = source.partition("genomic:")
    gene, _, rsid = rest.partition(":")
    panel_path = repo.root / Path(GENOMIC_PANEL_RELPATH)
    if not panel_path.is_file():
        return CitationCheck(
            source=source,
            outcome="unresolved",
            reason="no genomic panel has been built for this case file",
            claim=claim,
        )
    text = panel_path.read_text(encoding="utf-8")
    if source in text:
        return CitationCheck(
            source=source, outcome="resolved", reason="genotype on the panel", claim=claim
        )
    # The ref is grammatical and the marker may be in the panel while having
    # no genotype — distinguish that from a marker nobody measured.
    if rsid and rsid in text and gene and gene in text:
        return CitationCheck(
            source=source,
            outcome="unverifiable",
            reason="marker is on the panel but was not called on the array",
            claim=claim,
        )
    return CitationCheck(
        source=source,
        outcome="unresolved",
        reason="no such gene/marker pair on the genomic panel",
        claim=claim,
    )


def _check_source(
    source: str,
    claim: str,
    db: LabsDb,
    repo: DataRepo,
    pmid_verifier: PmidVerifier | None,
) -> CitationCheck:
    if source.startswith("labs:"):
        return _check_labs_ref(source, claim, db)
    if source.startswith("doc:"):
        return _check_doc_ref(source, claim, db)
    if source.startswith("encounter:"):
        return _check_encounter_ref(source, claim, repo)
    if source.startswith("pmid:"):
        return _check_pmid_ref(source, claim, pmid_verifier)
    if source.startswith("genomic:"):
        return _check_genomic_ref(source, claim, repo)
    if source.startswith("patient-report:"):
        # Always resolved: it cites the patient's own statement, and
        # grammar-validity (enforced by `casefile.schema`) is enough.
        return CitationCheck(
            source=source, outcome="resolved", reason="patient's own statement", claim=claim
        )
    return CitationCheck(  # pragma: no cover - schema validation excludes this in practice
        source=source, outcome="unresolved", reason="unrecognized source ref grammar", claim=claim
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def _iter_evidence(ops: Sequence[LedgerOp]) -> Iterator[Evidence]:
    for op in ops:
        if isinstance(op, AddHypothesis):
            yield from op.hypothesis.evidence_for
            yield from op.hypothesis.evidence_against
        elif isinstance(op, AddEvidence):
            yield op.evidence


def check_evidence_citations(
    evidence: Sequence[Evidence],
    db: LabsDb,
    repo: DataRepo,
    *,
    pmid_verifier: PmidVerifier | None = None,
) -> CitationReport:
    """Resolve every `Evidence.source` ref in `evidence` directly (no
    `LedgerOp` wrapping needed) — the primitive `check_ops_citations`
    delegates to, and also usable standalone by a caller that already has a
    plain `list[Evidence]` in hand (e.g. `reason.stages`'s Phase-2
    abstention contract, which inspects one applied `Hypothesis.evidence_for`
    at a time, not a `LedgerOp` sequence)."""
    checks = [_check_source(item.source, item.claim, db, repo, pmid_verifier) for item in evidence]
    return CitationReport(checks=checks)


def check_ops_citations(
    ops: Sequence[LedgerOp],
    db: LabsDb,
    repo: DataRepo,
    *,
    pmid_verifier: PmidVerifier | None = None,
) -> CitationReport:
    """Resolve every `Evidence.source` ref carried by `ops` (from
    `AddHypothesis.hypothesis.evidence_for`/`evidence_against` and
    `AddEvidence.evidence`) — the primitive `check_diff_citations` and the
    Challenger's `additional_ops` check (`reason.stages`) both call."""
    return check_evidence_citations(
        list(_iter_evidence(ops)), db, repo, pmid_verifier=pmid_verifier
    )


def check_diff_citations(
    diff: LedgerDiff,
    db: LabsDb,
    repo: DataRepo,
    *,
    pmid_verifier: PmidVerifier | None = None,
) -> CitationReport:
    """Resolve every evidence source ref in `diff.ops` (PLAN.md Phase 2:
    "every evidence claim's source ref ... is resolved by code"). Pure code,
    no LLM call, no mutation of `db`/`repo`."""
    return check_ops_citations(diff.ops, db, repo, pmid_verifier=pmid_verifier)


def build_retry_feedback(report: CitationReport) -> str:
    """Render `report.failing` as feedback text for a same-generation
    retry: names each failed ref, why it failed, and instructs the model to
    cite only sources that exist or drop the claim (mirrors
    `composer_stage`'s gate-guided rewrite feedback, PR #94)."""
    lines = ["The following evidence source ref(s) failed citation verification:"]
    for check in report.failing:
        lines.append(f"- {check.source} [{check.outcome}]: {check.reason}")
    lines.append(
        "Fix this by citing only a source ref that actually resolves to real, matching "
        "data, or by dropping the claim entirely if no valid source exists. Return the "
        "complete corrected result in the same schema."
    )
    return "\n".join(lines)


def log_citation_report(repo: DataRepo, report: CitationReport, *, dag_node: str) -> None:
    """Append one JSON line recording `report`'s outcome counts (and full
    detail on any failing ref) to `logs/citation-checks.jsonl` — the
    provenance trail for this Phase-2 slice (PLAN.md "Provenance &
    re-evaluation policy"), following the same append-only JSONL pattern as
    `reason.client.LlmClient`'s `logs/api-audit.jsonl`."""
    path = repo.root / CITATION_LOG_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "dag_node": dag_node,
        "counts": report.counts,
        "failing": [c.model_dump(mode="json") for c in report.failing],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

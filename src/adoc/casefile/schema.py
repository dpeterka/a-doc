"""Pydantic v2 models for the case file: ledger, hypotheses, evidence, diffs.

See `PLAN.md` "Key schemas" and "Provenance & re-evaluation policy". These
models are pure data shapes; the invariants that govern how a `LedgerDiff` is
allowed to mutate a `Ledger` live in `adoc.casefile.ledger` (deterministic
code, never delegated to a model per `CLAUDE.md` code conventions).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

# --- shared literal vocabularies -------------------------------------------------

Tier = Literal["most-likely", "expanded", "cant-miss"]
ProbabilityBucket = Literal["high", "moderate", "low", "minimal"]
HypothesisStatus = Literal[
    "active",
    "patient-proposed",
    "challenged",
    "ruled-out",
    "confirmed-by-doctor",
    "parked",
]
Origin = Literal["model", "patient", "doctor", "challenger"]
EvidenceStrength = Literal["strong", "moderate", "weak"]

# --- source-ref grammar (PLAN.md "Key schemas") -----------------------------------
#
#   labs:<analyte-slug>:<YYYY-MM-DD>
#   doc:<filename>#p<int>
#   encounter:<filename>
#   pmid:<digits>
#   patient-report:<YYYY-MM-DD>

_DATE_RE = r"\d{4}-\d{2}-\d{2}"
_SLUG_RE = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_FILENAME_RE = r"[^\s#]+"

SOURCE_REF_PATTERN = re.compile(
    rf"^(?:"
    rf"labs:{_SLUG_RE}:{_DATE_RE}"
    rf"|doc:{_FILENAME_RE}#p\d+"
    rf"|encounter:{_FILENAME_RE}"
    rf"|pmid:\d+"
    rf"|patient-report:{_DATE_RE}"
    rf")$"
)

HYPOTHESIS_ID_PATTERN = re.compile(rf"^{_SLUG_RE}$")
MONDO_ID_PATTERN = re.compile(r"^MONDO:\d+$")


def validate_source_ref(value: str) -> str:
    """Validate a claim source ref against the grammar; raise ValueError if invalid."""
    if not SOURCE_REF_PATTERN.match(value):
        raise ValueError(
            f"invalid source ref {value!r}: must match labs:<slug>:<date> | "
            "doc:<file>#p<int> | encounter:<file> | pmid:<digits> | "
            "patient-report:<date>"
        )
    return value


class Provenance(BaseModel):
    """Stamped on every persisted LLM-derived artifact (PLAN.md "Provenance...")."""

    app_version: str
    prompt_template_version: str
    model_id: str
    dag_node: str
    timestamp: datetime


class Evidence(BaseModel):
    """A single evidence-for/evidence-against claim, grounded in a source ref."""

    claim: str
    source: str
    strength: EvidenceStrength

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        return validate_source_ref(value)


class Hypothesis(BaseModel):
    """One differential-ledger entry.

    Note on `last_challenged_version`: it doubles as the hypothesis's
    "freshness clock" — set to the ledger version at which the hypothesis was
    created (by `AddHypothesis`) *and* reset by `RecordChallenge`. This is
    what `ledger.stale_hypotheses` / invariant (c) compare against. Whether
    the hypothesis has ever been *substantively* challenged (as opposed to
    merely created) is tracked separately by `last_challenged` (a date),
    which `AddHypothesis` never sets — only `RecordChallenge` does. This
    split is what lets invariant (b) tell "just created" apart from
    "created, then actually challenged in a later diff".
    """

    id: str
    name: str
    mondo: str | None = None
    tier: Tier
    probability: ProbabilityBucket
    prior_probability: ProbabilityBucket | None = None
    status: HypothesisStatus
    origin: Origin
    first_proposed: date
    evidence_for: list[Evidence] = Field(default_factory=list)
    evidence_against: list[Evidence] = Field(default_factory=list)
    discriminators: list[str] = Field(default_factory=list)
    challenger_notes: str = ""
    last_challenged: date | None = None
    last_challenged_version: int | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not HYPOTHESIS_ID_PATTERN.match(value):
            raise ValueError(f"invalid hypothesis id {value!r}: must be a stable slug")
        return value

    @field_validator("mondo")
    @classmethod
    def _check_mondo(cls, value: str | None) -> str | None:
        if value is not None and not MONDO_ID_PATTERN.match(value):
            raise ValueError(f"invalid mondo id {value!r}: expected 'MONDO:<digits>'")
        return value


class Ledger(BaseModel):
    """The full `differential-ledger.yaml` document."""

    version: int
    updated: datetime
    schema_version: Literal[1] = 1
    hypotheses: list[Hypothesis] = Field(default_factory=list)


# --- LedgerDiff: a discriminated union of ops --------------------------------------
#
# Produced by an LLM stage, applied by deterministic code in `ledger.py`.
# `RemoveHypothesis` is deliberately not defined: history is never deleted,
# only status changes (e.g. to `ruled-out` or `parked`).


class AddHypothesis(BaseModel):
    op: Literal["add_hypothesis"] = "add_hypothesis"
    hypothesis: Hypothesis


class UpdateHypothesis(BaseModel):
    op: Literal["update_hypothesis"] = "update_hypothesis"
    id: str
    tier: Tier | None = None
    probability: ProbabilityBucket | None = None
    status: HypothesisStatus | None = None
    discriminators: list[str] | None = None


class AddEvidence(BaseModel):
    op: Literal["add_evidence"] = "add_evidence"
    id: str
    for_or_against: Literal["for", "against"]
    evidence: Evidence


class RecordChallenge(BaseModel):
    op: Literal["record_challenge"] = "record_challenge"
    id: str
    note: str


LedgerOp = Annotated[
    AddHypothesis | UpdateHypothesis | AddEvidence | RecordChallenge,
    Field(discriminator="op"),
]


class LedgerDiff(BaseModel):
    """A structured, code-applied mutation of a `Ledger`."""

    provenance: Provenance
    rationale: str
    ops: list[LedgerOp]

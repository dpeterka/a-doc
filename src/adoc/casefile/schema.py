"""Pydantic v2 models for the case file: ledger, hypotheses, evidence, diffs.

See `PLAN.md` "Key schemas" and "Provenance & re-evaluation policy". These
models are pure data shapes; the invariants that govern how a `LedgerDiff` is
allowed to mutate a `Ledger` live in `adoc.casefile.ledger` (deterministic
code, never delegated to a model per `CLAUDE.md` code conventions).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

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
#   engine:<engine-name>:<YYYY-MM-DD>

_DATE_RE = r"\d{4}-\d{2}-\d{2}"
# Slugs are model-generated from real analyte names, which include %, ., (),
# / and other punctuation ("% SATURATION", "B. MIYAMOTOI AB (IGG)") - a live
# challenger verdict died on 'labs:%-saturation:...'. Accept any run of
# non-colon, non-whitespace characters; the slug's SEMANTIC resolution
# against the labs table is Phase 2's citation checker, not this regex.
_SLUG_RE = r"[^\s:]+"
# Spaces are legal: real ingested filenames look like "Comprehensive
# Clinical Context and Longitudinal Health History.docx". Excluding
# whitespace made every such document uncitable — a real run died on
# exactly that name. `#` stays excluded because it delimits the optional
# `#p<int>` page suffix, and newlines because a ref is one line.
_FILENAME_RE = r"[^#\n]+"
# Deliberately a CLOSED set, unlike the other slugs here. Every other ref
# names something the patient's record already contains, so the grammar has
# to accept whatever is on file; an engine ref names a component of this
# system, and the list of engines is known at build time. A typo'd
# `engine:liricl:...` should be a validation error, not a citation that
# resolves to nothing.
_ENGINE_RE = r"(?:lirical|semsim)"

SOURCE_REF_PATTERN = re.compile(
    rf"^(?:"
    rf"labs:{_SLUG_RE}:{_DATE_RE}"
    # `#p<int>` is OPTIONAL. Requiring it assumed every document is
    # paginated, which was true when the only citable documents were
    # scanned PDFs. The document-text corpus (ADR 0015) made `.docx` and
    # plain-text records citable too, and they have no pages — a real run
    # died rejecting `doc:Comprehensive Clinical Context and Longitudinal
    # Health History.docx`. A page-less ref means "this document"; the
    # resolver and the citation checker both already handle that form.
    rf"|doc:{_FILENAME_RE}(?:#p\d+)?"
    rf"|encounter:{_FILENAME_RE}"
    rf"|pmid:\d+"
    rf"|patient-report:{_DATE_RE}"
    # A phenotype engine's own verdict, dated by the review that ran it:
    # `engine:lirical:2026-08-31`. LIRICAL's likelihood ratio and the
    # similarity index's Resnik score are real, reproducible observations
    # about this patient's phenotype, and a hypothesis that exists BECAUSE an
    # engine ranked it has to be able to say so. Without a scheme of its own
    # that evidence had nowhere to point: `doc:` and `encounter:` describe
    # files that do not exist for a computation, and citing a `pmid:` for the
    # engine's method would attribute a claim about this patient to a paper
    # that never saw her.
    #
    # The engine name is a fixed slug, not a free run: the review report for
    # that date carries the full ranking, so `engine:lirical:<date>` resolves
    # to something a reader can actually go and check.
    rf"|engine:{_ENGINE_RE}:{_DATE_RE}"
    rf")$"
)

HYPOTHESIS_ID_PATTERN = re.compile(rf"^{_SLUG_RE}$")
MONDO_ID_PATTERN = re.compile(r"^MONDO:\d+$")


# A model that has just written a ref tends to want to explain it, and appends
# a parenthetical: `patient-report:2026-09-20 (as referenced in proposed diff;
# not yet corroborated by clinician exam)`. The ref itself is perfectly valid —
# only the commentary is not. Stripping it recovers the citation instead of
# discarding a real one over punctuation. Applied ONLY after a plain match has
# already failed, so a filename that legitimately contains parentheses is never
# touched.
_TRAILING_ANNOTATION_RE = re.compile(r"\s*\([^()]*\)\s*$")


def normalize_source_ref(value: str) -> str | None:
    """The valid ref inside `value`, or `None` if there isn't one.

    Salvage, not guesswork: this only strips a trailing parenthetical and
    surrounding whitespace. It never rewrites a scheme, invents a date, or
    maps one ref onto another.
    """
    candidate = value.strip()
    if SOURCE_REF_PATTERN.match(candidate):
        return candidate
    stripped = _TRAILING_ANNOTATION_RE.sub("", candidate).strip()
    if stripped != candidate and SOURCE_REF_PATTERN.match(stripped):
        return stripped
    return None


def validate_source_ref(value: str) -> str:
    """Validate a claim source ref against the grammar; raise ValueError if invalid."""
    normalized = normalize_source_ref(value)
    if normalized is None:
        raise ValueError(
            f"invalid source ref {value!r}: must match labs:<slug>:<date> | "
            "doc:<file>#p<int> | encounter:<file> | pmid:<digits> | "
            "patient-report:<date>"
        )
    return normalized


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

    @model_validator(mode="before")
    @classmethod
    def _drop_unciteable_evidence(cls, data: Any) -> Any:
        """Drop evidence whose source ref cannot be salvaged, keep the rest.

        ADR 0028's rule, applied where it plainly was not. A `field_validator`
        on `Evidence.source` raises, and a raise inside a nested model fails
        the WHOLE payload: a live Challenger turn died on two bad refs out of
        one hypothesis's evidence and took every valid op in the verdict with
        it. One unciteable claim must cost itself and nothing else.

        Logged loudly, because a silently dropped claim is a claim nobody
        knows was made. The hypothesis survives with the evidence that does
        resolve; if that leaves it with none, the ledger invariants — not this
        filter — are what decide whether it may stand.
        """
        if not isinstance(data, dict):
            return data
        for field in ("evidence_for", "evidence_against"):
            items = data.get(field)
            if not isinstance(items, list):
                continue
            kept = []
            for item in items:
                source = (
                    item.get("source") if isinstance(item, dict) else getattr(item, "source", None)
                )
                if source is None or normalize_source_ref(str(source)) is not None:
                    kept.append(item)
                else:
                    logger.warning(
                        "dropping evidence with unciteable source %r from hypothesis %r",
                        source,
                        data.get("id", "<unknown>"),
                    )
            data[field] = kept
        return data

    id: str
    name: str
    plain_language: str = ""
    """One or two sentences saying what this condition IS, in words a patient
    can read.

    The name alone is not communication: "Primary ovarian insufficiency /
    menopausal-range hypogonadism" is precise and tells the person whose case
    file it is nothing at all. Defaulted so every existing ledger round-trips;
    populated going forward by the challenge sweep, which visits every active
    hypothesis on every review and so backfills the ones created before this
    field existed."""
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
    """Findings that would tell this hypothesis apart from its neighbours.

    Undocumented until now, and nothing ever asked a stage to populate it:
    the only mention in any prompt was `test_chooser.md` telling the chooser
    not to duplicate one. On the live ledger that left 11 of 50 populated and
    3 of the 39 retirement-eligible, so the mechanism existed and could never
    fire (ADR 0035).
    """

    rule_out: str = ""
    """The finding that would KILL this hypothesis, stated when it is created.

    Distinct from `discriminators`, and pointing the other way in time. A
    discriminator separates this hypothesis from a neighbour; a rule-out is
    the specific result that ends it — "a normal repeat FSH on a draw four or
    more weeks later", "a negative cartilage biopsy".

    This is what lets a hypothesis die of natural causes. Without it a
    well-supported but wrong hypothesis lives forever, because nothing ever
    defines what would settle it: across twelve ledger versions not one of
    fifty hypotheses had ever left `active`.

    Defaulted so every existing ledger round-trips, exactly as
    `plain_language` was.
    """

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
    plain_language: str | None = None
    tier: Tier | None = None
    probability: ProbabilityBucket | None = None
    status: HypothesisStatus | None = None
    discriminators: list[str] | None = None
    rule_out: str | None = None
    """Settable after creation so a later review can supply the falsification
    condition for a hypothesis that predates the field (ADR 0035)."""


class AddEvidence(BaseModel):
    op: Literal["add_evidence"] = "add_evidence"
    id: str
    for_or_against: Literal["for", "against"]
    evidence: Evidence


MIN_SUBSTANTIVE_NOTE_LENGTH = 20


class RecordChallenge(BaseModel):
    op: Literal["record_challenge"] = "record_challenge"
    id: str
    note: str

    @field_validator("note")
    @classmethod
    def _check_note_is_substantive(cls, value: str) -> str:
        """A challenge note must carry actual substance —
        schema-level `min_length` alone can't express "after stripping
        whitespace", so this validator does the strip-then-length check.
        A model (or test double) stamping "." or "reviewed" across every
        hypothesis is not a substantive challenge (see also
        `reason.review`'s challenge-sweep/adjudication completeness
        contracts, which enforce the same floor at the DAG layer)."""
        if len(value.strip()) < MIN_SUBSTANTIVE_NOTE_LENGTH:
            raise ValueError(
                "RecordChallenge.note must be substantive: at least "
                f"{MIN_SUBSTANTIVE_NOTE_LENGTH} characters after stripping whitespace"
            )
        return value


LedgerOp = Annotated[
    AddHypothesis | UpdateHypothesis | AddEvidence | RecordChallenge,
    Field(discriminator="op"),
]


class LedgerDiff(BaseModel):
    """A structured, code-applied mutation of a `Ledger`."""

    provenance: Provenance
    rationale: str
    ops: list[LedgerOp]

"""The intake fact store (`docs/adr/0011-conversational-agentic-onboarding.md`).

The conversational intake engine (`intake/agent.py`) never writes case-file
artifacts directly and never decides, on its own say-so, that a section is
"done" — every patient statement it captures becomes an `IntakeFact` here
first, and a deterministic gate (`section_completion_blockers`) is what
actually decides whether a section may close. This module is plain,
unit-tested code — CLAUDE.md's "deterministic logic ... never delegated to
a model" applies to the gate every bit as much as it does to the ledger
invariants in `casefile.ledger`.

**Op-level tolerance.** A malformed op must never cost the patient her
whole turn: `apply_ops` applies every op that is valid given the store's
current state and collects everything else — a duplicate `add_fact` id, an
`update_fact`/`retract_fact` referencing an id that doesn't exist — into
`AppliedResult.rejected` instead of raising. The closed-vocabulary fields
(`kind`, `precision`, `attribution`, `clarification_status`, and `section`,
derived from `SECTIONS` so it cannot drift) are `Literal`s specifically so
the *shape* of a bad op (an unrecognized topic key, e.g. a model emitting
`section="note"` — a valid `kind`, not a section) is rejected by
structured-output validation before it ever reaches this module at all;
the tolerance here is the second, defense-in-depth layer for whatever a
closed vocabulary can't prevent (duplicate/unknown ids are inherently
free-form). `intake.agent.run_intake_turn` is what spends the one
feedback-guided retry this enables — see that function's docstring.

Facts are never deleted, only retracted (`RetractFact`): `status` flips to
`"retracted"` and a `FactRevision` is appended to `history`, exactly like
`UpdateFact` appends one for every field it changes. This mirrors
`casefile.schema.LedgerOp`'s "no `RemoveHypothesis`, only status changes"
design for the same audit-trail reason.

**Why `NewFact` exists separately from `IntakeFact`.** `IntakeFact.provenance`
is required — every persisted fact must carry a real
`{app_version, prompt_template_version, model_id, dag_node, timestamp}`
stamp (CLAUDE.md code conventions). But the model producing an `AddFact` op
(via `LlmClient.complete(schema=IntakeTurnResult)`) has no reliable way to
know its own `model_id`, the running `app_version`, or the exact prompt
version it was served — the same reason `reason.stages._LedgerDiffPayload`
lets a model emit `ops` without a `LedgerDiff.provenance` field for the
Ledger-Maintainer to fill in after the fact. `NewFact` is the model-facing
shape (no `provenance`/`status`/`history`); `IntakeFactsStore.apply_ops`
is what turns one into a real `IntakeFact`, stamping `provenance` from the
single `Provenance` instance callers pass in for this turn.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from ruamel.yaml import YAML

from adoc.casefile.schema import Provenance
from adoc.intake.sections import SECTIONS

INTAKE_FACTS_RELPATH = "case/intake-facts.yaml"

MIN_UPDATE_NOTE_LENGTH = 10

SECTION_KEYS: frozenset[str] = frozenset(spec.key for spec in SECTIONS)

# Derived from `SECTIONS` (never hand-maintained) so the closed vocabulary
# can't drift from the section registry it mirrors. A `Literal`, not a bare
# `str`: the model can emit an invalid section (e.g. `"note"`, which is a
# valid `kind` but not a section) — with a `Literal`, the provider's own
# structured-output validation rejects that before it ever reaches this
# module, instead of failing deep inside `apply_ops` and losing the whole
# turn (see module docstring).
_SECTION_KEY_VALUES: tuple[str, ...] = tuple(spec.key for spec in SECTIONS)
SectionKey = Literal[_SECTION_KEY_VALUES]  # type: ignore[valid-type]

FactKind = Literal[
    "basic",
    "symptom",
    "event",
    "diagnosis",
    "patient_theory",
    "relative",
    "location",
    "medication",
    "supplement",
    "allergy",
    "provider",
    "insurance",
    "note",
]
"""`"location"` (added for the `geography` topic, `docs/adr/0018-intake-
clinical-progression-and-continuity.md`) covers a residence, a trip, or an
environmental/occupational exposure — disambiguated by
`fields["category"]` (`"residence"` (default) | `"travel"` | `"exposure"`),
the same "coarse kind, `fields` carries the nuance" convention
`diagnosis`/`attribution` already uses."""

Precision = Literal["exact", "approx", "unknown_after_probe", "unasked"]
Attribution = Literal["doctor_diagnosed", "patient_reported", "patient_assumption"]
ClarificationStatus = Literal["needs_probe", "resolved"]
FactStatus = Literal["active", "retracted"]
Corroboration = Literal["corroborated", "contradicted", "unverified"]
"""Set only by `intake.corroborate.corroborate_facts` (deterministic code,
never the `intake_agent` model) — see `docs/adr/0013-fact-corroboration.md`.
Defaults to `"unverified"` so old facts files without this field (and any
fact kind corroboration deliberately never touches, e.g. medications) load
and stay unverified rather than erroring."""

FieldValue = str | int | float | bool | None

_FACT_ID_RE = re.compile(r"^[^\s:]+$")


def _check_fact_id(value: str) -> str:
    if not _FACT_ID_RE.match(value):
        raise ValueError(f"invalid fact id {value!r}: must be a stable, whitespace-free slug")
    return value


class FactRevision(BaseModel):
    """One entry in an `IntakeFact.history` — facts are never deleted, only
    revised or retracted, and every mutation after creation appends one of
    these."""

    timestamp: datetime
    change: str
    prior_statement: str


class IntakeFact(BaseModel):
    """One patient-grounded fact captured during (or after) onboarding.

    `statement` is a one-two sentence, patient-grounded description (never
    the model's diagnostic gloss on it). `fields` carries the structured
    bits a completion gate or a section writer needs (`allergen`,
    `reaction`, `severity`, `dose`, `relation`, `by_whom`, `year`,
    `reasoning`, ...) — kept as a loose string-keyed dict rather than a
    per-kind schema because a fact's shape genuinely varies by `kind` and
    this store must stay agnostic to any one section's schema (that
    mapping lives in `intake.convert.facts_to_section_data`).
    """

    id: str
    section: SectionKey
    kind: FactKind
    statement: str
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    date_approx: str | None = None
    precision: Precision = "unasked"
    attribution: Attribution = "patient_reported"
    clarification_status: ClarificationStatus = "resolved"
    status: FactStatus = "active"
    corroboration: Corroboration = "unverified"
    corroboration_source: str | None = None
    """A source ref reusing the casefile grammar (`casefile.schema.
    SOURCE_REF_PATTERN`, minus `pmid:`/`patient-report:` which don't apply
    here): `doc:<file>#p<int>` | `labs:<slug>:<date>` | `encounter:<file>`.
    Set only alongside `corroboration="corroborated"`."""
    corroboration_note: str | None = None
    """A short deterministic explanation of the corroboration state (e.g.
    "ER note dated 2024-03-02 within 45 days of reported timing"), or the
    reason a `"contradicted"` state was reached. `None` for `"unverified"`."""
    reported_on: date | None = None
    """The date this fact was created or last touched by a patient turn
    (intake or a later visit) — stamped by `IntakeFactsStore.apply_ops` from
    the applying `Provenance.timestamp`, never by the model. `None` only for
    facts written before this field existed (backward compat)."""
    follow_up: bool = False
    """Explicitly flagged by the `intake_agent` (via `AddFact`/`UpdateFact`,
    never inferred) as something to revisit on a later visit — the real
    mechanism `docs/adr/0018-intake-clinical-progression-and-continuity.md`'s
    post-intake continuity note is built from
    (`intake.agent.build_continuity_note`), not a guess derived from
    corroboration or clarification status. The model clears it (another
    `update_fact` with `follow_up=False`) once it has actually revisited the
    topic with the patient. Defaults to `False` for facts written before
    this field existed."""
    provenance: Provenance
    history: list[FactRevision] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _check_fact_id(value)


class NewFact(BaseModel):
    """The model-facing shape of a not-yet-persisted fact — everything an
    `AddFact` op supplies. See the module docstring for why `provenance`/
    `status`/`history` are deliberately absent here."""

    id: str
    section: SectionKey
    kind: FactKind
    statement: str
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    date_approx: str | None = None
    precision: Precision = "unasked"
    attribution: Attribution = "patient_reported"
    clarification_status: ClarificationStatus = "resolved"
    follow_up: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _check_fact_id(value)


# --- ops: a discriminated union, mirroring casefile.schema.LedgerOp -----------------


class AddFact(BaseModel):
    op: Literal["add_fact"] = "add_fact"
    fact: NewFact

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_shape(cls, data: Any) -> Any:
        """Accept the fact's fields written flat alongside `op`.

        Observed live: the model emitted
        `{"op": "add_fact", "id": ..., "section": ..., "fields": {...}}`
        instead of nesting them under `fact`. That failed structured-output
        validation, which fails the WHOLE turn before `apply_ops`'s
        per-op tolerance can salvage anything — so one shape slip cost the
        patient an entire message of family history.

        The nested form remains what the prompt asks for and what this
        emits; this only lifts an unambiguous flat payload into place. It
        does nothing when `fact` is present, and anything still malformed
        after lifting fails validation exactly as before.
        """
        if not isinstance(data, dict) or "fact" in data:
            return data
        lifted = {k: v for k, v in data.items() if k != "op"}
        if not lifted:
            return data
        return {"op": data.get("op", "add_fact"), "fact": lifted}


class UpdateFact(BaseModel):
    op: Literal["update_fact"] = "update_fact"
    id: str
    statement: str | None = None
    fields: dict[str, FieldValue] | None = None
    date_approx: str | None = None
    precision: Precision | None = None
    attribution: Attribution | None = None
    clarification_status: ClarificationStatus | None = None
    follow_up: bool | None = None
    note: str
    """Why this update is being made — min `MIN_UPDATE_NOTE_LENGTH` chars
    after stripping whitespace, same substantive-note floor as
    `casefile.schema.RecordChallenge.note` (a bare "." or "fixed" is not an
    audit trail)."""

    @field_validator("note")
    @classmethod
    def _check_note_is_substantive(cls, value: str) -> str:
        if len(value.strip()) < MIN_UPDATE_NOTE_LENGTH:
            raise ValueError(
                f"UpdateFact.note must be substantive: at least {MIN_UPDATE_NOTE_LENGTH} "
                "characters after stripping whitespace"
            )
        return value


class RetractFact(BaseModel):
    op: Literal["retract_fact"] = "retract_fact"
    id: str
    reason: str


IntakeFactOp = Annotated[
    AddFact | UpdateFact | RetractFact,
    Field(discriminator="op"),
]


class AppliedResult(BaseModel):
    """What `IntakeFactsStore.apply_ops` did, by fact id.

    `rejected` is one human-readable reason per op that could not be
    applied given the store's current state (duplicate `add_fact` id,
    `update_fact`/`retract_fact` referencing an id that doesn't exist) —
    those ops are simply skipped, never raised (see module docstring)."""

    added: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    retracted: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


class IntakeError(Exception):
    """Raised for a fact-store operation that is invalid in a way `apply_ops`
    itself cannot recover from (e.g. `intake.agent._spec_by_key` on an
    internal registry lookup that should be impossible to miss). No longer
    raised by `apply_ops` for a bad op — see `AppliedResult.rejected`."""


# --- persistence --------------------------------------------------------------------


def load_intake_facts(path: Path) -> list[IntakeFact]:
    """Load `case/intake-facts.yaml`. A missing file yields an empty list."""
    if not path.exists():
        return []
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh) or {}
    return [IntakeFact.model_validate(item) for item in data.get("facts", [])]


def save_intake_facts(path: Path, facts: Sequence[IntakeFact]) -> None:
    """Write `facts` to `path` as stable, human-diffable YAML."""
    data: dict[str, Any] = {"facts": [fact.model_dump(mode="json") for fact in facts]}
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


# --- the store ------------------------------------------------------------------------


class IntakeFactsStore:
    """Loads/holds/saves `case/intake-facts.yaml` for one data repo.

    `apply_ops` is plain deterministic code (CLAUDE.md code conventions): it
    never calls a model, and every op is validated against the store's
    current facts before it is applied. Unlike a ledger diff's all-or-
    nothing invariant checking, a bad op here never costs the whole batch:
    an op that is invalid given the store's current state (unknown id,
    duplicate id on add) is skipped and named in `AppliedResult.rejected`;
    every other, valid op in the same batch still applies normally (see
    module docstring — this is what stops one malformed op from losing an
    entire patient turn).
    """

    def __init__(self, repo_root: Path) -> None:
        self._path = repo_root / INTAKE_FACTS_RELPATH
        self._facts: list[IntakeFact] = load_intake_facts(self._path)

    @property
    def facts(self) -> list[IntakeFact]:
        return list(self._facts)

    def active_facts(self, section: str | None = None) -> list[IntakeFact]:
        return [
            fact
            for fact in self._facts
            if fact.status == "active" and (section is None or fact.section == section)
        ]

    def get(self, fact_id: str) -> IntakeFact | None:
        for fact in self._facts:
            if fact.id == fact_id:
                return fact
        return None

    def apply_ops(self, ops: Sequence[IntakeFactOp], provenance: Provenance) -> AppliedResult:
        working = [fact.model_copy(deep=True) for fact in self._facts]
        by_id = {fact.id: index for index, fact in enumerate(working)}
        result = AppliedResult()

        for op in ops:
            if isinstance(op, AddFact):
                if op.fact.id in by_id:
                    result.rejected.append(
                        f"add_fact {op.fact.id!r}: duplicate fact id (already on file)"
                    )
                    continue
                # `section` is a `Literal` (see module docstring) so this
                # should be unreachable via a properly-validated op — kept
                # as a defense-in-depth belt alongside that suspenders.
                if op.fact.section not in SECTION_KEYS:  # pragma: no cover
                    result.rejected.append(
                        f"add_fact {op.fact.id!r}: unknown intake section {op.fact.section!r}"
                    )
                    continue
                new_fact = IntakeFact(
                    id=op.fact.id,
                    section=op.fact.section,
                    kind=op.fact.kind,
                    statement=op.fact.statement,
                    fields=dict(op.fact.fields),
                    date_approx=op.fact.date_approx,
                    precision=op.fact.precision,
                    attribution=op.fact.attribution,
                    clarification_status=op.fact.clarification_status,
                    follow_up=op.fact.follow_up,
                    status="active",
                    reported_on=provenance.timestamp.date(),
                    provenance=provenance,
                    history=[],
                )
                working.append(new_fact)
                by_id[new_fact.id] = len(working) - 1
                result.added.append(new_fact.id)

            elif isinstance(op, UpdateFact):
                if op.id not in by_id:
                    result.rejected.append(f"update_fact {op.id!r}: unknown fact id")
                    continue
                index = by_id[op.id]
                current = working[index]
                revision = FactRevision(
                    timestamp=provenance.timestamp,
                    change=op.note,
                    prior_statement=current.statement,
                )
                data = current.model_dump()
                if op.statement is not None:
                    data["statement"] = op.statement
                if op.fields is not None:
                    data["fields"] = {**current.fields, **op.fields}
                if op.date_approx is not None:
                    data["date_approx"] = op.date_approx
                if op.precision is not None:
                    data["precision"] = op.precision
                if op.attribution is not None:
                    data["attribution"] = op.attribution
                if op.clarification_status is not None:
                    data["clarification_status"] = op.clarification_status
                if op.follow_up is not None:
                    data["follow_up"] = op.follow_up
                data["reported_on"] = provenance.timestamp.date()
                data["provenance"] = provenance
                data["history"] = [*current.history, revision]
                working[index] = IntakeFact.model_validate(data)
                result.updated.append(op.id)

            elif isinstance(op, RetractFact):
                if op.id not in by_id:
                    result.rejected.append(f"retract_fact {op.id!r}: unknown fact id")
                    continue
                index = by_id[op.id]
                current = working[index]
                revision = FactRevision(
                    timestamp=provenance.timestamp,
                    change=f"retracted: {op.reason}",
                    prior_statement=current.statement,
                )
                data = current.model_dump()
                data["status"] = "retracted"
                data["provenance"] = provenance
                data["history"] = [*current.history, revision]
                working[index] = IntakeFact.model_validate(data)
                result.retracted.append(op.id)

        self._facts = working
        return result

    def apply_corroboration(
        self, updates: Sequence[CorroborationUpdate], *, at: datetime
    ) -> list[str]:
        """Apply `intake.corroborate.corroborate_facts`' computed updates.

        Unlike `apply_ops`, this never restamps `provenance` — corroboration
        is deterministic code re-evaluating already-captured facts against
        already-ingested documentation, not a new LLM-derived artifact (see
        `docs/adr/0013-fact-corroboration.md`), so the fact's original
        `provenance` (from whichever turn actually captured/last touched it)
        is left untouched. A `FactRevision` is still appended so the change
        is visible in `history`, and `corroboration_source is None`/
        `corroboration_note is None`. `reported_on` is also left untouched —
        corroboration is not a patient statement. Skips (silently) any
        `fact_id` no longer present (e.g. retracted since the sweep was
        computed) and any update whose target state already matches the
        fact's current one (idempotent — matches `corroborate_facts`'
        own "returns updates only where the computed state differs"
        contract, but re-checked here too since `updates` may be stale by
        the time it's applied). Returns the ids actually touched.
        """
        working = [fact.model_copy(deep=True) for fact in self._facts]
        by_id = {fact.id: index for index, fact in enumerate(working)}
        touched: list[str] = []

        for update in updates:
            index = by_id.get(update.fact_id)
            if index is None:
                continue
            current = working[index]
            if (
                current.corroboration == update.corroboration
                and current.corroboration_source == update.corroboration_source
                and current.corroboration_note == update.corroboration_note
            ):
                continue

            change = f"corroboration: {current.corroboration} -> {update.corroboration}"
            if update.corroboration_note:
                change = f"{change} ({update.corroboration_note})"
            revision = FactRevision(timestamp=at, change=change, prior_statement=current.statement)
            data = current.model_dump()
            data["corroboration"] = update.corroboration
            data["corroboration_source"] = update.corroboration_source
            data["corroboration_note"] = update.corroboration_note
            data["history"] = [*current.history, revision]
            working[index] = IntakeFact.model_validate(data)
            touched.append(update.fact_id)

        self._facts = working
        return touched

    def save(self) -> None:
        save_intake_facts(self._path, self._facts)


@dataclass(frozen=True)
class CorroborationUpdate:
    """One fact's newly-computed corroboration state
    (`intake.corroborate.corroborate_facts`), applied via
    `IntakeFactsStore.apply_corroboration`. Defined here (not in
    `intake.corroborate`) so that module can import `IntakeFact`/
    `Corroboration` from this one without a circular import."""

    fact_id: str
    corroboration: Corroboration
    corroboration_source: str | None = None
    corroboration_note: str | None = None


# --- deterministic completion gates (THE core safety of this feature) ---------------


def section_completion_blockers(facts: Sequence[IntakeFact], section_key: str) -> list[str]:
    """Plain code, never a model: the reasons `section_key` may NOT close
    yet, given the currently active facts filed to it. An empty list means
    the section is clear to close.

    Rules (each names the offending fact's `statement` so the blocker is
    legible on its own, e.g. in the deterministic reply line
    `intake.agent.run_intake_turn` appends when a close is refused):

    (a) any active fact with `clarification_status == "needs_probe"` blocks;
    (b) any active `kind == "diagnosis"` fact with
        `attribution == "doctor_diagnosed"` missing BOTH `fields['by_whom']`
        and `fields['year']` blocks;
    (c) any active `kind == "diagnosis"` fact with
        `attribution == "patient_assumption"` and no `fields['reasoning']`
        blocks;
    (d) any active `kind in {"event", "diagnosis"}` fact with
        `precision == "unasked"` blocks.
    """
    blockers: list[str] = []
    for fact in facts:
        if fact.status != "active" or fact.section != section_key:
            continue

        if fact.clarification_status == "needs_probe":
            blockers.append(
                f"still needs a follow-up before this can be pinned down: {fact.statement!r}"
            )

        if fact.kind == "diagnosis":
            missing_by_whom = not fact.fields.get("by_whom")
            missing_year = not fact.fields.get("year")
            if fact.attribution == "doctor_diagnosed" and missing_by_whom and missing_year:
                blockers.append(
                    f"diagnosed-by unclear (no clinician or year on file): {fact.statement!r}"
                )
            elif fact.attribution == "patient_assumption" and not fact.fields.get("reasoning"):
                blockers.append(
                    f"the patient's own reasoning for this suspicion hasn't been captured: "
                    f"{fact.statement!r}"
                )

        if fact.kind in ("event", "diagnosis") and fact.precision == "unasked":
            blockers.append(f"timing never asked: {fact.statement!r}")

    return blockers

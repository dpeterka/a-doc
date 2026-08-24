"""The conversational intake engine (`docs/adr/0011-conversational-agentic-onboarding.md`).

Replaces the form-style, one-shot-per-section flow the patient otherwise
gets from `intake.wizard.IntakeWizard` with an agentic chat: every patient
message is screened (`reason.safety.red_flag_screen`, CLAUDE.md rule 2/5),
then handed with the current onboarding context to the `intake_agent`
model role, which proposes typed `intake.facts` ops — never writes a case
file directly. Those ops are applied by `IntakeFactsStore.apply_ops`
(plain code), and `intake.facts.section_completion_blockers` (also plain
code) is the ONLY thing that may close a section: the model can request a
close (`IntakeTurnResult.section_complete`), but the deterministic gate
gets the final word, exactly the way `casefile.ledger`'s invariants get
the final word over an LLM-proposed `LedgerDiff`.

`run_intake_turn` is deliberately not a `reason.dag.Dag` — it is one model
call per turn (no Ledger-Maintainer/Challenger split), so the DAG runner's
machinery (contracts across multiple nodes) has nothing to add here; the
completion gate and the treatment-gate check below are the two
deterministic checks this module needs, and both are plain function calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from adoc import __version__
from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import Provenance
from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.intake.convert import facts_to_section_data
from adoc.intake.facts import (
    INTAKE_FACTS_RELPATH,
    SECTION_KEYS,
    IntakeError,
    IntakeFactOp,
    IntakeFactsStore,
    section_completion_blockers,
)
from adoc.intake.sections import SECTIONS, SectionSpec
from adoc.intake.wizard import (
    INTAKE_STATE_RELPATH,
    IntakeState,
    SectionState,
    load_intake_state,
    save_intake_state,
    write_section,
)
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, LlmError, Message
from adoc.reason.safety import red_flag_screen, treatment_gate

INTAKE_AGENT_PROMPT_VERSION = "1"
INTAKE_TRANSCRIPT_RELPATH = "case/intake-transcript.jsonl"

DOC_DIGEST_MAX_LINES = 60
TRANSCRIPT_CONTEXT_TURNS = 20

_SPEC_BY_KEY: dict[str, SectionSpec] = {spec.key: spec for spec in SECTIONS}

_WITHHELD_MESSAGE = (
    "I recorded what you told me, but I withheld my reply because it failed one of "
    "a-doc's built-in safety checks before it could reach you (the same deterministic "
    "guard that blocks treatment/dosing language everywhere in this app). Nothing is "
    "wrong with your case file. Please try rephrasing, and we'll pick this back up."
)

_INTAKE_AGENT_SYSTEM_PROMPT = f"""[intake-agent-v{INTAKE_AGENT_PROMPT_VERSION}]
You are the intake assistant for a single-patient longitudinal medical case-file tool
(a-doc). You conduct onboarding as a natural conversation, one section at a time, and
you are the only thing standing between a vague answer and a case file quietly full of
guesses.

SAFETY (non-negotiable):
- Never diagnose. Never suggest, name, or imply a diagnosis of your own.
- Never give treatment or dosing advice, not even phrased as a suggestion. Your job is
  to record what the patient says, nothing else.
- Capture facts ONLY from what the patient actually states. Never invent, infer, or
  embellish a detail the patient did not say.

WHAT YOU DO EACH TURN:
1. Read the patient's message, the section checklist, the current section's open
   items, the active facts already on file, the documents/encounters already on file,
   and the recent conversation (all supplied below).
2. Decide what fact ops (add_fact / update_fact / retract_fact) this message
   justifies. Every fact you add or touch must be traceable to something the patient
   actually said this turn or earlier in the conversation.
3. Write a short `message`: an acknowledgment of what you just recorded, plus AT MOST
   TWO focused follow-up questions. Never pile on more than two questions in one turn
   -- this is a conversation, not an interrogation. Zero questions (a plain
   acknowledgment) is fine when nothing needs asking.

PROBING VAGUENESS:
A generic term ("allergies", "stomach issues", "a while ago", "some medications") gets
exactly ONE concrete follow-up asking what specifically, when, and how severe/often. Do
not ask a second time if the patient still doesn't know -- record what you have and
move on. Example: the patient says "my dad has allergies" -> add a `relative` fact with
`clarification_status="needs_probe"`, and ask (as one of your two questions) which
allergens, what reaction, how severe, and roughly how old their dad is. Once the
patient answers (even partially, or says they don't know the details), update the same
fact and set `clarification_status="resolved"` -- an honestly-incomplete answer still
resolves the probe; never nag twice.

TIMING (events and diagnoses):
Every `event` and `diagnosis` fact must have its timing asked ONCE. Ask whether the
patient has a rough estimate ("about 5 years ago") or an exact date, and record
whichever they give in `date_approx`, setting `precision` to `"approx"` or `"exact"`
accordingly. If the patient does not know or declines to say, set
`precision="unknown_after_probe"` and move on -- never ask again for that fact. A
brand-new event/diagnosis fact defaults to `precision="unasked"`; ask about its timing
before the turn ends whenever you introduce one.

ATTRIBUTION (diagnoses and suspected conditions):
When the patient states a condition as settled fact ("I have lupus", "I have cancer"),
do not take it at face value silently -- ask (one of your two questions): "was that
diagnosed by a clinician -- who, and roughly when?" If a clinician confirmed it: record
`kind="diagnosis"`, `attribution="doctor_diagnosed"`, and capture `fields.by_whom` and
`fields.year` from the answer. If it is the patient's own conclusion (no clinician
involved, or they're not sure one ever said it): record `attribution="patient_assumption"`
and ask, non-judgmentally, "that's worth tracking -- what makes you think that?",
capturing their answer in `fields.reasoning`. Never treat a patient assumption as if it
were confirmed, and never argue with it -- just record it accurately and move on.

CROSS-REFERENCING DOCUMENTS ALREADY ON FILE:
The "Documents & encounters already on file" section below lists what has already been
ingested (dated documents, encounter files). When the patient describes an event/visit/
test that plausibly matches one of these (similar date, similar description), say so
explicitly and ask them to confirm or distinguish it -- e.g. "I have a record of an ER
note dated 2024-03-02 -- is that this visit, or a different one?" Use their answer to
either note the match in the fact's `statement` or keep the two as distinct events.

CORRECTIONS, AT ANY TIME:
The patient may correct or add to ANY previously recorded fact at any point, including
facts in sections that are already closed. When they do, find the matching fact by id
in the "Active facts on file" list below and emit an `update_fact` op for it (never a
duplicate `add_fact`) with a substantive `note` explaining the change. Always restate in
your `message` what you changed ("Got it -- updated your penicillin allergy to say
hives, not a rash."). To remove something the patient says is wrong or no longer
applies, use `retract_fact` with a `reason` -- never silently drop it (it stays in
history, marked retracted).

SECTIONS AND OUT-OF-ORDER INFORMATION:
Onboarding covers one section at a time (see the checklist below), but never refuse
information the patient volunteers early or out of order -- file it to the RIGHT
section via its own fact ops and continue the current section's conversation. Set
`wants_section` only when the patient explicitly wants to jump to, or come back to, a
different section right now ("let's talk about my medications", "can we go back to my
allergies"). Set `section_complete=true` only when you judge the CURRENT section's
conversation genuinely done. You do not have to check the completion gate yourself --
the system will refuse the close and tell the patient what's still open if a vague,
undated, or unattributed fact is left in the section; when that happens, ask about
exactly what it names next turn.

FACT FIELDS CONVENTIONS:
`fields` is a flat set of key/value pairs -- use plain keys matching what the case file
expects for that section (symptoms: onset/frequency/triggers/severity; diagnoses:
by_whom/year/reasoning/status; relatives: relation/conditions/age_at_onset/deceased/
age_at_death; medications & supplements: name/dose/frequency/still_taking/notes;
allergies: allergen/reaction/severity; providers: name/specialty/org; care team:
insurer). Where a field is naturally a list (e.g. a relative's conditions), write it as
one comma-separated string. Every `id` you invent must be a short, stable, lowercase
slug with no spaces or colons (e.g. `father-allergy`, `2019-er-chest-pain`) -- reuse the
exact same id every time you touch the same fact again.

Respond only with the structured result (message, ops, section_complete, wants_section)
-- never free text outside that schema.
"""


class IntakeTurnResult(BaseModel):
    """What the `intake_agent` model returns for one turn."""

    message: str
    ops: list[IntakeFactOp] = Field(default_factory=list)
    section_complete: bool = False
    wants_section: str | None = None


class IntakeOutcome(BaseModel):
    """What `run_intake_turn` returns to a caller (CLI REPL or the web route)."""

    kind: Literal["urgent", "reply", "withheld", "error"]
    text: str
    section_key: str | None = None
    """The section the conversation is on after this turn (`None` once every
    section is complete)."""


# --------------------------------------------------------------------------
# Doc digest (deterministic — no LLM call)
# --------------------------------------------------------------------------


def build_doc_digest(db: LabsDb, repo: DataRepo) -> str:
    """A small, deterministic summary of what's already on file: ingested
    documents (dated ones first, newest first), a labs row-count + date
    span, and any recorded encounter files. Genomic documents are excluded
    (CLAUDE.md/PLAN.md: genomic data never reaches an LLM call, including
    indirectly by name in a digest a model reads). Capped at
    `DOC_DIGEST_MAX_LINES` lines.
    """
    lines: list[str] = ["Documents already on file:"]
    overviews = [o for o in db.documents_overview() if o.document.doc_type != GENOMIC_DOC_TYPE]
    dated = sorted(
        (o for o in overviews if o.document.doc_date is not None),
        key=lambda o: o.document.doc_date,  # type: ignore[return-value, arg-type]
        reverse=True,
    )
    undated = [o for o in overviews if o.document.doc_date is None]
    ordered = dated + undated

    if not ordered:
        lines.append("- none yet")
    else:
        cap = max(DOC_DIGEST_MAX_LINES - 12, 1)
        shown = ordered[:cap]
        for overview in shown:
            doc = overview.document
            date_str = doc.doc_date.isoformat() if doc.doc_date else "undated"
            lines.append(f"- {date_str} - {doc.doc_type}: {doc.filename}")
        remaining = len(ordered) - len(shown)
        if remaining > 0:
            lines.append(f"- (+{remaining} more)")

    rows = db.all_non_rejected_rows()
    lines.append("")
    lines.append("Labs on file:")
    if rows:
        dates = sorted(row.date for row in rows)
        lines.append(
            f"- {len(rows)} lab result row(s), spanning {dates[0].isoformat()} to "
            f"{dates[-1].isoformat()}"
        )
    else:
        lines.append("- no lab results recorded yet")

    lines.append("")
    lines.append("Encounters already recorded:")
    encounters_dir = repo.root / "case" / "encounters"
    encounter_files = (
        sorted((p for p in encounters_dir.iterdir() if p.suffix == ".md"), reverse=True)
        if encounters_dir.is_dir()
        else []
    )
    if not encounter_files:
        lines.append("- none yet")
    else:
        for path in encounter_files[:10]:
            encounter = read_encounter(path)
            fm = encounter.frontmatter
            title = path.stem.split("--", 1)[-1].replace("-", " ")
            lines.append(f"- {fm.date.isoformat()} [{fm.type}]: {title}")
        if len(encounter_files) > 10:
            lines.append(f"- (+{len(encounter_files) - 10} more)")

    return "\n".join(lines[:DOC_DIGEST_MAX_LINES])


# --------------------------------------------------------------------------
# Transcript persistence
# --------------------------------------------------------------------------


def _transcript_path(repo: DataRepo) -> Path:
    return repo.root / INTAKE_TRANSCRIPT_RELPATH


def read_intake_transcript(repo: DataRepo, *, limit: int | None = None) -> list[dict]:
    """`case/intake-transcript.jsonl` entries, oldest first. `limit`, if
    given, keeps only the most recent `limit` entries."""
    path = _transcript_path(repo)
    if not path.exists():
        return []
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries[-limit:] if limit is not None else entries


def _append_transcript_turn(repo: DataRepo, patient_text: str, reply: IntakeOutcome) -> None:
    path = _transcript_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": now, "role": "patient", "text": patient_text}) + "\n")
        fh.write(
            json.dumps(
                {"timestamp": now, "role": "assistant", "kind": reply.kind, "text": reply.text}
            )
            + "\n"
        )


def _render_transcript(entries: list[dict]) -> str:
    if not entries:
        return "(this is the first turn)"
    lines = []
    for entry in entries:
        role = entry.get("role", "?")
        text = entry.get("text", "")
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Context assembly (deterministic)
# --------------------------------------------------------------------------


def _spec_by_key(key: str) -> SectionSpec:
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise IntakeError(f"no such intake section: {key!r}")
    return spec


def _first_incomplete_key(state: IntakeState) -> str | None:
    for spec in SECTIONS:
        if state.sections.get(spec.key, SectionState()).status != "complete":
            return spec.key
    return None


def _render_section_checklist(state: IntakeState) -> str:
    lines = []
    for spec in SECTIONS:
        section_state = state.sections.get(spec.key, SectionState())
        marker = "x" if section_state.status == "complete" else " "
        current = " <- current" if spec.key == state.cursor else ""
        lines.append(
            f"- [{marker}] {spec.title} ({spec.key}), status={section_state.status}{current}"
        )
    return "\n".join(lines)


def _render_open_items(facts_store: IntakeFactsStore, current_key: str | None) -> str:
    if current_key is None:
        return "(onboarding is complete; the patient may still correct or add facts to any section)"
    blockers = section_completion_blockers(facts_store.facts, current_key)
    if not blockers:
        return "(none - this section is clear to close whenever the conversation is done)"
    return "\n".join(f"- {b}" for b in blockers)


def _render_active_facts(facts_store: IntakeFactsStore) -> str:
    facts = facts_store.active_facts()
    if not facts:
        return "facts: []"
    lines = ["facts:"]
    for fact in facts:
        lines.append(f"  - id: {fact.id}")
        lines.append(f"    section: {fact.section}")
        lines.append(f"    kind: {fact.kind}")
        lines.append(f"    statement: {fact.statement!r}")
        if fact.date_approx:
            lines.append(f"    date_approx: {fact.date_approx}")
        lines.append(f"    precision: {fact.precision}")
        lines.append(f"    attribution: {fact.attribution}")
        lines.append(f"    clarification_status: {fact.clarification_status}")
        if fact.fields:
            fields_str = ", ".join(f"{k}: {v}" for k, v in fact.fields.items())
            lines.append(f"    fields: {{{fields_str}}}")
    return "\n".join(lines)


def _build_turn_context(
    repo: DataRepo,
    db: LabsDb,
    state: IntakeState,
    facts_store: IntakeFactsStore,
    current_key: str | None,
) -> str:
    current_title = (
        _spec_by_key(current_key).title if current_key else "(none - onboarding complete)"
    )
    transcript = _render_transcript(read_intake_transcript(repo, limit=TRANSCRIPT_CONTEXT_TURNS))
    return (
        f"## Section checklist\n\n{_render_section_checklist(state)}\n\n"
        f"## Current section: {current_title}\n\n"
        f"## Open items blocking this section's close\n\n"
        f"{_render_open_items(facts_store, current_key)}\n\n"
        f"## Active facts on file\n\n{_render_active_facts(facts_store)}\n\n"
        f"## Documents & encounters already on file\n\n{build_doc_digest(db, repo)}\n\n"
        f"## Recent conversation\n\n{transcript}\n"
    )


def _render_blocker_followup(blockers: list[str]) -> str:
    lines = ["Before I can close this section out, a few things still need pinning down:"]
    lines.extend(f"- {b}" for b in blockers)
    return "\n".join(lines)


def _write_section_from_facts(
    repo: DataRepo, facts_store: IntakeFactsStore, section_key: str
) -> list[str]:
    spec = _spec_by_key(section_key)
    data = facts_to_section_data(facts_store.facts, section_key)
    section_data = spec.schema.model_validate(data)
    return write_section(repo, section_key, section_data)


# --------------------------------------------------------------------------
# The turn entry point
# --------------------------------------------------------------------------


def run_intake_turn(client: LlmClient, repo: DataRepo, db: LabsDb, text: str) -> IntakeOutcome:
    """Run one conversational onboarding turn.

    Red-flag screening always runs first and short-circuits before any
    client call or persistence (mirroring `reason.stages`'s red-flag-first
    ordering) — zero API calls, nothing written, on a flagged turn.
    """
    screen = red_flag_screen(text)
    if screen.flagged:
        state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
        return IntakeOutcome(kind="urgent", text=screen.message or "", section_key=state.cursor)

    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    for spec in SECTIONS:
        state.sections.setdefault(spec.key, SectionState())
    facts_store = IntakeFactsStore(repo.root)
    original_key = state.cursor

    context = _build_turn_context(repo, db, state, facts_store, original_key)
    user_content = f"{context}\n\n## Patient message\n\n{text}\n"

    try:
        result = client.complete(
            "intake_agent",
            system=_INTAKE_AGENT_SYSTEM_PROMPT,
            messages=[Message(role="user", content=user_content)],
            schema=IntakeTurnResult,
        )
    except LlmError as exc:
        return IntakeOutcome(
            kind="error",
            text=f"Sorry, I couldn't process that: {exc}",
            section_key=original_key,
        )

    turn = result.parsed
    assert isinstance(turn, IntakeTurnResult)

    provenance = Provenance(
        app_version=__version__,
        prompt_template_version=INTAKE_AGENT_PROMPT_VERSION,
        model_id=result.model_id,
        dag_node="intake-agent",
        timestamp=datetime.now(UTC),
    )

    try:
        applied = facts_store.apply_ops(turn.ops, provenance)
    except IntakeError as exc:
        return IntakeOutcome(
            kind="error",
            text=f"Sorry, something in that update didn't apply cleanly: {exc}",
            section_key=original_key,
        )

    touched_ids = [*applied.added, *applied.updated, *applied.retracted]
    touched_sections = {
        fact.section for fact_id in touched_ids if (fact := facts_store.get(fact_id)) is not None
    }

    artifacts: list[str] = []

    # Amend mode: a correction/addition to an already-closed section
    # regenerates that section's case-file artifact(s) immediately, even
    # though this turn isn't closing anything (requirement: facts are
    # editable at any time, during AND after onboarding).
    for section_key in touched_sections:
        section_state = state.sections.setdefault(section_key, SectionState())
        if section_state.status == "complete":
            artifacts.extend(_write_section_from_facts(repo, facts_store, section_key))
        elif section_state.status == "pending":
            section_state.status = "awaiting_confirmation"

    blocker_note = ""
    if turn.section_complete and original_key is not None:
        blockers = section_completion_blockers(facts_store.facts, original_key)
        if blockers:
            blocker_note = "\n\n" + _render_blocker_followup(blockers)
        else:
            artifacts.extend(_write_section_from_facts(repo, facts_store, original_key))
            section_state = state.sections[original_key]
            section_state.status = "complete"
            section_state.completed_at = datetime.now(UTC)
            state.cursor = _first_incomplete_key(state)

    wants_valid_section = turn.wants_section and turn.wants_section in SECTION_KEYS
    if wants_valid_section and turn.wants_section != state.cursor:
        target_key = turn.wants_section
        assert target_key is not None
        target_state = state.sections.setdefault(target_key, SectionState())
        if target_state.status == "complete":
            has_facts = bool(facts_store.active_facts(target_key))
            target_state.status = "awaiting_confirmation" if has_facts else "pending"
            target_state.completed_at = None
        state.cursor = target_key

    reply_text = turn.message + blocker_note
    gate = treatment_gate(reply_text)
    outcome = (
        IntakeOutcome(kind="reply", text=reply_text, section_key=state.cursor)
        if gate.passed
        else IntakeOutcome(kind="withheld", text=_WITHHELD_MESSAGE, section_key=state.cursor)
    )

    # Persist only on full success (IntakeError/LlmError above already
    # returned before any of this — nothing is written for those turns).
    facts_store.save()
    save_intake_state(repo.root / INTAKE_STATE_RELPATH, state)
    _append_transcript_turn(repo, text, outcome)

    paths = [
        INTAKE_FACTS_RELPATH,
        INTAKE_STATE_RELPATH,
        INTAKE_TRANSCRIPT_RELPATH,
        *sorted(set(artifacts)),
    ]
    commit_label = original_key or "post-completion correction"
    repo.commit(f"feat(intake): conversational turn ({commit_label})", paths=paths)

    return outcome

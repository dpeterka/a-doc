"""The conversational intake engine — a single "initial visit" conversation
(`docs/adr/0012-initial-visit-conversation.md`, superseding 0011's sectioned
stepping while keeping its fact model, deterministic gates, and writer
reuse). Every patient message is handed with the current onboarding context
to the `intake_agent` model role, which proposes typed `intake.facts` ops
plus, optionally, `topics_covered` and `intake_complete` — never writes a
case file directly and never decides, on its own say-so, that a topic or
the whole intake is "done." There is no automated emergency screening here
(see `docs/adr/0021*.md` for why): intake is historical narrative by
construction.

There is no UI stepper and no "current topic": the internal topic keys
(`intake.sections.SECTIONS`) still exist because the same schemas/writers
they drive (`intake.convert.facts_to_section_data`, `intake.wizard.write_section`)
are reused verbatim for case-file output, but nothing patient-facing ever
names, numbers, lists, or steps through them. `intake.facts.
section_completion_blockers` (plain code) is the ONLY thing that may mark a
topic covered — the model can propose a topic is covered, or that the whole
intake is complete, but the deterministic gate gets the final word, exactly
the way `casefile.ledger`'s invariants get the final word over an
LLM-proposed `LedgerDiff`.

`run_intake_turn` is deliberately not a `reason.dag.Dag` — it is one model
call per turn (no Ledger-Maintainer/Challenger split), so the DAG runner's
machinery (contracts across multiple nodes) has nothing to add here; the
coverage/wrap-up gates and the treatment-gate check below are the
deterministic checks this module needs, and both are plain function calls.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from adoc import __version__
from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import Provenance
from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.intake.convert import facts_to_section_data
from adoc.intake.corroborate import corroborate_facts
from adoc.intake.coverage import (
    INTAKE_STATE_RELPATH,
    CoverageState,
    TopicCoverage,
    load_coverage_state,
    save_coverage_state,
)
from adoc.intake.facts import (
    INTAKE_FACTS_RELPATH,
    SECTION_KEYS,
    AppliedResult,
    IntakeError,
    IntakeFact,
    IntakeFactOp,
    IntakeFactsStore,
    section_completion_blockers,
)
from adoc.intake.sections import SECTIONS, SectionSpec
from adoc.intake.wizard import write_section
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, LlmError, Message
from adoc.reason.safety import treatment_gate

logger = logging.getLogger(__name__)

INTAKE_AGENT_PROMPT_VERSION = "6"
INTAKE_TRANSCRIPT_RELPATH = "case/intake-transcript.jsonl"

DOC_DIGEST_MAX_LINES = 60
TRANSCRIPT_CONTEXT_TURNS = 20

# docs/adr/0015-document-text-corpus.md: verbatim excerpts from documents the
# patient has already provided (a written history, a doctor's note),
# retrieved against the CURRENT message so the agent can say "your history
# mentions X" instead of asking her to retype it. A smaller cap than
# `reason.context`'s diagnostic-turn budget (`MAX_DOCUMENT_EXCERPT_CHARS`) —
# the intake turn context is already large (coverage map, gate status,
# active facts, doc digest, transcript), so this stays modest on purpose.
DOC_EXCERPT_LIMIT = 4
DOC_EXCERPT_MAX_CHARS = 2000

# A patient who pastes a long written history in one message must never be
# truncated or refused, but the model needs an explicit steer or it tries to
# process/ask about all of it at once. ~4000 characters is comfortably past
# an ordinary conversational turn (a few paragraphs) while still well short
# of anything that risks the model's context budget on its own -- a module
# constant so the threshold is easy to retune from one place.
LONG_MESSAGE_THRESHOLD_CHARS = 4000

# Appended to the turn context (never to the patient's own text, which is
# never truncated or refused) when a message crosses the threshold above --
# tells the model, in its own bookkeeping context, to acknowledge the volume
# warmly and steer to one thing at a time instead of trying to process
# everything in a single reply (see `_INTAKE_AGENT_SYSTEM_PROMPT`'s "LONG
# PASTES" section, which this note points at).
_LONG_MESSAGE_NOTE = (
    "\n\n## Note: this message is unusually long\n\n"
    f"The patient's message above is over {LONG_MESSAGE_THRESHOLD_CHARS} characters -- "
    "likely a pasted history or written summary. Follow the LONG PASTES guidance: "
    "acknowledge warmly that you received a lot of detail, capture what you can "
    "confidently pull out of it as fact ops, and steer `message` to ONE thing at a time. "
    "Never refuse it and never ask her to shorten or resend it.\n"
)

# How many completions `run_intake_turn` may spend applying one turn's fact
# ops: the first attempt plus one feedback-guided retry naming exactly which
# ops `IntakeFactsStore.apply_ops` rejected and why -- mirrors
# `reason.stages.composer_stage`'s gate-guided rewrite and the citation
# checker's retry (PLAN.md Phase 2). Still-rejected ops after the retry are
# simply dropped and logged; the turn still replies either way (see
# `run_intake_turn`'s docstring -- one malformed op must never cost the
# patient her whole turn).
_OPS_RETRY_ATTEMPTS = 2

_SPEC_BY_KEY: dict[str, SectionSpec] = {spec.key: spec for spec in SECTIONS}

# The very first assistant message of a patient's initial visit — a
# constant, never an LLM call, per the product redesign
# (docs/adr/0018-intake-clinical-progression-and-continuity.md, refining
# 0012): greet, then ask ONE short, concrete opening question — the same
# first thing a clinician actually asks a new patient (age, sex at birth) —
# so the conversation is driven, incremental, and never invites a
# wall-of-text answer the way an open "what brings you in?" did. Deliberately
# NOT "what's been bothering you" — that belongs at the END of the intended
# arc (see `_INTAKE_AGENT_SYSTEM_PROMPT`'s "INTENDED ARC"), once individual
# stats, family history, geography, and a review of the existing record are
# already in hand; asking it first is exactly what produced the wall-of-text
# failure this rewrite fixes. `web.routes.chat` renders this into the page
# when the shared chat transcript is empty and intake is incomplete, and
# writes it into that transcript on the first patient turn so history stays
# coherent; `intake.cli`'s REPL prints the same constant at session start.
INTAKE_OPENER_MESSAGE = (
    "Hi — I'm glad you're here. This first conversation is how we build your case "
    "file together, so anything you share becomes part of the record you and your "
    "doctors can rely on. Let's start simple: how old are you, and what was your sex "
    "at birth?\n\n"
    "We'll take it a step at a time -- you don't need to cover everything today, and "
    "your existing records and documents are already on file, so we can work from "
    "those together as we go."
)

_WITHHELD_MESSAGE = (
    "I recorded what you told me, but I withheld my reply because it failed one of "
    "a-doc's built-in safety checks before it could reach you (the same deterministic "
    "guard that blocks treatment/dosing language everywhere in this app). Nothing is "
    "wrong with your case file. Please try rephrasing, and we'll pick this back up."
)

# Human phrasing for each internal topic key — used ONLY in the
# deterministic wrap-up steering line (never shown as a list/checklist;
# folded into one conversational sentence). Never expose the bare key
# itself to the patient.
_TOPIC_PHRASES: dict[str, str] = {
    "basics": "a few basics about you",
    "symptoms": "what you've been experiencing",
    "events": "the major medical events in your history",
    "prior_diagnoses": "any diagnoses or suspicions you or a doctor have raised",
    "family_history": "your family's health history",
    "geography": "where you live and have lived, plus any travel or exposures",
    "medications": "the medications you're taking",
    "supplements": "any supplements you take",
    "allergies": "your allergies or reactions",
    "care_team": "who's on your care team",
    "document_drop": "getting your existing records on file",
}

# --------------------------------------------------------------------------
# The intended clinical arc (docs/adr/0018-intake-clinical-progression-and-
# continuity.md) — GUIDANCE only, derived from the same coverage map
# `intake.coverage.CoverageState` already tracks. This is deliberately NOT a
# state machine: `run_intake_turn` never refuses or reorders anything a
# patient volunteers, and a topic later in this list can be covered before
# one earlier in it (existing behavior, unchanged). All this drives is which
# ONE topic the "Suggested next step" context hint below points the model
# toward once the current thread winds down — the same thing an experienced
# clinician's own mental checklist does, silently.
#
# Order mirrors the owner's stated arc: individual stats -> family history ->
# geography -> review the existing record (events/prior diagnoses/documents,
# where the document digest + excerpts genuinely pay off) -> what's recent
# (current symptoms/state) LAST, not first — the inversion that fixes the
# old wall-of-text-inviting opener. medications/supplements/allergies/
# care_team are folded in between record-review and "what's recent" since
# they are typically short, factual asks with no strong ordering
# requirement of their own.
_ARC_STEERING_ORDER: tuple[str, ...] = (
    "basics",
    "family_history",
    "geography",
    "events",
    "prior_diagnoses",
    "document_drop",
    "medications",
    "supplements",
    "allergies",
    "care_team",
    "symptoms",
)

# Extra, stage-specific guidance folded into the "Suggested next step" hint
# for topics that need more than just their `_TOPIC_PHRASES` phrase — the
# record-review cluster (walk the digest/excerpts with her rather than
# re-asking) and the final "what's recent" stage.
_ARC_STAGE_NOTE: dict[str, str] = {
    "events": (
        "This is the record-review stage: walk her through what the documents/encounters "
        "list above already shows (a hospitalization, an ER visit, a procedure) and ask her "
        "to confirm or correct it, citing what you're referencing (\"your records show a "
        "hospitalization in early 2024 and an ER visit before that -- can we go through "
        'those?") -- rather than asking her to retype her whole history from scratch.'
    ),
    "prior_diagnoses": (
        "Still the record-review stage: check any prior diagnoses she raises against what is "
        "already on file (the documents/encounters above, or the excerpts below) before "
        "treating it as new."
    ),
    "document_drop": (
        "Still the record-review stage: once you've walked the existing record with her, ask "
        "whether there's anything else worth adding to what's already on file."
    ),
    "symptoms": (
        "This is the 'what's recent' stage, deliberately last -- now is the time to ask what's "
        "been going on lately and what, if anything, has changed."
    ),
}


def _next_arc_topic(coverage: CoverageState) -> str | None:
    """The first not-yet-covered topic in `_ARC_STEERING_ORDER` — the ONE
    thing the "Suggested next step" hint points the model toward. `None`
    once every topic in the arc is covered (nothing left to steer toward)."""
    for key in _ARC_STEERING_ORDER:
        if key in SECTION_KEYS and not _is_covered(coverage, key):
            return key
    return None


def _render_arc_guidance(coverage: CoverageState) -> str:
    """Internal-only guidance text (never shown to the patient, never a
    rule the model must obey) naming the one topic to gently steer toward
    once the patient's own current thread winds down. See
    `_ARC_STEERING_ORDER`'s docstring — a patient volunteering something
    else first is always captured immediately regardless of this hint."""
    next_key = _next_arc_topic(coverage)
    if next_key is None:
        return "(every topic in the intended arc has been explored -- steer toward wrapping up.)"
    phrase = _TOPIC_PHRASES.get(next_key, next_key)
    note = _ARC_STAGE_NOTE.get(next_key)
    hint = f"Once the current thread winds down, gently steer toward {phrase}."
    return f"{hint} {note}" if note else hint


_INTAKE_AGENT_SYSTEM_PROMPT = f"""[intake-agent-v{INTAKE_AGENT_PROMPT_VERSION}]
You are conducting a patient's first visit for a-doc, a single-patient longitudinal
medical case-file tool -- the way an experienced clinician runs an initial intake
conversation, not a form. There is no checklist to show the patient and no rigid state
machine underneath you -- but a real clinician also does not ask a brand-new patient
"what's been bothering you?" as the very first question; they build up incrementally,
one or two facts at a time, starting from who the patient is before asking what's
wrong. Follow the patient's own narrative first and always: let them start wherever
they want, capture whatever they bring up immediately regardless of "the right time,"
and only when a thread has genuinely wound down do you gently steer -- using the
INTENDED ARC below and the "Suggested next step" hint in your context -- toward
something you haven't heard about yet, with a natural bridge ("you mentioned your knee
surgery -- that reminds me, has anything like that run in your family?" / "before we
move on, can I ask about any medications you're taking?"). Never mention "sections,"
"topics," "categories," a checklist, a percentage, "the arc," or how much is "left to
cover" -- the patient should experience one continuous conversation, never a form with
a progress bar.

INTENDED ARC (soft guidance, never a gate -- a patient volunteering something out of
order is always captured right away, never deferred or refused):
1. Individual stats -- age and sex at birth first (short, warm, one or two facts at a
   time), then height/weight and occupation as the conversation flows there naturally.
2. Family history -- parents, siblings, grandparents; what runs in the family.
3. Geography -- where she lives now and has lived before, travel of note, and any
   environmental/occupational exposure tied to a place or trip.
4. Review the existing record WITH her -- once the above is in hand, walk through what
   is already on file (the documents/encounters digest and any matched excerpts in your
   context below) rather than asking her to retype her history: "your records show a
   hospitalization in early 2024 and an ER visit before that -- can we go through
   those?" Cite what you're referencing. This is also where prior diagnoses and
   remaining document drop naturally live.
5. What's recent -- only now do you ask what's been going on lately and what has
   changed, the way a clinician circles back to "so what brings you in today" only
   after already knowing who the patient is.
Medications, supplements, allergies, and care team fit in wherever they come up
naturally, typically around stage 4-5. In an ordinary turn, build the record up
incrementally -- respond to and ask about the one or two facts actually in front of you
rather than front-loading a long list of questions (LONG PASTES below covers the
different case of a patient volunteering a lot of detail in one message).

This case file quietly organizes what you capture under a fixed set of internal
bookkeeping topics (never named to the patient): basics, symptoms, events,
prior_diagnoses, family_history, medications, supplements, allergies, care_team,
document_drop. Capture facts for WHATEVER topic the patient's message actually
touches, in whatever order they bring it up -- never wait for "the right time" to
record something, and never refuse information volunteered early or out of order.

SAFETY (non-negotiable):
- Never diagnose. Never suggest, name, or imply a diagnosis of your own.
- Never give treatment or dosing advice, not even phrased as a suggestion. Your job is
  to record what the patient says, nothing else.
- Capture facts ONLY from what the patient actually states. Never invent, infer, or
  embellish a detail the patient did not say.

WHAT YOU DO EACH TURN:
1. Read the patient's message, the topic coverage map, what's currently blocking each
   uncovered topic, the active facts already on file, the documents/encounters already
   on file, and the recent conversation (all supplied below -- for your own
   bookkeeping, never to relay to the patient).
2. Decide what fact ops (add_fact / update_fact / retract_fact) this message
   justifies. Every fact you add or touch must be traceable to something the patient
   actually said this turn or earlier in the conversation.
3. Write a short `message`: a natural acknowledgment of what you just heard, plus AT
   MOST TWO focused follow-up questions -- asked the way a clinician actually talks,
   never as a numbered list. Zero questions (a plain acknowledgment, or a closing
   summary) is fine when nothing needs asking.
4. When a topic feels genuinely explored -- including when the patient explicitly says
   there's nothing to report ("no autoimmune stuff in my family that I know of" is
   real, complete coverage of family history) -- list its internal key in
   `topics_covered`. You do not need to verify this yourself; the system quietly holds
   back any topic that still has something unresolved and keeps it open for a later
   turn, without ever telling the patient why.
5. Once you judge EVERY topic genuinely covered -- every thread you can think of has
   been explored, nothing feels left hanging -- set `intake_complete=true` and write a
   warm closing `message` the way a clinician wraps up a first visit: summarize that
   you now have a good picture (no clinical judgment, no diagnosis, no hint at what you
   think is going on), and invite them to send over any records they have or ask
   anything on their mind. If the system finds something still open, it quietly holds
   the close and steers you back -- just keep talking naturally when that happens.

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
involved, or they're not sure one ever said it): record
`attribution="patient_assumption"` and ask, non-judgmentally, "that's worth tracking --
what makes you think that?", capturing their answer in `fields.reasoning`. Never treat
a patient assumption as if it were confirmed, and never argue with it -- just record it
accurately and move on.

CROSS-REFERENCING DOCUMENTS ALREADY ON FILE:
The "Documents & encounters already on file" section below lists what has already been
ingested (dated documents, encounter files). When the patient describes an event/visit/
test that plausibly matches one of these (similar date, similar description), say so
explicitly and ask them to confirm or distinguish it -- e.g. "I have a record of an ER
note dated 2024-03-02 -- is that this visit, or a different one?" Use their answer to
either note the match in the fact's `statement` or keep the two as distinct events.

DOCUMENT EXCERPTS (her own prior words -- system-retrieved, read-only to you):
The "Relevant excerpts from her own prior documents" section below shows verbatim
passages -- system-retrieved by matching THIS message against documents she has already
provided (a written history she submitted, a doctor's note, a prior report) -- judged
relevant to what she just said. These are her own words, already on record: reference
them instead of asking her to retype something she already gave you (e.g. "your history
mentions joint pain starting in March -- is that still going on, or has it changed?").
"(none matched for this message)" just means nothing on file matched -- proceed normally,
and never mention "excerpts," "retrieval," or any other internal mechanism by name to the
patient.

FACT CORROBORATION (system-computed -- read-only to you):
Each active fact listed below carries a `corroboration` status computed by deterministic code
from documents/labs/encounters already on file -- you never set this yourself, and it has
nothing to do with anything you say. When a fact you are currently discussing is marked
`corroboration: contradicted`, raise the discrepancy conversationally EXACTLY ONCE, in plain
language ("the record shows X -- can you help me reconcile that?"), then record whatever the
patient says in reply with an `update_fact` op and a substantive note. Never argue with the
patient about it, never raise the same contradiction a second time once it's been asked about,
and never say the words "corroboration," "contradicted," or any other internal label out loud --
describe the actual discrepancy instead.

CORRECTIONS, AT ANY TIME:
The patient may correct or add to ANY previously recorded fact at any point, including
facts belonging to a topic you've already moved past. When they do, find the matching
fact by id in the "Active facts on file" list below and emit an `update_fact` op for it
(never a duplicate `add_fact`) with a substantive `note` explaining the change. Always
restate in your `message` what you changed ("Got it -- updated your penicillin allergy
to say hives, not a rash."). To remove something the patient says is wrong or no longer
applies, use `retract_fact` with a `reason` -- never silently drop it (it stays in
history, marked retracted).

LONG PASTES:
A patient sometimes pastes a very long written history in one message -- when that
happens you will see a note marking it in the context below. Never refuse it and never
ask her to shorten or resend it. Instead: acknowledge warmly that you got a lot of
detail, quietly capture whatever you can confidently pull out of it as fact ops (still
grounded ONLY in what she actually wrote), and steer your `message` to ONE thing at a
time rather than trying to ask about or process everything in a single reply.

FOLLOW-UPS (for a later visit):
When something is worth explicitly checking back on next time -- a symptom you'd like
an update on, something left open that isn't quite ready to resolve now -- set
`follow_up=true` on that fact (via `add_fact` or `update_fact`). This is the ONLY
mechanism that carries something forward to a future visit's opening context, so use it
deliberately: real follow-up items only, not everything you hear. Once a later visit's
conversation genuinely revisits it, clear the flag with `update_fact(follow_up=false)`
and a note. Never mention "follow-up flags" or this mechanism by name to the patient --
just raise it naturally when the moment comes ("last time you mentioned the rash was
spreading -- how has that been?").

FACT FIELDS CONVENTIONS:
`fields` is a flat set of key/value pairs -- use plain keys matching what the case file
expects for that internal topic (symptoms: onset/frequency/triggers/severity;
diagnoses: by_whom/year/reasoning/status; relatives: relation/conditions/age_at_onset/
deceased/age_at_death; locations (geography topic): place/date_approx/current/
category, where `category` is `"residence"` (default -- omit it for an ordinary
residence), `"travel"`, or `"exposure"` (an environmental/occupational exposure tied to
a place or trip -- tick/outdoor exposure, well water, farm work, a regional outbreak);
medications & supplements: name/dose/frequency/still_taking/notes; allergies:
allergen/reaction/severity; providers: name/specialty/org; care team: insurer). Where a
field is naturally a list (e.g. a relative's conditions), write it as one
comma-separated string. Every `id` you invent must be a short, stable, lowercase slug
with no spaces or colons (e.g. `father-allergy`, `2019-er-chest-pain`) -- reuse the
exact same id every time you touch the same fact again.

Respond only with the structured result (message, ops, topics_covered, intake_complete)
-- never free text outside that schema, and never a word to the patient about
"sections," "topics," gates, or what's left to cover.
"""


class IntakeTurnResult(BaseModel):
    """What the `intake_agent` model returns for one turn."""

    message: str
    ops: list[IntakeFactOp] = Field(default_factory=list)
    topics_covered: list[str] = Field(default_factory=list)
    intake_complete: bool = False


class IntakeOutcome(BaseModel):
    """What `run_intake_turn` returns to a caller (CLI REPL or the web
    chat route)."""

    kind: Literal["reply", "withheld", "error"]
    text: str


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
# Transcript persistence (intake's own audit log — see module docstring;
# `web.routes.chat` separately persists into the shared patient-facing chat
# transcript so the patient sees one continuous conversation)
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
# Coverage helpers
# --------------------------------------------------------------------------


def _spec_by_key(key: str) -> SectionSpec:
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise IntakeError(f"no such intake topic: {key!r}")
    return spec


def _is_covered(coverage: CoverageState, key: str) -> bool:
    return coverage.topics.get(key, TopicCoverage()).covered


def _mark_covered(coverage: CoverageState, key: str, *, when: datetime) -> None:
    coverage.topics[key] = TopicCoverage(covered=True, covered_at=when)


def _auto_cover_document_drop(repo: DataRepo, coverage: CoverageState, *, when: datetime) -> None:
    """Mirrors `intake.wizard`'s document-drop auto-skip: when documents
    are already on file (a seeded/curated deployment: `sources/` is
    non-empty), there is nothing to ask about getting records on file —
    mark the topic covered automatically instead of prompting the patient
    to upload what's already there. A genuinely fresh, empty data repo
    still gets asked in the normal course of conversation."""
    if _is_covered(coverage, "document_drop"):
        return
    sources = repo.root / "sources"
    has_documents = sources.is_dir() and any(
        entry.name != ".gitkeep" for entry in sources.iterdir()
    )
    if has_documents:
        _mark_covered(coverage, "document_drop", when=when)


def intake_is_complete(repo: DataRepo) -> bool:
    """Whether the initial-visit conversation has been marked complete
    (`CoverageState.intake_complete`) — the trigger that unlocks the
    diagnostic chat pipeline (`web.routes.chat`) and ends the CLI REPL."""
    return load_coverage_state(repo.root / INTAKE_STATE_RELPATH).intake_complete


def _render_coverage_map(coverage: CoverageState) -> str:
    lines = []
    for spec in SECTIONS:
        marker = "covered" if _is_covered(coverage, spec.key) else "not yet covered"
        lines.append(f"- {spec.title} ({spec.key}): {marker}")
    return "\n".join(lines)


def _render_gate_status(facts_store: IntakeFactsStore, coverage: CoverageState) -> str:
    lines: list[str] = []
    for spec in SECTIONS:
        if _is_covered(coverage, spec.key):
            continue
        blockers = section_completion_blockers(facts_store.facts, spec.key)
        if blockers:
            lines.append(f"- {spec.title} ({spec.key}):")
            lines.extend(f"    - {b}" for b in blockers)
    return "\n".join(lines) if lines else "(nothing currently blocked)"


def _render_active_facts(facts_store: IntakeFactsStore) -> str:
    facts = facts_store.active_facts()
    if not facts:
        return "facts: []"
    lines = ["facts:"]
    for fact in facts:
        lines.append(f"  - id: {fact.id}")
        lines.append(f"    topic: {fact.section}")
        lines.append(f"    kind: {fact.kind}")
        lines.append(f"    statement: {fact.statement!r}")
        if fact.date_approx:
            lines.append(f"    date_approx: {fact.date_approx}")
        lines.append(f"    precision: {fact.precision}")
        lines.append(f"    attribution: {fact.attribution}")
        lines.append(f"    clarification_status: {fact.clarification_status}")
        lines.append(f"    corroboration: {fact.corroboration}")
        if fact.corroboration == "contradicted" and fact.corroboration_note:
            lines.append(f"    corroboration_note: {fact.corroboration_note!r}")
        if fact.fields:
            fields_str = ", ".join(f"{k}: {v}" for k, v in fact.fields.items())
            lines.append(f"    fields: {{{fields_str}}}")
    return "\n".join(lines)


def _render_document_excerpts(db: LabsDb, text: str) -> str:
    """Verbatim excerpts from documents already on file, ranked against the
    patient's CURRENT message (docs/adr/0015-document-text-corpus.md) —
    never paraphrased, capped at `DOC_EXCERPT_MAX_CHARS` total characters.
    Genomic files are structurally excluded (nothing ever populates
    `document_text` for one — see `ingest.doctext`'s module docstring), so
    this can never surface genotype data even indirectly.
    """
    hits = db.search_document_text(text, limit=DOC_EXCERPT_LIMIT)
    if not hits:
        return "(none matched for this message)"

    lines: list[str] = []
    budget = DOC_EXCERPT_MAX_CHARS
    for hit in hits:
        snippet = hit.snippet.strip()
        if not snippet or budget <= 0:
            continue
        line = f"- {hit.source_ref}: {snippet}"
        if len(line) > budget:
            line = line[: max(budget - 1, 0)].rstrip() + "…"
        lines.append(line)
        budget -= len(line)

    return "\n".join(lines) if lines else "(none matched for this message)"


def _build_turn_context(
    repo: DataRepo,
    db: LabsDb,
    coverage: CoverageState,
    facts_store: IntakeFactsStore,
    text: str,
) -> str:
    transcript = _render_transcript(read_intake_transcript(repo, limit=TRANSCRIPT_CONTEXT_TURNS))
    return (
        "## Topic coverage so far (internal bookkeeping only — never mention topics, "
        "sections, or checklists to the patient)\n\n"
        f"{_render_coverage_map(coverage)}\n\n"
        "## Suggested next step (internal guidance only -- the patient's own thread always "
        "comes first; this only shapes where you steer once it winds down)\n\n"
        f"{_render_arc_guidance(coverage)}\n\n"
        "## What's currently blocking an uncovered topic (internal only)\n\n"
        f"{_render_gate_status(facts_store, coverage)}\n\n"
        f"## Active facts on file\n\n{_render_active_facts(facts_store)}\n\n"
        f"## Documents & encounters already on file\n\n{build_doc_digest(db, repo)}\n\n"
        "## Relevant excerpts from her own prior documents, for THIS message "
        "(her own words — reference them, don't re-ask)\n\n"
        f"{_render_document_excerpts(db, text)}\n\n"
        f"## Recent conversation\n\n{transcript}\n"
    )


def _render_wrapup_refusal(coverage: CoverageState, blockers_anywhere: list[str]) -> str:
    uncovered_keys = [spec.key for spec in SECTIONS if not _is_covered(coverage, spec.key)]
    lines: list[str] = []
    if uncovered_keys:
        phrases = ", ".join(_TOPIC_PHRASES.get(key, key) for key in uncovered_keys)
        lines.append(f"Before I can wrap up, I'd still like to hear about {phrases}.")
    if blockers_anywhere:
        lines.append("A couple of things could use a bit more detail:")
        lines.extend(f"- {b}" for b in blockers_anywhere)
    return "\n".join(lines)


def _write_section_from_facts(
    repo: DataRepo, facts_store: IntakeFactsStore, section_key: str
) -> list[str]:
    spec = _spec_by_key(section_key)
    data = facts_to_section_data(facts_store.facts, section_key)
    section_data = spec.schema.model_validate(data)
    return write_section(repo, section_key, section_data)


def _write_section_from_facts_safe(
    repo: DataRepo, facts_store: IntakeFactsStore, section_key: str
) -> list[str]:
    """Best-effort wrapper around `_write_section_from_facts` -- the
    stated invariant this module upholds end to end (see `run_intake_turn`'s
    and `run_visit_capture`'s docstrings): facts are the source of truth,
    a case-file artifact is always DERIVED from them, and a failure while
    deriving one (`facts_to_section_data`'s conversion, the section
    schema's `model_validate`, or a `wizard._write_*` writer) must degrade
    -- log the topic and the reason, skip just that one artifact -- rather
    than raise and cost the patient her whole turn. The live incident this
    fixes: a family-history relative's `age_at_onset` didn't survive
    `model_validate` because the schema forced an exact integer (see
    `intake.sections.Relative.age_at_onset`'s docstring); that field is now
    fixed at the source, but this wrapper is the general backstop for
    every OTHER way rendering a topic's artifact could fail, known or not
    yet discovered -- the facts themselves are unaffected either way and
    the artifact can always be regenerated once whatever broke is fixed.
    """
    try:
        return _write_section_from_facts(repo, facts_store, section_key)
    except Exception:
        logger.exception(
            "intake turn: writer for topic %r failed; skipping this artifact this turn "
            "(the underlying facts are already persisted and unaffected -- the artifact "
            "can be regenerated on a later turn once the cause is fixed)",
            section_key,
        )
        return []


def _build_ops_retry_feedback(rejected: list[str]) -> str:
    """Feedback text for the ONE ops-retry `run_intake_turn` spends when
    `IntakeFactsStore.apply_ops` rejects part of a turn's ops -- same shape
    as `reason.citations.build_retry_feedback`: name what failed and why,
    ask for a corrected re-emission of just those ops, same schema."""
    lines = ["The following fact op(s) from your last response could not be applied:"]
    lines.extend(f"- {reason}" for reason in rejected)
    lines.append(
        "Re-emit corrected versions of ONLY these ops in `ops` (fix the id/reference "
        "problem named above, or drop the op if it no longer applies). Do not repeat any "
        "other op -- it already applied successfully. `message`, `topics_covered`, and "
        "`intake_complete` are ignored for this retry; only `ops` will be used."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The turn entry point
# --------------------------------------------------------------------------


def run_intake_turn(client: LlmClient, repo: DataRepo, db: LabsDb, text: str) -> IntakeOutcome:
    """Run one conversational onboarding turn.

    No automated emergency screening (see `docs/adr/0021*.md` for why):
    intake is historical narrative by construction, and matched constantly.

    **The invariant this function upholds end to end:** no single malformed
    field, op, artifact conversion, or derived write may cost the patient
    her turn. At most she loses the one detail that was actually
    unrecoverable (never the message, and never facts that DID apply
    cleanly). This has broken in five different shapes in production so
    far — an unrecognized `section` value aborting the whole op batch, an
    `add_fact` emitted flat instead of nested failing structured-output
    parsing outright, a placeholder date string crashing `confirm()`, a
    rigidly-typed `age_at_onset` rejecting "late 30s", and a source-ref
    pattern rejecting real filenames — different layers, same shape: a
    strict boundary meeting real variability. Concretely, in THIS function:
    `IntakeFactsStore.apply_ops` is tolerant by design (applies every valid
    op, collects the rest in `.rejected`, never raises); this function
    spends ONE feedback-guided retry on top of that — mirroring
    `reason.stages.composer_stage`'s gate-guided rewrite and the citation
    checker's retry — naming exactly which ops were rejected and why, so
    the model can re-emit corrected versions (`message`/`topics_covered`/
    `intake_complete` always come from the FIRST attempt: the retry is
    narrowly about fixing `ops`). Facts are persisted BEFORE anything
    derived from them is attempted (a rendering failure must never risk
    losing what was already captured); every per-topic artifact write goes
    through `_write_section_from_facts_safe`, which degrades to "skip this
    artifact, log why" rather than raising; and the corroboration sweep is
    similarly non-fatal. Still-rejected ops after the retry (or a retry
    call that itself fails) are simply dropped and logged; the turn still
    replies normally either way. The one case this function cannot fully
    protect against is structured-output parsing failing on BOTH attempts —
    there are no ops to apply at all in that case — but even then the raw
    exchange is still written to the transcript before returning, so the
    patient's own words are never silently dropped from the record.
    """
    now = datetime.now(UTC)
    coverage = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    _auto_cover_document_drop(repo, coverage, when=now)
    facts_store = IntakeFactsStore(repo.root)

    context = _build_turn_context(repo, db, coverage, facts_store, text)
    user_content = f"{context}\n\n## Patient message\n\n{text}\n"
    # Never truncate or refuse a long paste -- just steer the model to
    # handle it gracefully (acknowledge the volume, extract what it can,
    # ask about one thing at a time).
    if len(text) > LONG_MESSAGE_THRESHOLD_CHARS:
        user_content += _LONG_MESSAGE_NOTE

    messages: list[Message] = [Message(role="user", content=user_content)]
    turn: IntakeTurnResult | None = None
    last_parsed: IntakeTurnResult | None = None
    applied = AppliedResult()
    retry_feedback: str | None = None
    # A schema retry spends one of the same bounded attempts the ops retry
    # uses, so a turn is never more than `_OPS_RETRY_ATTEMPTS` model calls
    # however it fails.
    schema_retry_used = False

    for _attempt in range(_OPS_RETRY_ATTEMPTS):
        if retry_feedback is not None:
            assert last_parsed is not None
            messages = [
                *messages,
                Message(role="assistant", content=last_parsed.model_dump_json()),
                Message(role="user", content=retry_feedback),
            ]
        try:
            result = client.complete(
                "intake_agent",
                system=_INTAKE_AGENT_SYSTEM_PROMPT,
                messages=messages,
                schema=IntakeTurnResult,
            )
        except LlmError as exc:
            if turn is None:
                # A schema-validation failure on the FIRST attempt would
                # otherwise cost the patient her whole message — observed
                # live, when the model emitted an `add_fact` op's fields
                # flat instead of nested and a turn of family history was
                # lost. `facts.AddFact` now lifts that specific shape, but
                # the general case still needs a second chance: retry once,
                # feeding the validation error back so the model can
                # re-emit. Only then give up.
                if not schema_retry_used:
                    schema_retry_used = True
                    logger.warning("intake turn: schema validation failed, retrying once: %s", exc)
                    messages = [
                        *messages,
                        Message(
                            role="user",
                            content=(
                                "Your previous reply could not be parsed into the required "
                                f"structure. The validation error was:\n\n{exc}\n\n"
                                "Re-send the SAME turn, correcting only the structure. Each op "
                                'must be shaped exactly as {"op": "add_fact", "fact": {...}} — '
                                "the fact's own fields belong inside `fact`, never beside `op`."
                            ),
                        ),
                    ]
                    continue
                # Genuinely nothing to apply here (structured-output parsing
                # failed twice) -- there are no facts to persist, but the
                # patient's own words must not vanish without a trace: still
                # write the exchange to the transcript so a later turn's
                # context (and any human review) sees that she said
                # something here, even though it couldn't be captured as
                # structured facts.
                # The patient must never be shown the raw exception. A live
                # run put a pydantic traceback and an errors.pydantic.dev URL
                # in the chat window ("Input should be a valid list ..."),
                # which is meaningless to her and reads like the app broke.
                # The detail belongs in the log, where it is actionable.
                logger.warning("intake turn: giving up after schema retry: %s", exc)
                error_outcome = IntakeOutcome(
                    kind="error",
                    text=(
                        "Sorry — I had trouble recording that one. Nothing is wrong with "
                        "your case file. Could you say it again, or put it a little "
                        "differently?"
                    ),
                )
                _append_transcript_turn(repo, text, error_outcome)
                return error_outcome
            logger.warning("intake turn: ops-retry call failed, keeping first attempt: %s", exc)
            break

        parsed = result.parsed
        assert isinstance(parsed, IntakeTurnResult)
        last_parsed = parsed
        if turn is None:
            turn = parsed

        round_provenance = Provenance(
            app_version=__version__,
            prompt_template_version=INTAKE_AGENT_PROMPT_VERSION,
            model_id=result.model_id,
            dag_node="intake-agent",
            timestamp=now,
        )
        round_applied = facts_store.apply_ops(parsed.ops, round_provenance)
        applied = AppliedResult(
            added=[*applied.added, *round_applied.added],
            updated=[*applied.updated, *round_applied.updated],
            retracted=[*applied.retracted, *round_applied.retracted],
            rejected=round_applied.rejected,
        )
        if not applied.rejected:
            break
        retry_feedback = _build_ops_retry_feedback(applied.rejected)

    assert turn is not None
    for reason in applied.rejected:
        logger.warning("intake turn: dropping fact op that failed to apply: %s", reason)

    # Persist the facts BEFORE attempting anything derived from them:
    # facts are the source of truth (a case-file artifact is always
    # regenerable from them, never the reverse), so a failure in the
    # rendering below must never risk losing what this turn already
    # captured. This is a bare `.save()` -- the git commit (which also
    # covers coverage state, the transcript, and any artifacts) still
    # happens once at the end of the turn, but the on-disk facts file
    # itself is never left depending on a writer's success.
    facts_store.save()

    touched_ids = [*applied.added, *applied.updated, *applied.retracted]
    touched_topics = {
        fact.section for fact_id in touched_ids if (fact := facts_store.get(fact_id)) is not None
    }

    artifacts: list[str] = []

    # Amend mode: a correction/addition to an already-covered topic
    # regenerates that topic's case-file artifact(s) immediately, even
    # though this turn isn't newly covering anything (facts are editable
    # at any time, during AND after the initial visit). A writer failure
    # here is never fatal to the turn -- see `_write_section_from_facts_safe`.
    for topic_key in touched_topics:
        if _is_covered(coverage, topic_key):
            artifacts.extend(_write_section_from_facts_safe(repo, facts_store, topic_key))

    # --- deterministic topic-coverage veto: code, not the model, decides ---
    for topic_key in turn.topics_covered:
        if topic_key not in SECTION_KEYS or _is_covered(coverage, topic_key):
            continue
        if section_completion_blockers(facts_store.facts, topic_key):
            continue  # vetoed silently — routine turns never surface gate mechanics
        artifacts.extend(_write_section_from_facts_safe(repo, facts_store, topic_key))
        _mark_covered(coverage, topic_key, when=now)

    # --- wrap-up: intake_complete is accepted only when every topic is
    # covered and no blocker remains anywhere; a refused proposal appends
    # ONE deterministic, conversationally-phrased steering line ---
    wrapup_note = ""
    if turn.intake_complete and not coverage.intake_complete:
        blockers_anywhere = [
            blocker
            for spec in SECTIONS
            for blocker in section_completion_blockers(facts_store.facts, spec.key)
        ]
        all_covered = all(_is_covered(coverage, spec.key) for spec in SECTIONS)
        if all_covered and not blockers_anywhere:
            coverage.intake_complete = True
        else:
            wrapup_note = "\n\n" + _render_wrapup_refusal(coverage, blockers_anywhere)

    reply_text = turn.message + wrapup_note
    # `recording_only`: intake is a SCRIBE conversation — asking "which
    # medication, and what dose?" and reading a list back for confirmation
    # is its job (the `medications`/`supplements` topics require exactly
    # that). The bare-dosage rule made it withhold its own reply to a
    # patient who had just said she could not remember her medication.
    # Instruction-shaped output ("start taking 50 mcg daily") still trips
    # the imperative rule, which is what rule 5 actually guards.
    gate = treatment_gate(reply_text, recording_only=True)
    if not gate.passed:
        # Previously invisible: a withheld intake reply logged nothing, so
        # the only way to notice was reading the conversation.
        logger.warning(
            "intake: reply withheld by the treatment gate; offending span(s): %s",
            "; ".join(f"{s.text!r} ({s.reason})" for s in gate.spans),
        )
    outcome = (
        IntakeOutcome(kind="reply", text=reply_text)
        if gate.passed
        else IntakeOutcome(kind="withheld", text=_WITHHELD_MESSAGE)
    )

    # Corroboration sweep (deterministic, no LLM call): re-check every fact
    # this turn added or updated against already-ingested documentation.
    # Skipped on a pure-retract turn (nothing new to corroborate) —
    # `docs/adr/0013-fact-corroboration.md`. Non-fatal like the writers
    # above: this reads documents/encounters/labs off disk and is not
    # immune to a malformed source (e.g. a broken encounter file), and a
    # failure here must degrade to "corroboration state stays as it was"
    # rather than cost the turn its reply, its facts, or its commit.
    if applied.added or applied.updated:
        try:
            corroboration_updates = corroborate_facts(facts_store.facts, db, repo)
            facts_store.apply_corroboration(corroboration_updates, at=now)
        except Exception:  # noqa: BLE001 - see comment above: must never cost the turn
            logger.exception(
                "intake turn: corroboration sweep failed; leaving facts' corroboration state "
                "unchanged for this turn"
            )

    # Persist again to capture the corroboration sweep's updates on top of
    # the early save above (which already protected this turn's own facts
    # regardless of what happens below). An LlmError above already returned
    # before any of this — nothing is written for that turn; a rejected op
    # never blocks the persist of everything else that DID apply — see
    # this function's docstring.
    facts_store.save()
    save_coverage_state(repo.root / INTAKE_STATE_RELPATH, coverage)
    _append_transcript_turn(repo, text, outcome)

    paths = [
        INTAKE_FACTS_RELPATH,
        INTAKE_STATE_RELPATH,
        INTAKE_TRANSCRIPT_RELPATH,
        *sorted(set(artifacts)),
    ]
    repo.commit("feat(intake): conversational turn", paths=paths)

    return outcome


# --------------------------------------------------------------------------
# Post-intake continuity: last visit, current state, follow-ups
# (docs/adr/0018-intake-clinical-progression-and-continuity.md)
# --------------------------------------------------------------------------

OPEN_QUESTIONS_RELPATH = "case/questions-open.md"

# How far back "recently changed" reaches for continuity purposes -- mirrors
# `web.routes.onboard.RECENT_WINDOW_DAYS` (the Intake record page's "Since
# last visit" strip), kept as its own constant here so this module never
# needs to import from `web` (layering: web depends on intake, not the
# reverse).
RECENT_FACT_WINDOW_DAYS = 14

# A returning chat session counts as the start of a NEW "visit" once this
# much time has passed since the previous transcript entry -- short enough
# that a same-sitting back-and-forth is never mistaken for a new visit, long
# enough that a same-day-but-later check-in still counts as one. Consumed by
# `web.routes.chat` (which owns the chat transcript, and therefore "when did
# we last talk"), not by anything in this module.
VISIT_GAP_THRESHOLD_HOURS = 4


def active_follow_ups(facts_store: IntakeFactsStore) -> list[IntakeFact]:
    """Active facts explicitly flagged `follow_up=True` (`IntakeFact.
    follow_up` — set only by the model, via `add_fact`/`update_fact`, never
    inferred). Order matches the store's own (insertion) order, so callers
    that want "the most relevant one" consistently get the same fact for a
    given store state — this is what a returning visit's greeting and the
    Intake record page both read from."""
    return [f for f in facts_store.active_facts() if f.follow_up]


class ContinuityInfo(BaseModel):
    """What a post-intake visit should open already knowing
    (docs/adr/0018): when the patient last talked to a-doc, what was
    explicitly flagged to revisit, and what else is still open. Computed
    fresh from durable state on every call — nothing here is itself
    persisted. `last_visit_at` is supplied by the caller (`web.routes.chat`
    owns the chat transcript this module never reads) rather than computed
    here."""

    last_visit_at: datetime | None = None
    follow_ups: list[IntakeFact] = Field(default_factory=list)
    unresolved_facts: list[IntakeFact] = Field(default_factory=list)
    recent_facts: list[IntakeFact] = Field(default_factory=list)
    open_questions: str | None = None


def _read_open_questions(repo: DataRepo) -> str | None:
    path = repo.root / OPEN_QUESTIONS_RELPATH
    if not path.exists():
        return None
    text = repo.read(OPEN_QUESTIONS_RELPATH).strip()
    return text or None


def build_continuity_info(
    repo: DataRepo,
    facts_store: IntakeFactsStore,
    *,
    last_visit_at: datetime | None,
    now: datetime,
) -> ContinuityInfo:
    """Assemble `ContinuityInfo` from durable state: active facts flagged
    `follow_up`, active facts still `clarification_status="needs_probe"`,
    facts `reported_on` within `RECENT_FACT_WINDOW_DAYS` of `now`, and
    `case/questions-open.md` if non-empty. `last_visit_at` is passed through
    unchanged — the caller (which owns the chat transcript) decides what
    counts as "last visit"."""
    active = facts_store.active_facts()
    unresolved = [f for f in active if f.clarification_status == "needs_probe"]
    cutoff = now.date() - timedelta(days=RECENT_FACT_WINDOW_DAYS)
    recent = sorted(
        (f for f in active if f.reported_on is not None and f.reported_on >= cutoff),
        key=lambda f: f.reported_on,  # type: ignore[arg-type, return-value]
        reverse=True,
    )
    return ContinuityInfo(
        last_visit_at=last_visit_at,
        follow_ups=active_follow_ups(facts_store),
        unresolved_facts=unresolved,
        recent_facts=recent,
        open_questions=_read_open_questions(repo),
    )


def _opening_line(delta: timedelta) -> str:
    """A short, conversational opening sentence naming a time gap -- never a
    raw duration or an exact timestamp (this must read like a person talks,
    not a log line). Full sentences, not just a duration fragment, so the
    grammar stays natural for very recent gaps ("We talked earlier today.")
    as well as longer ones ("It's been about 3 weeks since we last
    talked.")."""
    days = delta.days
    if days <= 0:
        hours = int(delta.total_seconds() // 3600)
        return "We talked earlier today." if hours <= 1 else f"We talked about {hours} hours ago."
    if days == 1:
        return "We talked yesterday."
    if days < 14:
        return f"It's been about {days} days since we last talked."
    if days < 60:
        weeks = max(days // 7, 1)
        return f"It's been about {weeks} week{'s' if weeks != 1 else ''} since we last talked."
    months = max(days // 30, 1)
    return f"It's been about {months} month{'s' if months != 1 else ''} since we last talked."


def render_continuity_note(info: ContinuityInfo, *, now: datetime) -> str | None:
    """A short, conversational note for the first reply of a new post-intake
    visit -- code-composed (not model-authored), so it is deterministic and
    testable and `web.routes.chat` can prepend it. Returns `None` when there
    is nothing worth saying (no prior
    visit on file yet) — never a status dump: at most one line naming how
    long it's been, plus at most one more line naming the single most
    relevant open item (a follow-up first, then an unresolved fact, then a
    generic nudge toward open questions on file)."""
    if info.last_visit_at is None:
        return None
    lines = [_opening_line(now - info.last_visit_at)]
    if info.follow_ups:
        subject = info.follow_ups[0].statement.rstrip(".")
        lines.append(f"Last time I made a note to check back on: {subject} -- how has that been?")
    elif info.unresolved_facts:
        subject = info.unresolved_facts[0].statement.rstrip(".")
        lines.append(f"One thing we hadn't fully pinned down yet: {subject}.")
    elif info.open_questions:
        lines.append(
            "There's also an open question or two on file from before, whenever you want to "
            "pick that back up."
        )
    return " ".join(lines)


# --------------------------------------------------------------------------
# Interval history: silent post-intake visit capture
# (docs/adr/0013-fact-corroboration.md, "visits grow the record")
# --------------------------------------------------------------------------

VISIT_CAPTURE_PROMPT_VERSION = "2"

_VISIT_CAPTURE_SYSTEM_PROMPT = f"""[visit-capture-v{VISIT_CAPTURE_PROMPT_VERSION}]
You are a silent background process for a-doc, a single-patient longitudinal medical case-file
tool. The patient is in an ORDINARY follow-up chat turn -- their initial visit is already
complete, and a separate diagnostic assistant is handling (or has already handled) this message
conversationally. Your only job is to notice whether this one message contains genuinely NEW or
CHANGED patient-reported information worth adding to the permanent case file: a new or worsening
symptom, a new medical event, a medication or supplement change, a new diagnosis or suspicion
reported from an outside doctor, a new allergy, a new family-history or geography detail, and so
on.

You never reply to the patient -- there is no `message` field in your output, and nothing you
produce is ever shown to them. Emitting NO ops at all is the correct, expected result for MOST
turns: a question, small talk, a request for information, or discussion of an already-recorded
symptom with no new detail all deserve zero ops. Over-capturing -- recording something already on
file, or promoting routine conversation into a spurious "new" fact -- is worse than
under-capturing. When in doubt, emit nothing.

When you do emit ops, use the exact same `add_fact`/`update_fact`/`retract_fact` shapes, internal
topic keys, `kind` values, and `fields` conventions the initial-visit engine uses (basics,
symptoms, events, prior_diagnoses, family_history, geography, medications, supplements,
allergies, care_team, document_drop). Check the active facts already on file below before adding
anything -- if this message updates something already recorded, emit `update_fact` (never a
duplicate `add_fact`) with a substantive note. Never diagnose, never give treatment or dosing
advice, and never invent or embellish a detail the patient did not actually state.

FOLLOW-UPS FLAGGED ON A PRIOR VISIT: the context below lists any facts a previous visit flagged
`follow_up=true` as worth checking back on. If this message genuinely addresses one (the patient
gives an update on it), emit `update_fact` for it with the new information AND `follow_up=false`
to clear the flag. You may also set `follow_up=true` on a new or updated fact yourself when
something in THIS message is worth explicitly checking back on next time -- use it deliberately,
not for everything you hear.

Respond only with the structured result (`ops`) -- never free text, never anything addressed to
the patient.
"""


class VisitCaptureResult(BaseModel):
    """What the `intake_agent` model returns for one silent visit-capture
    pass. No `message` field: this pass is silent, the patient never sees
    it (see module docstring)."""

    ops: list[IntakeFactOp] = Field(default_factory=list)


class CaptureResult(BaseModel):
    """What `run_visit_capture` did for one post-intake chat turn."""

    applied: AppliedResult = Field(default_factory=AppliedResult)
    error: str | None = None


def _render_follow_ups(facts_store: IntakeFactsStore) -> str:
    follow_ups = active_follow_ups(facts_store)
    if not follow_ups:
        return "(none flagged)"
    return "\n".join(f"- id: {fact.id} -- {fact.statement!r}" for fact in follow_ups)


def _build_capture_context(db: LabsDb, repo: DataRepo, facts_store: IntakeFactsStore) -> str:
    return (
        f"## Active facts on file\n\n{_render_active_facts(facts_store)}\n\n"
        "## Follow-ups flagged on a prior visit (system-tracked; clear one with "
        "update_fact(follow_up=false) once genuinely revisited)\n\n"
        f"{_render_follow_ups(facts_store)}\n\n"
        f"## Documents & encounters already on file\n\n{build_doc_digest(db, repo)}\n"
    )


def run_visit_capture(client: LlmClient, repo: DataRepo, db: LabsDb, text: str) -> CaptureResult:
    """A silent, best-effort capture pass run after an ordinary (post-intake)
    chat turn completes successfully — the "weekly visit" reality: patient
    statements keep accumulating structured facts on every visit, not just
    during onboarding. Uses the `intake_agent` model role with its own,
    dedicated, silent prompt (never the initial-visit conversation prompt —
    this pass never talks to the patient). Applies any resulting ops, the
    same corroboration sweep as an intake turn, and the same
    already-covered-topic artifact regeneration — exactly like
    `run_intake_turn`, just with `dag_node="visit-capture"` in the stamped
    provenance and no reply/coverage/wrap-up handling (there is nothing left
    to onboard).

    Deliberately fails soft end to end, never just at the LLM call: an
    `LlmError` from the client call is caught, logged, and reported back via
    `CaptureResult.error`. `apply_ops` itself is tolerant of a bad op
    (unknown/duplicate id — see `intake.facts`), so a single malformed op
    never costs this pass its other, valid ones; anything rejected is
    logged and otherwise ignored (this pass is silent, so there is no reply
    to append it to). Facts are persisted BEFORE any artifact is derived
    from them (same reasoning as `run_intake_turn`), each artifact write
    goes through `_write_section_from_facts_safe` so a rendering failure
    only skips that one artifact, and everything after that early save
    (corroboration, the final save, the commit) is wrapped so a failure
    there degrades to a reported `.error` rather than raising. This
    function must NEVER raise and break the calling chat turn, whose
    diagnostic/informational reply has already succeeded by the time this
    runs (`web.routes.chat`, which documents exactly this contract).
    Persists (and commits) only when at least one op actually changed the
    store — the common case (a turn with nothing new to capture, or one
    whose only ops were all rejected) touches disk not at all.
    """
    facts_store = IntakeFactsStore(repo.root)
    user_content = (
        f"{_build_capture_context(db, repo, facts_store)}\n\n## Patient message\n\n{text}\n"
    )

    try:
        result = client.complete(
            "intake_agent",
            system=_VISIT_CAPTURE_SYSTEM_PROMPT,
            messages=[Message(role="user", content=user_content)],
            schema=VisitCaptureResult,
        )
    except LlmError as exc:
        logger.warning("visit-capture: LLM call failed, skipping this turn's capture: %s", exc)
        return CaptureResult(error=str(exc))

    turn = result.parsed
    assert isinstance(turn, VisitCaptureResult)
    if not turn.ops:
        return CaptureResult()

    now = datetime.now(UTC)
    provenance = Provenance(
        app_version=__version__,
        prompt_template_version=VISIT_CAPTURE_PROMPT_VERSION,
        model_id=result.model_id,
        dag_node="visit-capture",
        timestamp=now,
    )

    applied = facts_store.apply_ops(turn.ops, provenance)
    for reason in applied.rejected:
        logger.warning("visit-capture: dropping fact op that failed to apply: %s", reason)

    touched_ids = [*applied.added, *applied.updated, *applied.retracted]
    if not touched_ids:
        # Nothing actually changed the store (every op was rejected, or --
        # unreachable today since an empty `ops` list already returned
        # above -- some other no-op case): touch disk not at all.
        return CaptureResult(applied=applied)

    # Persist BEFORE attempting anything derived from the facts -- same
    # reasoning as `run_intake_turn`: a failure below must never cost this
    # silent pass the facts it already applied.
    facts_store.save()

    coverage = load_coverage_state(repo.root / INTAKE_STATE_RELPATH)
    touched_topics = {
        fact.section for fact_id in touched_ids if (fact := facts_store.get(fact_id)) is not None
    }
    artifacts: list[str] = []
    for topic_key in touched_topics:
        if _is_covered(coverage, topic_key):
            artifacts.extend(_write_section_from_facts_safe(repo, facts_store, topic_key))

    # Everything past this point (corroboration, the final save, the
    # commit) touches the filesystem/db in ways this pass cannot fully
    # guarantee against, and per the module contract this function must
    # never raise into the calling chat turn -- degrade to a reported
    # `.error` instead. The facts themselves are already safely persisted
    # by the early save above regardless of what happens here.
    try:
        corroboration_updates = corroborate_facts(facts_store.facts, db, repo)
        facts_store.apply_corroboration(corroboration_updates, at=now)

        facts_store.save()
        paths = [INTAKE_FACTS_RELPATH, *sorted(set(artifacts))]
        repo.commit("feat(intake): visit-capture pass", paths=paths)
    except Exception as exc:  # noqa: BLE001 - see module docstring: must never crash the chat turn
        logger.exception(
            "visit-capture: post-write step failed; facts were already persisted above"
        )
        return CaptureResult(applied=applied, error=str(exc))

    return CaptureResult(applied=applied)

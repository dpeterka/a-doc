# ADR 0018 — Clinical intake progression + post-intake continuity

- Status: Accepted (2026-08-25)
- Refines ADR 0012 (which stands unchanged: no visible stepper, no topic
  list, no percentage — that rule is not being revisited). Extends ADR 0011
  (`intake/facts.py`'s fact model, deterministic gates) and ADR 0013 (visit
  capture, corroboration) with two more mechanisms: a soft steering order
  and a persisted follow-up flag.

## Context

Direct owner feedback on the merged conversational engine:

> Chat for intake is still not what I would expect… Start the chain as a
> human would. Ask how old, sex. Keep it constrained to build incrementally.
> individual stats, family history, geography, then it should be able to
> read from past history to review current state with the patient, then
> last identify what is recent. Once intake is done the chatbot should be
> able to have a concept of the last time spoken, current state and follow
> ups.

Two distinct problems, both real:

1. **The opener was still backwards.** `INTAKE_OPENER_MESSAGE` asked "what's
   been bothering you the most lately, or what brings you in?" as the FIRST
   question of the FIRST visit — before the agent knew anything about the
   patient. A real clinician never opens that way; asking it first is
   exactly what invites the wall-of-text answer the ADR 0014 red-flag
   warn-not-block fix already had to work around. The system prompt also
   claimed "there is no fixed order" — true in the sense that nothing should
   ever refuse an out-of-order answer, but false in the sense that an
   experienced clinician plainly *does* have an intended progression (they
   just never announce it). Nothing captured that intended order anywhere,
   so the model had no steering signal beyond "follow the patient."
2. **Nothing carried forward between visits.** `intake/agent.py` already
   distinguishes the initial visit (`run_intake_turn`) from later visits
   (`run_visit_capture`, ADR 0013), but a later visit opens with zero sense
   that a previous one ever happened — no "how long has it been," no memory
   of anything flagged as worth checking on, nothing surfaced anywhere a
   patient could see it. ADR 0013's own context named this gap ("assumes
   essentially a weekly visit with the agent") without closing it.

Also missing entirely: geography. This patient's labs show regional/
tick-borne infectious exposure signal that the case file has no dedicated
place to record — residence history, travel, and environmental/occupational
exposure tied to a place currently have nowhere to go except
`BasicsSection.exposures` (which is occupational-only by original design)
or nowhere at all.

## Decision

### 1. An intended arc, expressed as steering guidance, not a state machine

`intake/agent.py` gains `_ARC_STEERING_ORDER`, a fixed tuple of topic keys
in the owner's stated order (individual stats → family history → geography
→ review the existing record [events / prior diagnoses / document drop] →
what's recent [current symptoms] last, medications/supplements/allergies/
care_team folded in between), plus `_next_arc_topic` (first not-yet-covered
key in that order, read straight from the existing `CoverageState`) and
`_render_arc_guidance` (the one-line internal hint this produces, with
extra stage-specific notes for the record-review cluster and the final
"what's recent" stage). This is rendered into `_build_turn_context` as a new
"Suggested next step" section, alongside — never replacing — the existing
coverage map and blocker list.

This is deliberately **not** a gate. `run_intake_turn`'s actual behavior is
unchanged: a patient volunteering something from a later stage is captured
immediately, exactly as before (ADR 0011/0012's core commitment). The arc
only shapes which ONE topic the model is nudged toward once a thread winds
down — the same thing a clinician's own mental checklist does, silently, and
the system prompt says so explicitly ("the patient's own thread always comes
first"). No new state is persisted for this — `_next_arc_topic` is a pure
function of the coverage map that already exists.

`INTAKE_AGENT_PROMPT_VERSION` bumps 5 → 6: the system prompt gains an
"INTENDED ARC" section describing the five stages and the "one or two facts
at a time, not a batch" incremental-build constraint, and a "RECORD REVIEW"
framing for the events/prior_diagnoses/document_drop cluster — explicitly
telling the model these excerpts and the document digest are the patient's
own prior words, to be referenced and confirmed ("your records show a
hospitalization in early 2024 and an ER visit before that — can we go
through those?"), not re-asked from scratch. This context already existed
(ADR 0015's document excerpts, the doc digest) — this only tells the model
*when*, clinically, to lean on it.

`INTAKE_OPENER_MESSAGE` is rewritten: a greeting, one framing sentence, then
one concrete question — age and sex at birth — instead of the open "what's
been bothering you." That question moves to the end of the arc, in the
"what's recent" stage, asked in conversation once the agent already has a
picture of the patient rather than as the cold open.

### 2. A `geography` topic

A new topic (`intake/sections.py`'s `GeographySection`: `residences`
[place/date_approx/current], `travel` [short strings], `exposures` [short
strings, environmental/occupational, tied to a place or trip]) is added to
`SECTIONS`, positioned after `family_history`. It is wired through every
layer a topic touches, the same way every prior topic was:

- **Fact kind**: one new `IntakeFact.kind` value, `"location"`, covering all
  three lists — disambiguated by `fields["category"]`
  (`"residence"` (default) | `"travel"` | `"exposure"`), the same "coarse
  kind, `fields` carries the nuance" convention `diagnosis`/`attribution`
  already uses, so this did not require a wider fact-kind explosion.
- **Conversion** (`intake/convert.py`): `_geography_data` splits active
  `location` facts by `fields["category"]` into the section's three lists.
- **Case-file writer** (`intake/wizard.py`): `_write_geography` writes its
  own whole file, `case/geography.md` — following `family-history.md`'s/
  `care-team.md`'s pattern (a topic with multiple structured lists gets its
  own file) rather than another `case-summary.md` block, so the summary
  doesn't accumulate an ever-growing address history.
- **Coverage/gates**: `SECTION_KEYS` is derived from `SECTIONS` (never
  hand-maintained), so `geography` participates in the existing coverage
  map and completion gate automatically — no gate-specific code needed.
- **Migration**: `intake/coverage.py`'s `CoverageState.topics` is a plain
  `dict[str, TopicCoverage]`, and `_is_covered` reads it via
  `.get(key, TopicCoverage())`. A coverage-state file written before this
  topic existed — whether new-style (`topics:` present, just missing the
  `geography` key) or old-style (migrated via `_migrate_legacy`, which never
  populated a key the pre-migration file didn't have) — loads without error
  and reports `geography` as uncovered, exactly like any genuinely
  unstarted topic. Tested directly (`test_intake_coverage.py`).

### 3. Post-intake continuity: `follow_up` + `ContinuityInfo`

**The follow-up mechanism is a field, not an inference.** `IntakeFact` gains
`follow_up: bool = False`, settable only by the model via `AddFact`/
`UpdateFact` (never derived from corroboration, clarification status, or
anything else) — the same "model proposes a typed op, code applies it"
discipline every other fact field already follows. The `intake_agent`
system prompt (both `run_intake_turn`'s and `VISIT_CAPTURE_PROMPT_VERSION`
2's silent capture prompt) instructs the model to set it deliberately when
something is genuinely worth checking back on, and to clear it
(`update_fact(follow_up=false)`) once a later visit actually revisits the
topic.

**`ContinuityInfo` (`intake/agent.py`)** assembles the three things a visit
should open knowing, straight from durable state: `follow_ups` (active facts
flagged `follow_up`), `unresolved_facts` (active, `clarification_status=
"needs_probe"`), `recent_facts` (`reported_on` within
`RECENT_FACT_WINDOW_DAYS`), and `open_questions` (`case/questions-open.md`
if non-empty). `last_visit_at` is supplied by the caller rather than read
here — this module never touches the chat transcript (`web` depends on
`intake`, not the reverse; the transcript is `web.casefile_helpers`'
concern).

**The greeting is deterministic, code-composed text — not a model call.**
`render_continuity_note` builds a short (one-to-two-line), conversational
note the same way `red_flag_warning_prefix` already builds the red-flag
warning: fixed composition logic, not delegated to a model, so it is
testable and cannot be silently dropped or reworded by anything the
diagnostic/informational pipeline returns. `web.routes.chat.chat_send`
captures `last_chat_at(repo)` *before* appending the current turn's patient
message (so it reflects the previous visit, not this one), and — only once
intake is complete, and only when that gap exceeds
`VISIT_GAP_THRESHOLD_HOURS` (4h, distinguishing a new visit from a
same-sitting back-and-forth) — prepends the note to the first successful
informational/diagnostic reply, exactly where `_with_red_flag_warning`
already prepends its own prefix. It never fires on an urgent/withheld/error
outcome. It is deliberately short — at most one line naming how long it's
been, plus at most one more line naming the single most relevant open item
(a follow-up first, then an unresolved fact, then a generic nudge toward
open questions) — never a dump of everything open.

**The same three things are surfaced structurally on the Intake record
page** (`web/routes/onboard.py`'s `onboard_review`, `onboard_review.html`'s
new "Where things stand" section): last-spoke date, flagged follow-ups, and
still-open items (unresolved facts, open questions) — for a patient who
wants to check what the agent thinks is outstanding without waiting for the
next visit's greeting to say so.

**Why this lives in `intake/agent.py` + `web/routes/chat.py` rather than the
diagnostic/informational prompts themselves**: the same "code composes and
inserts fixed text; the model cannot suppress or soften it" discipline
CLAUDE.md rule 3 already requires of the red-flag warning applies here —
continuity is patient-facing framing, not part of the actual diagnostic
reasoning, so it does not belong inside the Ledger-Maintainer/Challenger/
Composer prompts or their DAG contracts. Keeping it as a deterministic
wrapper around whatever those stages return is the more conservative
placement, not just an implementation convenience.

## Consequences

- **`INTAKE_AGENT_PROMPT_VERSION` bumps to `"6"`, `VISIT_CAPTURE_PROMPT_
  VERSION` bumps to `"2"`.** Both reuse the existing `intake_agent` role —
  no `models.yaml` binding change, so CLAUDE.md rule 4's eval-comparison
  requirement does not apply, though the red-team/safety suite still gates
  every prompt edit per rule 2.
- **11 topics, not 10.** Every place that previously assumed "10 sections"
  (tests included) is updated; `SECTION_KEYS`/`_SUPPORTED_SECTIONS` derive
  from `SECTIONS` rather than being hand-maintained, so nothing else needed
  a parallel update.
- **The arc is a hint the model can and sometimes will deviate from** — by
  design. A patient who leads with her symptoms on turn one still gets them
  captured immediately; the arc only affects what the model steers toward
  next, and only once a thread has wound down. This is intentionally weaker
  than a gate — the product complaint ADR 0012 already fixed was exactly a
  state machine that refused/deferred out-of-order input, and this ADR does
  not want to reintroduce that failure mode by another name.
- **The continuity note is intentionally terse and can be wrong about
  what's "most relevant."** It always picks the first matching item in
  store order (first follow-up, else first unresolved fact) rather than
  ranking by clinical importance — a deliberate simplicity trade for now,
  revisitable if it proves to pick badly in practice.

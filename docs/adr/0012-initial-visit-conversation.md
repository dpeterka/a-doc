# 0012. Onboarding becomes one continuous "initial visit" conversation

Status: Accepted. Supersedes the sectioned-stepping aspect of 0011 — 0011's
fact model (`intake/facts.py`), deterministic completion gates, and writer
reuse (`intake/convert.py`, `intake/wizard.py`'s `write_section`) all stand
unchanged; only the conversation's *shape* and the *state machine* driving
it changed.

## Context

0011 replaced one-shot section extraction with a conversational engine, but
kept the wizard's ten-section structure as the thing the conversation
walked through: a visible section list with a progress bar
(`/onboard`'s `_onboard_panel.html`), a "current section" cursor
(`intake-state.yaml`'s `cursor`), and a model-proposed `section_complete`
that advanced the cursor to the next section in a fixed order.

Direct product-owner feedback on the merged result (PR #95):

> Onboarding retained the 10-section approach in the UI. This should not
> occur. Simply "initial visit" or start chat where chat is aware it needs
> to onboard. The section approach seems ill done. As a patient who sees
> doctors regularly — revisit the design approach for onboarding such that
> it can occur conversationally.

The complaint is specific: not that the underlying fact model or gates were
wrong (they weren't touched), but that the *patient-facing shape* was still
a form wearing a chat costume — a visible stepper, a percentage, a "current
section" the conversation had to formally close before moving on. A patient
who has done this many times with real clinicians recognizes that an
intake visit doesn't work that way: a good clinician follows the patient's
own narrative and only steers back to what's missing once a thread winds
down, and the patient never sees (or needs to see) the clinician's own
mental checklist.

## Decision

**Sections become invisible topic coverage.** The ten `SECTIONS` entries
(`intake/sections.py`) keep their exact keys, Pydantic schemas, and writers
— they still drive case-file output (`facts_to_section_data`,
`write_section`) and an `IntakeFact.section` still files a fact under one
of them. What changes is that nothing patient-facing ever names, numbers,
lists, or steps through them again. `intake/coverage.py` replaces
`intake-state.yaml`'s cursor/per-section status machine with a flat
per-topic coverage map (`CoverageState.topics: dict[str, TopicCoverage]`,
each just `covered: bool` + `covered_at`) and a separate, monotonic
`intake_complete: bool`. There is no "current topic." `load_coverage_state`
transparently migrates an old-style (sections/cursor) file on read
(`complete` -> `covered`), so a repo onboarded partway under 0011's shape
before this change keeps its progress.

**Code, not the model, still decides coverage.** The `intake_agent` model
may propose `topics_covered: list[str]` — topic keys it judges genuinely
explored, including "the patient stated there's nothing to report" (a
topic with zero active facts trivially has zero blockers, so this falls
out of the existing gate for free). `intake.agent.run_intake_turn` accepts
a proposed topic only when `intake.facts.section_completion_blockers`
(unchanged) finds nothing blocking it; a vetoed topic simply stays
uncovered, silently — routine turns never surface gate mechanics to the
patient. The model may also propose `intake_complete: true`; code accepts
it only when EVERY topic is covered and no blocker remains anywhere,
re-checking blockers fresh rather than trusting the coverage map alone (a
correction can reopen a previously-covered topic with a new vague fact).
A refused wrap-up appends ONE deterministic, conversationally-phrased
steering line mapping uncovered topic keys to human phrases and reusing
each blocker's already-legible statement — never as a checklist, never
naming a topic key.

**One chat surface.** `/onboard` and `/onboard/send` become redirects to
`/chat`. `web.routes.chat`'s `chat_send` checks `intake.agent.
intake_is_complete(repo)` on every turn: while false, the turn goes through
`run_intake_turn`; once true, forever after, turns go through the existing
`route_turn`/diagnostic pipeline, unchanged. Both paths append into the
SAME transcript (`web.casefile_helpers.append_chat_entry`/`read_recent_chat`)
so the patient experiences one continuous conversation with no visible
seam. `intake.agent` also keeps writing its own `case/intake-transcript.jsonl`
— unrelated to the patient-facing transcript, it is what
`run_intake_turn`'s own context builder reads back as "recent conversation"
and remains the audit trail existing tests rely on.

**A deterministic opener, not a model call.** The very first assistant
message of a patient's initial visit is a constant
(`intake.agent.INTAKE_OPENER_MESSAGE`): a greeting, one sentence explaining
that this conversation builds the case file, then "What's been going on?
Start wherever you like." `GET /chat` renders it when the shared transcript
is empty and intake is incomplete (not yet persisted); it is written into
the real transcript on the first patient turn so later renders — and the
intake engine's own context — see coherent history.

**`/onboard/review` survives, retitled "Intake record."** A read-only page
grouping every active fact by internal topic, with the same
attribution/precision badges and "Correct this" affordance (now pointing
at `/chat`) — a record page is not a stepper, so grouping by topic there is
fine; it is the conversation itself that must never show topics.

**The `intake_agent` prompt is rewritten (version bumped to `"2"`)** around
the initial-visit frame: follow the patient's narrative, never impose an
order, never mention sections/topics/checklists/percentages to the
patient; capture facts for whatever topic a message touches, in whatever
order it arrives; steer gently with a natural bridge once a thread winds
down; at most two questions per turn. Every rule from 0011 is retained in
spirit (probe vagueness once; timing asked once, `unknown_after_probe`
accepted; doctor-diagnosed vs. patient-assumption attribution, captured
non-judgmentally; document/encounter cross-referencing from the digest;
corrections at any time; never diagnose or treat; capture only what's
stated). The turn schema drops `section_complete`/`wants_section` in favor
of `topics_covered`/`intake_complete`; the context builder drops "current
section + checklist" for "coverage map (internal) + active facts + doc
digest + recent transcript."

**The CLI (`adoc onboard`) follows the same shape**: prints the opener (or
an amend-mode greeting if already complete), then free conversation with
no section display of any kind; exits once `intake_is_complete` turns true
after a turn, or on real EOF (Ctrl-D) — state is saved on every turn
either way. `--legacy-wizard` (`intake.wizard.IntakeWizard` +
`run_onboarding_session`) is untouched: same state file shape
(`sections`/`cursor`), same tests, still a working (if intentionally
old-shaped) fallback.

## Consequences

- **No mechanical rename of `SECTIONS`/`SectionSpec`/`section_completion_blockers`/
  `write_section`.** These identifiers keep their names (and the `section`
  field on `IntakeFact`/fact ops) because 0011's writers and gates are
  reused verbatim, per this ADR's own instruction to keep "the same keys,
  schemas, writers." Only the conversation-facing vocabulary ("topic," not
  "section") and the state machine changed. A future pass could rename
  these purely for internal clarity, but that is cosmetic and out of scope
  here — behavior, not naming, was the product complaint.
- **Two onboarding code paths still coexist** (conversational default vs.
  `--legacy-wizard`), for the same reason 0011 kept them: the wizard's
  writers and resumable state machine stay the proven fallback and the
  shared output layer, now reused by a materially different conversation
  shape on top.
- **A repo that mixed `--legacy-wizard` and the default engine** in the
  same intake would see one engine's state-file shape get read (and
  reasonably migrated) by the other, but never round-trips back — this is
  an accepted edge case, not a supported workflow; the two paths are
  designed as alternatives, not to be interleaved.
- **One more prompt version to track** (`INTAKE_AGENT_PROMPT_VERSION = "2"`)
  — no `models.yaml` binding change, so no `adoc eval` comparison report is
  required (CLAUDE.md rule 4 only gates binding changes, not prompt-text
  edits within the same role), though the red-team/safety suite still
  gates every prompt edit per CLAUDE.md rule 2.

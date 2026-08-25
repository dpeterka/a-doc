# ADR 0014 — Red-flag screening warns, it does not block

- Status: Accepted (2026-08-25)
- Supersedes the blocking half of the red-flag contract described in PLAN.md
  "Safety"; the screen itself (`reason/safety.py::red_flag_screen`) is
  unchanged.

## Context

`red_flag_screen` is a deterministic keyword/regex screen for emergency
presentations. It is deliberately conservative and does **no** negation or
tense detection — its own docstring notes that "no chest pain" and "chest
pain went away years ago" both match. That was justified on the grounds that
"a false positive costs a little friction; a false negative could delay real
emergency care."

That trade was correct for an ordinary chat turn and wrong for intake.
Recounting medical history is the *entire task* of an initial visit, so a
long history reliably contains phrases the screen watches for. In live use
the real patient opened her first visit by pasting a written medical history
and was met with an emergency message instead of a conversation. The turn was
discarded: no reply, no facts captured, nothing persisted. Retrying with any
faithful account of her history would have produced the same result.

So the false-positive cost was not "a little friction" — it was a total block
on the product's first-run experience, for a patient whose history is exactly
what the system exists to record. And a warning that fires on nearly every
message trains its reader to ignore it, which erodes the very signal the
screen exists to send.

The operator of this system is a single, informed adult who knows what is and
is not an emergency, and who owns the tool. That is unusual context, and it
matters: a gate justified for anonymous public users is not automatically
justified here.

## Decision

Keep the screen. Change the response from **block** to **warn**.

1. `red_flag_screen` is untouched — same rules, same terms, same matching,
   same conservatism. Do not add negation or tense heuristics to it.
2. It still runs **first**, before any model call, in the entry points that
   own the patient conversation (`web/routes/chat.py`, `intake/agent.py`).
3. On a match, a fixed warning naming the matched category is **prepended by
   code** to the reply, after the model has produced it. The model never sees
   it, cannot suppress it, cannot soften it, and cannot reword it.
4. The turn proceeds normally. Her history gets recorded.
5. The duplicate screen inside `reason/stages.py`'s entry points was removed,
   so the block cannot silently reappear one layer down. `safety.guarded_turn`
   remains for any caller that wants fail-closed behavior, unused by us.
6. The opener carries no emergency disclaimer. For this operator it is noise.

### The pinned contract, replaced rather than deleted

The red-team suite previously pinned *"a red-flag turn makes zero API
calls"* (`red_flag_zero_api_calls`). That property is meaningless once the
turn is allowed to proceed. It is replaced — not dropped — by
`red_flag_always_warns`: **a red-flag match is always surfaced to the
patient, in fixed text chosen by code, that nothing the model returns can
remove.** The test proves it by composing a reply that mentions no emergency
at all and asserting the warning is still attached.

This is deliberately still a pinned safety contract under CLAUDE.md rule 2.
The rule forbids weakening these tests *to make a change pass*; it does not
forbid the owner from deciding, explicitly and on the record, that a
different property is the one worth guaranteeing.

## Consequences

- A patient can read a genuine emergency warning and keep typing. Accepted
  deliberately: this mirrors triage practice, where a clinician asks "is this
  happening right now?" and proceeds when the answer is no.
- Every match is logged with its category, so false-positive frequency is
  measurable rather than assumed.
- The warning must stay short and specific. If it grows into a wall of
  disclaimer text it will be skipped, which returns us to the failure mode
  this ADR exists to fix.
- If false positives become frequent enough to be noise even as warnings, the
  next step is *not* loosening the screen's terms — it is a targeted, logged
  patient attestation ("this is history, not now") bound to specific text.
  That was considered here and judged unnecessary once the block was removed.

## Alternatives rejected

- **Negation/tense detection in the screen.** Turns a deterministic,
  auditable gate into a fragile NLP problem in the one place that must never
  be clever.
- **Exempt intake only.** The same false positive strands an ordinary visit
  where the patient recounts history, which is most visits.
- **Relax the matched terms.** Trades a false-positive problem for a
  false-negative one, in the direction that actually endangers someone.

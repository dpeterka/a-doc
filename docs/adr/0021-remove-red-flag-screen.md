# ADR 0021 — Remove the red-flag emergency screen entirely

- Status: Accepted (2026-08-25)
- **Supersedes ADR 0014 in full.** ADR 0014 ("red-flag screening warns, it
  does not block") is no longer the current design; this ADR is the
  record of what replaced it and why. `docs/adr/0014-red-flag-warn-not-
  block.md` is left in place as history, not deleted, but its Decision no
  longer describes the running system.

## Context

`reason/safety.py::red_flag_screen` was a deterministic keyword/regex
screen for emergency presentations (chest pain, stroke signs, anaphylaxis,
suicidality, severe bleeding, sepsis/meningitis, anticoagulant
emergencies). ADR 0014 changed its response from blocking the turn to
prepending a fixed warning and letting the turn proceed, on the reasoning
that blocking made intake unusable — recounting medical history is the
entire task of an initial visit, and the screen's own conservative,
no-negation-detection design meant a long history reliably tripped it.

The warn-not-block design was itself still wrong, for two compounding
reasons surfaced in live use with the actual patient this tool is for:

1. **It matched constantly, on narrative, not on anything acute.** Intake
   in particular is historical narrative by construction — years of ER
   visits, infections, family deaths, past injuries. Every one of those is
   exactly the kind of phrase the screen watches for. A warning attached to
   nearly every message stops being read at all (ADR 0014 itself
   anticipated this as a risk).
2. **It matched things that were never medical at all.** The screen has no
   understanding of context, only keywords. In one real intake turn, the
   patient wrote: *"Our home has a septic system and a well."* — a plumbing
   detail, offered in passing. The screen matched "septic" and fired the
   sepsis/meningitis category, and the fixed warning ("this sounds like it
   could be a medical emergency...") was prepended to an ordinary reply
   about her water supply. This is not a tunable edge case; it is the
   screen doing exactly what it was built to do — bare keyword matching —
   applied to a domain where a huge amount of ordinary language collides
   with medical-sounding words.

Put plainly: in the entire time this screen ran against real use, it
produced only false positives and never once caught a real emergency,
because the premise underneath it doesn't hold for this product. a-doc is
a single-patient, longitudinal case-file tool operated by one informed
adult. Someone having a genuine medical emergency right now is not opening
a case-management app and typing about it — they are calling 911 or going
to an ER. The screen's entire value proposition assumed a class of user
and a class of interaction (an anonymous, possibly-in-crisis user typing
into a general-purpose assistant) that does not describe how this specific
tool is actually used.

A narrower fix was tried and rejected during this same investigation:
requiring the match to be paired with the patient directly asking a
concern-question ("should I be worried", "is this serious"). That still
leaves a keyword screen making judgment calls about what counts as a
"concern question" over more collision-prone natural language, still
needs the same term-level whack-a-mole the plumbing incident exposed
(there is no reason to believe "septic system" is the only such
collision — it is simply the one that was caught), and still keeps a
mechanism whose central premise the owner has explicitly rejected. Once
the premise is gone, a narrower version of the same mechanism doesn't
rescue it.

## Decision

**Remove the red-flag screen entirely.** Not disabled, not narrowed —
deleted:

- `reason/safety.py`: `red_flag_screen`, `RedFlagResult`, `RedFlagCategory`,
  the category rule tables/terms/messages, and the already-deprecated
  `guarded_turn` wiring are gone. `treatment_gate` (dosing/prescriptive
  output gate, CLAUDE.md rule 5) is completely untouched by this ADR — see
  ADR 0020 for its own, separate change.
- `intake/agent.py`: no screening call, no warning prefix. Intake replies
  are whatever the model and the deterministic coverage/wrap-up gates
  produce, same as any other turn.
- `web/routes/chat.py`: no screening call, no warning wrapper, on either
  the informational or the diagnostic route.
- `reason/stages.py`, `reason/tools.py`: no residual `RedFlagResult` arms
  on any return type; `run_diagnostic_turn` returns `PatientReply`,
  `run_informational_turn` returns `LlmResult`, plainly.
- `evals/suites/redteam.py` and `tests/fixtures/redteam.yaml`: the
  `red_flag_categories` scoring block and the `red_flag_always_warns`
  scenario are gone. The treatment-gate, patient-theory-anchoring, and
  missing-challenger-fails-closed cases are untouched — they pin unrelated
  properties.
- CLAUDE.md and PLAN.md's safety descriptions no longer mention a red-flag
  screen.

**What is deliberately given up:** there is now no automated emergency
detection anywhere in the system. If the patient describes a genuine
emergency mid-conversation, nothing in this app will flag it, warn on it,
or change its behavior because of it. This is the accepted position for a
single-user tool operated by an informed adult, made explicitly by the
product owner, and it is recorded here — rather than silently
disappearing — precisely so it can be revisited if the operating
assumption ever changes (e.g. if this tool is ever used by anyone other
than its one operator).

### The pinned contract, retired rather than replaced

ADR 0014 replaced `red_flag_zero_api_calls` with `red_flag_always_warns` as
the property CI pins. That property is retired outright, not replaced by a
third variant: there is no longer a red-flag mechanism for any test to pin
a contract about. `tests/test_safety.py`, `tests/test_stages.py`,
`tests/test_web_chat.py`, and `tests/test_intake_agent.py` had their
red-flag-specific tests removed rather than left asserting nothing; the
tests that remain in each file (treatment-gate behavior, ordinary chat/
intake flow) are unaffected and still pass.

This is still a deliberate, on-the-record change to a pinned safety
contract under CLAUDE.md rule 2 — the rule forbids weakening these tests
*to make a change pass*; it does not forbid the owner from deciding,
explicitly and with a documented incident behind it, that a mechanism
should not exist at all.

## Consequences

- A patient can write anything — including a description of a genuine
  emergency — and get an ordinary reply with no warning banner attached.
  This mirrors how the tool is actually used and removes a mechanism that,
  in practice, only ever produced noise and one embarrassing false
  positive.
- Intake narrative in particular is no longer at risk of an incongruous
  emergency banner attached to a story about the patient's plumbing, her
  father's heart attack a decade ago, or any other ordinary historical
  detail.
- `docs/adr/0014-red-flag-warn-not-block.md` remains in the repo as
  history — it explains a real design step that was tried, and superseding
  it in place (rather than deleting it) keeps that reasoning legible
  instead of erasing it.
- If this tool ever serves more than one operator, or an operator who
  might genuinely be in crisis while using it, this decision should be the
  first thing revisited — it depends entirely on the "an informed adult
  who is not typing through an emergency" premise holding.

## Alternatives rejected

- **Narrow surfacing to only a direct "is this serious?" question**
  (considered and drafted during this investigation, then rejected). Still
  a keyword-driven mechanism vulnerable to the same class of collision the
  plumbing incident demonstrated, just on a smaller surface; still
  requires maintaining term lists and a second "is this a concern
  question" detector; does not address the owner's actual objection, which
  is to the premise that this app should be doing emergency triage at all.
- **Keep the screen, fix only the "septic" collision.** Whack-a-mole: fixes
  the one collision that happened to be caught in front of a real user and
  does nothing about the next one, while leaving in place a mechanism
  whose value the owner has rejected on principle for this product.
- **A patient attestation ("this is history, not now") bound to specific
  matched text**, floated in ADR 0014 as the next step if warnings became
  noise. Adds UI and interaction surface to a mechanism being removed
  outright; solving a problem for a feature that no longer exists.

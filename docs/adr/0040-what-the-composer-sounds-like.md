# ADR 0040 — What the composer sounds like

Status: accepted (2026-09-02)

Third item in the adversarial-review adoption track (PLAN.md). Closes
PAT-08. Builds on [ADR 0039](0039-how-a-review-reads.md).

## Context

PAT-08 says the composer prompt and the treatment gate "produce sterile,
defensive responses that repeat legal disclaimers on every turn, alienating
the patient", and proposes three fixes: a warm persona ("Your Case
Co-Pilot"), moving disclaimers to a footer, and framing output as questions
to ask a specialist.

Measured against the code, three of those four claims are already false:

- **The composer prompt is not sterile.** It already mandates "plain,
  compassionate language a non-technical reader can follow — explain jargon
  inline", and requires phrasing like "this may be worth asking your doctor
  about". Nothing in it is defensive.
- **A persistent footer disclaimer already exists**, on every page:
  `base.html` renders `<footer class="disclaimer">` from
  `templating.DISCLAIMER_TEXT`. PAT-08 asks for work that shipped.
- **Output is already framed as questions to bring.** The report has a
  `## What to ask your doctor` section; chat replies carry "Tests to ask
  your doctor about".
- **Nothing repeats per turn.** The treatment gate's `_REWRITE_INSTRUCTION`
  is an instruction to the *model*, never shown to anyone. When the gate
  does block patient-facing text, chat returns
  `_CONTRACT_VIOLATION_MESSAGE`, which already opens by saying the case file
  was still updated and "Nothing is wrong with your account."

One claim is true, and it is not about the composer at all. It is in the
deterministic renderer:

> **The same 160-character classification disclaimer renders once per
> criteria set — 7 times per report, 1,120 characters, 17.7% of the whole
> criteria section.**

Measured by rendering all 7 registered scorers over synthetic rows against
the ADR 0039 renderer: `chars=6326 full_disclaimer_reps=7`.

Measuring it also turned up a second thing PAT-08 did not mention.
`CriteriaResult.citation` — the published criteria a doctor can look up,
which is what its docstring says it is for — **was rendered nowhere.** Seven
sets, 461 characters of citation, dead on the model since the field was
added.

## Decision

### 1. State the classification disclaimer once, and show the citation

The criteria section opens with the disclaimer as a full paragraph, before
the first set. Each set then carries a one-line marker naming its published
citation instead of repeating the whole sentence:

    _Classification criteria (Aringer M, et al. Arthritis Rheumatol.
    2019;71(9):1400-1412.), not diagnostic._

This is more prominent, not less: 160 characters in italics under the
seventh `###` heading is the position a reader has already learned to skip.
Said once, at the top, in prose, it is the first thing read in the section.

`CriteriaResult.disclaimer` **stays on the model**. The change is to the
renderer alone. The report renderer is the only consumer today — the web
view renders the report's markdown rather than the results — so the field is
carried for the next one, and the property "every criteria result carries
its disclaimer" stays a schema property with its own test rather than
something the renderer has to remember.

### 2. The persona is rejected

PAT-08 asks for a "warm, collaborative clinical advocacy persona (Your Case
Co-Pilot)". **Rejected**, and recorded here so it is not proposed again.

PLAN.md names over-trust and framing drift as risk 3, pinned by the red-team
suite as a required CI check. This system's honest job is leads, structure
and preparation — its measured exact-hit rate on true diagnostic-odyssey
cases is low, and PLAN.md says the UI should say so. A "co-pilot" is a
teammate flying the same aircraft: the name claims shared authority over a
decision the system must never appear to share. Warmth is already in the
prompt; borrowed authority is not warmth.

The failure mode is concrete. A patient who reads the output as a co-pilot's
opinion has a reason to weigh it against her rheumatologist's. That is the
one outcome this system is built not to cause.

## Consequences

- **Measured: `chars=6326 reps=7 citations=0` → `chars=6367 reps=0
  citations=7`.** Forty-one characters longer, not shorter. Six duplicate
  sentences go and seven previously invisible citations arrive; the two
  nearly cancel. Worth stating plainly, because the first draft of this ADR
  claimed "net shorter" against a figure measured on the pre-0039 renderer.
  The gain here is that the statement is made once where it will be read and
  that a doctor can now look the criteria up — not a size reduction.
- A test pins that the disclaimer appears exactly once in a rendered
  criteria section holding more than one set — the property is "said once",
  and "said seven times" must fail it as surely as "said never" does.
- `prompts/composer.md` is **not edited** by this ADR. Its version stays at
  3 (ADR 0039). An ADR that changes no prompt still belongs in the record:
  the finding was raised, measured, and mostly answered by existing code.

## Alternatives considered

**Drop the per-set marker entirely.** Rejected. A reader who jumps to one
`###` heading from the table of contents would then see a point total with
nothing qualifying it.

**Adopt the persona but keep the disclaimers.** Rejected — the disclaimers
are not what makes a persona risky. The claim of shared authority is.

**Leave the repetition alone.** Rejected on the measurement: 17.7% of a
section spent saying one thing seven times is not a rounding error, and it
crowded out the citation that should have been there instead.

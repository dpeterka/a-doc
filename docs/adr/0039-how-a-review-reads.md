# ADR 0039 — How a review reads

Status: accepted (2026-09-02)

Builds on [ADR 0037](0037-an-uncited-cant-miss-lead-does-not-lead.md) and
[ADR 0038](0038-how-a-hypothesis-ends.md).

## Context

The reader of every artifact this system produces is a non-technical patient
with an undiagnosed condition, brain fog, and fatigue. An adversarial review
(PAT-01, PAT-02, PAT-04) said the output does not read that way. Measured
against the last real review:

- **52,969 characters.** No summary anywhere; the first section is a list of
  what the blind panel changed.
- **`Can't-Miss` is the literal on-screen label** for a tier holding 10
  entries, among them a hereditary cancer syndrome and a hereditary
  angio-oedema. `_TIER_LABELS` renders it verbatim as a chip on every card.
- **Criteria render as arithmetic**: `**6 of 10 points**`, beside `LR 12.4`
  and `similarity 3.81`. Three numbers on three incommensurable scales, none
  of them a probability, all of them reading like one.

One claim in the review is wrong and worth recording so it is not re-fixed
later: PAT-01 says the report "opens with raw operational metrics and an
immediate deep dive into analyte trends". It opens with
`## What changed this week`; the metrics appendix is the last section. The
problem is size and the absence of a summary, not order.

## Decision

### 1. A three-part summary at the top, derived — not generated

The report gains a `## The short version` block before anything else:

- **What changed** — accepted divergences, retirements, trend alerts
- **What is being looked at now** — the leading group, by name
- **What to raise first** — the top one or two test-chooser items

PAT-01 proposes a `patient_summary` LLM node. **Rejected.** All three bullets
are already computed by earlier nodes; a model call here would add a fourth
frontier call to a 17-minute review, and could disagree with the very
sections it sits above. CLAUDE.md's rule applies directly — deterministic
logic is plain code, never delegated.

The summary therefore cannot say anything the report does not already say.
That is the property worth having: it is a view, not a second opinion.

### 2. The can't-miss tier is labelled for what it is

`Can't-Miss` → **"Safety checklist"**, in both the web chips and the report.

The tier is a list of conditions a clinician systematically excludes. Named
as it was, it read to the person whose case file it is as a list of
catastrophes she might have. The rename is a label change only: the schema
value `cant-miss` is unchanged, and so is every ledger on disk.

The rename has to reach the composer prompt too, or the narrative says
`Can't-Miss` on the same page the chips say "Safety checklist".
`prompts/composer.md` goes to version 3, and now also instructs the tier be
rendered with the sentence that being on the list is not a claim the patient
has the condition. Its own words for the tier were the last place the old
label survived in patient-facing output.

Each entry carries a status derived from ADR 0038's machinery:

| Condition | Chip |
|---|---|
| `status == "ruled-out"` | Ruled out |
| has a `rule_out_check`, unmet | One test would settle this |
| has `rule_out` prose only | Needs a specific finding |
| neither | Being tracked |

"One test would settle this" is only sayable because ADR 0038 made rule-outs
machine-checkable. Without it every chip would read the same.

### 3. Criteria lead with meaning, not arithmetic

Each criteria set opens with a sentence naming what it found, and the point
table moves inside a `<details>` block titled for a clinician.

The floor rule is stated in words rather than left implied by two numbers:
`points` is what the record can prove, `points_possible` is what a clinician
attributing the `possible` items would add. A reader seeing `6 of 10` has no
way to know the 6 is a floor.

`LR` and `similarity` keep their own labels and scales (ADR 0036 forbids
combining them), and gain one clause each saying what kind of number they
are: a likelihood ratio is "not a percent chance, and not comparable with
the similarity score", and a similarity is "higher means more overlap, not
more likely". A reader handed two bare numbers will combine them whatever
ADR 0036 says about the code.

## Consequences

- The summary duplicates content by design. If a section changes and the
  summary does not, the summary is wrong — so both derive from the same
  artifacts, and a test pins that a retirement appearing in the report also
  appears in the summary.
- `Can't-Miss` disappears from the interface but stays in the data. Anything
  reading the raw ledger still sees `cant-miss`.
- Nothing here is hidden. The `<details>` block is collapsed, not removed,
  and no criterion, lead, or number is dropped from the report.
- The hallucination-eval fixtures still contain `Can't-Miss` as *simulated
  model output*. They are inputs to the output gate, not label definitions,
  and rewriting safety fixtures to match a rename is the kind of edit
  CLAUDE.md rule 2 forbids. They stay.
- `prompts/composer.md` at version 3 means artifacts stamped
  `prompt_template_version: 2` were written under the old label. That is the
  point of the stamp; no back-fill.

## Alternatives considered

**Generate the summary with a model (PAT-01 as written).** Rejected above:
a fourth frontier call that can contradict the report beneath it.

**Shorten the report itself.** Rejected. Every section is there because a
clinician or the patient asked for it, and the reader who wants detail is the
same reader on a different day. A summary serves both; deletion serves one.

**Rename the tier in the schema too.** Rejected. It would rewrite every
hypothesis on the ledger and every ADR that discusses the tier, to change
what a person sees — which a label already does.

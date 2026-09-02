# ADR 0041 — The appointment agenda

Status: accepted (2026-09-02)

Fourth item in the adversarial-review adoption track (PLAN.md). Closes
PAT-07. Builds on [ADR 0037](0037-an-uncited-cant-miss-lead-does-not-lead.md),
[ADR 0038](0038-how-a-hypothesis-ends.md) and
[ADR 0039](0039-how-a-review-reads.md).

## Context

The weekly review is the right artifact for the patient and the wrong one
for the consultation. PAT-07 puts it plainly: a 15-minute appointment has no
room for a 52,969-character report, and a specialist handed one on a phone
screen reads a patient who has been on the internet — the exact outcome this
system exists to avoid.

PLAN.md has listed a "1-page appointment prep export" under phase 4 since
the plan was written. ADR 0039 made the review readable; it did not make it
*handable*.

Unlike PAT-01 and PAT-08, PAT-07's premise checks out. There is no export
module, no route, and no way to get anything but the full report out of the
system.

## Decision

A new deterministic module `casefile/export.py` builds an `Agenda`, and
`web/routes/export.py` serves it at `GET /export/agenda` (with
`/export/agenda.md` for anyone who would rather paste it into a portal
message). No model call: every field is copied from the ledger, the labs
database, or `regimen.yaml`.

Three properties are enforced in code, each with a negative-controlled test.

### 1. One page is a bound, not an aspiration

Every section has a hard cap and the whole render has a line budget. The
budget is derived from the print stylesheet rather than fitted to whatever
the renderer produced:

```
US Letter 11in − 2 × 0.5in margins  = 720pt printable height
9.5pt font × 1.25 line-height       = 11.875pt per line
720 / 11.875                        = 60 lines
less 17 worst-case table rows × 2pt = −2.9 lines
capacity                            ≈ 57 lines
```

`export_agenda.html` is written to those exact numbers, so the derivation is
checkable against the CSS instead of being a claim about a page nobody has
printed. Worst case measures **54** lines, leaving 3 of slack.

This needed to be a test, not a comment. Two attempts overflowed before the
caps were right: the first (8 labs / 3 leads / 10 regimen / 3 asks) rendered
**57 lines against a 46-line budget**, and counting newlines instead of
*wrapped* lines hid another 10 — a 200-character evidence claim is one
newline and three printed lines. `rendered_lines` counts what the page will
show, and `AGENDA_MAX_CLAIM_CHARS = 96` keeps every content row to exactly
one line so the arithmetic holds regardless of how verbose a model was on
the day it wrote the ledger.

### 2. Everything dropped is counted

Truncating to fit is honest only if the reader is told what did not fit.
`Agenda.omitted` carries a line per capped section, per empty section, and —
the case that matters most — per *missing source*:

> No medication or supplement list is on file, so none is shown — this is not
> a statement that nothing is being taken.

That is this repository's recurring failure mode (see
`docs/deployment-dependencies.md`) arriving on paper. A page with no
medication table and no note does not read as "unknown" to a clinician, it
reads as "takes nothing" — and whether a thyroid or antibody result is real
can depend on the answer, which is the entire reason `regimen.py` exists.
`case/regimen.yaml` gets a row in the dependencies table for it.

### 3. A medication list is a record, not advice

The medication table is the single most useful thing on the page, and
`safety.treatment_gate` blocks **every** phrasing of one. Measured:

| Text | Full gate |
|---|---|
| `Hydroxychloroquine 200 mg twice daily` | blocked — dosage pattern |
| `Biotin 10000 mcg daily` | blocked — dosage pattern |
| `Currently taking: hydroxychloroquine` | blocked — reads as an imperative |

The answer is not a new exemption. `treatment_gate(recording_only=True)`
already exists for exactly this: the scribe posture ADR 0020 built for
intake, which drops the bare-dosage rule and keeps the imperative rule that
CLAUDE.md rule 5 actually exists to enforce. The regimen block is gated that
way; every narrative line on the page takes the full gate. `safety.py` is
**not modified by this ADR.**

One measured gap needed a second guard. `recording_only` **passes**
`"increase to 400 mg"`, because the imperative rule requires a drug-like
token near the verb and a bare quantity has none. So a dose cell is *also*
shape-checked against a quantity grammar — a number and a unit, nothing
else. No instruction fits it. A dose that fails renders as
`as reported — see case file`, and the row still appears: knowing she takes
the drug matters more than the amount, and a silently dropped row hides a
drug from a doctor.

The gate runs at **render** time, not only at build time — the same reason
`routes.ledger._gate_hypothesis_text` re-gates evidence claims on every
render. `apply_diff` never runs the gate, so model-written text can be at
rest in the ledger from before any future redaction fix. A page that fails
is refused outright: it is about to be printed and handed to a clinician,
and there is no version of this artifact where partial beats absent.

### 4. No patient identifier is printed

PAT-07 asks for a header carrying "patient demographics". **Declined.**

`case/identifiers.yaml` exists to define what gets *scrubbed* (ADR 0017).
Reading it to render PII would invert the one file in the system whose whole
purpose is removal. And the artifact is the most PHI-dense thing this system
could produce — a full differential with a name and date of birth on it,
destined for a bag, a car, or a waiting-room table.

The page carries a blank line to fill in by hand instead. She is standing in
front of the doctor holding it; her name is the one fact in the room that is
not in doubt.

### 5. What reaches the page

- **Leads:** active, with non-empty `evidence_for`, ranked by probability
  then tier. An uncited can't-miss placeholder never appears — ADR 0037 kept
  it off the patient's page, and it matters more here: a hereditary syndrome
  listed with nothing behind it is what makes a clinician stop reading. A
  retired lead (ADR 0038) never appears either; handing a doctor a lead the
  patient already excluded wastes the appointment.
- **What would settle it:** ADR 0038's `rule_out_check` in words, or its
  prose fallback.
- **The asks:** the review's test-chooser items when there has been a
  review; otherwise derived from what would settle each lead, so a patient
  with an appointment tomorrow and no review this week still gets a usable
  page.
- **Abnormal labs:** `labs.queries.abnormal_summary`, with the `comparator`.
  A `<` on 0.1 means the assay could not measure below 0.1; printing a bare
  `0.1` turns a detection limit into a measurement, which is a different
  clinical fact on a page a doctor will act on.

## Consequences

- A new route surface. It sits behind the same session auth as everything
  else, and a test pins that both paths redirect to `/login` unauthenticated.
- The one-page bound is a **line-count** bound verified by test plus an
  arithmetic derivation from the stylesheet. **Measured against the real case
  file on release day: 54 lines against the 57 budget, 6 of 32 abnormal
  results shown, 3 leads, 7 regimen rows, 5 omitted notes, 0 gate failures.**
  What is *not* verified is a physically printed page. That is the honest
  residual: the first time someone prints this, the numbers should be checked
  against paper.
- **`asks` is empty on the real record.** No test-chooser items are supplied
  on this path and no hypothesis carries a `rule_out_check` yet — ADR 0038
  shipped the field and PLAN.md records that it is empty on all 46 leads until
  a review or a human fills one in. The page says so rather than showing a
  blank section, and the section becomes useful the first time a review
  writes a rule-out.
- `AGENDA_MAX_CLAIM_CHARS = 96` elides long evidence claims. A doctor
  scanning a page needs a scannable line and the full claim is in the case
  file, but it does mean the agenda is not a substitute for the report.
- Changing `export_agenda.html`'s font size or line height silently breaks
  the derivation. The stylesheet carries a comment saying so; the budget
  constant carries the arithmetic.

## Alternatives considered

**Render a PDF server-side.** Rejected. It means a new binary dependency
(wkhtmltopdf, WeasyPrint's native stack) and therefore a row in
`deployment-dependencies.md` for something that fails silently, to replace
a browser feature the patient already has on the print dialog she is
already opening.

**Widen `treatment_gate` with a "record" category.** Rejected — and it was
unnecessary: `recording_only` is that category, already built, already
justified, already tested. Editing `safety.py` is the highest-risk change
available in this repository and this ADR needed none of it.

**Let the composer write the agenda.** Rejected for ADR 0039's reason and
one more: a page a patient hands to a specialist as fact must not contain a
sentence no deterministic check has seen. Every line here is copied.

**Print everything and let the doctor skim.** Rejected. That is the current
behaviour and it is what PAT-07 correctly describes as leading to immediate
dismissal.

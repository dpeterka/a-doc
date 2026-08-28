# ADR 0032 — Patient-reported data is first-class, and the patient can correct the record

- Status: Accepted (2026-08-28)
- Completes the pattern ADR 0031 began for the regimen, and closes the two
  gaps an evaluation of three real conversational scenarios exposed.

## Context

The system modelled document-derived data as authoritative and
patient-derived data as prose. Three sentences a patient would plausibly
type, traced through the code, showed what that costs.

**"I am also on Biotin which I started taking two months ago."** Handled, once
ADR 0031's regimen record existed — but tracing it found the parser knew
neither relative time nor month names, so the date silently vanished and
`"June 2026"` resolved to `2026-01-01`. Fixed separately; it is the reason
this evaluation happened at all.

**"I had a lab in November 2024 but my portal only keeps a year. My iron was
high."** `LabResult.source_doc` is a required 64-character sha256, so a
remembered value had no representation. It became prose in an intake fact and
was invisible to trends, `query_labs`, the criteria scorers and every lab
section. The requirement is correct — a remembered value must never sit in the
measured series the citation checker guards — but the behaviour was not
strictness, it was loss. The missing year is precisely what she is telling us
about.

**"You reported I had a pituitary scan in 2026. This did not occur."**
`grep -rn "dispute"` returned nothing. `retract_fact` reaches intake facts
only, and the MRI is an ingested encounter, so her correction became a new
patient-report encounter while the original stayed — still cited, still
shaping the differential, still reappearing in the next review with full
confidence. Of the three this is the most serious: telling a system it is
wrong and watching nothing change is corrosive in a way a missing feature is
not, documents genuinely are wrong sometimes, and a phantom study shapes a
differential exactly as a real one does.

## Decision

**`case/reported-results.yaml`** records results the patient remembers,
separately and permanently labelled. It holds a direction (`high`/`low`/…)
as readily as a number, because people recall "my iron was high" far more
often than a value, and a record that only held numbers would discard the
commonest case. It is **never merged into the measured series** — there is
deliberately no `to_lab_result` — and it carries `patient-report:<date>` so a
reasoner quoting one is quoting something checkable.

`corroborate_reported` checks each unverified claim against the measured
series deterministically, within a 45-day window of the remembered date.
**`contradicted` is as valuable an outcome as `corroborated`**: a remembered
"high" against a measured normal may mean a different analyte, a different
year, or someone else's result, and a differential built on the memory would
be built on sand. An **undated** memory is matched against nothing, because
picking the nearest row would manufacture a correspondence out of nothing.

**`case/disputes.yaml`** records patient objections to items on file. A
dispute **never deletes anything**. The archived document remains the source
of truth — she may be misremembering, and a system that erased records on
request would be worse than one that ignored them. What a dispute does is
make the conflict visible wherever the item appears, stop it being read as
established, and put it in front of a human. Only a human moves a dispute off
`open`; a model that could dismiss a patient's correction would defeat the
point of recording it.

`not-mine` is its own kind: a misfiled document belonging to someone else is
a different and more serious problem than a wrong date, and should not be
buried in a generic category.

**Both arrive through the existing visit-capture pass** (prompt v4), applied
before its `ops` early return, with the guards `regimen_chat` established: a
reported analyte must appear in the patient's own message, and a dispute must
name a ref that actually resolves — logged loudly when dropped, because a
discarded dispute is a patient correction going unheard.

## Consequences

- The context pack gains a `reported_results` section, rendered apart from
  the labs sections and labelled on every line, and disputed encounters are
  marked inline rather than hidden.
- **A dispute has no resolution UI yet.** It sits `open` until someone edits
  the file. That is the next piece of work, and until it exists the marker is
  visible to the reasoner but there is no in-app way for a human to uphold or
  dismiss it.
- Corroboration is not yet run automatically after an ingest. It is a pure
  function over the two stores, so wiring it into the ingest pipeline is
  additive — but today a document that answers an old memory does not close
  the loop until something calls it.
- Reported results do not reach the criteria scorers or the trajectory
  analysis. They are deliberately outside the measured series, so any use of
  them there needs an explicit decision about how an unverified value is
  weighed — which is a clinical question, not a plumbing one.

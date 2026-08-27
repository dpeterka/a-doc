# ADR 0028 — A bad citation costs the citation, not the review

- Status: Accepted (2026-08-27)
- Applies ADR 0016's strip-not-fail posture to the review path, and reverses
  a pin introduced days earlier by ADR 0022's citation work.

## Context

The blind panel had been reaching the ledger with 24 hypotheses and zero
evidence items. The fix gave `BlindEvidenceItem` a `source` field and told
the prompt to cite rows rather than values. It worked: on the first real run
the panel cited densely.

Then the review died. Four of its refs looked like this:

```
other:monospot_(heterophile)_screen:2026-03-17
other:left_total_hip_percent_change_vs_2024:2026-08-04
```

Real analytes, real dates, invented prefix. `BlindEvidenceItem.source` was
validated in a Pydantic field validator, which **raises** — so four bad refs
failed `BlindDifferentialPayload`, which failed the panel member, which
failed a 14-node, 12-minute review that had already spent two model calls.
The `_resolvable_evidence` filter written specifically to drop-and-log such
refs never ran, because validation happened a layer above it.

Two distinct causes, and the interesting one is not the validator.

**The pack never showed the ref.** Document excerpts have always been
rendered with their own `doc:<file>#p<page>`. Lab rows were rendered as
`- CRP: 8.5 mg/L [H] — 2026-05-02` and nothing more, so a model asked to
cite a lab value had to CONSTRUCT `labs:<slug>:<date>` from a display label
and a date, guessing the slug convention. The panel guessed the prefix from
the nearest thing that looked authoritative: the labs section groups rows
under panel headings, and un-curated analytes sit under a heading literally
called **Other**. The model was doing something reasonable with what it
could see.

This is the third instance of one recurring defect in this system: *a model
asked to reproduce a generated identifier it was never shown.* Intake fact
IDs and divergence IDs were the first two.

## Decision

**1. Show the ref beside every lab row.** `_labs_ref` renders
`` `labs:<slug>:<date>` `` after each row in both labs subsections. Citing
becomes copying.

The slug is not the stored name. Analyte names are display strings —
`IGF-1 Z-Score`, `Free T4:T3 Ratio` — and the grammar's slug is `[^\s:]+`,
forbidding exactly the whitespace and colons those contain. The first
implementation interpolated `row.name` and so would have printed an invalid
ref beside **1178 of 2079** real rows: the identical defect one layer down,
caught only by running against the real corpus rather than the synthetic
fixtures, which happened to use already-slugified names. `_labs_ref`
collapses runs of non-alphanumerics to `-`, which is *normalization-
preserving*: `citations._normalize_slug` strips non-alphanumerics anyway, so
`igf-1-z-score` and `IGF-1 Z-Score` reduce to the same key and the ref
resolves back to its row. Measured after the fix: 568 refs rendered, 0
grammar-invalid, 0 unresolvable.

Two tests pin this — one that every ref the pack emits resolves, and one
built from deliberately hostile names, because the first test passed while
the bug was live.

**2. `BlindEvidenceItem.source` is an unvalidated `str`.** Filtering happens
in `_resolvable_evidence`, after the payload parses. `Evidence.source`
*keeps* its raising validator — that is the ledger's own type and should
reject bad refs — so `_resolvable_evidence` now checks the grammar
defensively before constructing one.

**3. The prompt says copy, never construct** (`blind_reviewer.md` v4), and
names the failure explicitly: a panel heading is not a ref prefix.

## Consequences

- Nothing unresolvable reaches the ledger; that guarantee is unchanged. What
  changes is whether the other 23 hypotheses survive one bad ref.
- **This reverses a pinned property**, which is why it is an ADR (CLAUDE.md
  hard rule 2). `test_a_malformed_ref_is_rejected_at_the_schema_boundary`
  asserted the raise; it is replaced by
  `test_a_malformed_ref_costs_the_citation_not_the_review`. The property
  worth pinning was never "bad refs are rejected early" but "bad refs never
  reach the ledger AND never take the review down with them" — the first
  version bought the first half at the cost of the second.
- Refs are dropped silently from the patient's perspective and loudly in the
  logs. A hypothesis whose every citation was malformed still renders as
  uncited, which understates it. Accepted: the alternative is a review that
  produces nothing at all.
- The pack grows by roughly one ref per lab row. Measured against the blind
  panel's 31,232-token budget this is a few thousand characters — but it is
  the labs section, which is the section that grows with the corpus. If the
  budget is ever the binding constraint, this is a place to look.

## Addendum (same day): citations must survive agreement

Deploying the above fixed the crash — 14/14 nodes, 796s, **zero dropped
refs**, every citation the panel produced was well-formed and resolved. Then
measuring the result showed the user-visible symptom was still there: 25
hypotheses in the ledger, **1** with any evidence.

A `Divergence`, by definition, exists only where the panel and the ledger
*disagree*. Citations therefore survived exclusively on disagreement. An
accepted `panel_only` divergence became a new hypothesis carrying its refs —
that is the 1. Everywhere else `compute_divergences` recorded the name in
`covered_norms` and dropped the evidence on the floor, and a
`probability_mismatch` pooled only the *mismatched* members' citations,
discarding those of members who happened to agree.

That inverts the intent. The hypotheses both the ledger and an independent
blind panel endorse are the best-supported ones in the case, and they were
precisely the ones rendering as uncited. A hypothesis could only ever be
cited by the review that created it; the 21 added before citations existed
could never gain one.

**Decision.** `DivergenceSet.panel_citations` maps ledger hypothesis id to
pooled citations for every hypothesis the panel *named*, agreement included.
`build_review_ledger_diff` emits `AddEvidence` for each resolvable one,
**not gated on the adjudicator's decision**: a resolvable ref is a fact about
the data, not a verdict on a probability, and it passes the citation checker
by construction. Dedup is against the hypothesis's existing evidence on
`(normalized claim, source)`, because `apply_diff` appends `AddEvidence`
blindly and a weekly review would otherwise re-add the same citation forever.

**Consequences.**

- Evidence accumulates across reviews instead of only at creation. The bound
  on growth is the dedup key, so a panel rephrasing the same claim about the
  same row *will* add a second item. Acceptable; watch it.
- `confirmed-by-doctor` hypotheses are untouched — they are not in
  `ACTIVE_STATUSES`, so `compute_divergences` never matches them and the
  raised bar of invariant (d) is never approached.
- Two properties are pinned: that agreement still yields citations (the test
  asserts no divergence is produced for the agreeing hypothesis, so it cannot
  pass for the wrong reason), and that a second review does not duplicate.

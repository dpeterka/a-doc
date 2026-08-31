# ADR 0037 — An uncited can't-miss lead does not lead the page

Status: accepted (2026-08-31)

## Context

The owner, reading his wife's differential:

> empty priority items appear prioritized
>
> Hereditary breast and ovarian cancer syndrome (BRCA founder variant,
> Ashkenazi ancestry) — Can't-Miss status: Active confidence: low raised by:
> Raised by the challenger check
> Evidence for: No citations recorded yet

`group_hypotheses` put **any** can't-miss hypothesis, at any probability,
into the `leading` group — the one headed "Worth discussing now". That was
deliberate: the can't-miss tier exists because the cost of missing one is
catastrophic and asymmetric, which is also why `casefile.retirement` protects
those entries absolutely.

But the tier is a safety net, and the Challenger is *expected* to raise
entries in it speculatively — "if this were true, missing it would be
catastrophic" — before anything supports them. That is the tier working as
designed. The consequence nobody had looked at is what it does to the page: a
lead with no citations, low confidence and no supporting finding was printed
above leads the patient's own labs point at, with identical visual weight.

For someone reading her own case file, that is not a cosmetic problem. It
says the system considers an unsupported genetic-cancer hypothesis one of the
things most worth discussing now.

## Decision

Within the leading group, **substantiated leads come first**. A hypothesis is
unsubstantiated when it has no `evidence_for` **and** its probability is
`low` or `minimal`.

An unsubstantiated can't-miss lead:

- **stays in the leading group.** It is not folded away and not hidden. The
  tier's whole purpose is that these remain visible.
- **sorts last within it.** It is the last thing read in that group rather
  than the first.

Ordering is otherwise unchanged: `sort_hypotheses`'s tier-then-probability
order is preserved inside each half, so the change is a stable partition, not
a re-sort.

Nothing about the ledger, the tier, or retirement changes. This is a
presentation decision and lives entirely in `web/casefile_helpers.py`.

## What this changes about a pinned test

`test_a_large_ledger_leads_with_what_matters_and_folds_the_tail` pinned
"can't-miss at ANY probability, then high/moderate" and expected
`["h4", "h2", "h3"]`. It now expects `["h4", "h3", "h2"]`, because `h2` is a
can't-miss with `minimal` probability and no evidence — precisely the
placeholder this ADR is about.

Recorded here rather than edited quietly, per CLAUDE.md rule 2: changing
*which* property a test pins requires an ADR.

## Alternatives considered

**Hide uncited can't-miss leads behind the disclosure.** Rejected. The tier
means the cost of missing one is catastrophic; folding it away to tidy the
page is the failure mode the tier exists to prevent, and it is the same
instinct ADR 0035 refused when it excluded can't-miss from retirement.

**Require the Challenger to cite anything it raises as can't-miss.**
Rejected, and worth stating plainly: a can't-miss lead is often raised
*because* nothing has been looked for yet. Requiring a citation would
suppress exactly the leads most worth naming — the ones nobody has tested.

**Drop the tier from the ordering entirely and sort on evidence alone.**
Rejected: a supported can't-miss lead should still outrank a supported
`expanded` one, and it does.

# ADR 0052 — Every review records its own boundary

- **Status**: accepted
- **Date**: 2026-09-08
- **Supersedes**: nothing
- **Related**: ADR 0045 (tier caps), ADR 0050 (emerging tier), ADR 0051 (abnormal is a comparison)

## Context

The convergence track is six releases whose entire purpose is to make the
differential smaller and better directed. Each one was measured the same way:
by hand, once, on the day it deployed, against a number recalled from the
release before.

That method produced two wrong numbers in a fortnight.

- An **"8 emerging"** projection, taken from a raw date measurement made
  *before* `MAX_SOURCES_TO_STAY_EMERGING` was written. The real figure was 2.
  The projection was measuring a mechanism that did not exist yet.
- A **"187 of 2079"** before/after for ADR 0051, where 2079 is every stored
  row ever and the change acted on the 536-row latest panel. The honest
  figure was 32 → 49.

Neither was a coding error. Both were the same structural problem: the
baseline lived in one person's memory and in terminal scrollback, so nothing
could contradict it.

There is a second, quieter version of the same problem. `Retirement.to_status`
records **what happened** to a lead, not **which rule did it** — and three
different rules write `parked`: no supporting evidence, gone stale, and the
ADR 0045 tier cap. A count of parked leads therefore credits whichever
mechanism the reader has in mind. "The cap folded 14" and "staleness parked
14" are indistinguishable in the artifact.

## Decision

**Every review appends one line to `case/convergence.jsonl`** — a
`ConvergenceSnapshot`: the board's shape, the counts the review worked from,
and the `app_version` that produced them. The review report renders the delta
against the previous line under *What moved since the last review*.

**`Retirement` gains a `cause`** naming the rule that proposed it, and the
snapshot splits the run's removals by it.

Three properties make this a boundary rather than another recollection:

1. **Append-only.** A snapshot is what was true on a date. A re-run that
   overwrote it would erase the boundary it came to mark.
2. **Versioned.** A count with no version says the board changed, not what
   changed it.
3. **No delta against an absent baseline.** The first reading of a new counter
   did not *add* the whole board. `delta(None)` is `{}`, and the report says
   "starting line" — precisely the false before/after that motivated this.

Everything counted is a count over the ledger the review just committed. No
model call, no judgement: CLAUDE.md's rule that deterministic logic is plain
code covers this exactly.

## Consequences

- The next full review's impact is attributable from disk instead of from
  recollection, which is what this was for.
- A review can now be asked a question it could not answer before: *did the
  last release do anything?*
- The snapshot is written inside the DAG (`convergence_snapshot`), and
  `render_report` declares an edge to it. Reached through the `results` sink
  instead, a reordering could print the previous review's boundary under this
  review's date (ADR 0043).
- A failed write is logged and swallowed. A review that produced a real
  differential must not be lost because a measurement log could not be
  written — and a corrupt line is skipped for the same reason.
- Writing this found a live instance of the recurring bug class: the first
  draft counted `status == "retired"`, and there is no such status. The check
  could not fire and would have reported 0 retirements forever, which looks
  exactly like a differential nothing ever leaves.

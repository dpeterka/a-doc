# ADR 0019 — Event-triggered deep review (marker + cooldown + floor), and merging the review UI into the ledger

- Status: Accepted (2026-08-25)

## Context

The deep review (PLAN.md session loop (c): blind re-differential panel,
divergence adjudication, staleness scan, test chooser, committed/tagged
artifact) ran on a fixed `cron(0 6 ? * SUN *)` EventBridge schedule
(`deploy/cfn/ecs.yaml`'s `ReviewRule`) — once a week, regardless of
whether anything happened in between.

Direct owner feedback:

> Re-evaluate the weekly review idea. A new document or chat in the UI
> could trigger a review. This would change it from an EventBridge
> scheduled event to more of a job/event nature.

Pure event-triggering — fire a full review the moment a document lands or
a chat turn changes the ledger, and nothing else — is wrong on its own,
for two reasons this ADR has to preserve:

1. **The blind panel's value is highest when nothing new has arrived.** It
   exists to counteract the ledger anchoring itself (ADR 0002's blind-
   reviewer rule: the panel never sees the current ledger, precisely so it
   can't just agree with it). A stale ledger during a QUIET period is
   exactly when a fresh, independent look matters most — event-only
   triggering means a quiet period is never re-examined, inverting the
   panel's purpose. `scan_staleness` is likewise inherently time/version
   based, not event-based — it exists to catch drift that accumulates
   with no single triggering event.
2. **Thrash and cost.** The blind panel is three frontier models over full
   context (PLAN.md: "$8-20/deep review"). `ingest.pipeline` handles
   inbox files one at a time internally, and a Dropbox app-folder sync can
   land a dozen files within seconds of each other — naive per-event
   triggering would fire a dozen full reviews for one Dropbox drop.

## Decision

**Event-triggered, with coalescing, a cooldown ceiling, and a time floor.**
Three timers, each covering what the other two cannot:

| Timer | Value | Purpose |
|---|---|---|
| **Signal** (the "review wanted" marker) | set by ingest / a ledger-changing chat turn | Materiality — a full review is only worth running when something changed |
| **Cooldown** | 6 hours (`FULL_REVIEW_COOLDOWN`) | Thrash/cost ceiling — caps how often the expensive blind panel can fire even under a steady stream of material events |
| **Floor** | 7 days (`FULL_REVIEW_FLOOR`) | Preserves the blind panel's anti-anchoring purpose and `scan_staleness`'s time-based check as a worst case — a full review runs at least this often even with NO marker at all |

### The marker: `reason.review_trigger`

`work/review-wanted.json` (gitignored, derived — same tier as
`work/entailment-cache.json`/`work/entailment-deferred.json`, not part of
the patient's case-file record). A list of `{reason, at}` entries, set by
`mark_review_wanted(repo, reason)`:

- `ingest.pipeline` — once per `ingest_file`/`ingest_inbox`/
  `ingest_directory` call, over the WHOLE resulting `IngestReport`, not
  once per file — this is what makes a 12-file Dropbox drop coalesce into
  ONE marker update instead of firing 12 review considerations. A no-op
  when nothing was actually ingested (every outcome was a duplicate or an
  error).
- `reason.stages.run_diagnostic_turn` — only when the diagnostic DAG's
  `apply` node actually committed a diff (`sink["apply"]` populated),
  never merely because `run()` returned without raising: `apply`'s own
  PRECONDITIONS (citation/entailment/abstention checks) can block
  `_apply_fn` from ever running, and a LATER node's postcondition (e.g.
  composer's `treatment_gate`) can still fail even though `apply` already
  committed — the marker call runs from a `finally` block keyed on
  `sink["apply"]`'s presence so it tracks "did the ledger actually
  change," not "did the turn look successful." An informational turn
  never reaches this code path at all (`run_informational_turn` never
  calls `build_diagnostic_dag`).
- `run_post_ingest_dag` (`adoc ingest --reason`) deliberately does NOT
  mark again — the ingest step that fed it already marked for the same
  underlying event.

Cleared (`clear_review_marker`) ONLY after a full review returns
successfully. An exception anywhere in the DAG leaves the marker exactly
as it was, so the very next tick tries again — a crashed run never loses
the signal.

### The runner: keep the existing scheduled-task mechanism

Rather than adding SQS/Lambda or granting the always-on web task
`ecs:RunTask` (see "Alternatives rejected" below), `ReviewRule`'s
`ScheduleExpression` changes from the weekly cron to `rate(30 minutes)`,
and `adoc review` (unchanged command, unchanged task definition) now
decides for itself whether to actually run a full review this invocation:

- **Cheap parts, every tick**: `deterministic_trend_scan` (no LLM) and
  `sweep_deferred_entailment_claims` (no LLM in the common case — an
  empty deferred-claims queue is a no-op; a non-empty one calls the
  `entailment_verifier` role, not the blind panel). No frontier-model
  calls happen on a tick that doesn't run a full review.
- **Full review** (blind panel + adjudication + staleness + test chooser
  + committed/tagged artifact) only when `should_run_full_review` says so:
  the marker is set AND the cooldown has elapsed, OR no full review has
  run within the floor window (which also covers "no full review has ever
  run" — the very first tick on a fresh data repo).
- `reason.review.run_review_tick` is the one function both the scheduled
  tick and a human calling `adoc review` go through — `should_run_full_
  review` is a pure function (marker, `last_full_review_at`, `now` in;
  `(bool, reason)` out), unit-tested with no I/O.

**30-minute tick interval, chosen over the "rate(30 minutes)" suggestion
as the actual value**: short enough that the cooldown/floor (hours/days)
dominate the user-visible latency — a marker set right after a tick waits
at most ~30 minutes to even be CONSIDERED, negligible next to a 6-hour
cooldown — while being far cheaper than a per-event trigger (each tick
with nothing due costs one deterministic trend scan against `labs.sqlite`,
no LLM call in the common case).

**Why `last_full_review_at` comes from a git tag, not a second persisted
timestamp**: every full review already creates a `review-*` git tag
(`DataRepo.tag`, unchanged). `DataRepo.latest_tag_time("review-")` reads
the most recent one's commit time — committed, durable across an EFS
restore, and impossible to drift out of sync with reality the way a
separate `work/last-full-review.json` could (e.g. if a review committed
but a subsequent timestamp-file write failed). One source of truth, not
two.

**Collision-safe review artifact naming**: the old naming
(`case/reviews/{date}-review.md`, tag `review-{date}`) assumed at most one
full review per day, true under a weekly cron but no longer true under a
6-hour cooldown (up to 4 full reviews could land on the same calendar
day). `reason.review._review_relpath_and_tag` now checks for a same-day
collision and appends a colon-free `THHMMSS` suffix only when one exists —
the common case (first review of the day) is byte-identical to the old
naming, so existing `report.tag == "review-2026-08-23"`-style assertions
are untouched.

### `--force`

`adoc review --force` bypasses marker/cooldown/floor entirely and always
runs a full review — how a human asks for one on demand, and how anything
scripted against `adoc review` expecting a guaranteed full run (`adoc
eval`, an on-call check) keeps working. Plain `adoc review` (no flags) now
goes through the same gating the scheduled tick uses, since it IS the same
entry point — this is a deliberate behavior change from "always runs a
full review," but nothing in this repo (README, CI, `eval.yml`) calls
`adoc review` expecting the old unconditional behavior, and `--force`
covers every case that did.

### Ledger invariants and DAG contracts unchanged

The review's DAG topology, node contracts, and the ledger invariants it
mutates through are byte-for-byte unchanged from before this ADR — only
WHETHER `run_weekly_review` is called changed, never what it does.
`apply_review_diff` still goes through `DataRepo.apply_ledger_diff`, which
holds `repo._lock` across load -> apply -> save -> append-history ->
commit in one critical section — the same lock a diagnostic chat turn's
`apply_stage` uses, so a full review firing more often (up to 4x/day at
the cooldown ceiling, versus 1x/week before) is exactly as protected
against concurrent-write clobbering as it always was; nothing about
event-triggering changes that path.

### UI merge: `/ledger` and `/reviews` become one screen

Owner feedback, directly downstream of the trigger change:

> full picture and weekly review would likely just be the same screen if
> we go this route.

`/ledger` ("The full picture," the live differential) and `/reviews` (a
dated index of review artifacts) were two nav entries for what is, once a
review can fire on new evidence, approximately the same underlying
picture. Merged into one screen at `/ledger`:

- **Current differential** at the top — unchanged rendering from before
  (tier/status/origin chips, gated evidence claims/discriminators/
  challenger notes via `redact_gated_text`).
- **Latest review inline** — the most recent `case/reviews/*.md`,
  re-gated at render time and rendered in full (its "Why this review ran"
  line, trend alerts, metrics appendix, and test-chooser items are all
  already one cohesive document — showing it inline rather than
  re-parsing it into separate template fields keeps one source of truth).
- **Prior reviews as history** — every older review still linked by its
  permalink (`/reviews/{filename}`, unchanged route).
- Two independent empty states: no hypotheses yet (unchanged copy) and no
  reviews yet (rewritten to describe the event-triggered mechanism —
  "runs automatically... at most once every 6 hours, and at least once
  every 7 days" — rather than a false "every Sunday" claim, and no longer
  claiming a review needs a prior diagnostic conversation, since the
  floor alone can trigger one on an empty ledger).

`/reviews` (the old index) redirects (301) to `/ledger` rather than
404ing — a link to it may exist in a committed review's markdown or an
old chat-transcript entry (same reasoning as `/onboard` -> `/chat` in
`docs/adr/0012`). `/reviews/{filename}` — the actual permalink every
review is reached by, and the audit trail this whole design depends on
staying reachable — is unchanged. `base.html`'s nav drops "Weekly
reviews," keeping one "Full picture" entry.

## Alternatives rejected

- **Pure event-driven, no floor/cooldown.** Rejected in the Context
  section above: inverts the blind panel's anti-anchoring purpose during
  quiet periods, and a burst of ingest/chat events would fire a burst of
  $8-20 full reviews.
- **App-initiated `ecs:RunTask`** (the always-on web task calls ECS
  directly when a material event happens, instead of polling on a
  schedule). Rejected: grants the web task's `TaskRole` `ecs:RunTask` +
  `iam:PassRole` onto `JobsTaskDefinition`'s roles — a new, broad IAM
  capability on the task that's reachable from the public ALB, for a
  single-patient app where a 30-minute polling latency against an
  hours/days-scale cooldown/floor is imperceptible. Not worth the attack
  surface.
- **SQS + Lambda** (ingest/chat publish a "review wanted" event; a Lambda
  consumes it and calls `ecs:RunTask`, or runs the review itself off-task).
  Rejected: a new queue, a new compute platform (Lambda), and a new IAM
  role, replacing what a single boolean-ish marker file plus an existing
  scheduled task already does — CLAUDE.md's infrastructure section already
  favors the minimum footprint (CloudFormation-managed ECS Fargate, no
  extra moving parts) for a single-patient system.

# ADR 0046 — Which minute

Status: accepted (2026-09-02)

Closes PAT-06. Builds on [ADR 0043](0043-the-declared-graph-is-the-real-graph.md).

## Context

PAT-06 says a diagnostic chat turn takes 30–90+ seconds and "the UI displays
only a static loading spinner, causing patients to believe the application
has frozen", and proposes replacing it with an SSE-driven stepper.

**The premise is half wrong, and the half it gets wrong is the valuable
half.** `chat.html` already renders a labelled, `aria-live` indicator:

> Thinking. A question about your differential works through your whole case
> file and can take a few minutes — you can leave this page open.

It also carries a dropped-connection message, and a comment recording why:
without `hx-indicator`/`hx-disabled-elt` the page did not move at all, htmx
queued a repeat submit behind the in-flight one, and the honest read of that
was "the Send button stopped working" — *which is exactly how it was
reported*. That line was written in response to this exact complaint.

What is genuinely missing is not reassurance. It is **which minute**:
whether the wait is nearly over or has barely started.

## Decision

A stage ticker, additive to the existing indicator rather than replacing it.

### 1. A hook on the DAG, not a second transport

`dag.run` takes an optional `on_node_start(name, step, total)`, called as
each node **starts** — the point is to say what is happening now, not what
just finished. A hook that raises is caught and logged: a status line must
never be able to fail a reasoning run.

### 2. A polled fragment, not Server-Sent Events

PAT-06 proposes SSE. **Declined.** It means a second transport for a form
htmx already handles, a background thread so the request can stream while
the DAG runs, and a new failure mode where the reply exists but the
connection carrying it has gone. The alternative is a 2-second `hx-get`
against a dict, using the request/response shape that already works.

The ticker polls only while the pending indicator is showing, so a quiet
page makes no requests.

### 3. One slot, deliberately

`reason/progress.py` holds ONE in-flight turn, not a map keyed by request.
The web service runs a single ECS task at a time (CLAUDE.md), there is one
patient, and htmx disables Send and queues a second submit behind the first
— two concurrent diagnostic turns is not a state this application reaches.
A per-request key would need the page to know its own id before the POST
that creates it: a JavaScript problem invented to solve a concurrency
problem that does not exist.

If it ever does exist, the failure is that two turns overwrite each other's
*progress label*. No reply, no ledger write and no audit record depends on
this module.

### 4. Stage labels only

Nothing from the turn is published — only the node name mapped through a
fixed table, so a poll can carry no patient content even if the response
were cached or logged. An unmapped node shows `Working...` rather than an
internal identifier, and a test pins **every node of the DAG the application
actually builds** against the label table, so adding a stage cannot silently
ship `Working...` to the patient. A test over a hardcoded list of four names
would have passed in that case.

The labels say what is happening to her records, not which module runs:
`Reading your history and recent results...`, not `ledger_maintainer`.

### 5. A stuck turn stops reporting

A turn that dies between stages would otherwise leave the page reporting a
stage forever. After 15 minutes without an update the tracker reads as
finished — longer than the longest plausible turn, short enough that a stuck
one resolves.

## Consequences

- One new GET route, behind the same session auth as everything else. A
  fragment that reveals only whether a turn is running is low value to an
  attacker, but an unauthenticated endpoint exposing when the patient is
  using the app is still a leak, and a test pins the redirect.
- The existing indicator's wording is unchanged, and a test asserts it stays
  — the ticker is additive. If PAT-06's replacement had been taken
  literally, the honest "can take a few minutes" line would have been
  removed in favour of a stepper that says less.
- Two guards in `ProgressTracker` are redundant given other guards, and are
  kept with comments and direct tests saying so: the `finished` check in
  `note`, and `finished` starting `True`. Same posture as ADR 0043's batch
  snapshot — a check nothing exercises is the shape of every silent-absence
  bug here.
- `dag.run` gains a parameter used by one caller. The review DAG does not
  use it: nobody is watching a weekly batch.

## Alternatives considered

**SSE, as proposed.** Declined above.

**Replace the indicator with a stepper.** Rejected. The line that sets the
expectation is the part that was written in response to this complaint being
made once already; a stepper showing "step 2 of 4" without it invites the
reader to expect four quick steps.

**Estimate a time remaining.** Rejected. Stage durations vary by more than
an order of magnitude with model latency and context size, and a countdown
that is wrong is worse than no countdown — it converts "this is slow" into
"this is broken".

**Persist progress so it survives a reload.** Rejected as scope. The reply
itself already survives — `chat_send` writes the transcript, and the
dropped-connection message says the answer will appear on its own.

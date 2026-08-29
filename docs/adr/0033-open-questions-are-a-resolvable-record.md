# ADR 0033 — Open questions are a resolvable record, not a rendering

Status: accepted (2026-08-29)

## Context

The next-appointment list existed only as `case/questions-open.md`, written
wholesale by every review from the test-chooser's output.

That made a question a *rendering* rather than a *thing*. It had no identity,
so nothing could record that it had been dealt with. The loop the system was
supposed to close never closed:

1. A review asks "list every supplement and dose".
2. She answers in chat. `run_visit_capture` records the supplements as facts —
   correctly, and they reach the regimen.
3. The next review regenerates the list from the ledger. The ledger still has
   the hypotheses that motivated the question, so the chooser proposes it
   again.
4. She is asked the same question a second time.

Nothing was broken in any single step. The answer was captured, the facts
were right, the chooser was reasoning correctly from what it had. The chooser
simply has no memory of what she said between reviews, and there was nowhere
for that memory to live.

This is the same shape as ADR 0032's addendum — a derived artifact standing
where a record belonged — but a step worse: there was no record anywhere, so
nothing could be corrected by changing a read path.

## Decision

`case/questions-open.yaml` becomes the record. `questions-open.md` becomes a
rendering of it.

**Questions have stable ids.** The id is a slug of the panel text, derived
rather than assigned, so a review re-proposing the same panel lands on the
same question and inherits its state. Rewording the panel does break the link
— that is a real limit, accepted deliberately: the merge opens a new question
rather than guessing which old one a changed string meant.

**A review merges, it does not overwrite.** `merge_proposed` refreshes wording
and `last_asked_on` from the newest run, because the newer phrasing is the
better one to show if it ever reopens. `status`, `answered_on` and
`answer_note` are the store's to keep. A question the chooser stops proposing
is **not deleted** — the ledger moves between runs and an item can drop out of
one and return in the next; deleting would lose the answer and re-ask from
scratch.

**The model matches, code applies.** `VisitCaptureResult` gains
`answered_question_ids`. Deciding *which* queued question a message answers is
genuine semantic judgement and is not something deterministic code can do; but
validating the ids, closing the questions and persisting the change is plain
code. An id matching nothing is logged and dropped — ADR 0028's rule that one
bad identifier costs its own claim and never the rest of the payload.

**The capture pass is shown the ids.** It cannot report an identifier it has
never seen; that is the standing rule from ADR 0028, and the gloss backfill
that produced zero glosses is what happens when it is ignored. The open
questions are rendered into the capture context with their ids, `audience:
you` first, since those are the ones a chat message plausibly closes.

**Resolution happens before the `ops` early return.** `run_visit_capture`
returns early when a turn produced no fact ops. Regimen changes were already
applied before that return for a documented reason — "I stopped the selenium"
warrants no fact op and is exactly what that path exists to catch. Answered
questions are identical: "yes, biotin 10mg and vitamin D" may add no new fact
when both are already on file, and still definitively answers the question
that asked for them. This was caught by a test, not by reading: the first
wiring sat after the return and silently did nothing for precisely the
messages it was written for.

## Consequences

- A question she has answered is not asked again. The chooser may still
  propose it; the store filters it out before rendering, and logs how many it
  suppressed.
- **The link is by panel text, so rewording reopens.** If the chooser
  materially rephrases a panel between reviews, the old question stays
  answered and a new one opens alongside it. This is visible in the store
  rather than silent, but it is the main way this design degrades.
- **Answering is only as good as the model's matching.** The prompt is
  deliberately strict — a message that touches a topic does not answer a
  question about it — and errs toward missing an answer, because a missed
  answer costs one repeated question while a wrong one silently buries a
  question nobody ever answered.
- **There is no way to reopen a question yet.** If she answers something
  wrongly, or her answer stops being true, nothing walks it back short of
  editing the YAML. A reopen path belongs with the dispute-resolution UI that
  ADR 0032 also left outstanding.
- The context pack still reads `questions-open.md` rather than the store. The
  markdown is now derived from the store and already excludes answered items,
  so the content is correct; but by this project's own rule the read path
  should be the record. Left as follow-up rather than widened here.
- Nothing backfills the store from the reviews already on disk. The first
  review after this change populates it, and until then every question reads
  as open — which is what they are.

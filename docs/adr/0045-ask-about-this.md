# ADR 0045 — Ask about this

Status: accepted (2026-09-02)

Closes PAT-05. Builds on [ADR 0039](0039-how-a-review-reads.md).

## Context

Reading a confusing finding in the review, the patient has to open `/chat`
and retype the finding from memory before she can ask about it. The only
cross-link that exists is a bare "Start a conversation →" at the bottom of
the case file.

The friction is real and small. What is not small is the obvious
implementation.

## Decision

Every `##` section of the rendered review, and every hypothesis card, gains
an **"Ask about this"** link that opens `/chat?ask=<question>` with the
composer **pre-filled**.

### It pre-fills. It does not send.

PAT-05 asks for a button that "sends a pre-seeded contextual payload to
`/chat/send`". **Declined**, for three reasons that compound:

1. **A diagnostic turn writes the ledger.** `apply` commits its diff before
   the composer ever speaks — `routes/chat.py` says so where it handles a
   `ContractViolation`: "this turn's case-file update already happened". A
   link that mutates the case file is a mutation disguised as navigation.
2. **It costs minutes and several frontier calls.** `chat.html` already
   carries the honest warning: "can take a few minutes — you can leave this
   page open."
3. **The question decides the answer.** An auto-sent excerpt is a question
   she never wrote, phrased by a heading, aimed at a model that will treat
   it as her words. Letting her see and edit it first is not friction; it is
   the part that makes the answer hers.

Pre-filling costs one page render and no model call — a test pins that,
using transports that fail if invoked.

### The seeded text is user input

It arrives in a URL, so it is treated as such: whitespace-collapsed,
truncated to the same `max_message_chars` limit `chat_send` enforces, and
rendered as the textarea's *content* where Jinja escapes it. A link that
arrived already over the limit would be a dead end she could not diagnose.

### The question is phrased in her voice

`Can you explain the "<section>" part of my review in plain terms?` — not
PAT-05's `Explain the following review finding in plain terms: <excerpt>`.
A payload written in the system's voice reads to the model as an instruction
and to her as something she did not say. The card version names the lead and
asks the two things a patient actually wants: what it is, and why it is on
the list.

### Only `##`, and only where it belongs

Second-level headings only: a link under every `###` inside a criteria set
would bury a report ADR 0039 has just finished shortening. And the links come
from a separate `markdown_lite_asks` filter rather than a default, so a chat
reply cannot render an offer to explain itself in chat.

## Consequences

- One new query parameter on an existing route. No new endpoint, no new
  state, no JavaScript.
- The excerpt is **not** included in the seeded question — only the section
  name. Excerpts are long, they are model-written, and they would put review
  prose into a URL. The chat turn has the whole case file anyway.
- If a heading is renamed, an old link still works: it seeds a question
  naming a section that no longer exists, which reads as a slightly odd
  question rather than an error.

## Alternatives considered

**Auto-send, as written.** Declined above.

**A confirm step before sending.** Rejected as the worst of both: it is a
pre-fill with an extra click, and the thing she would be confirming is text
she still cannot edit.

**An HTMX drawer that keeps her on the review page.** Rejected for now. The
reply takes minutes and the transcript is the record of it; a drawer either
loses that or duplicates `/chat`. Navigating to the conversation is honest
about what is happening.

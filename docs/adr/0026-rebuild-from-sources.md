# ADR 0026 — Rebuild from sources; human review is source data

- Status: Accepted (2026-08-26)
- Replaces the unbuilt `--re-extract` concept referenced by `PLAN.md` and
  `labs/db.py`. Supersedes ADR 0025's `adoc labs-revalidate` plan.

## Context

ADR 0025 fixed extraction but left the question of what to do about data
already stored. Investigating that turned up two things.

**There is no re-ingestion path, and the documented one was never built.**
`adoc backfill` takes a directory and nothing else; documents dedupe on
sha256, so re-feeding an ingested file returns `duplicate` and extracts
nothing. `PLAN.md:85` and `labs/db.py`'s docstring both describe
`adoc backfill --re-extract [--since|--doc]` as though it exists. It does
not. Consequently `insert_results`'s careful re-extraction logic — cases
(a)–(d), reviving rejected rows, flipping a conflict to `pending` rather
than overwriting a human's correction — is unreachable for its stated
purpose.

The owner's judgement: `--re-extract` is a malformed concept. The sources
are archived and immutable; the simple, honest operation is to **start over
from the same files**.

**But 587 of 2033 rows carry human review:**

    1467  auto
     396  corrected    <- a person fixed these by hand
     162  confirmed
      29  rejected
       8  pending

The 396 corrections are the expensive part. They encode ground truth the
extractor got *wrong*, so re-extraction reproduces the same 396 errors and
someone re-does the work.

This exposes the actual architectural defect. `labs.sqlite` is explicitly a
DERIVED artifact (PLAN.md "State"), rebuildable from `labs-export.jsonl`.
But human review decisions live only inside it, entangled with the
extractor's output in that same export. A person's judgement is not derived
from anything — it is primary data, and it was being stored as though it
were a by-product of extraction.

## Decision

**1. `case/review-decisions.jsonl` — the human layer becomes source data.**

One committed, append-friendly, human-diffable record per review decision:
the row's identity, the status a person set (`confirmed` / `corrected` /
`rejected`), and for a correction the field values they chose. It lives in
the data repo alongside `labs-export.jsonl`, and it is *not* derived from
anything — nothing but a human writes it.

This is the change that makes "start over from zero" a safe, repeatable
operation rather than a one-time gamble.

**2. `adoc rebuild` — wipe derived state and re-ingest from `sources/`.**

    backup -> export decisions -> wipe -> re-ingest sources/ -> replay -> report

Dry-run by default. `sources/` is the immutable archive already kept for
exactly this reason, and `ingest_directory` never deletes or moves what it
reads (an existing, tested invariant), so the source of truth cannot be
damaged by a rebuild.

**3. Replay matches on identity that survives a rename.**

The UNIQUE key is `(date, name, specimen, source_doc)` and `name` is *in*
it — but the ADR 0025 fixes deliberately change `name` on some rows, so
matching on it would miss precisely the rows we touched. A decision is
therefore keyed on `(source_doc, date, specimen, normalized_name)`, where
normalization lowercases, strips non-alphanumerics and sheds trailing
connectives — the same shape PR #151 already uses to resolve citations
against renamed rows.

**4. An unmatched decision is surfaced, never guessed.**

If a decision's row no longer exists (the ADR 0025 gate legitimately
retired it, or extraction changed materially), the decision is reported as
unmatched and left unapplied. It is never force-applied to a
nearest-neighbour row: silently attaching a human's correction to a
different measurement than the one they reviewed is worse than losing it,
because it looks authoritative.

## Consequences

- Rebuilding becomes routine and cheap in human terms. Any future extractor
  or prompt improvement is "wipe and re-run", not a bespoke migration.
- The 396 corrections stop being hostage to a derived file.
- A rebuild still costs real model work: ~90 documents needing two-pass
  extraction (the 28 genomic files bypass the model entirely, by design).
  That is the price of not maintaining an incremental re-extraction path,
  and it is the right trade at this corpus size.
- Replay can only match what still exists. Rows the ADR 0025 gate retires
  (the three sentence-rows) will legitimately report unmatched decisions if
  a human had reviewed them — correct behaviour, and visible.
- `PLAN.md:85` and `labs/db.py`'s docstring must stop describing
  `--re-extract`. Documentation that promises an unbuilt feature is worse
  than none: this investigation began by reasoning from it as though it
  were real.
- `insert_results`'s (a)–(d) logic stays. It remains correct for its other
  caller — a genuinely new document colliding on an existing key — and a
  rebuild's replay deliberately does not route through it, because replay
  applies a *known human decision* rather than resolving a fresh
  extraction disagreement.

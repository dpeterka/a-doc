# ADR 0022 — A derived link index, not a graph store

- Status: Accepted (2026-08-25)
- Extends ADR 0003 (storage: git + SQLite). Does not change the system of record.

## Context

The question that prompted this: *is markdown a suitable format for this,
versus a knowledge-graph store — won't it get junky over time?*

Worth separating two things that look like one. The case file is not
actually markdown. The durable data is already structured and typed:
`intake-facts.yaml` (`IntakeFact` — section, kind, attribution, precision,
clarification status, corroboration), `differential-ledger.yaml`
(`Hypothesis` with typed `Evidence`, each carrying a validated source ref),
lab results in SQLite with an FTS5 index, and the document-text corpus of
ADR 0015. The `case/*.md` files — `family-history.md`, `medications.md`,
`geography.md` — are a **rendering** of that structured data for a human
reader. They are an output, not a store. A rendering failure that destroyed
source data was a real bug this month, and the fix was to make rendering
non-fatal precisely because the markdown is downstream.

The edges of a graph also already exist, and are already validated. Every
`Evidence` claim carries a source ref matching the grammar in
`casefile/schema.py`:

    labs:<analyte-slug>:<YYYY-MM-DD> | doc:<file>[#p<int>]
    encounter:<file> | pmid:<digits> | patient-report:<YYYY-MM-DD>

`IntakeFact.corroboration` links a patient-reported fact to the documents
and lab series that support or contradict it. Those are typed, directed
edges between hypotheses, facts, documents, encounters, and lab series.
They are written down. Nothing about adopting a graph database would create
information the system does not already have.

What is genuinely missing is the ability to **walk** those edges. Today a
ref is resolved one at a time, by the citation checker and the entailment
verifier, to answer "does this specific claim's source exist and support
it?" Nothing can answer "everything that touches the 2021 thyroid event",
or "which hypotheses depend on a document that a later document
contradicts", or "which facts have no corroboration at all."

The junk risk is real, but it is narrower than "markdown gets messy":

1. **Encounter proliferation.** Encounter filenames are slugified from
   model-written titles. There is no dedup. Two turns describing the same
   ER visit produce two encounter files with different names, and both
   become citable sources — so the same event can appear as independent
   corroboration of itself. A paragraph-length generated title also blew
   past the filesystem name limit (`OSError: [Errno 36]`), which is how the
   unbounded-title problem first surfaced.
2. **Context assembly by recency.** The context pack selects recent
   material. That is fine at today's volume and degrades predictably as
   facts accumulate: the relevant fact from 2021 loses to an irrelevant one
   from last week.

## Decision

**1. Git-plus-markdown remains the system of record.** No graph database.

The properties being bought are not query properties. Every change to the
case file is a git commit with provenance, so any statement can be traced
to the turn and model that produced it. History is diffable. The whole
store is a bundle that restores from S3 (ADR 0009). A doctor can be handed
`case/` and read it without our software. For a record intended to outlive
the app and be shown to clinicians who will never have access to this
system, those beat query elegance. A graph store would add a second system
of record, and the failure mode of two systems of record is that they
disagree.

**2. Add a derived link index in the existing `labs.sqlite`.**

A table of `(source_kind, source_id, ref_kind, ref_id, relation)` rebuilt
by parsing the committed YAML. Derived, never authoritative: droppable and
regenerable from git at any time, so it can never be the thing that is
wrong. This is the same posture the FTS5 index already takes toward
`document_text`.

It buys traversal — the queries above — and, more importantly, it is the
prerequisite for assembling context by **relevance** rather than recency,
which is the actual answer to "it will get junky over time." Junk is not a
storage-format problem; it is a retrieval problem.

**3. Deduplicate encounters on write.**

Match on `(date, type)` plus similarity of the summary, and update the
existing encounter rather than writing a second file. Same-event duplicates
are not merely untidy — they let one event corroborate itself, which
corrupts the corroboration signal that ADR 0013 depends on.

## Consequences

- Traversal queries become possible without changing what is authoritative,
  and the index can be deleted and rebuilt whenever the parse changes.
- The index must be rebuilt after any write to the ledger or intake facts,
  or it goes stale. Staleness is a wrong answer to a traversal query, not a
  wrong case file — bounded, but it needs a rebuild hook on the write path
  and a `--rebuild` command for after a restore.
- Encounter dedup means an encounter file can now be *updated* rather than
  only appended. Provenance stays intact because the update is a commit,
  but "one file, one turn" is no longer a property that holds.
- Relevance-ranked context assembly is enabled, not delivered. That is a
  separate change and should be measured with `adoc eval` against the
  current recency behavior before it is adopted.
- If traversal later turns out to want real graph algorithms (shortest
  path, centrality) rather than joins, this decision should be revisited.
  Nothing here forecloses that: the edges live in the YAML either way.

# 0003. Storage: two git repos (markdown + SQLite), no vector/graph DB in v1

Status: Accepted

## Context

At N=1 patient, the storage requirements are auditability, revertibility,
and the ability to correlate a small number (~100) of recurring lab
analytes over time — not large-scale retrieval. Research conclusion #4
(PLAN.md) found that for this scale, files + git + SQLite beat vector or
graph stores on every axis that matters here: a git history is a free,
human-diffable audit trail; SQLite FTS5 covers full-text search over
encounters/labs without an embedding pipeline; a vector or graph store adds
operational surface area (another service, another failure mode, another
thing to keep in sync with the git-tracked source of truth) without a
retrieval problem that actually requires it yet.

## Decision

State is split across **two git repositories**:

- The **code repo** (this one) — no PHI, has a GitHub remote, normal
  GitFlow.
- The **data repo** (`ADOC_DATA_DIR`, no remote) — the system of record:
  markdown case files, `differential-ledger.yaml`, immutable `sources/`
  (original documents + page images), and `labs.sqlite` (gitignored,
  rebuildable from a committed `labs-export.jsonl`).

Every mutation to the data repo is one git commit; weekly reviews are
tagged. SQLite + FTS5 is the only structured store in v1; no vector or
graph database is introduced until Phase 3 (Monarch KG as an independent
non-LLM differential engine) — and even then, `PLAN.md` explicitly notes
vectors are added only "if FTS5 demonstrably misses" (Phase 4).

## Consequences

- Every derived artifact must be rebuildable from immutable sources —
  `labs.sqlite` from `labs-export.jsonl` + git history, not the other way
  around. This is a required Phase-1 acceptance criterion.
- Because the data repo has no remote, CI and the code repo can never see
  real patient data — CI and code-repo tests use only synthetic fixtures
  under `tests/fixtures/` (CLAUDE.md rule 1).
- Schema changes to the ledger YAML or labs DDL are versioned migration
  scripts committed to the data repo itself, so state and software version
  travel together (PLAN.md "Provenance & re-evaluation policy").

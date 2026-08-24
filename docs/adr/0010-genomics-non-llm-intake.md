# 0010. Genomic files: archived and inventoried, never LLM-processed

Status: Accepted

## Context

Real-world intake for this patient includes genotype data: a 23andMe-style
raw text export (~17MB) and one BGZF-compressed `.bcf` file per chromosome
from an imputation service (up to ~34MB each, up to ~400MB across a full
set, often dropped as a single `.zip`). None of the existing intake kinds
fit this data:

- It is not a scanned/typed clinical document — running it through the
  vision/text double-pass extractor would be nonsensical (there is nothing
  to "read"), wasteful (hundreds of megabytes through a per-document LLM
  call), and a privacy overreach: raw genotype data is categorically more
  sensitive than lab values and clinical narrative, and PLAN.md's privacy
  section already treats "what gets sent to a model" as a deliberate,
  narrow surface — genotype data was never contemplated as part of it.
- One encounter per file, the normal per-document pattern, would produce
  20+ near-identical junk encounters (one per chromosome's `.bcf`) with
  nothing for a human or the reasoner to usefully read.
- The data repo's git history is meant to stay small (a handful of
  markdown/YAML files and PDFs) — committing hundreds of megabytes of raw
  genotype files into it would bloat every clone/bundle operation
  (including `adoc backup`'s git bundle) indefinitely.

## Decision

Genomic files get their own intake kind, detected by content
(`ingest/filetypes.py`'s `detect_intake_kind`, never by filename alone):
a 23andMe-style raw text export (sniffed from a leading `#`-commented
block + `rsid`-header line in the first ~2KB), a BGZF/gzip file with a
`.bcf`/`.vcf.gz`/`.bam`/`.fastq.gz`/`.fq.gz` suffix, a plain `.vcf`
(`##fileformat=VCF` header), or a plain FASTQ (`.fastq`/`.fq`, starting
with `@`).

A genomic file is:

1. **Archived byte-for-byte**, unmodified, under
   `sources/genomics/<sha256>__<original-filename>` (`ingest/genomics.py`)
   — sha256-addressed like every other archived source, so re-ingesting
   the same file is a no-op.
2. **Excluded from the data repo's git history.** `DataRepo`'s
   `.gitignore` gets a `sources/genomics/` line added lazily on first use
   (`_ensure_gitignore_excludes_genomics`) — the raw bytes live on disk
   under the data directory, but are never committed.
3. **Still backed up to S3.** `adoc backup`'s `_sync_sources` walks the
   `sources/` tree on disk, not git-tracked paths, so `sources/genomics/`
   is uploaded to `s3://$ADOC_BACKUP_BUCKET/latest/sources/` exactly like
   any other source document, and `adoc restore` brings it back the same
   way. Gitignored and S3-backed are independent, deliberately: git
   history stays small; the patient's data is still durably protected.
4. **Never read as a document, ever.** No vision call and no text
   extraction call is made against a genomic file's content — this is a
   CRITICAL DESIGN RULE, not merely today's default. There is no code
   path from a genomic file's bytes into any LLM request.
5. **Folded into one regenerated summary**, `case/genomics-inventory.md`
   (`regenerate_inventory`) — a table of every archived genomic file (name,
   sha256, size, ingested-at) plus a fixed explanatory paragraph on what
   having genotype data on file enables (and does not, yet: no variant
   analysis exists in this phase). This is what the reasoner and a human
   actually see, instead of 20+ near-duplicate junk encounters.
6. Given `documents.doc_type = "genomic_data"` in `labs.sqlite` — a plain
   string value (that table has no CHECK constraint on `doc_type`), so
   introducing it needed no schema migration.

## Consequences

- Adding a genuine genomics-analysis feature later (e.g. Exomiser/Phen2Gene
  against a VCF, per PLAN.md Phase 4) is additive: it would read directly
  from `sources/genomics/`, still with no LLM in the loop for the raw
  file content itself — this ADR's "never LLM-processed" rule constrains
  the *intake* path, not a future deterministic genomics engine.
- `sources/genomics/` growing to hundreds of megabytes never affects
  `adoc backup`'s git-bundle size or `git clone`/`fetch` performance on the
  data repo, because it was never committed in the first place.
- A reviewer auditing "what has this system ever sent to a model" can
  answer definitively for genomic files: nothing, by construction
  (`ingest/genomics.py`'s module docstring states this explicitly and
  `ingest/pipeline.py` routes `"genomic"`-kind files around every
  LLM-calling branch).

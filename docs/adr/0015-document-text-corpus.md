# 0015. Document-text corpus: extraction, storage, retrieval

Status: Accepted

## Context

Ingested documents were, until now, visible to the reasoner and the intake
agent only as METADATA — filename, date, doc type. A patient's own written
history (`.docx`, ingested per ADR 0008), and the narrative content inside
lab reports and imaging/consult PDFs (interpretive comments, radiology
impressions, pathology narratives), were never captured as structured
lab rows and were invisible to every conversation. The intake agent could
not reference a word of a document the patient had already provided, and
could ask her to retype what she'd already written.

The product owner's direction: extend this to "text in labs and reports as
well," not just the one narrative document type — a general document-text
layer, not a docx-specific fix.

Four design questions had to be settled: where the text lives, how it's
extracted, how genomics stays excluded, and how retrieval avoids blowing
the context budget.

## Decision

### 1. Storage location: a new top-level `doc-text/`, committed

Extracted text is written verbatim to `doc-text/<sha256>.txt`, one file per
document, keyed by the same sha256 `sources/` uses. `doc-text/` is a new
entry in `casefile.repo.DataRepo._TOP_LEVEL_DIRS`, created (with a
`.gitkeep`, like `sources/`) at `DataRepo.init_at`, and is **NOT**
gitignored — unlike `sources/genomics/`, it is committed.

Rejected alternatives:
- **Inside `sources/`**: `sources/`'s docstring and every existing comment
  in the codebase state it holds immutable ORIGINALS only. Extracted text
  is derived (re-running extraction reproduces it byte-for-byte, modulo
  `pdftotext`'s own determinism), so mixing it into `sources/` would blur
  a distinction the rest of the codebase already relies on (e.g. `adoc
  backup`'s `_sync_sources` walks `sources/` assuming everything there is
  an original to preserve, not a derived artifact to potentially
  regenerate).
- **`labs.sqlite` only, uncommitted**: `labs.sqlite` is gitignored and
  explicitly documented as a *derived* artifact, rebuilt from committed
  files (`labs-export.jsonl` for labs/documents). Storing the only copy of
  extracted text in sqlite would mean re-deriving 121+ documents' worth of
  text (a real `pdftotext`/`python-docx` pass over every archived original)
  on every fresh checkout or restore — not free, and not human-diffable
  (`git diff` over a `.sqlite` file is useless; over a `.txt` file it's the
  whole point of a "system of record" data repo per PLAN.md "State").

`doc-text/` being committed means the git bundle `adoc backup` already
produced (`_bundle_data_repo`) carries it in full — no new S3 sync leg was
needed in `backup.py`, unlike `sources/genomics/`'s gitignored/S3-only
split (ADR 0010). `labs.sqlite`'s `document_text`/`document_text_fts`
tables are the derived, rebuildable side: `ingest.doctext.
rebuild_document_text_from_files` repopulates them from the committed
`.txt` files, wired into `restore_from_bucket` right after `LabsDb.
rebuild_from_jsonl` rebuilds `documents`/`labs` (the same "sqlite is
derived, rebuild from committed files" property PLAN.md "State" already
promises for labs).

### 2. Extraction: `pdftotext`, not a second LLM pass

PDF text extraction shells out to `pdftotext -layout` (poppler-utils,
already a required system binary for `ingest.archive.pdftoppm_renderer`) —
free, fast, and deterministic, versus spending a vision/LLM call per
document purely to re-derive text that's usually already embedded in the
PDF's content stream. `.docx` reuses `ingest.docx.extract_docx_text`
(already deterministic, ADR 0008); `.txt`/`.md` is read verbatim. None of
the three calls a model — this mirrors ADR 0008's "docx is transcription,
not interpretation" principle, generalized to every non-genomic kind.

`ingest.doctext.PdfTextExtractor` is an injection seam (mirrors
`ingest.archive.PageRenderer`): a missing `pdftotext` binary is logged and
returns `None` rather than raising, so tests never depend on poppler being
installed, and a production environment without `pdftotext` degrades to
"no text layer" rather than failing ingest.

### 3. Genomics stays excluded — structurally, not by convention

ADR 0010's rule ("no vision or text extraction call is ever made against
[a genomic file]") is unchanged and this feature does not touch it. Two
independent guarantees:

- **Type-level**: `ingest.doctext.extract_text_for_kind`'s `kind` parameter
  is `ingest.archive.DocKind`, a `Literal["pdf", "docx", "text"]` with no
  `"genomic"` member. There is no value of that type a genomic file's
  archival path (`ingest.genomics.archive_genomic_file`, which never
  produces a `DocKind`) could ever produce — the exclusion is enforced by
  the type checker, not by a runtime `if`.
- **Call-site**: `ingest.pipeline._ingest_one` routes a `"genomic"`-kind
  file to `_ingest_genomic` *before* `archive_document` (and so the new
  `_extract_and_store_text_best_effort` call) is ever reached — unchanged
  from ADR 0010's existing control flow, just re-verified for this feature.
- **Backfill-level**: `ingest.doctext.backfill_document_text` skips every
  `documents` row with `doc_type == GENOMIC_DOC_TYPE` before it ever looks
  at that document's archived bytes, so a genomic document ingested before
  this feature existed can never retroactively gain a text-extraction call
  via `adoc backfill-doc-text`.

Tested directly: `test_ingest_doctext.py::test_genomic_documents_are_never_extracted`
ingests a genomic file, runs the backfill sweep, and asserts zero
`document_text` rows and zero calls into a `pdf_extractor` stub that raises
if ever invoked (mirrors `test_ingest_pipeline.py`'s existing
"exploding vision client" pattern for the same guarantee at the ingest
layer).

### 4. Retrieval: FTS5, ranked snippets, a hard character cap

`labs.sqlite` gains a `document_text` table (one row per page for a
paginated PDF — see below — one page-less row for docx/text) and a
`document_text_fts` FTS5 index, mirroring `labs_fts`'s existing
external-content-table pattern exactly (migration 3 in `labs/db.py`).
`LabsDb.search_document_text(query, limit=...)` returns ranked
`DocumentTextHit`s using sqlite's own `snippet()` function — never
hand-rolled truncation — each carrying a `doc:<filename>#p<page>`-style
`source_ref` (PLAN.md's existing source-ref grammar).

**Pagination**: `pdftotext`'s default output separates pages with a
form-feed (`\f`). `ingest.doctext._split_pages` keys off that character
alone: present, split into 1-indexed page rows; absent (docx, plain text,
or — `pdftotext` emits no leading/trailing form feed — a single-page PDF),
store one `page=None` row. This is a deliberate simplification: a
single-page PDF's citation renders as document-level (`doc:<filename>`)
rather than `doc:<filename>#p1`. The alternative (tracking a document's
kind explicitly to disambiguate) was rejected because `documents` doesn't
currently persist archival `kind` (`pdf`/`docx`/`text` — only `doc_type`,
a different, clinical classification) and because this exact same rule
drives BOTH the initial write (`store_document_text`) and a from-scratch
rebuild (`rebuild_document_text_from_files`) — they can never disagree
because they are the same code applied to the same stored text, which is
worth more than perfect page attribution for the one-page case.

**121+ documents of full text cannot go into a prompt.** Two context
surfaces, both capped, both query-dependent (a turn with no relevant match
adds nothing):
- `reason/context.py`'s `build_context` gained an optional `query`
  parameter (default `None`, so every existing caller is unaffected). When
  given (diagnostic turns pass the patient's raw turn text), it appends a
  "Relevant Document Excerpts" section — ranked snippets, quoted VERBATIM
  with their source ref, total capped at `MAX_DOCUMENT_EXCERPT_CHARS`
  (4000 characters, a module constant). Placed LAST in the fixed section
  order (after the ledger), deliberately: every other section is fully
  determined by repo/db state alone, so the one query-dependent,
  per-turn-variable section sits where its variability can never
  invalidate a prompt-cache prefix built over the stable sections before
  it.
- `reason/tools.py` gained `search_documents(db, query)`, a deterministic
  (no-LLM) helper alongside the existing `query_labs`/`search_case`, folded
  into the informational-turn MVP tool loop's always-run retrieval block —
  so an informational question can surface relevant document text
  on-demand exactly the way it already surfaces lab values and case-file
  grep hits.
- `intake/agent.py`'s turn context gained a "Relevant excerpts from her
  own prior documents" section, retrieved against the CURRENT patient
  message (`DOC_EXCERPT_LIMIT=4`, `DOC_EXCERPT_MAX_CHARS=2000` — a smaller
  budget than the diagnostic context's, since the intake turn context is
  already large). `INTAKE_AGENT_PROMPT_VERSION` bumped 4→5: the system
  prompt gained a "DOCUMENT EXCERPTS" section instructing the model these
  are the patient's own prior words — reference them, don't re-ask, and
  never name the retrieval mechanism to the patient.

Never paraphrased into any context: the point (module docstrings, this
ADR) is verbatim text a model can cite and a later verifier can check —
this is explicitly what unlocks Phase 2's "claim-level entailment
verifier" (PLAN.md Phase 2) checking a patient-facing claim against real
source text, not just a metadata match.

### 5. Ingest wiring, backfill, UI

`ingest.pipeline._extract_and_store_text_best_effort` runs once per newly
archived (non-duplicate, non-genomic) document, immediately after
archival, covering pdf/docx/text uniformly via `extract_text_for_kind`.
It **never fails an ingest** — any exception is caught, logged, and
ingest proceeds exactly as if the call had never been made (lab-row
extraction remains the primary job, per the module docstring). `adoc
backfill-doc-text` (`ingest.doctext.backfill_document_text`) covers
already-ingested documents that predate this feature or whose extraction
failed the first time — idempotent, no LLM calls, reports counts
(checked / already-covered / extracted / skipped-no-source /
skipped-genomic).

"Documents → Consumed" gained a "Text" column: a document with stored text
links to `/documents/consumed/{sha}/text`, a read-only page rendering the
verbatim extracted text. A document with none (extraction never ran,
failed, or — always — a genomic document) shows a plain dash.

## Consequences

- One new committed top-level directory (`doc-text/`); `_TOP_LEVEL_DIRS`
  and `DataRepo.init_at`'s initial commit both updated. Existing (older)
  data repos self-heal lazily — `store_document_text` creates the
  directory on first write, the same pattern `ingest.genomics.
  archive_genomic_file`'s `_ensure_gitignore_excludes_genomics` already
  uses for a different lazy-repair case.
- One new `labs.sqlite` migration (`document_text`/`document_text_fts`,
  migration 3) — additive, no existing table touched.
- `adoc backfill-doc-text` is a new, 17th CLI subcommand (CLAUDE.md
  updated).
- No new runtime Python dependency — `pdftotext` is an existing system
  binary already required for page-image rendering; `python-docx` was
  already a dependency (ADR 0008).
- Retrieval is deliberately conservative on relevance (plain FTS5 ranking,
  no semantic/embedding search) and on budget (hard character caps) —
  consistent with PLAN.md's "SQLite FTS5 before vectors" decision and this
  being a single-patient corpus small enough that FTS5 recall is
  sufficient without a vector store.

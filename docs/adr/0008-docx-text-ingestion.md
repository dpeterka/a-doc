# 0008. `.docx` ingested as TEXT, not converted to PDF/images

Status: Accepted

## Context

Real Dropbox drops contain `.docx` narrative documents (clinical history
the patient wrote themselves, supplement plans) alongside scanned PDFs.
`ingest/archive.py`'s `%PDF-` magic gate rejected every `.docx` outright —
it never reached archival, classification, or extraction.

Two designs were considered for supporting `.docx`:

1. Convert to PDF (via LibreOffice headless or similar) and run it through
   the existing PDF/vision pipeline unchanged.
2. Read the `.docx` directly as text and add a parallel TEXT path.

Option 1 adds a new system dependency (LibreOffice) to the container image
purely to re-derive text that is already cleanly structured in the
`.docx`'s XML — a lossy, heavier detour (render to PDF, then re-OCR/vision
the render) for a format that was never scanned in the first place.

## Decision

`.docx` is ingested as **TEXT**, never converted to PDF/images:

- **Detection** (`ingest/filetypes.py`, `detect_doc_kind`): content-based,
  not filename-based — `pdf` requires the `%PDF-` magic; `docx` requires
  the zip local-file-header magic AND a `.docx` suffix AND a real
  `[Content_Types].xml` entry in the zip's central directory (stdlib
  `zipfile`, no new dependency). Anything else is unsupported.
- **Archival** (`ingest/archive.py`): a `.docx` is archived immutably
  exactly like a PDF (sha256 dedupe, copy into `sources/`), but with **no
  page rendering** — `ArchivedDoc.page_paths == []`, `ArchivedDoc.kind ==
  "docx"`.
- **Extraction** (`ingest/docx.py`, `extract_docx_text`): a new pure-Python
  runtime dependency, `python-docx` — no LibreOffice, no PDF conversion.
  Paragraphs in document order, tables rendered as pipe-delimited rows;
  deterministic, no LLM, exactly like the vision extractors are meant to
  transcribe rather than interpret.
- **Classification and lab extraction** (`ingest/pipeline.py`,
  `ingest/extract.py`): the extracted text is classified via the
  `classifier` role and, if lab-classified, run through a cross-model TEXT
  double-pass (`double_pass_extract_text`, new docx-specific prompts,
  versioned like the vision prompts) using `LlmClient.complete` directly —
  `extractor_pass_a`/`extractor_pass_b`'s bound models (`claude-sonnet-5`,
  `gpt-5.2`) both handle plain text natively, so no vision call is made.
  The resulting rows flow through the *same* `reconcile`/insert/export
  gates a PDF lab report uses (`results[].page` always resolves to `1` —
  a docx has no page structure).
- **Narrative documents** (non-lab-classified `.docx`): become a full-text
  encounter — `casefile/encounters.py`'s `Encounter` gained an optional
  `extracted_text` field, rendered as a trailing `## Extracted text`
  section carrying the complete transcription (PLAN.md's context pack
  needs the full narrative, not a summary). Frontmatter `type` maps
  `imaging_report -> imaging`, everything else -> `patient-report` (a docx
  narrative has no clinician letterhead behind it; it reads as something
  the patient wrote or assembled, PLAN.md's "same door as doctor notes,
  labeled").
- **`VisionClient.client`**: a new public property exposing the wrapped
  `LlmClient`, so `ingest/pipeline.py`'s docx path can call
  `LlmClient.complete` directly without a second client being constructed
  or threaded through every signature.
- **Confirm queue**: a pending row whose source document has no page image
  (always true for a docx-sourced row) renders a text-fallback panel
  ("Text document — no page image; showing extracted text context
  instead...") rather than a broken `<img>`.

## Consequences

- One new runtime dependency, `python-docx` (pure Python — no change to
  the Dockerfile beyond `uv.lock`).
- `ArchivedDoc` gained a `kind: DocKind = "pdf"` field (defaulted, so
  existing construction sites are unaffected).
- `Encounter` gained an `extracted_text: str = ""` field; existing
  encounter files round-trip unchanged (the section is only emitted when
  non-empty).
- A lab-classified `.docx`'s rows carry no page-image provenance in the
  confirm queue — reviewers cross-check against the row's own extracted
  fields (and, once wired into the context pack, the encounter's full
  text) instead of a source-page image.

"""Cross-model double-pass extraction (PLAN.md "Ingestion", session loop (a)).

Pass A sends the archived PDF bytes as a single `PdfPart` to the
`extractor_pass_a` role (Anthropic, PDF-native, `models.yaml`). Pass B
sends the rendered page PNGs as `ImagePart`s to the `extractor_pass_b` role
(OpenAI-family - PDFs are never sent to it, see `vision.py`), under a
*differently-framed* prompt: pass A reads the document as a whole, pass B
is explicitly told it is transcribing independently, page by page, from
images, to avoid correlated same-framing errors between the two passes
(PLAN.md building-blocks note: "cross-model double-pass makes correlated
extraction errors far less likely than same-model-twice").

Both prompt texts are versioned module constants (provenance: PLAN.md
"Provenance & re-evaluation policy" - `prompt_template_version`).
"""

from __future__ import annotations

from adoc.ingest.archive import ArchivedDoc
from adoc.ingest.schema import DocumentExtraction
from adoc.ingest.vision import ImagePart, PdfPart, TextPart, VisionClient
from adoc.reason.client import LlmClient, Message

PROMPT_A_VERSION = "extractor-pass-a-v4"
PROMPT_A = f"""[{PROMPT_A_VERSION}]
You are extracting structured data from ONE scanned/printed medical
document (a lab report, clinical note, imaging report, or other document),
provided below as a PDF. Read every page.

Return a single DocumentExtraction:
- doc_type: your best classification (lab_report / clinical_note /
  imaging_report / other).
- facility, collection_date, report_date: from the document header, if
  present (use ISO dates; omit a field if it is absent or illegible).
- results: EVERY discrete lab analyte/result row you can find, each with
  the exact name as printed (name_raw), its numeric value (value) or
  textual value such as a titer or "positive"/"negative" (value_text), the
  unit as printed (unit_raw), the reference range as printed
  (ref_range_raw), any H/L/HH/LL/A flag as printed (flag_raw), the
  specimen it was drawn from (specimen), the 1-indexed page it appears on,
  and your confidence (high/medium/low) in this specific row. Give each
  result a concise, canonical test name in name_raw - never a sentence
  fragment and never a name ending in a verb like "... is" or "... was"
  (e.g. write "Potassium", not "Potassium level is"). A site/location
  prefix is fine and expected when the report distinguishes sites (e.g.
  "LEFT HIP", "L1-L4"). For a bone-density (DEXA) result, name it
  "<SITE> T-Score" or "<SITE> Z-Score" (e.g. "LEFT HIP femoral neck
  T-Score"); name a FRAX result in full, exactly as its own probability
  type is described (e.g. "FRAX 10-year probability of hip fracture",
  "FRAX 10-year probability of major osteoporotic fracture") - always keep
  the leading "FRAX". Do NOT emit derived interpretation/severity
columns as their own result rows: when a panel prints both a
measurement and an interpretation band for the same item (e.g. an
allergen IgE panel's "Class" column of 0-VI bands beside each
allergen's kU/L value), emit ONLY the measured row (the allergen's
IgE in kU/L, with its flag) - the band is derivable from the value
and is not a measurement.
- specimen: record this PER RESULT ROW, from the report's own section
  header or label immediately governing that row (e.g. a "URINALYSIS"
  section -> urine; a "Stool"/"Stool Culture" section -> stool; a serum
  chemistry panel such as a CMP/BMP, or any panel with no specimen stated
  otherwise -> serum; CBC panels are typically whole_blood; a CSF/spinal
  fluid analysis -> csf; a saliva panel -> saliva. Use one of: serum,
  plasma, whole_blood, urine, stool, csf, saliva, other. If the document
  does not state or clearly imply a specimen for a row, use "unknown" -
  never guess. This matters: a document with BOTH a urinalysis and a
  serum panel can print the SAME analyte name (e.g. "GLUCOSE") under each
  section, and these are two different results that must not be conflated.
- narrative_findings: free-text clinical observations or serology comments
  that are not discrete result rows (these matter for autoimmune workups -
  do not skip them).
- illegible_regions: page + a short description for any region you could
  not read with confidence.

Transcribe exactly what is printed. Do not infer, normalize, or guess a
value you cannot read - mark it illegible instead. Never fabricate a
result that is not present in the document.
"""

PROMPT_B_VERSION = "extractor-pass-b-v4"
PROMPT_B = f"""[{PROMPT_B_VERSION}]
You are independently re-transcribing ONE medical document from a sequence
of page images (one image per page, in the order given). This is a SECOND,
INDEPENDENT extraction pass used to cross-check a separate model's reading
of the same document - do not assume any other reading is correct; read
the images fresh, from scratch.

Work through the images in order. For every result row, table entry, or
discrete lab value visible on any page, emit one entry in `results` with:
the label exactly as printed (name_raw), the numeric (value) or textual
(value_text) reading, the printed unit (unit_raw), the printed reference
interval (ref_range_raw), any abnormal flag as printed (flag_raw), the
specimen it was drawn from (specimen), the page number matching that
image's position in the sequence (1-indexed), and a high/medium/low
confidence for that specific reading based on how legible the image is.
Give each result a concise, canonical test name in name_raw - never a
sentence fragment and never a name ending in a verb like "... is" or
"... was" (e.g. write "Potassium", not "Potassium level is"). A
site/location prefix is fine and expected when the report distinguishes
sites (e.g. "LEFT HIP", "L1-L4"). For a bone-density (DEXA) result, name
it "<SITE> T-Score" or "<SITE> Z-Score" (e.g. "LEFT HIP femoral neck
T-Score"); name a FRAX result in full, exactly as its own probability type
is described (e.g. "FRAX 10-year probability of hip fracture", "FRAX
10-year probability of major osteoporotic fracture") - always keep the
leading "FRAX". Do NOT emit derived interpretation/severity
columns as their own result rows: when a panel prints both a
measurement and an interpretation band for the same item (e.g. an
allergen IgE panel's "Class" column of 0-VI bands beside each
allergen's kU/L value), emit ONLY the measured row (the allergen's
IgE in kU/L, with its flag) - the band is derivable from the value
and is not a measurement.

For specimen, use the section header or label on the page that governs
that row (e.g. "URINALYSIS" -> urine; "Stool"/"Stool Culture" -> stool; a
serum chemistry panel, or an unlabeled panel -> serum; a CBC panel ->
whole_blood; a CSF/spinal fluid analysis -> csf; a saliva panel -> saliva;
anything else clearly stated -> other). Use "unknown" if the image gives
you no basis to tell - never guess. The same analyte name can legitimately
appear twice in one document under different specimens (e.g. "GLUCOSE" in
both a urinalysis section and a serum panel) - record each occurrence's
own specimen rather than assuming they are the same result.

Also set doc_type, facility, collection_date, and report_date from
whichever page(s) show them; collect any narrative or serology commentary
in narrative_findings; and list any page/region you genuinely cannot read
in illegible_regions with a short description (e.g. "page 2, bottom table,
smudged"). If a digit is blurry or ambiguous, prefer confidence=low over
guessing a specific reading.
"""


# Dense multi-page lab panels produce large JSON output; a low token limit
# can silently truncate a panel down to its first row or two. Sized for the
# largest panels seen plus headroom; truncation is also detected hard in
# vision.py via stop_reason.
EXTRACTION_MAX_TOKENS = 16384


def double_pass_extract(
    vision: VisionClient, archived: ArchivedDoc
) -> tuple[DocumentExtraction, DocumentExtraction]:
    """Run both extraction passes over `archived`. Returns `(pass_a, pass_b)`."""
    pass_a = vision.extract(
        "extractor_pass_a",
        system=PROMPT_A,
        parts=[
            PdfPart(
                data=archived.original_path.read_bytes(),
                filename=archived.original_path.name,
            )
        ],
        schema=DocumentExtraction,
        max_tokens=EXTRACTION_MAX_TOKENS,
    )

    page_parts: list[TextPart | ImagePart] = []
    total = len(archived.page_paths)
    for index, page_path in enumerate(archived.page_paths, start=1):
        page_parts.append(TextPart(text=f"Page {index} of {total}:"))
        page_parts.append(ImagePart(data=page_path.read_bytes(), page=index))

    pass_b = vision.extract(
        "extractor_pass_b",
        system=PROMPT_B,
        parts=page_parts,
        schema=DocumentExtraction,
        max_tokens=EXTRACTION_MAX_TOKENS,
    )

    return pass_a, pass_b


# --------------------------------------------------------------------------
# docx text double-pass (PLAN.md docx ingestion design decision: docx = TEXT
# documents; no vision needed - `extractor_pass_a`/`extractor_pass_b`'s
# bound models, claude-sonnet-5 and gpt-5.2, both handle plain text
# natively, so this goes through `LlmClient.complete` rather than
# `VisionClient.extract`).
#
# Reused as-is (not duplicated) for plain `.txt`/`.md` text documents too
# (genomics/filetypes task, item 3) - `ingest.pipeline._ingest_text_like`
# calls `double_pass_extract_text` for both docx and plain-text intake; the
# prompts below are already generic over "plain text", nothing docx-
# specific in the wording, so no rename was needed.
# --------------------------------------------------------------------------

DOCX_PROMPT_A_VERSION = "docx-extractor-pass-a-v4"
DOCX_PROMPT_A = f"""[{DOCX_PROMPT_A_VERSION}]
You are extracting structured data from ONE .docx document, provided below
as its full plain text (paragraphs in reading order; any tables rendered
as pipe-delimited rows). This document has no page images - it was
authored directly as text (a narrative document or a lab report exported
to Word), not scanned.

Return a single DocumentExtraction:
- doc_type: your best classification (lab_report / clinical_note /
  imaging_report / other).
- facility, collection_date, report_date: from the text, if mentioned (use
  ISO dates; omit a field if it is absent).
- results: EVERY discrete lab analyte/result row you can find in the text
  or in a rendered table, each with the exact name as written (name_raw),
  its numeric value (value) or textual value such as a titer or
  "positive"/"negative" (value_text), the unit as written (unit_raw), the
  reference range as written (ref_range_raw), any H/L/HH/LL/A flag as
  written (flag_raw), the specimen it was drawn from (specimen), `page`
  set to 1 for every row (this document has no page structure to report),
  and your confidence (high/medium/low) in this specific row. Give each
  result a concise, canonical test name in name_raw - never a sentence
  fragment and never a name ending in a verb like "... is" or "... was"
  (e.g. write "Potassium", not "Potassium level is"). A site/location
  prefix is fine and expected when the text distinguishes sites (e.g.
  "LEFT HIP", "L1-L4"). For a bone-density (DEXA) result, name it
  "<SITE> T-Score" or "<SITE> Z-Score" (e.g. "LEFT HIP femoral neck
  T-Score"); name a FRAX result in full, exactly as its own probability
  type is described (e.g. "FRAX 10-year probability of hip fracture",
  "FRAX 10-year probability of major osteoporotic fracture") - always keep
  the leading "FRAX". Do NOT emit derived interpretation/severity
columns as their own result rows: when a panel prints both a
measurement and an interpretation band for the same item (e.g. an
allergen IgE panel's "Class" column of 0-VI bands beside each
allergen's kU/L value), emit ONLY the measured row (the allergen's
IgE in kU/L, with its flag) - the band is derivable from the value
and is not a measurement.
- specimen: record this PER RESULT ROW, from whichever section heading or
  label in the text governs that row (e.g. an "Urinalysis" heading ->
  urine; a "Stool"/"Stool Culture" heading -> stool; a serum chemistry
  panel, or a panel with no specimen stated -> serum; a CBC panel ->
  whole_blood; a CSF/spinal fluid analysis -> csf; a saliva panel ->
  saliva). Use one of: serum, plasma, whole_blood, urine, stool, csf,
  saliva, other. If the text does not state or clearly imply a specimen
  for a row, use "unknown" - never guess. The same analyte name can
  legitimately appear twice in one document under different specimens
  (e.g. "GLUCOSE" under both a urinalysis section and a serum panel) -
  record each occurrence's own specimen rather than assuming they are the
  same result.
- narrative_findings: free-text clinical observations or serology comments
  that are not discrete result rows (these matter for autoimmune workups -
  do not skip them).
- illegible_regions: leave empty unless the text itself is garbled or
  truncated - there is no scan quality to assess in a text document.

Transcribe exactly what is written. Do not infer, normalize, or guess a
value that is not in the text - never fabricate a result that is not
present in the document.
"""

DOCX_PROMPT_B_VERSION = "docx-extractor-pass-b-v4"
DOCX_PROMPT_B = f"""[{DOCX_PROMPT_B_VERSION}]
You are independently re-reading the SAME .docx document's extracted text
a SECOND, INDEPENDENT time. This is used to cross-check a separate model's
reading of the same text - do not assume any other reading is correct;
read the text fresh, from scratch, as if this were the first time you had
seen it.

Work through the text top to bottom. For every result row, table entry, or
discrete lab value you find, emit one entry in `results` with: the label
exactly as written (name_raw), the numeric (value) or textual (value_text)
reading, the unit as written (unit_raw), the reference interval as written
(ref_range_raw), any abnormal flag as written (flag_raw), the specimen it
was drawn from (specimen), `page` set to 1 for every row (this document
has no page structure to report), and a high/medium/low confidence for
that specific reading based on how unambiguous the text is. Give each
result a concise, canonical test name in name_raw - never a sentence
fragment and never a name ending in a verb like "... is" or "... was"
(e.g. write "Potassium", not "Potassium level is"). A site/location
prefix is fine and expected when the text distinguishes sites (e.g.
"LEFT HIP", "L1-L4"). For a bone-density (DEXA) result, name it "<SITE>
T-Score" or "<SITE> Z-Score" (e.g. "LEFT HIP femoral neck T-Score"); name
a FRAX result in full, exactly as its own probability type is described
(e.g. "FRAX 10-year probability of hip fracture", "FRAX 10-year
probability of major osteoporotic fracture") - always keep the leading
"FRAX". Do NOT emit derived interpretation/severity
columns as their own result rows: when a panel prints both a
measurement and an interpretation band for the same item (e.g. an
allergen IgE panel's "Class" column of 0-VI bands beside each
allergen's kU/L value), emit ONLY the measured row (the allergen's
IgE in kU/L, with its flag) - the band is derivable from the value
and is not a measurement.

For specimen, use whichever section heading or label in the text governs
that row (e.g. "Urinalysis" -> urine; "Stool"/"Stool Culture" -> stool; a
serum chemistry panel, or an unlabeled panel -> serum; a CBC panel ->
whole_blood; a CSF/spinal fluid analysis -> csf; a saliva panel -> saliva;
anything else clearly stated -> other). Use "unknown" if the text gives
you no basis to tell - never guess. The same analyte name can legitimately
appear twice in one document under different specimens (e.g. "GLUCOSE"
under both a urinalysis section and a serum panel) - record each
occurrence's own specimen rather than assuming they are the same result.

Also set doc_type, facility, collection_date, and report_date from
wherever the text states them; collect any narrative or serology
commentary in narrative_findings; leave illegible_regions empty unless the
text itself is genuinely garbled or truncated. If a value is ambiguous,
prefer confidence=low over guessing a specific reading.
"""


def double_pass_extract_text(
    client: LlmClient, text: str
) -> tuple[DocumentExtraction, DocumentExtraction]:
    """Cross-model double-pass extraction over a `.docx` document's plain
    text (see `ingest.docx.extract_docx_text`). Both passes go through
    `LlmClient.complete` - a docx has no binary pages to send, and the
    `extractor_pass_a`/`extractor_pass_b` roles' bound models handle plain
    text natively. `results[].page` always resolves to 1 (see the prompts
    above) - a docx has no page structure to report.
    """
    pass_a = client.complete(
        "extractor_pass_a",
        system=DOCX_PROMPT_A,
        messages=[Message(role="user", content=text)],
        schema=DocumentExtraction,
        max_tokens=EXTRACTION_MAX_TOKENS,
    ).parsed
    pass_b = client.complete(
        "extractor_pass_b",
        system=DOCX_PROMPT_B,
        messages=[Message(role="user", content=text)],
        schema=DocumentExtraction,
        max_tokens=EXTRACTION_MAX_TOKENS,
    ).parsed
    assert isinstance(pass_a, DocumentExtraction)  # schema= guarantees this
    assert isinstance(pass_b, DocumentExtraction)
    return pass_a, pass_b

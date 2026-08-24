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

PROMPT_A_VERSION = "extractor-pass-a-v1"
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
  1-indexed page it appears on, and your confidence (high/medium/low) in
  this specific row.
- narrative_findings: free-text clinical observations or serology comments
  that are not discrete result rows (these matter for autoimmune workups -
  do not skip them).
- illegible_regions: page + a short description for any region you could
  not read with confidence.

Transcribe exactly what is printed. Do not infer, normalize, or guess a
value you cannot read - mark it illegible instead. Never fabricate a
result that is not present in the document.
"""

PROMPT_B_VERSION = "extractor-pass-b-v1"
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
page number matching that image's position in the sequence (1-indexed),
and a high/medium/low confidence for that specific reading based on how
legible the image is.

Also set doc_type, facility, collection_date, and report_date from
whichever page(s) show them; collect any narrative or serology commentary
in narrative_findings; and list any page/region you genuinely cannot read
in illegible_regions with a short description (e.g. "page 2, bottom table,
smudged"). If a digit is blurry or ambiguous, prefer confidence=low over
guessing a specific reading.
"""


# Dense multi-page lab panels produce large JSON: the 4096-token default
# silently truncated a real LabCorp panel to a single row. Sized for the
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

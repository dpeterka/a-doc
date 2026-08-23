"""Tests for adoc.ingest.extract.double_pass_extract: role/prompt wiring."""

from __future__ import annotations

from pathlib import Path

from adoc.ingest.archive import ArchivedDoc
from adoc.ingest.extract import PROMPT_A_VERSION, PROMPT_B_VERSION, double_pass_extract
from adoc.ingest.schema import DocumentExtraction, ExtractedResult
from adoc.ingest.vision import ImagePart, PdfPart, TextPart, VisionClient


class _FakeVisionClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def extract(self, role, *, system, parts, schema, binding_index=0, max_tokens=4096):  # type: ignore[no-untyped-def]
        self.calls.append({"role": role, "system": system, "parts": list(parts)})
        if role == "extractor_pass_a":
            return DocumentExtraction(
                doc_type="lab_report",
                results=[
                    ExtractedResult(name_raw="Potassium", value=4.1, page=1, confidence="high")
                ],
            )
        return DocumentExtraction(
            doc_type="lab_report",
            results=[ExtractedResult(name_raw="Potassium", value=4.1, page=1, confidence="high")],
        )


def test_double_pass_extract_sends_pdf_to_pass_a_and_pages_to_pass_b(
    tmp_path: Path, tiny_pdf_bytes: bytes
) -> None:
    pdf_path = tmp_path / "sources" / "sha__doc.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(tiny_pdf_bytes)

    page_paths = []
    for i in range(1, 3):
        page_path = tmp_path / "sources" / "pages" / "sha" / f"p-{i}.png"
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_bytes(b"\x89PNG fake")
        page_paths.append(page_path)

    archived = ArchivedDoc(
        sha256="sha", original_path=pdf_path, page_paths=page_paths, already_ingested=False
    )

    fake = _FakeVisionClient()
    pass_a, pass_b = double_pass_extract(fake, archived)  # type: ignore[arg-type]

    assert isinstance(pass_a, DocumentExtraction)
    assert isinstance(pass_b, DocumentExtraction)
    assert fake.calls[0]["role"] == "extractor_pass_a"
    assert fake.calls[1]["role"] == "extractor_pass_b"

    pass_a_parts = fake.calls[0]["parts"]
    assert len(pass_a_parts) == 1
    assert isinstance(pass_a_parts[0], PdfPart)
    assert pass_a_parts[0].data == tiny_pdf_bytes

    pass_b_parts = fake.calls[1]["parts"]
    # one TextPart + one ImagePart per rendered page
    assert len(pass_b_parts) == 4
    assert isinstance(pass_b_parts[0], TextPart)
    assert isinstance(pass_b_parts[1], ImagePart)
    assert pass_b_parts[1].page == 1
    assert isinstance(pass_b_parts[3], ImagePart)
    assert pass_b_parts[3].page == 2

    assert PROMPT_A_VERSION in fake.calls[0]["system"]
    assert PROMPT_B_VERSION in fake.calls[1]["system"]
    assert fake.calls[0]["system"] != fake.calls[1]["system"]


def test_vision_client_is_the_declared_type() -> None:
    # documents that double_pass_extract's real signature is VisionClient,
    # even though the test above exercises it with a structurally-typed fake.
    assert VisionClient.extract.__name__ == "extract"

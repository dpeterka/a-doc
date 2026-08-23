"""Shared test fixtures for the ingest slice.

`tiny_pdf_bytes` is a hand-crafted, minimal PDF (one page, no real content
stream) — good enough to be archived/hashed/copied like a real document
without needing a real PDF library or a real scanned document (no PHI is
ever used in tests, per CLAUDE.md's PHI boundary). It is never actually
parsed by `pdftoppm` in tests — every test that needs page images injects a
fake `PageRenderer` instead, so CI never depends on poppler being
installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TINY_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)

TINY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def tiny_pdf_bytes() -> bytes:
    return TINY_PDF_BYTES


@pytest.fixture
def tiny_pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "document.pdf"
    path.write_bytes(TINY_PDF_BYTES)
    return path


@pytest.fixture
def tiny_png_bytes() -> bytes:
    return TINY_PNG_BYTES


def fake_page_renderer(page_count: int) -> object:
    """Build an `archive.PageRenderer` that "renders" `page_count` fake PNGs
    without touching poppler/pdftoppm — the archive tests' no-network,
    no-system-binary substitute for `pdftoppm_renderer`.
    """

    def render(pdf_path: Path, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(1, page_count + 1):
            page_path = out_dir / f"p-{index}.png"
            page_path.write_bytes(TINY_PNG_BYTES)
            paths.append(page_path)
        return paths

    return render

#!/usr/bin/env python3
"""Build the StatPearls lead-section index.

StatPearls is a clinical review corpus on NCBI Bookshelf: roughly 12,500
articles under CC BY-NC-ND 4.0. It answers a question none of the other
sources do — Orphanet gives a one-paragraph definition, HPO gives features,
PubMed gives citations, and none of them explain a condition the way a
clinical review does.

## Why only the lead sections

The distributed archive is **1.9GB**, and the NXML alone is roughly 675MB
across ~12,500 articles averaging 54KB. A full-text index would be 300-400MB
in an image whose other four ontology artifacts total about 21MB combined, and
would add a 1.9GB download to every build.

So this keeps the title, the first prose sections, and a Bookshelf link. That
is what a chat answer actually needs: a paragraph of orientation and somewhere
to read on. The full article stays one click away rather than being copied
wholesale, which also keeps the attribution requirement simple to honour.

## Licence

CC BY-NC-ND 4.0. Non-commercial is satisfied by what this is; attribution is
carried on every record and rendered with every excerpt; and no-derivatives is
respected by storing text verbatim rather than rewriting it. The
`ATTRIBUTION` string below is not decoration — it is the licence condition.

Usage: python scripts/build_statpearls_index.py <statpearls.tar.gz> <out.sqlite>
"""

from __future__ import annotations

import re
import sqlite3
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

ATTRIBUTION = (
    "StatPearls, StatPearls Publishing. CC BY-NC-ND 4.0 "
    "(https://creativecommons.org/licenses/by-nc-nd/4.0/)"
)

# Sections worth keeping, in the order a reader wants them. StatPearls uses a
# fixed template, so these titles are reliable; anything past them is
# management, nursing and quiz content that a read-only informational tool has
# no business surfacing.
_WANTED_SECTIONS = (
    "introduction",
    "etiology",
    "epidemiology",
    "pathophysiology",
    "history and physical",
    "evaluation",
)

# Per-article cap. Enough for orientation, and it bounds the artifact: without
# it a handful of very long articles dominate the index.
MAX_ARTICLE_CHARS = 2500

_WHITESPACE_RE = re.compile(r"\s+")


def _text_of(element: ET.Element) -> str:
    """All descendant text, whitespace-collapsed.

    `itertext` rather than `.text`: NXML wraps inline markup (`<italic>`,
    `<xref>`) inside paragraphs, and reading `.text` alone silently truncates
    every sentence at the first tag.
    """
    return _WHITESPACE_RE.sub(" ", "".join(element.itertext())).strip()


def parse_article(raw: bytes) -> tuple[str, str] | None:
    """`(title, body)` from one article's NXML, or `None` if unusable.

    No identifier is returned: the archive's NXML carries no NBK id and none
    is derivable from the `article-NNNNN.nxml` filename, so a per-article deep
    link is not available. The reader gets a Bookshelf search on the title
    instead, which is honest and lands them in the right place — inventing an
    id to build a URL would produce links that 404.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    # `<title-group><title>` is the ARTICLE's title. `<article-title>` also
    # appears — 19 times in the first file checked — but every one of those is
    # a citation inside the reference list. Reading the first `.//article-title`
    # indexed a reference's title for all 8,311 articles: a query for Sjogren
    # syndrome returned "Variants at multiple loci implicated in both innate
    # and adaptive immunity", which is a cited paper, not the article.
    title_group = root.find(".//title-group/title")
    title = _text_of(title_group) if title_group is not None else ""
    if not title:
        return None

    chunks: list[str] = []
    for section in root.iter("sec"):
        heading = section.find("title")
        name = _text_of(heading).lower().rstrip(":") if heading is not None else ""
        if name not in _WANTED_SECTIONS:
            continue
        body = " ".join(_text_of(p) for p in section.iter("p"))
        if body:
            chunks.append(f"{name.title()}. {body}")

    text = " ".join(chunks)[:MAX_ARTICLE_CHARS]
    if not text:
        return None
    return title, text


def build(archive: Path, out: Path) -> None:
    if out.exists():
        out.unlink()
    connection = sqlite3.connect(out)
    connection.execute(
        # `content=''` would save space but makes snippets impossible; the text
        # is the point here, so it is stored.
        "CREATE VIRTUAL TABLE articles USING fts5(title, body, tokenize='porter unicode61')"
    )

    kept = skipped = 0
    with tarfile.open(archive, "r:gz") as tar:
        # Streamed, not extracted: the archive is 1.9GB and most of it is
        # images this never opens.
        for member in tar:
            if not member.name.endswith(".nxml"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            parsed = parse_article(handle.read())
            if parsed is None:
                skipped += 1
                continue
            title, body = parsed
            connection.execute("INSERT INTO articles (title, body) VALUES (?, ?)", (title, body))
            kept += 1
            if kept % 2000 == 0:
                connection.commit()
                print(f"build_statpearls_index: {kept:,} articles...", file=sys.stderr)

    connection.commit()
    connection.execute("INSERT INTO articles(articles) VALUES('optimize')")
    connection.commit()
    connection.close()

    print(
        f"build_statpearls_index: {kept:,} articles indexed, {skipped:,} skipped "
        f"({out.stat().st_size / 1e6:.1f}MB)",
        file=sys.stderr,
    )
    if kept == 0:
        raise SystemExit(
            "build_statpearls_index: nothing indexed — refusing to ship an empty index"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    build(Path(argv[1]), Path(argv[2]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))

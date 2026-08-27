"""Encounter markdown files: YAML frontmatter + fixed body sections.

PLAN.md "Key schemas" / "Encounter files": `case/encounters/YYYY-MM-DD--<slug>.md`,
frontmatter (date, type, provider, sources, symptoms) followed by `## Summary`,
`## New findings`, `## Plan / follow-ups`. Patient chat reports enter through
this same door as doctor notes, tagged `type: patient-report`.
"""

from __future__ import annotations

import hashlib
import io
import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

EncounterType = Literal[
    "lab-result", "specialist-visit", "imaging", "patient-report", "phone", "procedure"
]

_FRONTMATTER_DELIM = "---"
_SLUG_INVALID = re.compile(r"[^a-z0-9-]+")
_SLUG_DASHES = re.compile(r"-+")


# How precisely `EncounterFrontmatter.date` is actually known.
#
# A patient says "my thyroid failed in 2021" and the parser returns
# 2021-01-01 — indistinguishable downstream from "January 1st, 2021".
# "spring 2022" becomes 2022-01-01, the wrong season asserted to the day.
# For a diagnostic odyssey where sequence is the clinical signal that is
# fabricated precision, so the encounter records what it really knows.
DatePrecision = Literal["day", "month", "year", "approximate"]

# `EncounterFrontmatter.date` shadows the `date` TYPE inside the class body,
# so a second date-typed field cannot annotate itself as `date`. Alias it.
CalendarDate = date


class EncounterFrontmatter(BaseModel):
    date: date
    type: EncounterType
    provider: str | None = None
    sources: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    date_precision: DatePrecision = "day"
    """Defaults to `day` so every encounter written before this field
    existed round-trips unchanged — a document-sourced encounter genuinely
    is day-precise, and those are the majority."""
    reported_on: CalendarDate | None = None
    """When the patient TOLD us, as distinct from when it happened.

    `IntakeFact` already separates `date_approx` / `precision` /
    `reported_on`, which is the right model — but the encounter written from
    it kept only a single `date`, so the distinction was lost at exactly the
    point the case file becomes the durable record. `None` for a
    document-sourced encounter, where the document's own date is the fact.
    """


class Encounter(BaseModel):
    frontmatter: EncounterFrontmatter
    summary: str = ""
    new_findings: str = ""
    plan: str = ""
    extracted_text: str = ""
    """Full verbatim text of a source docx narrative document (PLAN.md docx
    ingestion: "narrative docs become full-text encounters" - the context
    pack needs the FULL extracted text, not a summary). Rendered as an
    optional trailing `## Extracted text` section; empty for every
    non-docx-sourced encounter, so existing encounter files round-trip
    unchanged."""


# A slug is built from model-written text (an event title), which is
# unbounded — a live intake turn produced a paragraph-long title and the
# resulting path blew past the filesystem limit with
# `OSError: [Errno 36] File name too long`. Most filesystems cap a single
# NAME at 255 bytes; 80 leaves ample room for the date prefix, the `.md`
# suffix, the disambiguating hash below, and any longer path around it.
SLUG_MAX_CHARS = 80


def slugify(text: str) -> str:
    """Turn free text into a filename-safe slug (lowercase, hyphen-separated).

    Bounded at `SLUG_MAX_CHARS`. When truncation happens the slug is cut at
    a word boundary and a short hash of the FULL text is appended, so two
    long titles sharing a prefix — very likely, since these are generated
    descriptions of related events — cannot collapse onto one filename and
    silently overwrite each other.
    """
    slug = text.strip().lower()
    slug = slug.replace(" ", "-")
    slug = _SLUG_INVALID.sub("", slug)
    slug = _SLUG_DASHES.sub("-", slug).strip("-")
    if not slug:
        return "encounter"
    if len(slug) <= SLUG_MAX_CHARS:
        return slug

    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]
    keep = SLUG_MAX_CHARS - len(digest) - 1
    head = slug[:keep].rsplit("-", 1)[0] or slug[:keep]
    return f"{head}-{digest}"


def encounter_filename(frontmatter: EncounterFrontmatter, slug: str) -> str:
    """`YYYY-MM-DD--<slug>.md` per the PLAN.md filename convention."""
    return f"{frontmatter.date.isoformat()}--{slugify(slug)}.md"


def render_encounter(encounter: Encounter) -> str:
    """Render an `Encounter` to markdown with a YAML frontmatter block."""
    yaml = YAML()
    yaml.default_flow_style = False
    frontmatter_data = encounter.frontmatter.model_dump(mode="json")

    buf = io.StringIO()
    yaml.dump(frontmatter_data, buf)

    body = (
        f"## Summary\n\n{encounter.summary.strip()}\n\n"
        f"## New findings\n\n{encounter.new_findings.strip()}\n\n"
        f"## Plan / follow-ups\n\n{encounter.plan.strip()}\n"
    )
    if encounter.extracted_text.strip():
        body += f"\n## Extracted text\n\n{encounter.extracted_text.strip()}\n"
    return f"{_FRONTMATTER_DELIM}\n{buf.getvalue()}{_FRONTMATTER_DELIM}\n\n{body}"


def parse_encounter(text: str) -> Encounter:
    """Parse markdown produced by `render_encounter` (or hand-authored, same shape)."""
    if not text.startswith(f"{_FRONTMATTER_DELIM}\n"):
        raise ValueError("encounter file must start with a '---' YAML frontmatter block")
    _, _, rest = text.partition(f"{_FRONTMATTER_DELIM}\n")
    frontmatter_text, sep, body = rest.partition(f"\n{_FRONTMATTER_DELIM}\n")
    if not sep:
        raise ValueError("encounter file frontmatter block is not closed with '---'")

    yaml = YAML(typ="safe")
    frontmatter_data = yaml.load(frontmatter_text) or {}
    frontmatter = EncounterFrontmatter.model_validate(frontmatter_data)

    sections = _split_sections(body)
    return Encounter(
        frontmatter=frontmatter,
        summary=sections.get("Summary", ""),
        new_findings=sections.get("New findings", ""),
        plan=sections.get("Plan / follow-ups", ""),
        extracted_text=sections.get("Extracted text", ""),
    )


def _split_sections(body: str) -> dict[str, str]:
    pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[title] = body[start:end].strip()
    return sections


def write_encounter(encounters_dir: Path, encounter: Encounter, slug: str) -> Path:
    """Render `encounter` and write it to `encounters_dir`, returning the path."""
    encounters_dir.mkdir(parents=True, exist_ok=True)
    path = encounters_dir / encounter_filename(encounter.frontmatter, slug)
    path.write_text(render_encounter(encounter), encoding="utf-8")
    return path


def read_encounter(path: Path) -> Encounter:
    """Read and parse an encounter markdown file."""
    return parse_encounter(path.read_text(encoding="utf-8"))

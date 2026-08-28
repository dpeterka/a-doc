"""Seed `case/regimen.yaml` from what is already on disk.

Deterministic and offline — no LLM call is made against the patient's
regimen, which keeps the whole list local (CLAUDE.md: deterministic logic is
never delegated to a model, and there is no reason to send this anywhere).

Two sources, in order of precision:

**A regimen encounter.** A curated list, written as
`- <Name> — <dose/frequency>[; <notes>]` bullets. Its date is what makes it
worth parsing: the document says what was being taken THAT DAY, which is
recorded as an attestation rather than as a start date (see
`RegimenEntry.attested_on`).

**The intake sections.** `intake.sections.Medication` / `Supplement` carry
`still_taking`, and that boolean is all the temporal information they have —
so entries from there arrive with no dates at all and are honestly marked as
such. They are still worth importing: knowing a substance is on the list at
all is what lets a later chat turn attach dates to it.

Prose bullets are skipped rather than guessed at. The source document mixes
instruction lines ("take with food", "space four hours apart") among the
items, and turning one of those into a supplement named "Take with food"
would put a fiction into the case file.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from adoc.casefile.encounters import read_encounter
from adoc.casefile.regimen import Regimen, RegimenEntry, merge_entries

ENCOUNTERS_RELDIR = "case/encounters"

# The name/dose separator the curated list uses. Both dash widths appear.
_SEPARATORS = ("—", "–")

# A name longer than this is prose, not a substance. Real entries in the
# source list top out well under it ("Vitamin D3 with K2").
_MAX_NAME_CHARS = 60

# A leading quantity: "500 mg", "5,000 IU", "1-2 capsules", "~400 mcg".
_DOSE_RE = re.compile(
    r"^\s*(~?\d[\d.,]*(?:\s*[-–—]\s*\d[\d.,]*)?\s*"
    r"(?:mg|mcg|ug|g|iu|ml|billion|cfu|capsules?|caps?|tabs?|tablets?|drops?|scoops?|softgels?)\b)",
    re.IGNORECASE,
)

# Bullets that are instructions rather than items. Checked against the NAME
# side only, so "Magnesium — take with food" still parses as magnesium.
_INSTRUCTION_HINTS = (
    "take ",
    "avoid",
    "space ",
    "do not",
    "don't",
    "wait ",
    "keep ",
    "note:",
    "if you",
    "with food",
    "empty stomach",
)


def _looks_like_prose(name: str) -> bool:
    """Whether the name side reads as an instruction rather than a substance."""
    lowered = name.strip().lower()
    if not lowered:
        return True
    if len(name) > _MAX_NAME_CHARS:
        return True
    if lowered.endswith("."):
        return True
    if any(lowered.startswith(hint) for hint in _INSTRUCTION_HINTS):
        return True
    # Unbalanced brackets mean the separator fell inside a parenthetical, so
    # the "name" is a fragment of a sentence. A real one got through: a
    # 46-character name ending in "(1", with "2 caps per dose)." as its
    # frequency.
    if name.count("(") != name.count(")"):
        return True
    # Substance names are short. "Vitamin D3 with K2" is four words; anything
    # much longer is a sentence that happens to contain a dash.
    if len(name.split()) > 5:
        return True
    # A sha-prefixed archive filename from the frontmatter's `sources:` list.
    return "__" in name or bool(re.fullmatch(r"[0-9a-f]{16,}.*", lowered))


def parse_regimen_bullets(body: str, *, attested: date, source_ref: str) -> list[RegimenEntry]:
    """Every `- Name — dose; notes` bullet in `body`, as entries.

    `attested` is the document's own date, recorded on each entry as a date
    the substance is known to have been in use. Nothing here invents a start
    date: the document does not state one, and a plausible guess would be
    indistinguishable from a fact once written to the case file.
    """
    entries: list[RegimenEntry] = []
    seen: set[str] = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith(("- ", "* ")):
            continue
        item = line[2:].strip()
        separator = next((sep for sep in _SEPARATORS if sep in item), None)
        if separator is None:
            continue
        name, _, remainder = item.partition(separator)
        name = name.strip()
        if _looks_like_prose(name):
            continue
        key = "".join(c for c in name.lower() if c.isalnum())
        if key in seen:
            # The source list repeats a few items across time-of-day sections;
            # one substance is one entry, and the repetition carries no extra
            # temporal information.
            continue
        seen.add(key)

        detail, _, notes = remainder.partition(";")
        detail = detail.strip()
        dose_match = _DOSE_RE.match(detail)
        dose = dose_match.group(1).strip() if dose_match else None
        frequency = (detail[dose_match.end() :] if dose_match else detail).strip() or None

        entries.append(
            RegimenEntry(
                name=name,
                kind="supplement",
                dose=dose,
                frequency=frequency,
                attested_on=[attested],
                attribution="self-started",
                reported_on=attested,
                sources=[source_ref],
                notes=notes.strip(),
            )
        )
    return entries


def find_regimen_encounters(repo_root: Path) -> list[Path]:
    """Encounter files whose slug names a regimen. Newest first."""
    directory = repo_root / ENCOUNTERS_RELDIR
    if not directory.is_dir():
        return []
    matches = [
        path
        for path in directory.glob("*.md")
        if any(word in path.stem.lower() for word in ("regimen", "supplement", "medication"))
    ]
    return sorted(matches, reverse=True)


def backfill_from_encounters(repo_root: Path, regimen: Regimen) -> tuple[Regimen, dict[str, int]]:
    """Fold every regimen encounter into `regimen`.

    Returns the new regimen and a per-file count, so the caller can report
    what was found rather than asserting success. A run that parses zero
    bullets is a silent failure otherwise — the file is on disk, the command
    exits 0, and the record stays empty.
    """
    counts: dict[str, int] = {}
    result = regimen
    for path in find_regimen_encounters(repo_root):
        encounter = read_encounter(path)
        entries = parse_regimen_bullets(
            path.read_text(encoding="utf-8"),
            attested=encounter.frontmatter.date,
            source_ref=f"encounter:{path.name}",
        )
        counts[path.name] = len(entries)
        result = merge_entries(result, entries)
    return result, counts

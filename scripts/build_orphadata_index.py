#!/usr/bin/env python3
"""Build the compact Orphanet index — definitions, prevalence, onset, inheritance.

The disease-lookup tool can already say what a condition characteristically
involves, from HPO annotations. It cannot say what the condition *is*, how rare
it is, when it usually starts, or how it is inherited. Orphanet curates exactly
that, and this compacts it.

Three source products, all CC-BY and already listed as a phase 3 knowledge
source in PLAN.md:

  en_product1.xml       52MB  name, synonyms, and the Definition text section
  en_product9_prev.xml  16MB  prevalence
  en_product9_ages.xml   7MB  average age of onset, type of inheritance

Parsed with `iterparse` and cleared as it goes: product1 alone is 52MB of XML
and building a full DOM for it would balloon the build container's memory for
no reason.

## Two judgements worth naming

**Definitions are stripped of markup, not passed through.** Orphanet embeds
`<i>` and `<sup>` as escaped entities inside `Contents`. This text can end up
in front of a patient, so the tags are removed rather than rendered — a
half-escaped italic in a chat reply is exactly the class of defect that
produced the literal `**` bug.

**One prevalence is chosen from many.** A disease can carry a dozen prevalence
records differing by type, geography and source. The rule, in order: a
validated worldwide point-prevalence class; then any point-prevalence class;
then any class at all; then the qualification alone ("Case(s)", "Unknown").
Every choice keeps the type and geography alongside it, so a reader is never
shown a bare number whose meaning has been quietly dropped.

Usage:
  python scripts/build_orphadata_index.py <product1.xml> <product9_prev.xml> \\
      <product9_ages.xml> <out.json>
"""

from __future__ import annotations

import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

# Definitions are prose written for clinicians but readable; anything much
# longer than this is a multi-paragraph review and would swamp a chat reply.
MAX_DEFINITION_CHARS = 900

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Preference order for picking one prevalence out of many. Point prevalence is
# the figure a reader means by "how common is this"; incidence and
# cases/families answer different questions and are kept only as a fallback.
_PREVALENCE_TYPE_RANK = {
    "Point prevalence": 0,
    "Prevalence at birth": 1,
    "Lifetime prevalence": 2,
    "Annual incidence": 3,
    "Cases/families": 4,
}


def _clean_text(raw: str) -> str:
    """Unescape, strip markup, collapse whitespace.

    Orphanet escapes real tags into the text, so unescaping first and stripping
    second is the only order that works: stripping first would find nothing.
    """
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub("", html.unescape(raw or ""))).strip()


def _iter_disorders(path: Path) -> Iterator[ET.Element]:
    """Yield each `<Disorder>` and free it, so a 52MB file costs almost nothing."""
    for _event, element in ET.iterparse(path, events=("end",)):
        if element.tag == "Disorder":
            yield element
            element.clear()


def _orpha_code(disorder: ET.Element) -> str | None:
    node = disorder.find("OrphaCode")
    if node is None or not (node.text or "").strip():
        return None
    return "ORPHA:" + node.text.strip()


def _text(parent: ET.Element | None, path: str) -> str:
    if parent is None:
        return ""
    node = parent.find(path)
    return (node.text or "").strip() if node is not None else ""


def parse_definitions(path: Path) -> dict[str, dict[str, object]]:
    """`ORPHA:code -> {name, definition}` from product1."""
    out: dict[str, dict[str, object]] = {}
    for disorder in _iter_disorders(path):
        code = _orpha_code(disorder)
        if code is None:
            continue
        entry: dict[str, object] = {"name": _text(disorder, "Name")}
        for section in disorder.iter("TextSection"):
            if _text(section, "TextSectionType/Name") != "Definition":
                continue
            contents = _clean_text(_text(section, "Contents"))
            if contents:
                entry["definition"] = contents[:MAX_DEFINITION_CHARS]
            break
        out[code] = entry
    return out


def parse_prevalence(path: Path) -> dict[str, dict[str, str]]:
    """`ORPHA:code -> {value, type, geography}` — one chosen from many."""
    out: dict[str, dict[str, str]] = {}
    for disorder in _iter_disorders(path):
        code = _orpha_code(disorder)
        if code is None:
            continue

        best: tuple[int, int, dict[str, str]] | None = None
        for prevalence in disorder.iter("Prevalence"):
            ptype = _text(prevalence, "PrevalenceType/Name")
            pclass = _text(prevalence, "PrevalenceClass/Name")
            qualification = _text(prevalence, "PrevalenceQualification/Name")
            geography = _text(prevalence, "PrevalenceGeographic/Name")
            validated = _text(prevalence, "PrevalenceValidationStatus/Name") == "Validated"

            value = pclass or qualification
            if not value:
                continue
            # Rank: prefer a real class over a bare qualification, then the
            # prevalence type, then worldwide, then validated.
            rank = (
                _PREVALENCE_TYPE_RANK.get(ptype, 9),
                (0 if pclass else 1)
                + (0 if geography == "Worldwide" else 2)
                + (0 if validated else 4),
            )
            candidate = {"value": value, "type": ptype, "geography": geography}
            if best is None or rank < (best[0], best[1]):
                best = (rank[0], rank[1], candidate)

        if best is not None:
            out[code] = best[2]
    return out


def parse_ages(path: Path) -> dict[str, dict[str, list[str]]]:
    """`ORPHA:code -> {onset, inheritance}` from product9_ages."""
    out: dict[str, dict[str, list[str]]] = {}
    for disorder in _iter_disorders(path):
        code = _orpha_code(disorder)
        if code is None:
            continue
        onset = [
            (name.text or "").strip()
            for entry in disorder.iter("AverageAgeOfOnset")
            for name in entry.findall("Name")
            if (name.text or "").strip()
        ]
        inheritance = [
            (name.text or "").strip()
            for entry in disorder.iter("TypeOfInheritance")
            for name in entry.findall("Name")
            if (name.text or "").strip()
        ]
        if onset or inheritance:
            out[code] = {"onset": sorted(set(onset)), "inheritance": sorted(set(inheritance))}
    return out


def build(product1: Path, prevalence: Path, ages: Path) -> dict[str, object]:
    definitions = parse_definitions(product1)
    prevalences = parse_prevalence(prevalence)
    age_data = parse_ages(ages)

    diseases: dict[str, dict[str, object]] = {}
    for code, entry in definitions.items():
        record: dict[str, object] = {"name": entry.get("name", "")}
        if entry.get("definition"):
            record["definition"] = entry["definition"]
        if code in prevalences:
            record["prevalence"] = prevalences[code]
        extra = age_data.get(code)
        if extra:
            if extra["onset"]:
                record["onset"] = extra["onset"]
            if extra["inheritance"]:
                record["inheritance"] = extra["inheritance"]
        diseases[code] = record

    with_definition = sum(1 for r in diseases.values() if r.get("definition"))
    print(
        f"build_orphadata_index: {len(diseases):,} disorders, "
        f"{with_definition:,} with a definition, {len(prevalences):,} with prevalence, "
        f"{len(age_data):,} with onset/inheritance",
        file=sys.stderr,
    )
    if not with_definition:
        raise SystemExit("build_orphadata_index: no definitions parsed — refusing to write")
    return {"diseases": diseases}


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    index = build(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    out = Path(argv[4])
    out.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    print(
        f"build_orphadata_index: wrote {out} ({out.stat().st_size / 1e6:.1f}MB)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))

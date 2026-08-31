#!/usr/bin/env python3
"""Build the compact phenotype-similarity index.

Semantic similarity needs two things the existing HPO index deliberately
dropped: which HPO terms each disease is annotated with, and how the terms
relate to one another. `build_hpo_index.py` keeps only labels and synonyms
because its job is matching free text to a term; it throws the graph away.

This produces the other half:

  parents   term -> its direct is_a parents, from `hp.json`
  diseases  disease id -> name and its DIRECT phenotype annotations,
            from `phenotype.hpoa`

Ancestor closure and information content are computed at load time rather than
baked in. The closure of ~19,000 terms is the large part of this data and it is
derivable from ~40,000 parent edges in well under a second, so storing it would
trade a real increase in image size for nothing.

Only `aspect == "P"` rows are kept — `phenotype.hpoa` also carries inheritance,
clinical course and modifier annotations, and mixing those into a phenotype
similarity would compare a patient's symptoms against "autosomal dominant".
`NOT` qualifiers are dropped too: an explicitly ABSENT finding is real
information, but it is not part of the disease's phenotype profile and
counting it as one would invert its meaning.

Usage: python scripts/build_semsim_index.py <hp.json> <phenotype.hpoa> <out.json>
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# Everything under this term is a phenotypic abnormality. The rest of HPO is
# scaffolding (inheritance, frequency, clinical modifiers) and has no place in
# a phenotype comparison.
PHENOTYPIC_ABNORMALITY = "HP:0000118"

# `phenotype.hpoa` column order, fixed by the HPO release format.
_ASPECT_COLUMN = 10
_QUALIFIER_COLUMN = 2
_HPO_ID_COLUMN = 3
_DISEASE_ID_COLUMN = 0
_DISEASE_NAME_COLUMN = 1
_MIN_COLUMNS = 11

# A disease annotated with only one or two terms cannot produce a meaningful
# similarity — it will either match almost nothing or match anything sharing
# that one term. Monarch's own tooling filters similarly.
MIN_TERMS_PER_DISEASE = 3


def _term_id(raw: str) -> str | None:
    """`http://purl.obolibrary.org/obo/HP_0001250` -> `HP:0001250`."""
    if "/HP_" not in raw:
        return None
    return "HP:" + raw.rsplit("HP_", 1)[1]


def parse_parents(hp_json: Path) -> dict[str, list[str]]:
    """Direct `is_a` parents for every phenotypic-abnormality term."""
    data = json.loads(hp_json.read_text())
    graph = data["graphs"][0]

    parents: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        if edge.get("pred") != "is_a":
            continue
        child = _term_id(edge.get("sub", ""))
        parent = _term_id(edge.get("obj", ""))
        if child and parent:
            parents[child].add(parent)

    deprecated = set()
    for node in graph.get("nodes", []):
        term = _term_id(node.get("id", ""))
        if term and node.get("meta", {}).get("deprecated"):
            deprecated.add(term)

    return {
        child: sorted(ps - deprecated) for child, ps in parents.items() if child not in deprecated
    }


def parse_annotations(hpoa: Path) -> dict[str, dict[str, object]]:
    """Disease id -> `{name, terms}` from `phenotype.hpoa`."""
    diseases: dict[str, dict[str, object]] = {}
    terms_by_disease: dict[str, set[str]] = defaultdict(set)
    names: dict[str, str] = {}

    with hpoa.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            columns = line.rstrip("\n").split("\t")
            if len(columns) < _MIN_COLUMNS:
                continue
            if columns[_ASPECT_COLUMN] != "P":
                continue
            if columns[_QUALIFIER_COLUMN].strip().upper() == "NOT":
                continue
            disease = columns[_DISEASE_ID_COLUMN].strip()
            term = columns[_HPO_ID_COLUMN].strip()
            if not disease or not term.startswith("HP:"):
                continue
            terms_by_disease[disease].add(term)
            names.setdefault(disease, columns[_DISEASE_NAME_COLUMN].strip())

    for disease, terms in terms_by_disease.items():
        if len(terms) < MIN_TERMS_PER_DISEASE:
            continue
        diseases[disease] = {"name": names.get(disease, disease), "terms": sorted(terms)}
    return diseases


def build(hp_json: Path, hpoa: Path) -> dict[str, object]:
    parents = parse_parents(hp_json)
    diseases = parse_annotations(hpoa)
    print(
        f"build_semsim_index: {len(parents):,} terms with parents, "
        f"{len(diseases):,} diseases with >= {MIN_TERMS_PER_DISEASE} annotations",
        file=sys.stderr,
    )
    if not diseases:
        raise SystemExit("build_semsim_index: no usable disease annotations — refusing to write")
    return {
        "root": PHENOTYPIC_ABNORMALITY,
        "parents": parents,
        "diseases": diseases,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    hp_json, hpoa, out = Path(argv[1]), Path(argv[2]), Path(argv[3])
    index = build(hp_json, hpoa)
    out.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    print(f"build_semsim_index: wrote {out} ({out.stat().st_size / 1e6:.1f}MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))

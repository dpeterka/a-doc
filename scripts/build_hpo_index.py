"""Build the compact HPO lookup index baked into the application image.

`hp.json` is 22MB of ontology the app does not need at runtime: matching a
patient's words to a term needs labels and synonyms, not axioms, edges or
provenance. This emits `{"terms": {id: label}, "lookup": {phrase: id}}`.

Ambiguous phrases are DROPPED, not resolved. If "discharge" reaches two
distinct HPO terms, picking one silently attaches a wrong clinical concept to
a patient — and a wrong phenotype term propagates into a differential engine
that ranks diseases by exactly those terms. A missing term costs recall; a
wrong one costs correctness.

Usage: python scripts/build_hpo_index.py <hp.json> <out.json>
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Phrases too generic to be worth matching: they appear as synonyms of very
# specific terms while meaning almost nothing on their own, so a literal match
# is far more likely to be a coincidence than a finding.
_STOP_PHRASES = {
    "abnormality",
    "disease",
    "disorder",
    "syndrome",
    "abnormal",
    "increased",
    "decreased",
    "high",
    "low",
    "pain",
    "mass",
    "lesion",
}

_MIN_PHRASE_CHARS = 4


def normalize(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.lower()).strip()


PHENOTYPIC_ABNORMALITY = "HP:0000118"
"""The only branch worth matching.

HPO's root has several children, and most are not findings: `Clinical
modifier` (Severe, Moderate), `Frequency`, `Mode of inheritance`, `Past
medical history`. Indexing them all matched "Severe", "Moderate",
"Frequency" and "Healthy" out of ordinary prose on the first real run — terms
that would go straight into LIRICAL as though they were symptoms and skew
every disease it ranks.
"""


def _descendants(edges: list[dict], root: str) -> set[str]:
    """Every term under `root`, following is_a edges upward."""
    children: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.get("pred") not in {"is_a", "rdfs:subClassOf"}:
            continue
        sub, obj = edge.get("sub", ""), edge.get("obj", "")
        if "/HP_" not in sub or "/HP_" not in obj:
            continue
        children["HP:" + obj.rsplit("HP_", 1)[1]].add("HP:" + sub.rsplit("HP_", 1)[1])

    seen: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def build(hp_json: Path) -> dict[str, object]:
    data = json.loads(hp_json.read_text())
    graph = data["graphs"][0]
    nodes = graph["nodes"]
    allowed = _descendants(graph.get("edges", []), PHENOTYPIC_ABNORMALITY)
    print(f"build_hpo_index: {len(allowed):,} terms under {PHENOTYPIC_ABNORMALITY}", file=sys.stderr)

    terms: dict[str, str] = {}
    phrase_to_ids: dict[str, set[str]] = defaultdict(set)

    for node in nodes:
        raw_id = node.get("id", "")
        if "/HP_" not in raw_id:
            continue
        label = node.get("lbl")
        if not label:
            continue
        meta = node.get("meta", {})
        if meta.get("deprecated"):
            continue
        term_id = "HP:" + raw_id.rsplit("HP_", 1)[1]
        if term_id not in allowed:
            continue
        terms[term_id] = label

        phrases = [label] + [s.get("val", "") for s in meta.get("synonyms", [])]
        for phrase in phrases:
            key = normalize(phrase)
            if len(key) < _MIN_PHRASE_CHARS or key in _STOP_PHRASES:
                continue
            phrase_to_ids[key].add(term_id)

    lookup = {phrase: next(iter(ids)) for phrase, ids in phrase_to_ids.items() if len(ids) == 1}
    dropped = len(phrase_to_ids) - len(lookup)
    print(f"build_hpo_index: {len(terms):,} terms", file=sys.stderr)
    print(f"build_hpo_index: {len(lookup):,} unambiguous phrases", file=sys.stderr)
    print(f"build_hpo_index: {dropped:,} ambiguous phrases dropped", file=sys.stderr)
    return {"terms": terms, "lookup": lookup}


if __name__ == "__main__":
    source, destination = Path(sys.argv[1]), Path(sys.argv[2])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(build(source), separators=(",", ":"), sort_keys=True))
    print(f"build_hpo_index: wrote {destination} ({destination.stat().st_size/1e6:.1f} MB)", file=sys.stderr)

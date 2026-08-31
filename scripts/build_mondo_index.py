#!/usr/bin/env python3
"""Build the compact Mondo cross-reference index.

Two engines rank diseases and the ledger names them, and until now all three
were reconciled by comparing normalised NAMES. That is the documented weak
point of `knowledge/lirical_divergence`: LIRICAL emits OMIM/ORPHA curies, the
similarity engine emits OMIM/ORPHA curies, a hypothesis carries a free-text
name, and a vocabulary mismatch reads as a real clinical disagreement.

Mondo is the bridge. It assigns one identifier per disease and cross-references
the source vocabularies, so `OMIM:154700` and `ORPHA:558` both resolve to
`MONDO:0007947` and are recognisably the same thing.

Three maps come out of it:

  xrefs   OMIM:/ORPHA:/DOID: id -> MONDO id
  labels  normalised label or synonym -> MONDO id
  names   MONDO id -> primary label

`labels` is what makes this useful today: not one of the fifty hypotheses on
the live ledger carries a `mondo` id, so resolving an engine's curie alone
would have nothing to compare against. Resolving the hypothesis NAME through
Mondo's labels and synonyms gives both sides an id.

Ambiguous labels are DROPPED, not guessed. A synonym that resolves to two
different diseases resolves to neither — the same rule `build_hpo_index.py`
applies, and for the same reason: a confident wrong match is worse than no
match, because nothing downstream can tell.

Usage: python scripts/build_mondo_index.py <mondo.json> <out.json>
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Vocabularies worth carrying. Mondo cross-references a dozen more (MESH,
# ICD, UMLS, GARD...) and none of them appear in this system's inputs, so
# storing them would only inflate the artifact.
_WANTED_PREFIXES = ("OMIM:", "ORPHA:", "Orphanet:", "DOID:")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Too short to be a disease name. Mondo synonyms include abbreviations like
# "MFS" that collide across unrelated diseases, and matching a hypothesis
# name on two or three characters would be worse than not matching.
_MIN_LABEL_CHARS = 6


def normalise_label(text: str) -> str:
    """The comparison key for a disease name.

    Same shape as the matcher in `knowledge.lirical_divergence` so the two
    agree, but deliberately WITHOUT that module's stopword removal: here the
    label is being resolved against a curated ontology rather than compared to
    another free-text name, so dropping "primary" or "syndrome" would merge
    entries the ontology deliberately distinguishes.
    """
    folded = text.lower().replace("ö", "o").replace("é", "e").replace("ü", "u")
    folded = re.sub(r"['’]s\b", "", folded)
    return " ".join(_NON_ALNUM_RE.sub(" ", folded).split())


def _mondo_id(raw: str) -> str | None:
    if "/MONDO_" not in raw:
        return None
    return "MONDO:" + raw.rsplit("MONDO_", 1)[1]


def build(mondo_json: Path) -> dict[str, object]:
    data = json.loads(mondo_json.read_text())
    nodes = data["graphs"][0]["nodes"]

    names: dict[str, str] = {}
    xrefs: dict[str, str] = {}
    label_to_ids: dict[str, set[str]] = defaultdict(set)

    for node in nodes:
        mondo = _mondo_id(node.get("id", ""))
        if mondo is None:
            continue
        meta = node.get("meta", {})
        if meta.get("deprecated"):
            continue
        label = node.get("lbl")
        if not label:
            continue
        names[mondo] = label

        for xref in meta.get("xrefs", []):
            value = str(xref.get("val", "")).strip()
            if not value.startswith(_WANTED_PREFIXES):
                continue
            # Orphanet appears under two prefixes across releases; normalise
            # to the one LIRICAL and phenotype.hpoa actually emit.
            if value.startswith("Orphanet:"):
                value = "ORPHA:" + value.split(":", 1)[1]
            # First writer wins: a source id mapped to two Mondo terms is an
            # upstream ambiguity, and picking the later one arbitrarily would
            # make the build non-deterministic across releases.
            xrefs.setdefault(value, mondo)

        phrases = [label] + [str(s.get("val", "")) for s in meta.get("synonyms", [])]
        for phrase in phrases:
            key = normalise_label(phrase)
            if len(key) >= _MIN_LABEL_CHARS:
                label_to_ids[key].add(mondo)

    # Ambiguous labels resolve to nothing. A confident wrong match is worse
    # than no match: nothing downstream can tell the difference.
    labels = {key: next(iter(ids)) for key, ids in label_to_ids.items() if len(ids) == 1}
    dropped = sum(1 for ids in label_to_ids.values() if len(ids) > 1)

    print(
        f"build_mondo_index: {len(names):,} diseases, {len(xrefs):,} cross-references, "
        f"{len(labels):,} unambiguous labels ({dropped:,} ambiguous dropped)",
        file=sys.stderr,
    )
    if not xrefs:
        raise SystemExit("build_mondo_index: no cross-references found — refusing to write")
    return {"names": names, "xrefs": xrefs, "labels": labels}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    index = build(Path(argv[1]))
    out = Path(argv[2])
    out.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    print(f"build_mondo_index: wrote {out} ({out.stat().st_size / 1e6:.1f}MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))

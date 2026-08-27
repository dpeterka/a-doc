"""Drop duplicate `entrez_id` rows from HGNC's complete set.

LIRICAL 2.4.1 loads `hgnc_complete_set.txt` into a map keyed by NCBI gene ID
with no merge function, so a single duplicated ID aborts it at bootstrap:

    IllegalStateException: Duplicate key NCBIGene:100874204
      (attempted merging values ALDH1L1-AS1 and SLC41A3-AS1)

Measured on the 2026-08 release: 2 collisions out of 44,403 distinct IDs,
both non-coding-RNA/pseudogene antisense entries annotated to no disease.
Keeping the first row per ID is therefore lossless for phenotype-driven
ranking. The rule is generic, not a hardcoded pair, because a later HGNC
release will collide somewhere else.

Standard library only: this runs inside the LIRICAL image, which has no
Python packages installed.
"""

from __future__ import annotations

import sys
from pathlib import Path


def dedupe(path: Path) -> int:
    """Rewrite `path` keeping the first row per non-empty `entrez_id`.

    Returns the number of rows dropped. Rows with a blank `entrez_id` are
    always kept — they cannot collide, and discarding them would silently
    shrink the gene table for no reason.
    """
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    if not lines:
        raise SystemExit(f"{path}: empty")

    header = lines[0].rstrip("\n").split("\t")
    try:
        index = header.index("entrez_id")
    except ValueError:
        raise SystemExit(f"{path}: no 'entrez_id' column in header") from None

    seen: set[str] = set()
    kept: list[str] = [lines[0]]
    dropped: list[str] = []
    for line in lines[1:]:
        parts = line.rstrip("\n").split("\t")
        entrez = parts[index].strip() if len(parts) > index else ""
        if entrez and entrez in seen:
            dropped.append(entrez)
            continue
        if entrez:
            seen.add(entrez)
        kept.append(line)

    path.write_text("".join(kept))
    return len(dropped)


if __name__ == "__main__":
    target = Path(sys.argv[1])
    count = dedupe(target)
    print(f"dedupe_hgnc: dropped {count} duplicate entrez_id row(s) from {target}")

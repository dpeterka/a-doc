"""Mondo cross-references — one identity for a disease across vocabularies.

Two engines rank diseases and the ledger names them, and all three were being
reconciled by comparing normalised NAMES. That is the documented weak point of
`lirical_divergence`: LIRICAL emits OMIM/ORPHA curies, the similarity engine
emits OMIM/ORPHA curies, a hypothesis carries free text, and a vocabulary
mismatch reads as a real clinical disagreement rather than as two names for
one disease.

This resolves all three onto a single Mondo identifier.

Two routes in, because the inputs are not the same shape:

  `resolve_curie`  OMIM:154700 / ORPHA:558 -> MONDO:0007947, via cross-reference
  `resolve_name`   "Marfan syndrome"       -> MONDO:0007947, via label or synonym

The name route is what makes this useful TODAY. Not one of the fifty
hypotheses on the live ledger carries a `mondo` id — the field exists in the
schema and nothing has ever populated it, exactly as `discriminators` did — so
resolving only the engines' curies would leave nothing to compare them
against.

Resolution is exact or nothing. An ambiguous synonym was dropped at build
time rather than guessed, and a name this cannot resolve returns `None` so the
caller falls back to string comparison and says so. A confident wrong match is
worse than no match, because nothing downstream can tell the difference.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Must match `scripts/build_mondo_index.py`'s floor, or a key normalised here
# will never be found in an index built there.
_MIN_LABEL_CHARS = 6


def normalise_label(text: str) -> str:
    """The comparison key, identical to the build script's.

    Deliberately WITHOUT the stopword removal `lirical_divergence` applies:
    there a free-text name is being compared to another free-text name, so
    dropping "syndrome" helps. Here a name is being resolved against a curated
    ontology, and dropping "primary" would merge entries Mondo deliberately
    keeps apart.
    """
    folded = text.lower().replace("ö", "o").replace("é", "e").replace("ü", "u")
    folded = re.sub(r"['’]s\b", "", folded)
    return " ".join(_NON_ALNUM_RE.sub(" ", folded).split())


def _curie_sort_key(curie: str) -> tuple[str, int, str]:
    """Vocabulary, then NUMERIC id, then the raw string.

    The numeric part matters: within one vocabulary a disease's general entry
    usually has a lower number than its subtypes, and a string sort puts
    `ORPHA:284963` ahead of `ORPHA:558`.
    """
    prefix, _, tail = curie.partition(":")
    try:
        return (prefix, int(tail), curie)
    except ValueError:
        # A non-numeric id (some vocabularies use letters) sorts last within
        # its prefix rather than crashing the comparison.
        return (prefix, 1 << 62, curie)


class MondoIndex:
    """Cross-references and labels, resolved to Mondo ids."""

    def __init__(
        self,
        names: dict[str, str],
        xrefs: dict[str, str],
        labels: dict[str, str],
    ) -> None:
        self._names = names
        self._xrefs = xrefs
        self._labels = labels
        # Inverted at load rather than stored: derivable from `xrefs` in
        # milliseconds, and a second copy in the artifact would be one more
        # thing to keep consistent. Needed because Orphanet data is keyed by
        # ORPHA while an engine may only give an OMIM id for the same disease.
        self._by_mondo: dict[str, list[str]] = {}
        for source, mondo_id in xrefs.items():
            self._by_mondo.setdefault(mondo_id, []).append(source)

    @property
    def size(self) -> int:
        return len(self._names)

    def label(self, mondo_id: str) -> str | None:
        return self._names.get(mondo_id)

    def resolve_curie(self, curie: str) -> str | None:
        """`OMIM:154700` / `ORPHA:558` -> `MONDO:...`.

        A curie that is already a Mondo id passes through, so a caller need
        not know which vocabulary it holds.
        """
        cleaned = curie.strip()
        if cleaned.startswith("MONDO:"):
            return cleaned if cleaned in self._names else None
        if cleaned.lower().startswith("orphanet:"):
            cleaned = "ORPHA:" + cleaned.split(":", 1)[1]
        return self._xrefs.get(cleaned)

    def resolve_name(self, name: str) -> str | None:
        """A disease name -> `MONDO:...`, via label or synonym.

        Exact on the normalised key or nothing. No fuzzy fallback: this is the
        component that decides whether two engines are talking about the same
        disease, and a near-miss here manufactures agreement that does not
        exist.
        """
        key = normalise_label(name)
        if len(key) < _MIN_LABEL_CHARS:
            return None
        return self._labels.get(key)

    def equivalent_curies(self, mondo_id: str, prefix: str = "") -> list[str]:
        """Every source id mapping to `mondo_id`, optionally one vocabulary.

        This is what lets an OMIM-only result reach Orphanet data: resolve the
        OMIM id to Mondo, then ask which ORPHA code denotes the same disease.

        Sorted NUMERICALLY, not lexicographically. A disease often carries
        several codes in one vocabulary — a general entry and its subtypes —
        and the lower number is the older, more general one. String sorting
        returned `ORPHA:284963` for Marfan syndrome, a subtype, in preference
        to `ORPHA:558`, the disease itself, because "2" sorts before "5".
        """
        found = self._by_mondo.get(mondo_id, [])
        if prefix:
            found = [c for c in found if c.startswith(prefix)]
        return sorted(found, key=_curie_sort_key)

    def orpha_code_for(self, *, curie: str = "", name: str = "") -> str | None:
        """The ORPHA code for a disease identified any way at all.

        An ORPHA code passes straight through, so a caller never has to branch
        on which vocabulary it happens to hold.
        """
        cleaned = curie.strip()
        if cleaned.startswith("ORPHA:"):
            return cleaned
        mondo_id = self.resolve(curie=cleaned, name=name)
        if mondo_id is None:
            return None
        codes = self.equivalent_curies(mondo_id, prefix="ORPHA:")
        return codes[0] if codes else None

    def resolve(self, *, curie: str = "", name: str = "") -> str | None:
        """Best available identity for a disease.

        Curie first: it is an assertion by whoever produced the data, while a
        name is a string that happens to match. Falls back to the name only
        when the curie resolves to nothing.
        """
        if curie:
            resolved = self.resolve_curie(curie)
            if resolved is not None:
                return resolved
        if name:
            return self.resolve_name(name)
        return None


@lru_cache(maxsize=2)
def load_mondo_index(path: Path) -> MondoIndex | None:
    """Load and cache the index, or `None` if absent or unreadable.

    Absent is ordinary, not an error: the index is a build artifact baked into
    the image and a local checkout will not have one. Callers fall back to
    name comparison, which is what they did before this existed.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        index = MondoIndex(names=data["names"], xrefs=data["xrefs"], labels=data["labels"])
    except FileNotFoundError:
        logger.info("mondo: no index at %s; falling back to name matching", path)
        return None
    except Exception as exc:  # noqa: BLE001 - a bad index must not fail a review
        logger.warning("mondo: could not load index at %s: %s", path, exc)
        return None
    logger.info("mondo: loaded %d diseases from %s", index.size, path)
    return index

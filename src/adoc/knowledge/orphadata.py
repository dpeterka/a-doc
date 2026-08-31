"""Orphanet reference data — what a condition is, how rare, when, how inherited.

The disease-lookup tool could already say what a condition characteristically
*involves*, from HPO annotations. It could not say what the condition IS. A
list of features is useful to a clinician and close to useless to the person
whose case file it is; a curated one-paragraph definition is the opposite.

Orphanet supplies four things HPO does not:

  definition    a curated paragraph, written to be read
  prevalence    how rare, with the type and geography kept alongside
  onset         typical age of onset
  inheritance   pattern, where one applies

Keyed by ORPHA code. A disease identified only by an OMIM id reaches this
through `MondoIndex.orpha_code_for`, which is why the Mondo work came first.

## Absence is reported, not hidden

Of 11,645 Orphanet disorders, 6,898 have a definition and 6,728 have a
prevalence — so roughly two in five have neither. Orphanet also records
"Unknown" as a real prevalence value for a disease nobody has measured, and
that is worth showing: "prevalence unknown" is a fact about the state of
knowledge, while a blank looks like a bug in this tool.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Prevalence(BaseModel):
    """How rare, with the qualifiers that give the number meaning.

    `type` and `geography` are not decoration. "1-5 / 10 000" means something
    different as a point prevalence in Europe than as an annual incidence
    worldwide, and dropping them would leave a number that reads as more
    universal than it is.
    """

    value: str
    type: str = ""
    geography: str = ""

    def render(self) -> str:
        bits = [self.value]
        if self.type:
            bits.append(self.type.lower())
        if self.geography and self.geography != "Worldwide":
            bits.append(f"in {self.geography}")
        return " — ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0]


class OrphaRecord(BaseModel):
    """One Orphanet disorder."""

    orpha_code: str
    name: str = ""
    definition: str = ""
    prevalence: Prevalence | None = None
    onset: list[str] = Field(default_factory=list)
    inheritance: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether this record carries nothing worth showing.

        A name alone is not worth a section: the caller already knows the name,
        which is how it found the record.
        """
        return not (self.definition or self.prevalence or self.onset or self.inheritance)


class OrphaIndex:
    """The compacted Orphanet products."""

    def __init__(self, diseases: dict[str, dict]) -> None:
        self._diseases = diseases

    @property
    def size(self) -> int:
        return len(self._diseases)

    def get(self, orpha_code: str) -> OrphaRecord | None:
        raw = self._diseases.get(orpha_code.strip())
        if raw is None:
            return None
        prevalence_raw = raw.get("prevalence")
        return OrphaRecord(
            orpha_code=orpha_code,
            name=str(raw.get("name", "")),
            definition=str(raw.get("definition", "")),
            prevalence=Prevalence(**prevalence_raw) if isinstance(prevalence_raw, dict) else None,
            onset=list(raw.get("onset", [])),
            inheritance=list(raw.get("inheritance", [])),
        )


@lru_cache(maxsize=2)
def load_orpha_index(path: Path) -> OrphaIndex | None:
    """Load and cache, or `None` if absent or unreadable.

    Absent is ordinary: the index is a build artifact and a local checkout
    will not have one. Callers fall back to the HPO-annotation view, which is
    what they showed before this existed.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        index = OrphaIndex(diseases=data["diseases"])
    except FileNotFoundError:
        logger.info("orphadata: no index at %s; definitions unavailable", path)
        return None
    except Exception as exc:  # noqa: BLE001 - a bad index must not fail a turn
        logger.warning("orphadata: could not load index at %s: %s", path, exc)
        return None
    logger.info("orphadata: loaded %d disorders from %s", index.size, path)
    return index


def render_record(record: OrphaRecord) -> list[str]:
    """The lines for one disorder, or nothing if it carries nothing.

    Ordered definition-first because that is what a reader wants and the rest
    is qualification. "Unknown" prevalence is shown rather than suppressed: it
    is a fact about the state of knowledge, and a blank would read as a
    failure of this tool.
    """
    if record.is_empty:
        return []
    lines: list[str] = []
    if record.definition:
        lines.append(f"- {record.definition}")
    if record.prevalence is not None:
        lines.append(f"- How common: {record.prevalence.render()}")
    if record.onset:
        lines.append(f"- Typically begins: {', '.join(record.onset)}")
    if record.inheritance:
        lines.append(f"- Inheritance: {', '.join(record.inheritance)}")
    return lines

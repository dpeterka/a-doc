"""Deterministic specimen back-fill for existing rows — NO LLM (CLAUDE.md:
deterministic logic is plain code with unit tests, never delegated to a
model).

`adoc labs-infer-specimen` (`cli.py`) runs `infer_unknown_specimens` as a
maintenance pass over rows still carrying the pre-migration default,
`specimen == "unknown"`. Inference looks ONLY at the row's source
document's filename/`doc_type` for an unambiguous keyword — never at the
row's own data — and is deliberately conservative: anything that doesn't
match a known keyword is left `"unknown"` rather than guessed. Running it
twice is a no-op the second time (idempotent): a row this pass already
updated is no longer `"unknown"`, so the second run's query simply finds
nothing left to do for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adoc.labs.db import LabsDb
from adoc.labs.models import Specimen

# Keyword -> specimen, checked against `f"{filename} {doc_type}".lower()`.
# Conservative on purpose (module docstring): only these two, unambiguous
# section/report keywords infer anything; every other row is left
# "unknown" rather than guessed at.
_KEYWORDS: tuple[tuple[tuple[str, ...], Specimen], ...] = (
    (("urinalysis", "urine"), "urine"),
    (("stool",), "stool"),
)


def infer_specimen_from_document(*, filename: str, doc_type: str) -> Specimen | None:
    """Infer a specimen from `filename`/`doc_type` keywords only.

    Returns `None` (never guess) unless one of `_KEYWORDS`'s keyword sets
    matches - see the module docstring.
    """
    haystack = f"{filename} {doc_type}".lower()
    for keywords, specimen in _KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return specimen
    return None


@dataclass(frozen=True)
class SpecimenInferenceReport:
    """Outcome of one `infer_unknown_specimens` pass."""

    updated: int
    remaining_unknown: int
    by_specimen: dict[str, int] = field(default_factory=dict)


def infer_unknown_specimens(db: LabsDb) -> SpecimenInferenceReport:
    """Update every row with `specimen == "unknown"` whose source document's
    filename/doc_type gives an unambiguous keyword match (see
    `infer_specimen_from_document`); leave the rest as `"unknown"`.

    Idempotent: a row this call updates is excluded from
    `LabsDb.rows_with_unknown_specimen()` on any later call, since its
    `specimen` is no longer `"unknown"`.
    """
    by_specimen: dict[str, int] = {}
    updated = 0
    remaining = 0
    documents: dict[str, tuple[str, str]] = {}

    for row in db.rows_with_unknown_specimen():
        if row.source_doc not in documents:
            doc = db.get_document(row.source_doc)
            documents[row.source_doc] = (doc.filename, doc.doc_type) if doc else ("", "")
        filename, doc_type = documents[row.source_doc]

        specimen = infer_specimen_from_document(filename=filename, doc_type=doc_type)
        if specimen is None:
            remaining += 1
            continue

        assert row.id is not None  # rows read back from the db always have one
        db.update_specimen(row.id, specimen)
        updated += 1
        by_specimen[specimen] = by_specimen.get(specimen, 0) + 1

    return SpecimenInferenceReport(
        updated=updated, remaining_unknown=remaining, by_specimen=by_specimen
    )

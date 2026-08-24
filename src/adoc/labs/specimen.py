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

**Combined-panel safeguard (D2)**: a document keyword is document-wide
("urinalysis-2026-05-02.pdf"), but a single document can print MORE than
one panel (a combined urinalysis + CBC/CMP visit report is common) — the
keyword alone can't tell which of the document's *rows* it actually
describes. Blindly stamping every still-unknown row of the document would
mislabel a CBC/CMP/thyroid/inflammation/autoimmune-serology row (drawn
from serum/whole blood) as urine/stool just because it shares a document
with a urinalysis section. Two guards, applied per document before any row
is touched:

  1. A row whose canonical analyte is a member of `SERUM_PANEL_ANALYTES`
     (the CBC/CMP/thyroid/inflammation/autoimmune-serology core panel,
     built from `labs.validate.ANALYTE_SPECS`) is NEVER stamped with a
     urine/stool keyword, even standing alone — that overlap (e.g.
     "glucose", printed on both a urinalysis dipstick and a serum chemistry
     panel) is exactly the ambiguity this dimension exists to catch, so the
     row is left `"unknown"` for a human to confirm instead.
  2. If a document's still-unknown rows include BOTH a serum-panel analyte
     AND a non-panel one (the mixed-panel signal - a real combined-panel
     document), the WHOLE document is too ambiguous to touch: no row in it
     is stamped at all, reported as "mixed panel - left unknown", even
     though some of those rows individually would have passed guard 1.

A pure urinalysis/stool document (no serum-panel analyte among its rows at
all) is unaffected by either guard - every eligible row still gets
stamped, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult, Specimen
from adoc.labs.validate import ANALYTE_SPECS, canonicalize

# Keyword -> specimen, checked against `f"{filename} {doc_type}".lower()`.
# Conservative on purpose (module docstring): only these two, unambiguous
# section/report keywords infer anything; every other row is left
# "unknown" rather than guessed at.
_KEYWORDS: tuple[tuple[tuple[str, ...], Specimen], ...] = (
    (("urinalysis", "urine"), "urine"),
    (("stool",), "stool"),
)

# The CBC/CMP/thyroid/inflammation/autoimmune-serology core panel (module
# docstring, D2) - drawn from serum/whole blood, never urine/stool.
# Deliberately excludes the vitamin/iron panel (vitamin D, ferritin, TSAT)
# and "score" analytes (T-score, Z-score, FRAX): those aren't part of the
# routine serum/whole-blood core panel a urinalysis/stool keyword could be
# confused with. Filtered against `ANALYTE_SPECS` itself (rather than a
# bare literal set) so a rename/removal there can't silently leave a stale
# name in this set.
_SERUM_PANEL_CANONICAL_NAMES: frozenset[str] = frozenset(
    {
        # CBC
        "WBC",
        "RBC",
        "hemoglobin",
        "hematocrit",
        "platelets",
        # CMP
        "sodium",
        "potassium",
        "creatinine",
        "ALT",
        "AST",
        "glucose",
        "calcium",
        "albumin",
        # Inflammation
        "CRP",
        "ESR",
        # Thyroid
        "TSH",
        "free T4",
        # Autoimmune serology
        "ANA titer",
        "anti-dsDNA",
        "RF",
        "anti-CCP",
        "C3",
        "C4",
    }
)

SERUM_PANEL_ANALYTES: frozenset[str] = frozenset(
    name for name in _SERUM_PANEL_CANONICAL_NAMES if name in ANALYTE_SPECS
)


def _canonical_analyte(row: LabResult) -> str:
    """`row`'s canonical analyte name, falling back to its stored `name`
    for one `canonicalize` doesn't recognize (unknown analytes are never
    treated as a serum-panel match - see `SERUM_PANEL_ANALYTES`)."""
    return canonicalize(row.name) or row.name


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
    """Outcome of one `infer_unknown_specimens` pass.

    `skipped_serum_panel` and `mixed_panel_docs` (D2) are always counted
    even though the affected rows stay `"unknown"` (folded into
    `remaining_unknown` too) - so a caller can tell "nothing matched a
    keyword" apart from "matched, but held back as ambiguous"."""

    updated: int
    remaining_unknown: int
    by_specimen: dict[str, int] = field(default_factory=dict)
    skipped_serum_panel: int = 0
    mixed_panel_docs: tuple[str, ...] = field(default_factory=tuple)


def infer_unknown_specimens(db: LabsDb) -> SpecimenInferenceReport:
    """Update every row with `specimen == "unknown"` whose source document's
    filename/doc_type gives an unambiguous keyword match (see
    `infer_specimen_from_document`), subject to the combined-panel
    safeguard (module docstring, D2); leave the rest as `"unknown"`.

    Idempotent: a row this call updates is excluded from
    `LabsDb.rows_with_unknown_specimen()` on any later call, since its
    `specimen` is no longer `"unknown"`. A document held back entirely by
    the mixed-panel guard is re-evaluated identically on the next run (none
    of its rows changed), so it stays reported as mixed-panel until a human
    resolves it (e.g. by correcting rows' specimens directly).
    """
    by_specimen: dict[str, int] = {}
    updated = 0
    remaining = 0
    skipped_serum_panel = 0
    mixed_panel_docs: list[str] = []
    documents: dict[str, tuple[str, str]] = {}

    rows_by_doc: dict[str, list[LabResult]] = {}
    for row in db.rows_with_unknown_specimen():
        rows_by_doc.setdefault(row.source_doc, []).append(row)

    for source_doc, rows in rows_by_doc.items():
        if source_doc not in documents:
            doc = db.get_document(source_doc)
            documents[source_doc] = (doc.filename, doc.doc_type) if doc else ("", "")
        filename, doc_type = documents[source_doc]

        specimen = infer_specimen_from_document(filename=filename, doc_type=doc_type)
        if specimen is None:
            remaining += len(rows)
            continue

        # Mixed-panel signal (D2, guard 2): this document's still-unknown
        # rows canonicalize to at least one serum-panel analyte AND at
        # least one non-panel one - too ambiguous to auto-stamp any of
        # them, even the non-panel ones that would individually pass
        # guard 1 below.
        canonical_names = {_canonical_analyte(row) for row in rows}
        has_panel_analyte = any(name in SERUM_PANEL_ANALYTES for name in canonical_names)
        has_non_panel_analyte = any(name not in SERUM_PANEL_ANALYTES for name in canonical_names)
        if has_panel_analyte and has_non_panel_analyte:
            mixed_panel_docs.append(source_doc)
            remaining += len(rows)
            continue

        for row in rows:
            if _canonical_analyte(row) in SERUM_PANEL_ANALYTES:
                # Guard 1: a serum/whole-blood core-panel analyte is never
                # stamped with a urine/stool keyword, even standing alone.
                skipped_serum_panel += 1
                remaining += 1
                continue

            assert row.id is not None  # rows read back from the db always have one
            db.update_specimen(row.id, specimen)
            updated += 1
            by_specimen[specimen] = by_specimen.get(specimen, 0) + 1

    return SpecimenInferenceReport(
        updated=updated,
        remaining_unknown=remaining,
        by_specimen=by_specimen,
        skipped_serum_panel=skipped_serum_panel,
        mixed_panel_docs=tuple(mixed_panel_docs),
    )

"""Build the phenotype profile from text already on disk.

Sources, in the order they are scanned:

**Encounter bodies** — every one, not just the recent window, now that they
are indexed and readable. Each match takes the encounter's own date, so a
symptom recorded in 2024 is dated 2024 rather than presented as current.

**Not the case summary or patient-theory files.** They were scanned in the
first version and it was a mistake of principle, not of volume (it added one
term). Those files discuss the ledger's own HYPOTHESES, and LIRICAL exists to
produce a differential *independently* of the ledger — the third
mechanistically independent check alongside the cross-family Challenger and
the ledger-blind panel. A profile built partly from hypothesis prose would
feed the engine the very conclusions it is supposed to arrive at on its own,
which is the anti-anchoring failure this system is designed around. A
phenotype term must come from something OBSERVED, not from something
proposed.

Deterministic throughout: `knowledge.hpo` matches published labels and
synonyms, so a phrase either resolves to a real term or it does not.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from adoc.casefile.encounters import read_encounter
from adoc.casefile.phenotype import PhenotypeProfile, PhenotypeTerm, merge_terms
from adoc.casefile.repo import DataRepo
from adoc.knowledge.hpo import HpoIndex

logger = logging.getLogger(__name__)

ENCOUNTERS_RELDIR = "case/encounters"
_PROSE_RELPATHS: tuple[str, ...] = ()
"""Deliberately empty — see the module docstring. Kept as a named constant
rather than deleting the loop, because the tempting change is to add
`case-summary.md` back for coverage and the reason not to belongs here."""


class PhenotypeBackfillReport(BaseModel):
    scanned: int = 0
    matched: int = 0
    present: int = 0
    excluded: int = 0
    skipped: list[str] = []


def backfill_phenotype(
    repo: DataRepo, index: HpoIndex, profile: PhenotypeProfile
) -> tuple[PhenotypeProfile, PhenotypeBackfillReport]:
    """Scan the case file and fold every HPO term found into `profile`."""
    report = PhenotypeBackfillReport()
    result = profile

    directory = repo.root / ENCOUNTERS_RELDIR
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            try:
                encounter = read_encounter(path)
                when = encounter.frontmatter.date
            except Exception:  # noqa: BLE001 - one bad file costs itself
                report.skipped.append(path.name)
                continue
            report.scanned += 1
            result = merge_terms(
                result,
                _terms_from(
                    index,
                    path.read_text(encoding="utf-8"),
                    source_ref=f"encounter:{path.name}",
                    when=when,
                ),
            )

    for relpath in _PROSE_RELPATHS:
        try:
            text = repo.read(relpath)
        except FileNotFoundError:
            continue
        report.scanned += 1
        # No date: these files are rolling summaries, not dated events, and
        # stamping them with today would claim a finding was observed today.
        result = merge_terms(result, _terms_from(index, text, source_ref=relpath, when=None))

    report.matched = len(result.entries)
    report.present = sum(1 for e in result.entries if e.present)
    report.excluded = report.matched - report.present
    return result, report


def _terms_from(
    index: HpoIndex, text: str, *, source_ref: str, when: object
) -> list[PhenotypeTerm]:
    from datetime import date as _date

    stamp = when if isinstance(when, _date) else None
    return [
        PhenotypeTerm(
            term_id=match.term_id,
            label=match.label,
            present=match.present,
            first_seen=stamp,
            last_seen=stamp,
            sources=[source_ref],
            matched_text=[match.context or match.matched_text],
        )
        for match in index.find_terms(text)
    ]

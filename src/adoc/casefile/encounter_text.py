"""Keep the encounter full-text index in step with the repo.

ADR 0015 gave *documents* a searchable corpus. Encounters never got one, so
an encounter body reached a reasoner as the one summary line
`_recent_encounters_section` renders — measured on the real case file, a
110-line regimen encounter contributed 107 characters and 3,446 characters
reached no model at all.

That gap is not specific to the regimen. Every patient-report encounter
written from a chat turn has it: once the encounter falls outside the recent
window, everything the patient said in it is unreachable.

The repo is the source of truth and `labs.sqlite` is derived (PLAN.md
"State"), so this module only ever rebuilds the index FROM the files on disk.
It never writes an encounter.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.labs.db import LabsDb

ENCOUNTERS_RELDIR = "case/encounters"


class EncounterTextSyncReport(BaseModel):
    """What a sync did — reported rather than assumed, so a run that indexes
    nothing is visible instead of looking like success."""

    indexed: int = 0
    pruned: int = 0
    failed: list[str] = []


def sync_encounter_text(repo: DataRepo, db: LabsDb) -> EncounterTextSyncReport:
    """Index every encounter body, and drop any the repo no longer has.

    Idempotent and cheap — encounters are a few dozen small markdown files —
    so it is safe to call after writing one and on every backfill.

    A file that fails to parse is recorded and skipped rather than aborting
    the sweep: one malformed encounter must not cost the index every other
    one.
    """
    report = EncounterTextSyncReport()
    directory = repo.root / ENCOUNTERS_RELDIR
    on_disk: set[str] = set()

    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            on_disk.add(path.name)
            try:
                encounter = read_encounter(path)
                when = encounter.frontmatter.date.isoformat()
            except Exception:  # noqa: BLE001 - a bad file costs itself, not the sweep
                report.failed.append(path.name)
                continue
            db.upsert_encounter_text(path.name, when, path.read_text(encoding="utf-8"))
            report.indexed += 1

    for stale in db.encounter_text_filenames() - on_disk:
        db.delete_encounter_text(stale)
        report.pruned += 1

    return report


def encounter_path(repo: DataRepo, filename: str) -> Path:
    return repo.root / ENCOUNTERS_RELDIR / filename

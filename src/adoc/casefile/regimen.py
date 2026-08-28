"""What the patient is taking, and **when** — `case/regimen.yaml`.

Medications and supplements were modelled by `intake.sections.Medication` /
`Supplement` as `name / dose / frequency / still_taking: bool / notes`. That
boolean is the entire temporal model, and it cannot answer the question this
case actually turns on:

    Was she taking biotin when the 2026-07-15 assay ran?

High-dose biotin distorts many hormone and antibody immunoassays. Whether her
FSH, thyroid and antibody results are real or artefactual therefore depends
on an *interval* overlapping a *specimen collection date*. A boolean cannot
express that, and it also cannot tell a supplement stopped two years ago from
one stopped last week — both are `still_taking=False`.

It was also the one place in the system that kept a boolean where everything
else models time properly: `EncounterFrontmatter` carries `date_precision`
and `reported_on` (ADR 0027), `IntakeFact` carries `date_approx` /
`precision` / `reported_on`, and `LabResult` separates specimen `date` from
`created_at`.

Three rules this module exists to enforce:

**Intervals, not flags.** Every entry has a start and an optional stop, each
with its own precision, because a patient says "since about last spring" far
more often than a date.

**A restart is a new interval, never a widened one.** "Took it in 2024,
stopped, restarted in 2026" is clinically different from "took it
continuously since 2024" — merging them would fabricate exposure across a gap
that may be exactly what a lab result reflects.

**Unknown is not false.** An entry with no start date is `unknown`, and
`active_on` reports it as such rather than guessing either way. Silently
treating an undated supplement as absent on a specimen date would produce a
confident wrong answer to the biotin question.

Nothing here calls a model. A reasoning stage or the intake agent proposes
changes; deterministic code applies them, the same posture as the ledger.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from adoc.casefile.encounters import DatePrecision

REGIMEN_RELPATH = "case/regimen.yaml"

RegimenKind = Literal["medication", "supplement"]

Attribution = Literal["prescribed", "self-started", "unknown"]
"""Who put the patient on it. A prescribed medication and a self-started
supplement carry different weight when weighing a hypothesis, and conflating
them loses that."""

Overlap = Literal["active", "stopped", "not-yet-started", "unknown"]
"""Whether an entry was in force on a given date. `unknown` is a real answer,
not a failure — see the module docstring."""


class RegimenEntry(BaseModel):
    """One medication or supplement, over one continuous interval."""

    name: str
    kind: RegimenKind = "supplement"
    dose: str | None = None
    frequency: str | None = None

    started: date | None = None
    started_precision: DatePrecision | None = None
    stopped: date | None = None
    stopped_precision: DatePrecision | None = None
    """`None` while still being taken. A stop date and a `None` start is a
    legitimate combination — a patient often knows when she stopped something
    far better than when she began."""

    attribution: Attribution = "unknown"
    reported_on: date | None = None
    """When the patient said this, as distinct from when it was true. The
    same split `EncounterFrontmatter` makes (ADR 0027): recall in 2026 about
    2021 is not a 2021 record."""
    sources: list[str] = Field(default_factory=list)
    """Source refs (`encounter:<file>`, `doc:<file>`, `patient-report:<date>`)
    so a regimen claim is checkable by the same machinery as any other
    (ADR 0028)."""
    notes: str = ""

    @property
    def still_taking(self) -> bool:
        """Kept as a derived property so existing callers keep working, but it
        is a VIEW of the interval, never the source of truth."""
        return self.stopped is None

    def overlaps(self, when: date) -> Overlap:
        """Whether this entry was in force on `when`.

        Returns `unknown` when there is no start date and the answer cannot be
        inferred from a stop date, rather than defaulting either way. A
        confident wrong answer here would directly mislead the assay-
        interference question this record exists to settle.
        """
        if self.started is None and self.stopped is None:
            return "unknown"
        if self.started is not None and when < self.started:
            return "not-yet-started"
        if self.stopped is not None and when > self.stopped:
            return "stopped"
        if self.started is None:
            # Stop date only: it was being taken up to that point, so a date
            # at or before the stop is "active" and anything else is unknown.
            return "active" if when <= self.stopped else "unknown"  # type: ignore[operator]
        return "active"


class Regimen(BaseModel):
    """Everything on file about what the patient takes."""

    entries: list[RegimenEntry] = Field(default_factory=list)
    updated: date | None = None

    def active_on(self, when: date) -> list[RegimenEntry]:
        """Entries in force on `when` — the alignment a lab row needs."""
        return [e for e in self.entries if e.overlaps(when) == "active"]

    def undated(self) -> list[RegimenEntry]:
        """Entries that cannot be placed in time at all.

        Surfaced deliberately rather than hidden: these are the ones that make
        an `active_on` answer incomplete, and a reader deserves to know the
        list is partial before relying on it."""
        return [e for e in self.entries if e.overlaps(date.today()) == "unknown"]

    def by_name(self, name: str) -> list[RegimenEntry]:
        """Every interval recorded for one substance, oldest first — a restart
        is a separate entry, so this is how the full history is read back."""
        key = _normalize(name)
        matching = [e for e in self.entries if _normalize(e.name) == key]
        return sorted(matching, key=lambda e: e.started or date.min)


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def load_regimen(path: Path) -> Regimen:
    """Load `case/regimen.yaml`; a missing file is an empty regimen."""
    if not path.is_file():
        return Regimen()
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    return Regimen.model_validate(raw or {})


def save_regimen(path: Path, regimen: Regimen) -> None:
    """Write `regimen` as stable, human-diffable YAML — the same
    `model_dump(mode="json")` convention `save_ledger` uses, so repeated saves
    of identical content produce byte-identical files and git diffs stay
    meaningful."""
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(regimen.model_dump(mode="json"), fh)


def merge_entries(regimen: Regimen, proposed: Iterable[RegimenEntry]) -> Regimen:
    """Fold `proposed` into `regimen`, deterministically.

    An entry matching an OPEN interval for the same substance updates that
    interval (filling in a dose, or recording a stop). Anything else is
    appended as a new interval — because a restart after a gap is a different
    exposure, and widening the old one would fabricate continuity across
    exactly the period a lab result might reflect.
    """
    merged = list(regimen.entries)
    for entry in proposed:
        key = _normalize(entry.name)
        open_match = next(
            (
                existing
                for existing in merged
                if _normalize(existing.name) == key and existing.stopped is None
            ),
            None,
        )
        if open_match is None:
            merged.append(entry)
            continue
        # Fill gaps and record a stop; never overwrite a known value with None.
        for field_name in ("dose", "frequency", "started", "started_precision", "reported_on"):
            if getattr(open_match, field_name) is None and getattr(entry, field_name) is not None:
                setattr(open_match, field_name, getattr(entry, field_name))
        if entry.stopped is not None:
            open_match.stopped = entry.stopped
            open_match.stopped_precision = entry.stopped_precision
        if entry.attribution != "unknown" and open_match.attribution == "unknown":
            open_match.attribution = entry.attribution
        for source in entry.sources:
            if source not in open_match.sources:
                open_match.sources.append(source)
    return Regimen(entries=merged, updated=regimen.updated)

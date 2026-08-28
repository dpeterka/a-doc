"""Results the patient remembers but has no document for — `case/reported-results.yaml`.

    "I know I had a lab in November 2024 but my portal only keeps a year.
     I know my iron was high."

Today that sentence becomes prose in an intake fact and is invisible to every
numeric consumer: trends, `query_labs`, the criteria scorers, the lab
sections. `LabResult.source_doc` is a required 64-character sha256, so a
remembered value has no representation at all.

That requirement is right and stays. A remembered value must never sit in the
measured series as though it were sourced — the whole citation apparatus
depends on a lab row tracing to a document. But the current behaviour is not
strictness, it is LOSS: the missing year is exactly the thing she is telling
us about, and a differential built without it is missing evidence she knows
exists.

So a reported result is a first-class record in its own file, with the same
posture the regimen record takes toward time: state what is known, never more.

**It is never merged into the measured series.** `to_lab_result` does not
exist here on purpose. These are read alongside labs and labelled, never
mixed into them.

**It can be corroborated later.** When a document finally arrives carrying
that analyte near that date, `corroborate` links the two — and says so when
the document DISAGREES, which is at least as informative.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from adoc.casefile.encounters import DatePrecision

REPORTED_RESULTS_RELPATH = "case/reported-results.yaml"

Verification = Literal["unverified", "corroborated", "contradicted"]
"""Mirrors `intake.facts.Corroboration`. A reported result starts
`unverified` and stays that way until a document speaks to it — it is never
promoted by repetition."""

Direction = Literal["high", "low", "normal", "positive", "negative", "unknown"]
"""What the patient actually remembers. People recall "my iron was high" far
more often than a number, and a record that can only hold numbers would
discard the commonest case."""


class ReportedResult(BaseModel):
    """One lab result the patient remembers."""

    analyte: str
    """Her words — "iron", "thyroid", "vitamin D". Canonicalised separately
    so the original is never lost."""
    canonical_name: str | None = None
    """The `ANALYTE_SPECS` name this maps onto, when it maps. `None` is
    common and fine: it means the reported result cannot be lined up with a
    measured series automatically, not that it is worthless."""

    direction: Direction = "unknown"
    value: float | None = None
    unit: str | None = None
    value_text: str | None = None

    when: date | None = None
    when_precision: DatePrecision | None = None
    reported_on: date | None = None
    """When she said it, as distinct from when the result is from."""

    verification: Verification = "unverified"
    corroborating_source: str | None = None
    """The `doc:`/`labs:` ref that confirmed or contradicted it."""
    note: str = ""
    sources: list[str] = Field(default_factory=list)
    """`patient-report:<date>` — so a reported result is citable by the same
    grammar as everything else (ADR 0028), and a reasoner quoting one is
    quoting something checkable back to the conversation that produced it."""


class ReportedResults(BaseModel):
    entries: list[ReportedResult] = Field(default_factory=list)
    updated: date | None = None

    def unverified(self) -> list[ReportedResult]:
        return [e for e in self.entries if e.verification == "unverified"]


def load_reported_results(path: Path) -> ReportedResults:
    if not path.is_file():
        return ReportedResults()
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    return ReportedResults.model_validate(raw or {})


def save_reported_results(path: Path, results: ReportedResults) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(results.model_dump(mode="json"), fh)


def _key(entry: ReportedResult) -> tuple[str, str]:
    analyte = "".join(c for c in (entry.canonical_name or entry.analyte).lower() if c.isalnum())
    return analyte, entry.when.isoformat() if entry.when else ""


def merge_reported(results: ReportedResults, proposed: list[ReportedResult]) -> ReportedResults:
    """Fold in new reports, without duplicating a repeat of the same claim.

    Deduped on (analyte, date). Saying the same thing twice is not two pieces
    of evidence, and a record that grew every time she mentioned her iron
    would overstate it.
    """
    existing = {_key(e): e for e in results.entries}
    for entry in proposed:
        current = existing.get(_key(entry))
        if current is None:
            existing[_key(entry)] = entry
            continue
        for source in entry.sources:
            if source not in current.sources:
                current.sources.append(source)
        # Fill gaps only; a vaguer repeat must not erase a detail already given.
        for field_name in ("value", "unit", "value_text", "canonical_name", "when_precision"):
            if getattr(current, field_name) is None and getattr(entry, field_name) is not None:
                setattr(current, field_name, getattr(entry, field_name))
        if current.direction == "unknown" and entry.direction != "unknown":
            current.direction = entry.direction
    return ReportedResults(entries=list(existing.values()), updated=results.updated)

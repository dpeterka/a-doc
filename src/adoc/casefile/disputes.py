"""When the patient says the record is wrong — `case/disputes.yaml`.

    "You reported I had a pituitary scan in 2026. This did not occur.
     Your information is wrong."

Before this there was no answer to that sentence. `retract_fact` reaches
intake facts only, and a pituitary MRI is an ingested encounter, so her
correction became a new patient-report encounter while the original stayed —
still cited, still shaping the differential, still reappearing in the next
review with full confidence.

Three reasons that mattered more than a missing feature:

- **Trust.** Telling a system it is wrong and watching nothing change is
  corrosive in a way an absent capability is not.
- **Documents are wrong sometimes** — misfiled, wrong patient, a study
  ordered and then cancelled, a duplicate under a different date.
- **It propagates silently.** A phantom study shapes a differential exactly
  as a real one does.

**A dispute never deletes anything.** The archived document remains the
source of truth: she may be misremembering, and a system that erased records
on request would be worse than one that ignored them. What a dispute does is
make the CONFLICT visible everywhere the item appears, stop the item being
asserted as established fact, and put it in front of a human.

Both sides are recorded, neither is silently overwritten.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

DISPUTES_RELPATH = "case/disputes.yaml"

DisputeStatus = Literal["open", "upheld", "dismissed"]
"""`open` until a human decides. `upheld` means the patient was right and the
item should be disregarded; `dismissed` means the record stands. Only a human
moves it off `open` — a model that could dismiss a patient's correction would
defeat the point of recording it."""

DisputeKind = Literal["did-not-occur", "wrong-date", "wrong-detail", "not-mine"]
"""What is being disputed. "not-mine" is separated because a misfiled
document belonging to someone else is a different and more serious problem
than a wrong date, and it should not be buried in a generic category."""


class Dispute(BaseModel):
    """One patient objection to something on file."""

    target: str
    """The disputed item's source ref — `encounter:<file>`, `doc:<file>`,
    `labs:<slug>:<date>`. Validated before a dispute is written: a dispute
    against something that does not exist would sit in the file forever
    matching nothing."""
    kind: DisputeKind = "wrong-detail"
    statement: str
    """The patient's own words. Kept verbatim because a paraphrase of a
    correction is exactly the wrong thing to store."""
    reported_on: date
    status: DisputeStatus = "open"
    resolution_note: str = ""
    resolved_on: date | None = None


class Disputes(BaseModel):
    entries: list[Dispute] = Field(default_factory=list)

    def open_targets(self) -> set[str]:
        """Refs with an unresolved dispute — what a renderer needs to know to
        mark an item."""
        return {d.target for d in self.entries if d.status == "open"}

    def for_target(self, target: str) -> list[Dispute]:
        return [d for d in self.entries if d.target == target]


def load_disputes(path: Path) -> Disputes:
    if not path.is_file():
        return Disputes()
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    return Disputes.model_validate(raw or {})


def save_disputes(path: Path, disputes: Disputes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(disputes.model_dump(mode="json"), fh)


def add_dispute(disputes: Disputes, dispute: Dispute) -> tuple[Disputes, bool]:
    """Record `dispute` unless the same objection is already open.

    Returns `(disputes, added)`. Repeating an objection does not create a
    second one — but it does not close the first either, and the returned
    flag lets a caller avoid a pointless commit.
    """
    for existing in disputes.entries:
        if existing.target == dispute.target and existing.status == "open":
            return disputes, False
    return Disputes(entries=[*disputes.entries, dispute]), True

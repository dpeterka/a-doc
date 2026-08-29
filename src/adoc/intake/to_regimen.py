"""Fold intake medication and supplement facts into the regimen record.

Intake wrote `case/medications.md` — 1,549 bytes of real patient-reported
medication on the live case file — and nothing ever read it. Adding that
prose to the context pack would make it visible, but it would still be the
wrong shape: a list of names cannot answer the question the record exists
for, which is whether she was taking something when a specimen was drawn.

`case/regimen.yaml` can (ADR 0031). It carries intervals with per-endpoint
precision, attestation dates, attribution and source refs, and the context
pack already renders it against lab collection dates. So medications and
supplements converge there rather than into a second, weaker representation.

**The temporal information intake captured is a boolean**, `still_taking`,
which is exactly the model ADR 0031 replaced. Converting it honestly:

- `still_taking=True` -> an OPEN interval attested on the date she said it.
  Not a start date: she said she takes it, not when she began.
- `still_taking=False` -> a CLOSED interval with an unknown start and an
  unknown stop. Recording today as the stop would claim she stopped during
  the conversation, and recording any start would be invention.

The second case is why `RegimenEntry.stopped` is not simply set to the
report date: "no longer taking" places the substance in the past without
saying when, and `overlaps()` answers `unknown` for it — which is the truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from adoc.casefile.regimen import Regimen, RegimenEntry, merge_entries
from adoc.intake.facts import IntakeFact


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def facts_to_regimen_entries(
    facts: Sequence[IntakeFact], *, reported_on: date
) -> list[RegimenEntry]:
    """Every active medication/supplement fact, as regimen entries."""
    entries: list[RegimenEntry] = []
    for fact in facts:
        if fact.kind not in {"medication", "supplement"}:
            continue
        if getattr(fact, "status", "active") not in {"active", None}:
            continue

        name = _text(fact.fields.get("name")) or fact.statement
        if not name:
            continue

        still_taking = bool(fact.fields.get("still_taking", True))
        entry = RegimenEntry(
            name=name,
            kind="medication" if fact.kind == "medication" else "supplement",
            dose=_text(fact.fields.get("dose")),
            frequency=_text(fact.fields.get("frequency")),
            # Intake does not distinguish prescribed from self-started, and
            # guessing from the kind would be wrong: plenty of supplements are
            # advised by a clinician and plenty of medications are not current.
            attribution="unknown",
            reported_on=reported_on,
            sources=[f"patient-report:{reported_on.isoformat()}"],
            notes=_text(fact.fields.get("notes")) or "",
        )
        if still_taking:
            entry.attested_on = [reported_on]
        # `still_taking=False` leaves BOTH endpoints unset: she is not on it
        # now, and nothing on file says when that changed. `overlaps()`
        # reports `unknown`, which is what is actually known.
        entries.append(entry)
    return entries


def merge_intake_medications(
    regimen: Regimen, facts: Sequence[IntakeFact], *, reported_on: date
) -> Regimen:
    """Fold intake-derived entries into `regimen`, without duplicating.

    `merge_entries` updates an open interval rather than appending, so a
    substance already recorded from a regimen document gains the intake
    dose/frequency instead of appearing twice under two spellings.
    """
    return merge_entries(regimen, facts_to_regimen_entries(facts, reported_on=reported_on))

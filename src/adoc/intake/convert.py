"""`facts_to_section_data`: maps active `IntakeFact`s onto the wizard's
existing per-section schemas (`intake.sections`), so `intake.wizard`'s
`_write_section`/`_write_*` functions stay the single source of truth for
case-file output (CLAUDE.md "Reuse the wizard's writers" — `wizard.py` is
not touched by this module beyond the small `write_section` export it adds
for this purpose).

**List-valued fields convention.** `IntakeFact.fields` is a flat
`dict[str, str | int | float | bool | None]` (facts.py) — no lists,
because a fact's `fields` doubles as the deterministic completion gates'
input and those gates only ever look at scalar keys (`by_whom`, `year`,
`reasoning`). Where a target section schema wants a list on one fact
(`Relative.conditions`, `BasicsSection.exposures`), the intake agent is
expected to record it as a single comma-separated string under the plural
key (`fields["conditions"] = "Hashimoto's, vitiligo"`); this module is
what splits it back into a list on the way to the section schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from adoc.intake.facts import IntakeFact

_SUPPORTED_SECTIONS = frozenset(
    {
        "basics",
        "symptoms",
        "events",
        "prior_diagnoses",
        "family_history",
        "medications",
        "supplements",
        "allergies",
        "care_team",
        "document_drop",
    }
)


class ConvertError(Exception):
    """Raised for a section key `facts_to_section_data` doesn't know how to convert."""


def _split_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _active(facts: Sequence[IntakeFact], section_key: str) -> list[IntakeFact]:
    return [f for f in facts if f.status == "active" and f.section == section_key]


def _basics_data(facts: Sequence[IntakeFact]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "age": None,
        "sex_at_birth": None,
        "height_cm": None,
        "weight_kg": None,
        "occupation": None,
    }
    exposures: list[str] = []
    for fact in facts:
        if fact.kind != "basic":
            continue
        for key in ("age", "sex_at_birth", "height_cm", "weight_kg", "occupation"):
            value = fact.fields.get(key)
            if value is not None:
                merged[key] = value
        exposures.extend(_split_list(fact.fields.get("exposures")))
        single_exposure = fact.fields.get("exposure")
        if single_exposure:
            exposures.append(str(single_exposure).strip())
    # de-dupe, preserve first-seen order
    seen: set[str] = set()
    deduped_exposures = [e for e in exposures if not (e in seen or seen.add(e))]  # type: ignore[func-returns-value]
    merged["exposures"] = deduped_exposures
    return merged


def _symptom_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "description": fact.fields.get("description") or fact.statement,
        "onset": fact.fields.get("onset") or fact.date_approx,
        "frequency": fact.fields.get("frequency"),
        "triggers": fact.fields.get("triggers"),
        "severity": fact.fields.get("severity"),
    }


def _event_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "date_approx": fact.date_approx,
        "title": fact.fields.get("title") or fact.statement,
        "description": fact.fields.get("description") or fact.statement,
    }


def _diagnosis_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": fact.fields.get("name") or fact.statement,
        "by_whom": fact.fields.get("by_whom"),
        "year": fact.fields.get("year"),
        "status": fact.fields.get("status") or "confirmed",
    }


def _patient_suspected_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": fact.fields.get("name") or fact.statement,
        "why": fact.fields.get("reasoning") or "",
    }


def _relative_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "relation": fact.fields.get("relation") or "",
        "conditions": _split_list(fact.fields.get("conditions")),
        "age_at_onset": fact.fields.get("age_at_onset"),
        "deceased": bool(fact.fields.get("deceased", False)),
        "age_at_death": fact.fields.get("age_at_death"),
    }


def _medication_like_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": fact.fields.get("name") or fact.statement,
        "dose": fact.fields.get("dose"),
        "frequency": fact.fields.get("frequency"),
        "still_taking": bool(fact.fields.get("still_taking", True)),
        "notes": fact.fields.get("notes") or "",
    }


def _allergy_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "allergen": fact.fields.get("allergen") or fact.statement,
        "reaction": fact.fields.get("reaction") or "",
        "severity": fact.fields.get("severity"),
    }


def _provider_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": fact.fields.get("name") or fact.statement,
        "specialty": fact.fields.get("specialty"),
        "org": fact.fields.get("org"),
    }


def facts_to_section_data(facts: Sequence[IntakeFact], section_key: str) -> dict[str, Any]:
    """Build the plain dict a `SectionSpec.schema.model_validate(...)` call
    (the same shape `IntakeWizard`'s `state.draft` already uses) expects for
    `section_key`, from every active fact filed to it."""
    if section_key not in _SUPPORTED_SECTIONS:
        raise ConvertError(f"no fact converter registered for section {section_key!r}")

    active = _active(facts, section_key)

    if section_key == "basics":
        return _basics_data(active)
    if section_key == "symptoms":
        return {"symptoms": [_symptom_data(f) for f in active if f.kind == "symptom"]}
    if section_key == "events":
        return {"events": [_event_data(f) for f in active if f.kind == "event"]}
    if section_key == "prior_diagnoses":
        diagnoses = [
            _diagnosis_data(f)
            for f in active
            if f.kind == "diagnosis" and f.attribution == "doctor_diagnosed"
        ]
        patient_suspected = [
            _patient_suspected_data(f)
            for f in active
            if (f.kind == "diagnosis" and f.attribution == "patient_assumption")
            or f.kind == "patient_theory"
        ]
        return {"diagnoses": diagnoses, "patient_suspected": patient_suspected}
    if section_key == "family_history":
        return {"relatives": [_relative_data(f) for f in active if f.kind == "relative"]}
    if section_key == "medications":
        return {"medications": [_medication_like_data(f) for f in active if f.kind == "medication"]}
    if section_key == "supplements":
        return {"supplements": [_medication_like_data(f) for f in active if f.kind == "supplement"]}
    if section_key == "allergies":
        return {"allergies": [_allergy_data(f) for f in active if f.kind == "allergy"]}
    if section_key == "care_team":
        providers = [_provider_data(f) for f in active if f.kind == "provider"]
        insurer = next(
            (
                str(f.fields["insurer"])
                for f in active
                if f.kind == "insurance" and f.fields.get("insurer")
            ),
            None,
        )
        return {"providers": providers, "insurer": insurer}
    if section_key == "document_drop":
        acknowledged = any(bool(f.fields.get("acknowledged")) for f in active if f.kind == "note")
        return {"acknowledged": acknowledged}
    msg = f"no fact converter registered for section {section_key!r}"
    raise ConvertError(msg)  # pragma: no cover - _SUPPORTED_SECTIONS guards every section_key

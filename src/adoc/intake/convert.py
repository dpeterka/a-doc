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

**Type-coercion boundary.** `IntakeFact.fields` values are typed
`str | int | float | bool | None` (facts.py) with no further constraint per
key, but the section schemas in `intake.sections` are pure rendering DTOs
now (the conversational agent, not these schemas, is what the model fills
in) — so every text-typed schema field this module populates from `fields`
reads through `_as_text` below, and `PriorDiagnosis.status` (the one
closed-vocabulary field sourced from `fields`) reads through
`_diagnosis_status`, which clamps to the schema's own default instead of
letting an out-of-vocabulary value raise. This is what keeps a fact whose
`fields` happen to hold the "wrong" JSON type (an int where a model phrased
a value that way) from failing `model_validate` the way `Relative.
age_at_onset` used to when it was typed `int` — see that field's docstring
in `intake.sections`. `intake.agent._write_section_from_facts_safe` is
still the last-resort backstop for anything even this boundary misses.
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
        "geography",
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


def _as_text(raw: Any) -> str | None:
    """Coerce a fact field's loosely-typed value (`IntakeFact.fields` is
    `dict[str, str | int | float | bool | None]` — `intake.facts`) to the
    free-form text a section-schema `str`/`str | None` field expects.

    `fields` is deliberately untyped per-key: a model may record an age as
    the JSON number `41` or the JSON string `"41"` depending on how it
    phrases an extraction, and neither is wrong. Every text-typed field
    below reads through this helper (matching the `str(raw)` convention
    `_split_list` above already used) so that boundary is safe by
    construction, rather than trusting each call site to remember it. A
    real crash motivated this: `Relative.age_at_onset` used to be typed
    `int`, but the same class of mismatch — any `fields` value landing in
    a `str`-typed schema field, e.g. a model emitting an int for a field
    named `notes` — is possible for every field in this module, not just
    that one. `agent.py`'s `_write_section_from_facts_safe` is the
    last-resort backstop for anything this (or a future schema change)
    still misses; this is what keeps the ordinary case from ever needing
    that backstop.
    """
    if raw is None:
        return None
    return str(raw)


_VALID_DIAGNOSIS_STATUS = frozenset({"confirmed", "suspected", "ruled-out"})


def _diagnosis_status(raw: Any, *, default: str) -> str:
    """Clamp a fact's free-form `fields["status"]` to `sections.
    DiagnosisStatus`'s closed 3-value vocabulary. Unlike the free-form text
    fields this module otherwise widens via `_as_text`, `status` stays a
    `Literal` in the schema on purpose (see `PriorDiagnosis.status`'s
    docstring) — it's a downstream classification the case file and, later,
    the Ledger-Maintainer's `origin: patient` ingestion key off, not
    patient-verbatim text. So the boundary responsibility is the opposite
    of `_as_text`'s: instead of widening the schema, this narrows whatever
    came out of the loosely-typed `fields` dict (a typo, an unexpected
    value, or nothing recorded) down to a value the schema actually
    accepts, falling back to `default` rather than letting
    `model_validate` raise on an out-of-vocabulary string.
    """
    text = _as_text(raw)
    return text if text in _VALID_DIAGNOSIS_STATUS else default


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
            value = _as_text(fact.fields.get(key))
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
        "description": _as_text(fact.fields.get("description")) or fact.statement,
        "onset": _as_text(fact.fields.get("onset")) or fact.date_approx,
        "frequency": _as_text(fact.fields.get("frequency")),
        "triggers": _as_text(fact.fields.get("triggers")),
        "severity": _as_text(fact.fields.get("severity")),
    }


def _event_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "date_approx": fact.date_approx,
        "title": _as_text(fact.fields.get("title")) or fact.statement,
        "description": _as_text(fact.fields.get("description")) or fact.statement,
    }


def _diagnosis_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": _as_text(fact.fields.get("name")) or fact.statement,
        "by_whom": _as_text(fact.fields.get("by_whom")),
        "year": _as_text(fact.fields.get("year")),
        "status": _diagnosis_status(fact.fields.get("status"), default="confirmed"),
    }


def _patient_suspected_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": _as_text(fact.fields.get("name")) or fact.statement,
        "why": _as_text(fact.fields.get("reasoning")) or "",
    }


def _relative_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "relation": _as_text(fact.fields.get("relation")) or "",
        "conditions": _split_list(fact.fields.get("conditions")),
        "age_at_onset": _as_text(fact.fields.get("age_at_onset")),
        "deceased": bool(fact.fields.get("deceased", False)),
        "age_at_death": _as_text(fact.fields.get("age_at_death")),
    }


def _residence_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "place": _as_text(fact.fields.get("place")) or fact.statement,
        "date_approx": fact.date_approx or _as_text(fact.fields.get("date_approx")),
        "current": bool(fact.fields.get("current", False)),
    }


def _geography_data(facts: Sequence[IntakeFact]) -> dict[str, Any]:
    """`location`-kind facts, split by `fields["category"]`
    (`"residence"` (default) | `"travel"` | `"exposure"`) into the three
    `GeographySection` lists."""
    location_facts = [f for f in facts if f.kind == "location"]
    residences = [
        _residence_data(f)
        for f in location_facts
        if f.fields.get("category", "residence") == "residence"
    ]
    travel = [
        _as_text(f.fields.get("place")) or f.statement
        for f in location_facts
        if f.fields.get("category") == "travel"
    ]
    exposures = [
        _as_text(f.fields.get("description")) or f.statement
        for f in location_facts
        if f.fields.get("category") == "exposure"
    ]
    return {"residences": residences, "travel": travel, "exposures": exposures}


def _medication_like_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": _as_text(fact.fields.get("name")) or fact.statement,
        "dose": _as_text(fact.fields.get("dose")),
        "frequency": _as_text(fact.fields.get("frequency")),
        "still_taking": bool(fact.fields.get("still_taking", True)),
        "notes": _as_text(fact.fields.get("notes")) or "",
    }


def _allergy_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "allergen": _as_text(fact.fields.get("allergen")) or fact.statement,
        "reaction": _as_text(fact.fields.get("reaction")) or "",
        "severity": _as_text(fact.fields.get("severity")),
    }


def _provider_data(fact: IntakeFact) -> dict[str, Any]:
    return {
        "name": _as_text(fact.fields.get("name")) or fact.statement,
        "specialty": _as_text(fact.fields.get("specialty")),
        "org": _as_text(fact.fields.get("org")),
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
    if section_key == "geography":
        return _geography_data(active)
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
                _as_text(f.fields["insurer"])
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

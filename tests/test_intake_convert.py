"""Tests for adoc.intake.convert.facts_to_section_data: mapping active
`IntakeFact`s onto the wizard's existing per-section schemas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adoc.casefile.schema import Provenance
from adoc.intake.convert import ConvertError, facts_to_section_data
from adoc.intake.facts import IntakeFact
from adoc.intake.sections import (
    AllergiesSection,
    CareTeamSection,
    EventsSection,
    FamilyHistorySection,
    GeographySection,
    MedicationsSection,
    PriorDiagnosesSection,
    SupplementsSection,
    SymptomsSection,
)


def _prov() -> Provenance:
    return Provenance(
        app_version="0.0.0-test",
        prompt_template_version="1",
        model_id="fake-model",
        dag_node="intake-agent",
        timestamp=datetime.now(UTC),
    )


def _fact(**overrides: object) -> IntakeFact:
    data: dict[str, object] = {
        "id": "f1",
        "section": "symptoms",
        "kind": "symptom",
        "statement": "placeholder",
        "provenance": _prov(),
    }
    data.update(overrides)
    return IntakeFact.model_validate(data)


def test_symptoms_conversion() -> None:
    fact = _fact(
        section="symptoms",
        kind="symptom",
        statement="Joint pain.",
        date_approx="2021",
        fields={"onset": "2021", "frequency": "daily", "triggers": "cold", "severity": "moderate"},
    )
    data = facts_to_section_data([fact], "symptoms")
    section = SymptomsSection.model_validate(data)
    assert section.symptoms[0].description == "Joint pain."
    assert section.symptoms[0].onset == "2021"
    assert section.symptoms[0].severity == "moderate"


def test_events_conversion() -> None:
    fact = _fact(
        section="events",
        kind="event",
        statement="ER visit for chest pain.",
        date_approx="2019-03",
        fields={"title": "ER visit"},
    )
    data = facts_to_section_data([fact], "events")
    section = EventsSection.model_validate(data)
    assert section.events[0].title == "ER visit"
    assert section.events[0].date_approx == "2019-03"


def test_prior_diagnoses_conversion_splits_doctor_diagnosed_and_patient_assumption() -> None:
    doctor = _fact(
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Hypothyroidism.",
        attribution="doctor_diagnosed",
        fields={"by_whom": "Dr. Lee", "year": 2018, "status": "confirmed"},
    )
    assumption = _fact(
        id="f2",
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Patient suspects lupus.",
        attribution="patient_assumption",
        fields={"reasoning": "joint pain and fatigue"},
    )
    theory = _fact(
        id="f3",
        section="prior_diagnoses",
        kind="patient_theory",
        statement="Patient theory: something autoimmune.",
        fields={"reasoning": "family history"},
    )
    data = facts_to_section_data([doctor, assumption, theory], "prior_diagnoses")
    section = PriorDiagnosesSection.model_validate(data)

    assert len(section.diagnoses) == 1
    assert section.diagnoses[0].name == "Hypothyroidism."
    assert section.diagnoses[0].by_whom == "Dr. Lee"
    assert section.diagnoses[0].year == 2018

    assert len(section.patient_suspected) == 2
    names = {s.name for s in section.patient_suspected}
    assert "Patient suspects lupus." in names
    assert "Patient theory: something autoimmune." in names


def test_family_history_conversion_splits_comma_separated_conditions() -> None:
    fact = _fact(
        section="family_history",
        kind="relative",
        statement="Patient's mother.",
        fields={
            "relation": "mother",
            "conditions": "Hashimoto's, vitiligo",
            "age_at_onset": 35,
            "deceased": False,
        },
    )
    data = facts_to_section_data([fact], "family_history")
    section = FamilyHistorySection.model_validate(data)
    assert section.relatives[0].relation == "mother"
    assert section.relatives[0].conditions == ["Hashimoto's", "vitiligo"]
    assert section.relatives[0].age_at_onset == 35


def test_geography_conversion_splits_residences_travel_and_exposures() -> None:
    residence = _fact(
        section="geography",
        kind="location",
        statement="Lives in rural Connecticut.",
        date_approx="2015-2020",
        fields={"place": "rural Connecticut"},
    )
    current_residence = _fact(
        id="f2",
        section="geography",
        kind="location",
        statement="Currently lives in Boston.",
        fields={"place": "Boston, MA", "current": True},
    )
    travel = _fact(
        id="f3",
        section="geography",
        kind="location",
        statement="Annual camping trips in upstate New York.",
        fields={"category": "travel"},
    )
    exposure = _fact(
        id="f4",
        section="geography",
        kind="location",
        statement="Frequent tick exposure while hiking.",
        fields={"category": "exposure", "description": "frequent tick exposure while hiking"},
    )
    data = facts_to_section_data([residence, current_residence, travel, exposure], "geography")
    section = GeographySection.model_validate(data)

    assert len(section.residences) == 2
    assert section.residences[0].place == "rural Connecticut"
    assert section.residences[0].date_approx == "2015-2020"
    assert section.residences[1].current is True
    assert section.travel == ["Annual camping trips in upstate New York."]
    assert section.exposures == ["frequent tick exposure while hiking"]


def test_medications_and_supplements_conversion() -> None:
    med = _fact(
        section="medications",
        kind="medication",
        statement="Levothyroxine.",
        fields={"name": "Levothyroxine", "dose": "50mcg", "frequency": "daily"},
    )
    data = facts_to_section_data([med], "medications")
    section = MedicationsSection.model_validate(data)
    assert section.medications[0].name == "Levothyroxine"
    assert section.medications[0].still_taking is True

    supp = _fact(
        id="f2",
        section="supplements",
        kind="supplement",
        statement="Biotin.",
        fields={"name": "Biotin", "still_taking": False},
    )
    supp_data = facts_to_section_data([supp], "supplements")
    supp_section = SupplementsSection.model_validate(supp_data)
    assert supp_section.supplements[0].name == "Biotin"
    assert supp_section.supplements[0].still_taking is False


def test_allergies_conversion() -> None:
    fact = _fact(
        section="allergies",
        kind="allergy",
        statement="Penicillin allergy.",
        fields={"allergen": "penicillin", "reaction": "hives", "severity": "moderate"},
    )
    data = facts_to_section_data([fact], "allergies")
    section = AllergiesSection.model_validate(data)
    assert section.allergies[0].allergen == "penicillin"
    assert section.allergies[0].reaction == "hives"


def test_care_team_conversion_with_provider_and_insurer() -> None:
    provider = _fact(
        section="care_team",
        kind="provider",
        statement="Dr. Lee.",
        fields={"name": "Dr. Lee", "specialty": "endocrinology"},
    )
    insurer = _fact(
        id="f2",
        section="care_team",
        kind="insurance",
        statement="Insurer.",
        fields={"insurer": "Acme Health"},
    )
    data = facts_to_section_data([provider, insurer], "care_team")
    section = CareTeamSection.model_validate(data)
    assert section.providers[0].name == "Dr. Lee"
    assert section.insurer == "Acme Health"


def test_document_drop_conversion_acknowledged() -> None:
    fact = _fact(
        section="document_drop",
        kind="note",
        statement="Patient acknowledged the document drop.",
        fields={"acknowledged": True},
    )
    data = facts_to_section_data([fact], "document_drop")
    assert data == {"acknowledged": True}


def test_basics_conversion_merges_across_multiple_facts_and_dedupes_exposures() -> None:
    first = _fact(
        section="basics",
        kind="basic",
        statement="Age and sex.",
        fields={"age": 41, "sex_at_birth": "female", "exposures": "mold, asbestos"},
    )
    second = _fact(
        id="f2",
        section="basics",
        kind="basic",
        statement="Occupation.",
        fields={"occupation": "software engineer", "exposure": "mold"},
    )
    data = facts_to_section_data([first, second], "basics")
    assert data["age"] == 41
    assert data["occupation"] == "software engineer"
    assert data["exposures"] == ["mold", "asbestos"]  # deduped, first-seen order


def test_retracted_facts_are_excluded_from_conversion() -> None:
    fact = _fact(
        section="allergies",
        kind="allergy",
        statement="Retracted allergy.",
        status="retracted",
        fields={"allergen": "shellfish"},
    )
    data = facts_to_section_data([fact], "allergies")
    assert data == {"allergies": []}


def test_unsupported_section_raises_convert_error() -> None:
    with pytest.raises(ConvertError):
        facts_to_section_data([], "not-a-real-section")

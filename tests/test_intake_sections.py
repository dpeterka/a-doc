"""Tests for adoc.intake.sections: schema shapes + the SECTIONS registry."""

from __future__ import annotations

from adoc.intake.sections import (
    SECTIONS,
    AllergiesSection,
    BasicsSection,
    CareTeamSection,
    DocumentDropSection,
    EventsSection,
    FamilyHistorySection,
    MedicationsSection,
    PriorDiagnosesSection,
    SupplementsSection,
    SymptomsSection,
)

_EXPECTED_ORDER = [
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
]


def test_sections_registry_has_ten_sections_in_plan_order() -> None:
    assert [spec.key for spec in SECTIONS] == _EXPECTED_ORDER
    assert len(SECTIONS) == 10


def test_sections_registry_keys_are_unique() -> None:
    keys = [spec.key for spec in SECTIONS]
    assert len(keys) == len(set(keys))


def test_every_section_has_a_non_empty_intro_and_extraction_prompt() -> None:
    for spec in SECTIONS:
        assert spec.intro.strip()
        assert spec.extraction_system_prompt.strip()
        assert spec.title.strip()


def test_every_section_schema_validates_an_empty_object() -> None:
    """Every field is optional/defaulted so a bare `{}` extraction is always
    valid — this is what lets `IntakeWizard._merge_section_data` treat a
    freshly-created draft (or a still-partial extraction) uniformly."""
    for spec in SECTIONS:
        instance = spec.schema.model_validate({})
        assert instance is not None


def test_basics_section_round_trip() -> None:
    data = {
        "age": 41,
        "sex_at_birth": "female",
        "height_cm": 165.0,
        "weight_kg": 63.5,
        "occupation": "software engineer",
        "exposures": ["mold at a prior workplace"],
    }
    section = BasicsSection.model_validate(data)
    assert section.model_dump(mode="json") == data


def test_symptoms_section_round_trip() -> None:
    data = {
        "symptoms": [
            {
                "description": "joint pain",
                "onset": "2021",
                "frequency": "daily",
                "triggers": "cold weather",
                "severity": "moderate",
            }
        ]
    }
    section = SymptomsSection.model_validate(data)
    assert section.symptoms[0].description == "joint pain"


def test_events_section_requires_title_and_date_approx() -> None:
    section = EventsSection.model_validate(
        {"events": [{"date_approx": "2019-03", "title": "ER visit", "description": "chest pain"}]}
    )
    assert section.events[0].title == "ER visit"


def test_prior_diagnoses_section_keeps_patient_suspected_separate() -> None:
    section = PriorDiagnosesSection.model_validate(
        {
            "diagnoses": [
                {
                    "name": "Hypothyroidism",
                    "by_whom": "Dr. Lee",
                    "year": 2018,
                    "status": "confirmed",
                }
            ],
            "patient_suspected": [{"name": "Lupus", "why": "joint pain + malar rash pattern"}],
        }
    )
    assert section.diagnoses[0].status == "confirmed"
    assert section.patient_suspected[0].name == "Lupus"


def test_family_history_section_round_trip() -> None:
    section = FamilyHistorySection.model_validate(
        {
            "relatives": [
                {
                    "relation": "mother",
                    "conditions": ["Hashimoto's"],
                    "age_at_onset": 35,
                    "deceased": False,
                    "age_at_death": None,
                }
            ]
        }
    )
    assert section.relatives[0].relation == "mother"


def test_medications_and_supplements_sections_are_independent_schemas() -> None:
    meds = MedicationsSection.model_validate(
        {"medications": [{"name": "Levothyroxine", "dose": "50mcg", "still_taking": True}]}
    )
    supps = SupplementsSection.model_validate(
        {"supplements": [{"name": "Biotin", "dose": "5mg", "still_taking": True}]}
    )
    assert meds.medications[0].name == "Levothyroxine"
    assert supps.supplements[0].name == "Biotin"


def test_allergies_section_round_trip() -> None:
    section = AllergiesSection.model_validate(
        {"allergies": [{"allergen": "penicillin", "reaction": "hives", "severity": "moderate"}]}
    )
    assert section.allergies[0].allergen == "penicillin"


def test_care_team_section_round_trip() -> None:
    section = CareTeamSection.model_validate(
        {
            "providers": [{"name": "Dr. Lee", "specialty": "endocrinology", "org": "City Clinic"}],
            "insurer": "Acme Health",
        }
    )
    assert section.providers[0].name == "Dr. Lee"
    assert section.insurer == "Acme Health"


def test_document_drop_section_defaults_to_unacknowledged() -> None:
    section = DocumentDropSection.model_validate({})
    assert section.acknowledged is False

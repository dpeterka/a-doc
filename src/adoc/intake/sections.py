"""Onboarding section schemas + the section registry.

PLAN.md "Onboarding & end-user experience" lists 11 sections (10 plus
`geography`, added by `docs/adr/0018-intake-clinical-progression-and-
continuity.md`), each backed by a Pydantic schema. This module defines
those schemas and `SECTIONS`, the ordered registry `wizard.py` drives the
state machine from: each entry
carries the section's `key` (stable id, also used as the intake-state.yaml
key and the git-commit-message tag), a human `title`, the extraction
`schema`, an `intro` prompt shown to the patient at the start of the
section, and an `extraction_system_prompt` sent to the LLM (as the `system`
prompt of an `LlmClient.complete(role="primary_reasoner", schema=...)` call)
to turn the patient's free text into a structured instance of `schema`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

# --- 1. Basics ----------------------------------------------------------------------


class BasicsSection(BaseModel):
    age: str | None = None
    """Free-form, like `MedicalEvent.date_approx` below — "42", "just
    turned 40", or "mid-40s" all survive as the patient actually said them,
    rather than being forced into an exact integer (a live crash: forcing
    `Relative.age_at_onset` to `int` broke on "late 30s" — the same risk
    applies here)."""
    sex_at_birth: str | None = None
    height_cm: str | None = None
    """Free-form and NOT assumed to be centimeters despite the field name
    (kept for backward compatibility with `intake.convert`'s fact-field
    key) — "5'9\"", "175cm", or "about average" all survive verbatim."""
    weight_kg: str | None = None
    """Same free-form convention as `height_cm` above."""
    occupation: str | None = None
    exposures: list[str] = Field(default_factory=list)


# --- 2. Current symptoms -------------------------------------------------------------


class SymptomEntry(BaseModel):
    description: str
    onset: str | None = None
    frequency: str | None = None
    triggers: str | None = None
    severity: str | None = None


class SymptomsSection(BaseModel):
    symptoms: list[SymptomEntry] = Field(default_factory=list)


# --- 3. Major medical event history --------------------------------------------------


class MedicalEvent(BaseModel):
    date_approx: str | None = None
    title: str
    description: str = ""


class EventsSection(BaseModel):
    events: list[MedicalEvent] = Field(default_factory=list)


# --- 4. Prior diagnoses & workups -----------------------------------------------------

DiagnosisStatus = Literal["confirmed", "suspected", "ruled-out"]


class PriorDiagnosis(BaseModel):
    name: str
    by_whom: str | None = None
    year: str | None = None
    """Free-form, like `MedicalEvent.date_approx` — "2018", "a few years
    ago", or "around 2015" all survive as the patient actually said them.
    `status` below stays a closed vocabulary deliberately (see its own
    note) — this field doesn't need to, since nothing downstream parses it
    as a number; `intake.corroborate` already tolerates non-numeric text
    here (`int(year)` wrapped in try/except)."""
    status: DiagnosisStatus = "suspected"
    """Kept as a closed 3-value vocabulary (unlike the free-form fields in
    this module) because it IS consumed as a controlled classification
    downstream — `intake.wizard._write_patient_theories` and, later, the
    first diagnostic run's Ledger-Maintainer pass, which reads
    `case/patient-theories.md` as `origin: patient` hypotheses (module
    docstring). A patient doesn't state this value verbatim; the intake
    agent classifies it, so strictness here is still load-bearing —
    `intake.convert._diagnosis_data` is responsible for clamping an
    out-of-vocabulary value from the loosely-typed fact `fields` dict to
    the schema default rather than letting model_validate raise."""


class PatientSuspectedDiagnosis(BaseModel):
    name: str
    why: str = ""


class PriorDiagnosesSection(BaseModel):
    diagnoses: list[PriorDiagnosis] = Field(default_factory=list)
    patient_suspected: list[PatientSuspectedDiagnosis] = Field(default_factory=list)


# --- 5. Family history ----------------------------------------------------------------


class Relative(BaseModel):
    relation: str
    conditions: list[str] = Field(default_factory=list)
    age_at_onset: str | None = None
    """Free-form, like `MedicalEvent.date_approx` — "late 30s", "as a
    teenager", or "approx. 5 years old" all survive as the patient actually
    said them, rather than being forced into an exact integer. THE live
    crash this schema originally shipped with (`int | None` rejected "late
    30s")."""
    deceased: bool = False
    age_at_death: str | None = None
    """Same free-form convention as `age_at_onset` above."""


class FamilyHistorySection(BaseModel):
    relatives: list[Relative] = Field(default_factory=list)


# --- 6. Geography & environmental exposure ---------------------------------------------

# docs/adr/0018-intake-clinical-progression-and-continuity.md: added so travel/
# regional exposure (e.g. tick-borne and other regional infectious risk) has a real
# home in the case file instead of being folded into `BasicsSection.exposures`
# (occupational-only) or dropped entirely.


class ResidenceEntry(BaseModel):
    place: str
    date_approx: str | None = None
    """Rough dates this residence covers ("2015-2020", "childhood") — same
    free-form convention as `MedicalEvent.date_approx`."""
    current: bool = False


class GeographySection(BaseModel):
    residences: list[ResidenceEntry] = Field(default_factory=list)
    travel: list[str] = Field(default_factory=list)
    exposures: list[str] = Field(default_factory=list)
    """Environmental/occupational exposures tied to a place or trip (tick
    habitat, well water, farm/agricultural work, a regional outbreak) —
    distinct from `BasicsSection.exposures`, which stays occupational."""


# --- 7. Medications ----------------------------------------------------------------------


class Medication(BaseModel):
    name: str
    dose: str | None = None
    frequency: str | None = None
    still_taking: bool = True
    notes: str = ""


class MedicationsSection(BaseModel):
    medications: list[Medication] = Field(default_factory=list)


# --- 8. Supplements ------------------------------------------------------------------------


class Supplement(BaseModel):
    name: str
    dose: str | None = None
    frequency: str | None = None
    still_taking: bool = True
    notes: str = ""


class SupplementsSection(BaseModel):
    supplements: list[Supplement] = Field(default_factory=list)


# --- 9. Allergies & reactions ----------------------------------------------------------------


class Allergy(BaseModel):
    allergen: str
    reaction: str = ""
    severity: str | None = None


class AllergiesSection(BaseModel):
    allergies: list[Allergy] = Field(default_factory=list)


# --- 10. Care team & insurance ------------------------------------------------------------------


class Provider(BaseModel):
    name: str
    specialty: str | None = None
    org: str | None = None


class CareTeamSection(BaseModel):
    providers: list[Provider] = Field(default_factory=list)
    insurer: str | None = None


# --- 11. Document drop -----------------------------------------------------------------------


class DocumentDropSection(BaseModel):
    acknowledged: bool = False


# --- registry ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionSpec:
    """One row of the intake wizard's section registry (`SECTIONS`)."""

    key: str
    title: str
    schema: type[BaseModel]
    intro: str
    extraction_system_prompt: str


SECTIONS: list[SectionSpec] = [
    SectionSpec(
        key="basics",
        title="Basics",
        schema=BasicsSection,
        intro=(
            "Let's start with the basics: your age, sex at birth, height and "
            "weight, and occupation."
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file tool. "
            "Extract the patient's basic demographic and exposure information "
            "from their message into the given schema. Leave a field null/empty "
            "if the patient did not mention it — never guess or fabricate a "
            "value. Capture age, height, and weight exactly as the patient stated "
            "them (a number, a range like 'mid-40s', or a description, including "
            "whichever unit they used) — never round, estimate, or convert units "
            "yourself. Never infer or add a diagnosis, treatment, or dosing advice."
        ),
    ),
    SectionSpec(
        key="symptoms",
        title="Current symptoms",
        schema=SymptomsSection,
        intro=(
            "Tell me about the symptoms you're currently experiencing, in your "
            "own words. For each one, mention when it started, how often it "
            "happens, anything that triggers it, and how severe it is."
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file tool. "
            "Extract a structured list of symptoms from the patient's narrative: "
            "for each symptom capture a short description plus onset, frequency, "
            "triggers, and severity when mentioned. Leave a field null if not "
            "mentioned. Never diagnose, and never add a symptom the patient did "
            "not describe."
        ),
    ),
    SectionSpec(
        key="events",
        title="Major medical event history",
        schema=EventsSection,
        intro=(
            "Let's walk through your major medical events, earliest first — "
            "hospitalizations, procedures, major diagnoses, anything that felt "
            "like a turning point. For each, give an approximate date and a "
            "short description."
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file tool. "
            "Extract the patient's major medical events into a list, each with "
            "an approximate date (`date_approx`, any granularity the patient "
            "gave — a year, a year-month, or a full date), a short title, and a "
            "description. When the patient gives NO timing for an event, set "
            "`date_approx` to null — NEVER write placeholder strings like "
            "'<UNKNOWN>' or 'unknown'. Keep vague-but-real timing words like "
            "'recently' verbatim. Only include events the patient actually "
            "described."
        ),
    ),
    SectionSpec(
        key="prior_diagnoses",
        title="Prior diagnoses & workups",
        schema=PriorDiagnosesSection,
        intro=(
            "Have you received any diagnoses (confirmed, suspected, or ruled "
            "out) from a doctor? Separately — is there anything *you* suspect "
            "might be going on, even if no doctor has said so?"
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file tool. "
            "Extract two lists from the patient's message: `diagnoses` — things "
            "a clinician told the patient, each with who said it, roughly when, "
            "and its status (confirmed/suspected/ruled-out); and "
            "`patient_suspected` — things the patient themselves suspects, with "
            "their reasoning in `why`. Keep these lists strictly separate: a "
            "patient's own theory belongs in `patient_suspected`, never in "
            "`diagnoses`, even if the patient states it with confidence."
        ),
    ),
    SectionSpec(
        key="family_history",
        title="Family history",
        schema=FamilyHistorySection,
        intro=(
            "Tell me about your family's medical history — parents, siblings, "
            "grandparents, aunts/uncles. Especially anything autoimmune, cancer, "
            "cardiac, or early/unexpected deaths."
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file tool. "
            "Extract a list of relatives, each with their relation, the "
            "conditions the patient mentioned for them, an approximate age at "
            "onset if given, and whether they are deceased (with age at death if "
            "given). Only include relatives the patient actually mentioned."
        ),
    ),
    SectionSpec(
        key="geography",
        title="Geography & environmental exposure",
        schema=GeographySection,
        intro=(
            "Where do you live now, and where else have you lived — roughly when? Any "
            "travel worth mentioning, or things about where you've lived or worked that "
            "could matter (well water, ticks/outdoor exposure, farm work, a regional "
            "outbreak, that kind of thing)?"
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file tool. Extract "
            "the patient's residence history into `residences` (place, approximate dates, "
            "and whether it's their CURRENT residence), any `travel` worth noting as a "
            "list of short strings, and any environmental/occupational `exposures` tied "
            "to a place or trip (well water, ticks/outdoor exposure, farm work, a "
            "regional outbreak) as a list of short strings. Leave a list empty if the "
            "patient did not mention anything for it — never guess or fabricate a place, "
            "a date, or an exposure."
        ),
    ),
    SectionSpec(
        key="medications",
        title="Medications",
        schema=MedicationsSection,
        intro=(
            "What medications are you currently taking, or have taken that "
            "seemed significant in the past? Dose isn't required — the name and "
            "roughly how often is enough."
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file "
            "tool. Extract a list of medications (current or past-significant) "
            "from the patient's message: name, dose/frequency if given, whether "
            "they're still taking it, and any notes. Never add a dosing "
            "recommendation — only record what the patient reports taking."
        ),
    ),
    SectionSpec(
        key="supplements",
        title="Supplements",
        schema=SupplementsSection,
        intro=(
            "What supplements or over-the-counter products do you take "
            "regularly (vitamins, herbal supplements, protein powders, etc.)?"
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file "
            "tool. Extract a list of supplements the patient takes, same shape "
            "as medications: name, dose/frequency if given, whether they're "
            "still taking it, and notes. Only record what the patient reports."
        ),
    ),
    SectionSpec(
        key="allergies",
        title="Allergies & reactions",
        schema=AllergiesSection,
        intro=(
            "Do you have any known allergies or bad reactions — medications, "
            "foods, environmental, contrast dye, latex, anything? What happens "
            "when you're exposed?"
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file "
            "tool. Extract a list of allergies/reactions the patient describes: "
            "the allergen, the reaction, and severity if mentioned. Only "
            "include what the patient actually reports."
        ),
    ),
    SectionSpec(
        key="care_team",
        title="Care team & insurance",
        schema=CareTeamSection,
        intro=(
            "Who's on your care team right now — primary care, any specialists "
            "you see — and who is your health insurer?"
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file "
            "tool. Extract the patient's current providers (name, specialty, "
            "organization if given) and their insurer. Only include what the "
            "patient states."
        ),
    ),
    SectionSpec(
        key="document_drop",
        title="Document drop",
        schema=DocumentDropSection,
        intro=(
            "Last step: if you have existing lab reports, imaging, or doctor "
            "notes as PDFs or photos, this is where you'll add them so the "
            "case file can be backfilled. Let me know once you're set, or if "
            "you'd rather do this later."
        ),
        extraction_system_prompt=(
            "You are the intake assistant for a personal medical case-file "
            "tool. Decide whether the patient has acknowledged the document "
            "drop instructions (they intend to add documents now or later, or "
            "explicitly confirm they understand) and set `acknowledged` "
            "accordingly. Default to false if genuinely unclear."
        ),
    ),
]

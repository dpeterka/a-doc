"""Tests for adoc.intake.wizard: the resumable onboarding state machine.

No network: every `LlmClient` here is built with a `ScriptedTransport` that
returns canned structured-output dicts keyed by section schema name, so
`primary_reasoner` calls never touch a real provider SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding
from adoc.intake.sections import SECTIONS
from adoc.intake.wizard import (
    INTAKE_STATE_RELPATH,
    IntakeError,
    IntakeState,
    IntakeWizard,
    SectionState,
    load_intake_state,
    save_intake_state,
)
from adoc.reason.client import (
    AnthropicProvider,
    LlmClient,
    LlmError,
    TransportRequest,
    TransportResponse,
)


class ScriptedTransport:
    """A fake transport returning canned `tool_input` dicts, queued by the
    Pydantic schema name of the request (order-independent across sections,
    FIFO within one section's queue)."""

    def __init__(self, queues: dict[str, list[dict[str, Any]]]) -> None:
        self._queues = {name: list(items) for name, items in queues.items()}
        self.calls: list[str] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        assert request.schema is not None, "intake extraction calls always pass a schema"
        name = request.schema.__name__
        self.calls.append(name)
        queue = self._queues.get(name)
        if not queue:
            raise AssertionError(f"no scripted response left for schema {name!r}")
        tool_input = queue.pop(0)
        return TransportResponse(text="", tool_input=tool_input, input_tokens=5, output_tokens=5)


def _make_client(queues: dict[str, list[dict[str, Any]]]) -> tuple[LlmClient, ScriptedTransport]:
    transport = ScriptedTransport(queues)
    provider = AnthropicProvider(api_key=None, transport=transport)
    client = LlmClient(
        {"primary_reasoner": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
    )
    return client, transport


# --- canned scripted-persona extractions -----------------------------------------------

BASICS = {
    "age": 41,
    "sex_at_birth": "female",
    "height_cm": 165.0,
    "weight_kg": 63.0,
    "occupation": "software engineer",
    "exposures": ["mold exposure at a prior workplace"],
}

SYMPTOMS_1 = {
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

SYMPTOMS_2 = {
    "symptoms": [
        {
            "description": "joint pain",
            "onset": "2021",
            "frequency": "daily",
            "triggers": "cold weather",
            "severity": "moderate",
        },
        {
            "description": "fatigue",
            "onset": "2022",
            "frequency": "most days",
            "triggers": None,
            "severity": "mild",
        },
    ]
}

EVENTS = {
    "events": [
        {
            "date_approx": "2019-03",
            "title": "ER visit chest pain",
            "description": "Went to the ER for chest pain; cardiac causes were ruled out.",
        },
        {
            "date_approx": "2021",
            "title": "Rheumatology referral",
            "description": "Referred to rheumatology for a joint-pain workup.",
        },
    ]
}

PRIOR_DIAGNOSES = {
    "diagnoses": [
        {"name": "Hypothyroidism", "by_whom": "Dr. Lee", "year": 2018, "status": "confirmed"}
    ],
    "patient_suspected": [
        {
            "name": "Systemic lupus erythematosus",
            "why": "joint pain, fatigue, and a family history of autoimmune disease",
        }
    ],
}

FAMILY_HISTORY = {
    "relatives": [
        {
            "relation": "mother",
            "conditions": ["Hashimoto's thyroiditis"],
            "age_at_onset": 35,
            "deceased": False,
            "age_at_death": None,
        },
        {
            "relation": "maternal aunt",
            "conditions": ["rheumatoid arthritis"],
            "age_at_onset": None,
            "deceased": True,
            "age_at_death": 72,
        },
    ]
}

MEDICATIONS_1 = {
    "medications": [
        {
            "name": "Levothyroxine",
            "dose": "50mcg",
            "frequency": "daily",
            "still_taking": True,
            "notes": "",
        }
    ]
}

MEDICATIONS_2_WITH_ADDITION = {
    "medications": [
        {
            "name": "Levothyroxine",
            "dose": "50mcg",
            "frequency": "daily",
            "still_taking": True,
            "notes": "",
        },
        {
            "name": "Ibuprofen",
            "dose": "200mg",
            "frequency": "as needed",
            "still_taking": True,
            "notes": "for joint pain flares",
        },
    ]
}

SUPPLEMENTS = {
    "supplements": [
        {
            "name": "Biotin",
            "dose": "5mg",
            "frequency": "daily",
            "still_taking": True,
            "notes": "hair/nails",
        }
    ]
}

ALLERGIES = {"allergies": [{"allergen": "penicillin", "reaction": "hives", "severity": "moderate"}]}

CARE_TEAM = {
    "providers": [{"name": "Dr. Lee", "specialty": "endocrinology", "org": "City Clinic"}],
    "insurer": "Acme Health",
}

DOCUMENT_DROP = {"acknowledged": True}


def _full_script() -> dict[str, list[dict[str, Any]]]:
    return {
        "BasicsSection": [BASICS],
        "SymptomsSection": [SYMPTOMS_1, SYMPTOMS_2],
        "EventsSection": [EVENTS],
        "PriorDiagnosesSection": [PRIOR_DIAGNOSES],
        "FamilyHistorySection": [FAMILY_HISTORY],
        "MedicationsSection": [MEDICATIONS_1, MEDICATIONS_2_WITH_ADDITION],
        "SupplementsSection": [SUPPLEMENTS],
        "AllergiesSection": [ALLERGIES],
        "CareTeamSection": [CARE_TEAM],
        "DocumentDropSection": [DOCUMENT_DROP],
    }


# --- the Phase-1 acceptance test: a full scripted persona run ---------------------------


def test_full_onboarding_run_produces_complete_committed_baseline_case_file(
    tmp_path: Path,
) -> None:
    """PLAN.md Phase-1 acceptance: "full onboarding run produces a complete
    committed baseline case file from a scripted persona"."""
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client(_full_script())
    wizard = IntakeWizard(repo, client)

    assert wizard.baseline_incomplete() is True

    section = wizard.current_section()
    assert section is not None and section.key == "basics"
    playback = wizard.submit("I'm 41, female, 165cm, 63kg, a software engineer.")
    assert "Here's what I've noted" in playback.text
    result = wizard.confirm()
    assert result.section_key == "basics"
    assert result.artifacts == ["case/case-summary.md"]

    section = wizard.current_section()
    assert section is not None and section.key == "symptoms"
    wizard.submit("Daily joint pain since 2021, worse in cold weather, moderate.")
    revised = wizard.revise("Also add fatigue since 2022, most days, mild.")
    assert "fatigue" in revised.text
    wizard.confirm()

    section = wizard.current_section()
    assert section is not None and section.key == "events"
    wizard.submit("March 2019 ER visit for chest pain; 2021 rheumatology referral.")
    result = wizard.confirm()
    assert len(result.artifacts) == 2

    assert wizard.current_section().key == "prior_diagnoses"  # type: ignore[union-attr]
    wizard.submit("Dr. Lee diagnosed hypothyroidism in 2018. I suspect lupus myself.")
    wizard.confirm()

    assert wizard.current_section().key == "family_history"  # type: ignore[union-attr]
    wizard.submit("My mother has Hashimoto's. My maternal aunt had RA and died at 72.")
    wizard.confirm()

    assert wizard.current_section().key == "medications"  # type: ignore[union-attr]
    wizard.submit("I take Levothyroxine 50mcg daily.")
    wizard.confirm()

    assert wizard.current_section().key == "supplements"  # type: ignore[union-attr]
    wizard.submit("I take biotin 5mg daily.")
    wizard.confirm()

    assert wizard.current_section().key == "allergies"  # type: ignore[union-attr]
    wizard.submit("Penicillin gives me hives, moderate.")
    wizard.confirm()

    assert wizard.current_section().key == "care_team"  # type: ignore[union-attr]
    wizard.submit("Dr. Lee at City Clinic; insurer is Acme Health.")
    wizard.confirm()

    assert wizard.current_section().key == "document_drop"  # type: ignore[union-attr]
    wizard.submit("Understood, I'll drop my old lab PDFs in later.")
    wizard.confirm()

    # --- wizard-level completion state -----------------------------------------------
    assert wizard.current_section() is None
    assert wizard.baseline_incomplete() is False
    assert wizard.progress() == (10, 10)

    # --- every target artifact exists with expected content --------------------------
    case_summary = repo.read("case/case-summary.md")
    assert "## Patient basics" in case_summary
    assert "Age: 41" in case_summary
    assert "software engineer" in case_summary
    assert "## Current symptoms" in case_summary
    assert "joint pain" in case_summary
    assert "fatigue" in case_summary
    assert "## Allergies & reactions" in case_summary
    assert "penicillin" in case_summary

    encounters_dir = root / "case" / "encounters"
    encounter_files = sorted(p.name for p in encounters_dir.glob("*.md"))
    assert encounter_files == [
        "2019-03-01--er-visit-chest-pain.md",
        "2021-01-01--rheumatology-referral.md",
    ]
    er_visit = (encounters_dir / "2019-03-01--er-visit-chest-pain.md").read_text(encoding="utf-8")
    assert "type: patient-report" in er_visit

    theories = repo.read("case/patient-theories.md")
    assert "Hypothyroidism" in theories
    assert "Systemic lupus erythematosus" in theories
    assert "origin: patient" in theories

    family = repo.read("case/family-history.md")
    assert "mother" in family
    assert "Hashimoto's thyroiditis" in family
    assert "maternal aunt" in family

    medications_md = repo.read("case/medications.md")
    assert "Levothyroxine" in medications_md
    assert "Biotin" in medications_md
    assert "interfere with" in medications_md  # biotin/lab-interference caveat

    care_team = repo.read("case/care-team.md")
    assert "Dr. Lee" in care_team
    assert "Acme Health" in care_team

    # --- intake-state.yaml round-trips ------------------------------------------------
    state = load_intake_state(root / INTAKE_STATE_RELPATH)
    assert set(state.sections) == {spec.key for spec in SECTIONS}
    assert all(section_state.status == "complete" for section_state in state.sections.values())
    assert all(section_state.completed_at is not None for section_state in state.sections.values())
    assert state.cursor is None

    # --- one git commit per section ---------------------------------------------------
    git_repo = Repo(root)
    messages = {c.message.strip() for c in git_repo.iter_commits()}
    assert len(messages) == 1 + len(SECTIONS)
    for spec in SECTIONS:
        assert f"feat(intake): complete {spec.title.lower()} section" in messages


# --- resumability ------------------------------------------------------------------------


def test_resumability_new_wizard_instance_continues_mid_flow(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client({"BasicsSection": [BASICS], "SymptomsSection": [SYMPTOMS_1]})
    wizard = IntakeWizard(repo, client)

    wizard.submit("basics info")
    wizard.confirm()
    wizard.submit("symptoms info")
    # deliberately do NOT confirm symptoms: leave it awaiting_confirmation

    # A brand new wizard instance against the same on-disk repo should
    # resume exactly where the first one left off.
    second_repo = DataRepo(root)
    second_client, _ = _make_client({})  # no further calls expected
    resumed = IntakeWizard(second_repo, second_client)

    assert resumed.progress() == (1, 10)
    section = resumed.current_section()
    assert section is not None and section.key == "symptoms"
    assert resumed.current_status() == "awaiting_confirmation"
    assert "joint pain" in resumed.prompt_for_current()


# --- revise/merge behavior ----------------------------------------------------------------


def test_revise_merges_corrections_over_prior_draft_without_losing_untouched_fields(
    tmp_path: Path,
) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client(
        {
            "BasicsSection": [
                {
                    "age": 41,
                    "sex_at_birth": "female",
                    "height_cm": 165.0,
                    "weight_kg": 63.0,
                    "occupation": None,
                    "exposures": [],
                },
                {
                    "age": None,
                    "sex_at_birth": None,
                    "height_cm": None,
                    "weight_kg": None,
                    "occupation": "software engineer",
                    "exposures": [],
                },
            ]
        }
    )
    wizard = IntakeWizard(repo, client)

    wizard.submit("41yo female, 165cm, 63kg.")
    revised = wizard.revise("Oh, I'm a software engineer.")

    assert "age: 41" in revised.text
    assert "occupation: software engineer" in revised.text

    result = wizard.confirm()
    case_summary = repo.read("case/case-summary.md")
    assert "Age: 41" in case_summary  # not lost by the revise
    assert "software engineer" in case_summary
    assert result.section_key == "basics"


# --- reopen: update without duplication -----------------------------------------------------


def test_reopen_updates_a_completed_section_without_duplicating_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client(
        {"MedicationsSection": [MEDICATIONS_1, MEDICATIONS_2_WITH_ADDITION]}
    )
    wizard = IntakeWizard(repo, client)

    # Fast-forward the cursor straight to "medications" by seeding state.
    state_path = root / INTAKE_STATE_RELPATH
    state = load_intake_state(state_path)
    for spec in SECTIONS:
        if spec.key == "medications":
            break
        state.sections[spec.key] = SectionState(status="complete", completed_at=datetime.now(UTC))
    state.cursor = "medications"
    save_intake_state(state_path, state)
    repo.commit("chore: seed state for test")
    wizard = IntakeWizard(repo, client)

    wizard.submit("I take Levothyroxine 50mcg daily.")
    wizard.confirm()
    medications_md_before = repo.read("case/medications.md")
    assert medications_md_before.count("Levothyroxine") == 1

    reopened = wizard.reopen("medications")
    assert reopened.key == "medications"
    assert wizard.current_section() is not None and wizard.current_section().key == "medications"  # type: ignore[union-attr]

    wizard.submit("Also add Ibuprofen 200mg as needed for joint pain flares.")
    wizard.confirm()

    medications_md_after = repo.read("case/medications.md")
    assert medications_md_after.count("Levothyroxine") == 1  # not duplicated
    assert medications_md_after.count("Ibuprofen") == 1


def test_reopen_of_events_overwrites_the_same_encounter_file(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client(
        {
            "EventsSection": [
                {"events": [{"date_approx": "2019-03", "title": "ER visit", "description": "v1"}]},
                {"events": [{"date_approx": "2019-03", "title": "ER visit", "description": "v2"}]},
            ]
        }
    )
    wizard = IntakeWizard(repo, client)

    state_path = root / INTAKE_STATE_RELPATH
    state = load_intake_state(state_path)
    for spec in SECTIONS:
        if spec.key == "events":
            break
        state.sections[spec.key] = SectionState(status="complete", completed_at=datetime.now(UTC))
    state.cursor = "events"
    save_intake_state(state_path, state)
    repo.commit("chore: seed state for test")
    wizard = IntakeWizard(repo, client)

    wizard.submit("ER visit in March 2019.")
    wizard.confirm()

    encounters_dir = root / "case" / "encounters"
    files_before = sorted(p.name for p in encounters_dir.glob("*.md"))
    assert files_before == ["2019-03-01--er-visit.md"]
    assert "v1" in (encounters_dir / "2019-03-01--er-visit.md").read_text(encoding="utf-8")

    wizard.reopen("events")
    wizard.submit("Update: it was actually a different description.")
    wizard.confirm()

    files_after = sorted(p.name for p in encounters_dir.glob("*.md"))
    assert files_after == ["2019-03-01--er-visit.md"]  # same filename, not duplicated
    assert "v2" in (encounters_dir / "2019-03-01--er-visit.md").read_text(encoding="utf-8")


# --- baseline_incomplete flips to False only once everything is complete ------------------


def test_baseline_incomplete_flips_to_false_only_after_last_section(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client({"DocumentDropSection": [DOCUMENT_DROP]})

    state_path = root / INTAKE_STATE_RELPATH
    state = IntakeState(
        sections={
            spec.key: SectionState(status="complete", completed_at=datetime.now(UTC))
            for spec in SECTIONS[:-1]
        }
        | {SECTIONS[-1].key: SectionState()},
        cursor=SECTIONS[-1].key,
    )
    save_intake_state(state_path, state)
    repo.commit("chore: seed state for test")

    wizard = IntakeWizard(repo, client)
    assert wizard.baseline_incomplete() is True

    wizard.submit("Sounds good, I'll drop docs in later.")
    wizard.confirm()

    assert wizard.baseline_incomplete() is False
    assert wizard.current_section() is None


# --- error paths -----------------------------------------------------------------------------


def test_confirm_without_a_prior_submit_raises_intake_error(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client({})
    wizard = IntakeWizard(repo, client)

    with pytest.raises(IntakeError):
        wizard.confirm()


def test_submit_after_onboarding_complete_raises_intake_error(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)
    client, _transport = _make_client({})

    state_path = root / INTAKE_STATE_RELPATH
    state = IntakeState(
        sections={
            spec.key: SectionState(status="complete", completed_at=datetime.now(UTC))
            for spec in SECTIONS
        },
        cursor=None,
    )
    save_intake_state(state_path, state)
    repo.commit("chore: seed complete state for test")

    wizard = IntakeWizard(repo, client)
    assert wizard.current_section() is None

    with pytest.raises(IntakeError):
        wizard.submit("anything")


def test_missing_structured_output_raises_llm_error(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)

    def no_tool_call(request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            text="not structured", tool_input=None, input_tokens=1, output_tokens=1
        )

    provider = AnthropicProvider(api_key=None, transport=no_tool_call)
    client = LlmClient(
        {"primary_reasoner": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
        max_retries=1,
    )
    wizard = IntakeWizard(repo, client)

    with pytest.raises(LlmError):
        wizard.submit("41yo female")


# --- intake-state persistence helpers -----------------------------------------------------


def test_load_intake_state_missing_file_returns_fresh_state(tmp_path: Path) -> None:
    state = load_intake_state(tmp_path / "does-not-exist.yaml")
    assert state.sections == {}
    assert state.cursor == SECTIONS[0].key


def test_save_and_load_intake_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "intake-state.yaml"
    state = IntakeState(
        sections={"basics": SectionState(status="complete", draft={"age": 41})},
        cursor="symptoms",
    )
    save_intake_state(path, state)
    loaded = load_intake_state(path)
    assert loaded.sections["basics"].status == "complete"
    assert loaded.sections["basics"].draft == {"age": 41}
    assert loaded.cursor == "symptoms"


def test_document_drop_auto_completes_when_sources_exist(tmp_path: Path) -> None:
    """A seeded deployment (documents already ingested into sources/) must
    not ask the patient to upload files they already provided."""
    repo = DataRepo.init_at(tmp_path / "data")
    (repo.root / "sources" / "abc__report.pdf").write_bytes(b"%PDF-fake")

    client, _transport = _make_client({})
    wizard = IntakeWizard(repo, client)

    assert wizard._state.sections["document_drop"].status == "complete"


def test_document_drop_still_asked_on_a_fresh_repo(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")

    client, _transport = _make_client({})
    wizard = IntakeWizard(repo, client)

    assert wizard._state.sections["document_drop"].status == "pending"


def test_undated_events_are_recorded_without_fabricating_dates(tmp_path: Path) -> None:
    """Real onboarding crash: the extractor emitted '<UNKNOWN>'/'recently'
    for events the patient couldn't date; confirm must record them in
    case/undated-events.md instead of raising."""
    from adoc.intake.sections import EventsSection, MedicalEvent
    from adoc.intake.wizard import UNDATED_EVENTS_RELPATH, _write_events

    repo = DataRepo.init_at(tmp_path / "data")
    data = EventsSection(
        events=[
            MedicalEvent(
                date_approx="<UNKNOWN>",
                title="ER visits for thyroid failure",
                description="Multiple ER visits, dates not specified.",
            ),
            MedicalEvent(
                date_approx="recently",
                title="Bone density loss diagnosis",
                description="Recently diagnosed, exact date not specified.",
            ),
            MedicalEvent(
                date_approx=None,
                title="Appendectomy",
                description="No timing given at all.",
            ),
            MedicalEvent(
                date_approx="March 2023",
                title="First TGAB evidence",
                description="Dated event still becomes an encounter.",
            ),
        ]
    )

    written = _write_events(repo, data)

    assert UNDATED_EVENTS_RELPATH in written
    undated = repo.read(UNDATED_EVENTS_RELPATH)
    assert "ER visits for thyroid failure" in undated
    assert "timing: recently" in undated
    assert "Appendectomy" in undated
    dated = [w for w in written if w.startswith("case/encounters/")]
    assert len(dated) == 1 and "2023" in dated[0]

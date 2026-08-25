"""The resumable onboarding intake state machine (PLAN.md "Onboarding & end-user
experience" mechanics).

`IntakeWizard` drives the 10 `SECTIONS` (see `sections.py`) one at a time:
for the section under the wizard's cursor, `prompt_for_current()` shows the
patient what to talk about (plus, once a draft exists, a playback of what's
recorded so far); `submit()`/`revise()` send the patient's free text to the
`primary_reasoner` role as a structured-output extraction, merging the
result over any prior draft for the section (so a correction only changes
the fields it actually addresses); `confirm()` writes that section's target
case-file artifact(s) and makes exactly one git commit. `reopen(key)` lets a
completed section be revisited later ("update my medications"). All of this
state — per-section status, the draft extraction, `completed_at`, and the
wizard's cursor — is persisted as `case/intake-state.yaml` in the data repo,
so a new `IntakeWizard` built against the same `DataRepo` resumes exactly
where a previous one left off.

**Why prior diagnoses never touch the ledger directly.** Section 4 (prior
diagnoses & the patient's own suspected diagnoses) is intentionally *not*
written to `differential-ledger.yaml`. The ledger's invariants (`ledger.py`)
make a direct, onboarding-time write invalid on its own terms: invariant (a)
requires the can't-miss tier to be non-empty whenever any hypothesis is
active, and invariant (b) forbids a patient-origin hypothesis from reaching
`tier=most-likely` in the diff that creates it — neither of those can be
satisfied by a bare "record what the patient said" write with no Challenger
pass. Instead, `confirm()` for this section writes `case/patient-theories.md`
— a plain record of what the patient reported and suspects. The first real
diagnostic run's Ledger-Maintainer prompt (the parallel reasoning slice)
already instructs it to ingest that file's contents as `origin: patient`
`AddHypothesis` ops, which then go through the normal DAG (Challenger, ledger
invariants) like any other hypothesis. This module only ever produces plain
markdown/encounter files and the intake-state file itself — it never
constructs a `LedgerDiff`.

**Idempotency.** Every writer in this module replaces its target rather than
appending: `case-summary.md`/`medications.md` blocks are located by an HTML
comment marker and replaced in place (or appended once if the marker is
absent); `family-history.md`/`care-team.md`/`patient-theories.md` are whole
files owned by one section and are overwritten wholesale; medical-event
encounter files are named deterministically from `(date, title)` so
re-confirming the same event overwrites the same file rather than creating a
duplicate. Re-confirming a section (via `reopen()`) is therefore always safe
to repeat.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.repo import DataRepo
from adoc.intake.sections import (
    SECTIONS,
    AllergiesSection,
    BasicsSection,
    CareTeamSection,
    DocumentDropSection,
    EventsSection,
    FamilyHistorySection,
    GeographySection,
    MedicationsSection,
    PriorDiagnosesSection,
    SectionSpec,
    SupplementsSection,
    SymptomsSection,
)
from adoc.reason.client import LlmClient, LlmError, Message

INTAKE_STATE_RELPATH = "case/intake-state.yaml"
CASE_SUMMARY_RELPATH = "case/case-summary.md"
MEDICATIONS_RELPATH = "case/medications.md"
FAMILY_HISTORY_RELPATH = "case/family-history.md"
GEOGRAPHY_RELPATH = "case/geography.md"
CARE_TEAM_RELPATH = "case/care-team.md"
PATIENT_THEORIES_RELPATH = "case/patient-theories.md"
ENCOUNTERS_RELDIR = "case/encounters"

DEFAULT_DROPBOX_FOLDER = "Dropbox/a-doc-inbox"

SectionStatus = Literal["pending", "awaiting_confirmation", "complete"]

_PLAYBACK_INTRO = "Here's what I've noted — anything wrong or missing?"

_BIOTIN_CAVEAT = (
    "Caveat: some supplements (notably high-dose biotin) can interfere with "
    "immunoassay lab tests and cause false results — mention supplement use "
    "to the lab or ordering clinician."
)


class IntakeError(Exception):
    """Raised for a wizard operation that is invalid in the wizard's current state."""


# --- persisted state -----------------------------------------------------------------


class SectionState(BaseModel):
    """Per-section persisted state, one entry per `SECTIONS` key."""

    status: SectionStatus = "pending"
    draft: dict[str, Any] | None = None
    completed_at: datetime | None = None


def _first_section_key() -> str | None:
    return SECTIONS[0].key if SECTIONS else None


class IntakeState(BaseModel):
    """The full `case/intake-state.yaml` document."""

    sections: dict[str, SectionState] = Field(default_factory=dict)
    cursor: str | None = Field(default_factory=_first_section_key)


class PlaybackMessage(BaseModel):
    """The result of `submit()`/`revise()`: a human-readable confirmation prompt."""

    section_key: str
    text: str


class CommitResult(BaseModel):
    """The result of `confirm()`: what was written and committed."""

    section_key: str
    commit_sha: str
    artifacts: list[str]


def load_intake_state(path: Path) -> IntakeState:
    """Load `case/intake-state.yaml`. A missing file yields a fresh `IntakeState`."""
    if not path.exists():
        return IntakeState()
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh) or {}
    return IntakeState.model_validate(data)


def save_intake_state(path: Path, state: IntakeState) -> None:
    """Write `state` to `path` as stable, human-diffable YAML."""
    data = state.model_dump(mode="json")
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


# --- section lookup helpers -----------------------------------------------------------


def _spec_by_key(key: str) -> SectionSpec:
    for spec in SECTIONS:
        if spec.key == key:
            return spec
    raise KeyError(f"no such intake section: {key!r}")


def _section_index(key: str) -> int:
    """1-based position of `key` in `SECTIONS`."""
    for index, spec in enumerate(SECTIONS, start=1):
        if spec.key == key:
            return index
    raise KeyError(f"no such intake section: {key!r}")


# --- merge + playback rendering --------------------------------------------------------


def _is_empty(value: Any) -> bool:
    return value is None or value in ("", [], {})


def _merge_section_data(base: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Merge a fresh extraction `new` over a prior draft `base`.

    A field the new extraction left empty/null (because the patient's most
    recent message didn't touch it) keeps its previous value; any
    non-empty field in `new` overwrites `base` outright (lists replace
    rather than concatenate, since a correction re-states the whole list
    for a field it touches at all).
    """
    if base is None:
        return new
    merged = dict(base)
    for key, value in new.items():
        if _is_empty(value):
            continue
        merged[key] = value
    return merged


def _render_playback(spec: SectionSpec, data: dict[str, Any]) -> str:
    """A generic, itemized human-readable rendering of a section draft."""
    lines: list[str] = []
    for field_name in spec.schema.model_fields:
        value = data.get(field_name)
        if _is_empty(value):
            continue
        label = field_name.replace("_", " ")
        if isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{label}:")
            for item in value:
                parts = [f"{k.replace('_', ' ')}: {v}" for k, v in item.items() if not _is_empty(v)]
                lines.append("  - " + ", ".join(parts))
        elif isinstance(value, list):
            lines.append(f"- {label}: " + ", ".join(str(v) for v in value))
        else:
            lines.append(f"- {label}: {value}")
    if not lines:
        return "(nothing recorded yet)"
    return "\n".join(lines)


# --- approximate-date parsing (events section) -----------------------------------------


def _parse_approx_date(text: str) -> date | None:
    """Best-effort parse of a patient-given approximate date into a `date`.

    Accepts a full ISO date, `YYYY-MM`, or a bare `YYYY`; falls back to the
    first 4-digit year found anywhere in the text (day/month default to the
    1st). Returns None when no year can be found at all - undated events are
    legitimate (patients often cannot date a hospitalization).
    """
    stripped = text.strip()
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{4})-(\d{2})", stripped)
    if match:
        return date(int(match.group(1)), int(match.group(2)), 1)
    match = re.search(r"\d{4}", stripped)
    if match:
        return date(int(match.group(0)), 1, 1)
    # No year anywhere ("recently", "<UNKNOWN>", "as a child"): the event is
    # real but undatable - callers record it as an undated event rather than
    # fabricating a date or failing the confirm (real onboarding crash).
    return None


# --- block-replace helper for shared files (case-summary.md, medications.md) -----------


def _replace_block(text: str, marker: str, heading: str, body: str) -> str:
    start = f"<!-- intake:{marker}:start -->"
    end = f"<!-- intake:{marker}:end -->"
    block = f"{start}\n## {heading}\n\n{body}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(block, text)
    separator = "\n\n" if not text.endswith("\n\n") else ""
    return f"{text.rstrip()}\n{separator}{block}\n"


# --- per-section writers: produce the PLAN.md target artifacts -------------------------


def _write_basics(repo: DataRepo, data: BasicsSection) -> list[str]:
    lines = []
    if data.age is not None:
        lines.append(f"- Age: {data.age}")
    if data.sex_at_birth:
        lines.append(f"- Sex at birth: {data.sex_at_birth}")
    if data.height_cm is not None:
        lines.append(f"- Height: {data.height_cm} cm")
    if data.weight_kg is not None:
        lines.append(f"- Weight: {data.weight_kg} kg")
    if data.occupation:
        lines.append(f"- Occupation: {data.occupation}")
    if data.exposures:
        lines.append("- Exposures: " + ", ".join(data.exposures))
    body = "\n".join(lines) if lines else "_Not yet recorded._"
    current = repo.read(CASE_SUMMARY_RELPATH)
    repo.write(CASE_SUMMARY_RELPATH, _replace_block(current, "basics", "Patient basics", body))
    return [CASE_SUMMARY_RELPATH]


def _write_symptoms(repo: DataRepo, data: SymptomsSection) -> list[str]:
    if not data.symptoms:
        body = "_None recorded._"
    else:
        lines = []
        for symptom in data.symptoms:
            details = [
                f"{label}: {value}"
                for label, value in (
                    ("onset", symptom.onset),
                    ("frequency", symptom.frequency),
                    ("triggers", symptom.triggers),
                    ("severity", symptom.severity),
                )
                if value
            ]
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"- {symptom.description}{suffix}")
        body = "\n".join(lines)
    current = repo.read(CASE_SUMMARY_RELPATH)
    repo.write(CASE_SUMMARY_RELPATH, _replace_block(current, "symptoms", "Current symptoms", body))
    return [CASE_SUMMARY_RELPATH]


def _write_allergies(repo: DataRepo, data: AllergiesSection) -> list[str]:
    if not data.allergies:
        body = "_None recorded._"
    else:
        lines = []
        for allergy in data.allergies:
            details = [d for d in (allergy.reaction, allergy.severity) if d]
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"- {allergy.allergen}{suffix}")
        body = "\n".join(lines)
    current = repo.read(CASE_SUMMARY_RELPATH)
    repo.write(
        CASE_SUMMARY_RELPATH,
        _replace_block(current, "allergies", "Allergies & reactions", body),
    )
    return [CASE_SUMMARY_RELPATH]


UNDATED_EVENTS_RELPATH = "case/undated-events.md"


def _write_events(repo: DataRepo, data: EventsSection) -> list[str]:
    written: list[str] = []
    encounters_dir = repo.root / ENCOUNTERS_RELDIR
    undated: list[str] = []
    for event in data.events:
        event_date = _parse_approx_date(event.date_approx) if event.date_approx else None
        summary = event.description.strip() or event.title
        if event_date is None:
            timing = f" (timing: {event.date_approx})" if event.date_approx else ""
            undated.append(f"- **{event.title}**{timing}: {summary}")
            continue
        frontmatter = EncounterFrontmatter(date=event_date, type="patient-report")
        encounter = Encounter(frontmatter=frontmatter, summary=summary)
        # `write_encounter` names the file deterministically from
        # `(date, slug(title))`, so re-confirming the same event (same date +
        # title) overwrites the same file rather than duplicating it.
        path = write_encounter(encounters_dir, encounter, event.title)
        written.append(str(path.relative_to(repo.root)))
    if undated:
        repo.write(
            UNDATED_EVENTS_RELPATH,
            "# Medical Events Without Dates\n\n"
            "Reported during onboarding without enough timing to place on the "
            "timeline - worth pinning down at a future appointment.\n\n"
            + "\n".join(undated)
            + "\n",
        )
        written.append(UNDATED_EVENTS_RELPATH)
    return written


def _write_patient_theories(repo: DataRepo, data: PriorDiagnosesSection) -> list[str]:
    lines = ["# Patient-Reported Diagnoses & Theories", "", "## Prior diagnoses & workups", ""]
    if data.diagnoses:
        for diagnosis in data.diagnoses:
            who = f" (by {diagnosis.by_whom})" if diagnosis.by_whom else ""
            year = f", {diagnosis.year}" if diagnosis.year is not None else ""
            lines.append(f"- {diagnosis.name}{who}{year} — status: {diagnosis.status}")
    else:
        lines.append("_None recorded._")
    lines += ["", "## Patient-suspected diagnoses (origin: patient, not yet on the ledger)", ""]
    if data.patient_suspected:
        for suspected in data.patient_suspected:
            why = f" — {suspected.why}" if suspected.why else ""
            lines.append(f"- {suspected.name}{why}")
    else:
        lines.append("_None recorded._")
    lines += [
        "",
        "_Note: this file is patient-reported and is not itself the differential "
        "ledger. The first diagnostic run's Ledger-Maintainer pass ingests these "
        "entries as `origin: patient` hypotheses, which then go through the "
        "normal Challenger + invariant-checked apply like any other hypothesis "
        "(see `adoc.intake.wizard` module docstring)._",
    ]
    content = "\n".join(lines) + "\n"
    repo.write(PATIENT_THEORIES_RELPATH, content)
    return [PATIENT_THEORIES_RELPATH]


def _write_family_history(repo: DataRepo, data: FamilyHistorySection) -> list[str]:
    lines = ["# Family History", ""]
    if not data.relatives:
        lines.append("_Not yet populated._")
    for relative in data.relatives:
        conditions = ", ".join(relative.conditions) if relative.conditions else "none reported"
        onset = f", onset age {relative.age_at_onset}" if relative.age_at_onset is not None else ""
        death = ""
        if relative.deceased:
            death_age = (
                f", age {relative.age_at_death}" if relative.age_at_death is not None else ""
            )
            death = f" (deceased{death_age})"
        lines.append(f"- **{relative.relation}**: {conditions}{onset}{death}")
    content = "\n".join(lines) + "\n"
    repo.write(FAMILY_HISTORY_RELPATH, content)
    return [FAMILY_HISTORY_RELPATH]


def _write_geography(repo: DataRepo, data: GeographySection) -> list[str]:
    """Own whole-file writer (like `_write_family_history`/`_write_care_team`)
    rather than a `case-summary.md` block — a residence history plus travel
    plus exposures is the same kind of multi-list structured content those
    sibling topics already get a dedicated file for, and it keeps
    `case-summary.md` from growing an ever-longer list of prior addresses."""
    lines = ["# Geography & Environmental Exposure", "", "## Residences", ""]
    if not data.residences:
        lines.append("_Not yet recorded._")
    else:
        for residence in data.residences:
            when = f" ({residence.date_approx})" if residence.date_approx else ""
            current = " — current" if residence.current else ""
            lines.append(f"- {residence.place}{when}{current}")
    lines += ["", "## Travel of note", ""]
    if data.travel:
        lines += [f"- {t}" for t in data.travel]
    else:
        lines.append("_None recorded._")
    lines += ["", "## Environmental & occupational exposures", ""]
    if data.exposures:
        lines += [f"- {e}" for e in data.exposures]
    else:
        lines.append("_None recorded._")
    content = "\n".join(lines) + "\n"
    repo.write(GEOGRAPHY_RELPATH, content)
    return [GEOGRAPHY_RELPATH]


def _write_medications(repo: DataRepo, data: MedicationsSection) -> list[str]:
    if not data.medications:
        body = "_None recorded._"
    else:
        lines = []
        for med in data.medications:
            status = "current" if med.still_taking else "past"
            details = [d for d in (med.dose, med.frequency) if d]
            detail_str = f", {', '.join(details)}" if details else ""
            notes = f" — {med.notes}" if med.notes else ""
            lines.append(f"- {med.name}{detail_str} ({status}){notes}")
        body = "\n".join(lines)
    current = repo.read(MEDICATIONS_RELPATH)
    repo.write(
        MEDICATIONS_RELPATH,
        _replace_block(current, "medications", "Current & past medications", body),
    )
    return [MEDICATIONS_RELPATH]


def _write_supplements(repo: DataRepo, data: SupplementsSection) -> list[str]:
    if not data.supplements:
        lines = ["_None recorded._"]
    else:
        lines = []
        for supp in data.supplements:
            status = "current" if supp.still_taking else "past"
            details = [d for d in (supp.dose, supp.frequency) if d]
            detail_str = f", {', '.join(details)}" if details else ""
            notes = f" — {supp.notes}" if supp.notes else ""
            lines.append(f"- {supp.name}{detail_str} ({status}){notes}")
    lines += ["", _BIOTIN_CAVEAT]
    body = "\n".join(lines)
    current = repo.read(MEDICATIONS_RELPATH)
    repo.write(MEDICATIONS_RELPATH, _replace_block(current, "supplements", "Supplements", body))
    return [MEDICATIONS_RELPATH]


def _write_care_team(repo: DataRepo, data: CareTeamSection) -> list[str]:
    lines = ["# Care Team", ""]
    if not data.providers:
        lines.append("_Not yet populated._")
    for provider in data.providers:
        specialty = f", {provider.specialty}" if provider.specialty else ""
        org = f" — {provider.org}" if provider.org else ""
        lines.append(f"- {provider.name}{specialty}{org}")
    lines.append("")
    lines.append(f"**Insurer:** {data.insurer}" if data.insurer else "**Insurer:** _not recorded_")
    content = "\n".join(lines) + "\n"
    repo.write(CARE_TEAM_RELPATH, content)
    return [CARE_TEAM_RELPATH]


def _write_document_drop(repo: DataRepo, data: DocumentDropSection) -> list[str]:  # noqa: ARG001
    # Nothing else to write: the document-drop section only records
    # acknowledgment. `confirm()` always commits `intake-state.yaml`, which
    # is where that fact is durably recorded.
    return []


def write_section(repo: DataRepo, key: str, data: BaseModel) -> list[str]:
    """Public entry point for `_write_section` — the conversational intake
    engine (`intake/agent.py`, via `intake/convert.py`) writes a section's
    case-file artifact(s) through this same function, so the per-section
    writers below stay the single source of truth for output regardless of
    which front end (the state-machine wizard or the fact-based agent)
    produced the section's data."""
    return _write_section(repo, key, data)


def _write_section(repo: DataRepo, key: str, data: BaseModel) -> list[str]:
    if key == "basics":
        assert isinstance(data, BasicsSection)
        return _write_basics(repo, data)
    if key == "symptoms":
        assert isinstance(data, SymptomsSection)
        return _write_symptoms(repo, data)
    if key == "events":
        assert isinstance(data, EventsSection)
        return _write_events(repo, data)
    if key == "prior_diagnoses":
        assert isinstance(data, PriorDiagnosesSection)
        return _write_patient_theories(repo, data)
    if key == "family_history":
        assert isinstance(data, FamilyHistorySection)
        return _write_family_history(repo, data)
    if key == "geography":
        assert isinstance(data, GeographySection)
        return _write_geography(repo, data)
    if key == "medications":
        assert isinstance(data, MedicationsSection)
        return _write_medications(repo, data)
    if key == "supplements":
        assert isinstance(data, SupplementsSection)
        return _write_supplements(repo, data)
    if key == "allergies":
        assert isinstance(data, AllergiesSection)
        return _write_allergies(repo, data)
    if key == "care_team":
        assert isinstance(data, CareTeamSection)
        return _write_care_team(repo, data)
    if key == "document_drop":
        assert isinstance(data, DocumentDropSection)
        return _write_document_drop(repo, data)
    raise IntakeError(f"no writer registered for section {key!r}")  # pragma: no cover


# --- the wizard --------------------------------------------------------------------------


class IntakeWizard:
    """The resumable onboarding state machine. See module docstring."""

    def __init__(
        self,
        repo: DataRepo,
        client: LlmClient,
        *,
        dropbox_folder: str = DEFAULT_DROPBOX_FOLDER,
    ) -> None:
        self._repo = repo
        self._client = client
        self._dropbox_folder = dropbox_folder
        self._state = self._load_state()

    # -- persistence ------------------------------------------------------------------

    def _state_path(self) -> Path:
        return self._repo.root / INTAKE_STATE_RELPATH

    def _load_state(self) -> IntakeState:
        state = load_intake_state(self._state_path())
        for spec in SECTIONS:
            state.sections.setdefault(spec.key, SectionState())
        self._auto_complete_document_drop(state)
        if state.cursor is not None and state.cursor not in {spec.key for spec in SECTIONS}:
            state.cursor = self._first_incomplete_key(state)
        return state

    def _auto_complete_document_drop(self, state: IntakeState) -> None:
        """When documents are already on file (a seeded/curated deployment:
        `sources/` is non-empty), the document-drop section has nothing to
        ask — mark it complete automatically instead of prompting the
        patient to upload what is already there. A fresh, empty data repo
        still gets the prompt."""
        section = state.sections.get("document_drop")
        if section is None or section.status == "complete":
            return
        sources = self._repo.root / "sources"
        has_documents = sources.is_dir() and any(
            entry.name != ".gitkeep" for entry in sources.iterdir()
        )
        if has_documents:
            section.status = "complete"
            section.completed_at = datetime.now(UTC)

    def _save_state(self) -> None:
        save_intake_state(self._state_path(), self._state)

    def _first_incomplete_key(self, state: IntakeState | None = None) -> str | None:
        state = state if state is not None else self._state
        for spec in SECTIONS:
            if state.sections[spec.key].status != "complete":
                return spec.key
        return None

    # -- read-only queries --------------------------------------------------------------

    def current_section(self) -> SectionSpec | None:
        if self._state.cursor is None:
            return None
        return _spec_by_key(self._state.cursor)

    def current_status(self) -> SectionStatus | None:
        spec = self.current_section()
        if spec is None:
            return None
        return self._state.sections[spec.key].status

    def baseline_incomplete(self) -> bool:
        """True until every section's status is `complete` — the web UI banner hook."""
        return any(state.status != "complete" for state in self._state.sections.values())

    def progress(self) -> tuple[int, int]:
        """`(sections_completed, total_sections)`."""
        completed = sum(1 for state in self._state.sections.values() if state.status == "complete")
        return completed, len(SECTIONS)

    def prompt_for_current(self) -> str:
        spec = self.current_section()
        if spec is None:
            return "Onboarding is complete — every section has been recorded and committed."

        state = self._state.sections[spec.key]
        index = _section_index(spec.key)
        lines = [f"[{index}/{len(SECTIONS)}] {spec.title}", "", spec.intro]

        if spec.key == "document_drop":
            lines += [
                "",
                f"Put existing PDFs or scans in your Dropbox folder "
                f"({self._dropbox_folder}), or upload them directly in the web UI "
                "once it's running.",
            ]

        if state.draft:
            lines += ["", "Currently recorded:", _render_playback(spec, state.draft)]
            if state.status == "awaiting_confirmation":
                lines += [
                    "",
                    "Anything wrong or missing? Tell me, or say it looks good to confirm.",
                ]

        return "\n".join(lines)

    # -- extraction ----------------------------------------------------------------------

    def _extract(self, user_text: str) -> PlaybackMessage:
        spec = self.current_section()
        if spec is None:
            raise IntakeError("onboarding is already complete; there is no section to submit to")

        state = self._state.sections[spec.key]
        result = self._client.complete(
            "primary_reasoner",
            system=spec.extraction_system_prompt,
            messages=[Message(role="user", content=user_text)],
            schema=spec.schema,
        )
        parsed = result.parsed
        if parsed is None:  # pragma: no cover - complete() guarantees this when schema is passed
            raise LlmError(f"section {spec.key!r}: no structured extraction returned")

        merged = _merge_section_data(state.draft, parsed.model_dump(mode="json"))
        spec.schema.model_validate(merged)  # re-validate the merged shape before persisting

        state.draft = merged
        state.status = "awaiting_confirmation"
        self._save_state()

        return PlaybackMessage(
            section_key=spec.key,
            text=f"{_PLAYBACK_INTRO}\n\n{_render_playback(spec, merged)}",
        )

    def submit(self, user_text: str) -> PlaybackMessage:
        """Extract the current section from `user_text`, merged over any prior draft."""
        return self._extract(user_text)

    def revise(self, user_text: str) -> PlaybackMessage:
        """Re-extract the current section, merging the patient's corrections in."""
        return self._extract(user_text)

    # -- commit ----------------------------------------------------------------------------

    def confirm(self) -> CommitResult:
        spec = self.current_section()
        if spec is None:
            raise IntakeError("onboarding is already complete; there is no section to confirm")

        state = self._state.sections[spec.key]
        if state.draft is None:
            raise IntakeError(f"section {spec.key!r} has no draft yet; call submit() first")

        data = spec.schema.model_validate(state.draft)
        artifacts = _write_section(self._repo, spec.key, data)

        state.status = "complete"
        state.completed_at = datetime.now(UTC)
        self._state.cursor = self._first_incomplete_key()
        self._save_state()

        paths = [*artifacts, INTAKE_STATE_RELPATH]
        message = f"feat(intake): complete {spec.title.lower()} section"
        sha = self._repo.commit(message, paths=paths)

        return CommitResult(section_key=spec.key, commit_sha=sha, artifacts=artifacts)

    # -- navigation (CLI 'skip' / 'back') ---------------------------------------------------

    def skip_current(self) -> SectionSpec | None:
        """Move the cursor to the next section, leaving this one's status untouched."""
        spec = self.current_section()
        if spec is None:
            return None
        index = _section_index(spec.key)
        self._state.cursor = SECTIONS[index].key if index < len(SECTIONS) else None
        self._save_state()
        return self.current_section()

    def go_back(self) -> SectionSpec | None:
        """Move the cursor to the previous section in registry order."""
        spec = self.current_section()
        current_index = _section_index(spec.key) if spec is not None else len(SECTIONS) + 1
        previous_index = max(1, current_index - 1)
        self._state.cursor = SECTIONS[previous_index - 1].key
        self._save_state()
        return self.current_section()

    # -- reopen a completed section ---------------------------------------------------------

    def reopen(self, key: str) -> SectionSpec:
        """Re-open a completed (or any) section for editing ("update my medications")."""
        spec = _spec_by_key(key)
        state = self._state.sections[spec.key]
        state.status = "awaiting_confirmation" if state.draft is not None else "pending"
        state.completed_at = None
        self._state.cursor = spec.key
        self._save_state()
        return spec

"""Tests for adoc.intake.corroborate: deterministic fact corroboration
(`docs/adr/0013-fact-corroboration.md`). No LLM calls anywhere here — every
test builds facts/documents/encounters directly and asserts on the computed
`CorroborationUpdate`s."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import Provenance
from adoc.intake.corroborate import corroborate_facts
from adoc.intake.facts import IntakeFact
from adoc.labs.db import LabsDb
from adoc.labs.models import DocumentStatus, ExtractionStatus, LabDocument, LabResult

TODAY = date(2026, 8, 24)


def _provenance() -> Provenance:
    return Provenance(
        app_version="0.0.0-test",
        prompt_template_version="1",
        model_id="fake-model",
        dag_node="intake-agent",
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _fact(**overrides: object) -> IntakeFact:
    data: dict[str, object] = {
        "id": "f1",
        "section": "events",
        "kind": "event",
        "statement": "placeholder",
        "provenance": _provenance(),
    }
    data.update(overrides)
    return IntakeFact.model_validate(data)


def _doc(
    sha: str, doc_date: date, doc_type: str = "clinical_note", filename: str = "doc.pdf"
) -> LabDocument:
    return LabDocument(
        sha256=sha,
        filename=filename,
        doc_type=doc_type,
        doc_date=doc_date,
        page_count=1,
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=DocumentStatus.COMPLETE,
    )


def _lab_row(sha: str, lab_date: date, name: str = "vitamin D") -> LabResult:
    return LabResult(
        date=lab_date,
        name=name,
        name_raw=name,
        value=30.0,
        ucum_unit="ng/mL",
        source_doc=sha,
        extraction_status=ExtractionStatus.AUTO,
        raw_json=json.dumps({"name_raw": name, "value": 30.0}),
    )


# --- event facts: tolerance windows by date_approx precision ---------------------------


def test_event_exact_date_within_14_days_corroborates(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 3, 10)))

    fact = _fact(
        kind="event",
        statement="ER visit for chest pain.",
        date_approx="2024-03-02",
        precision="exact",
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert len(updates) == 1
    assert updates[0].corroboration == "corroborated"
    assert updates[0].corroboration_source == "doc:doc.pdf#p1"


def test_event_exact_date_beyond_14_days_stays_unverified(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 4, 20)))

    fact = _fact(
        kind="event",
        statement="ER visit for chest pain.",
        date_approx="2024-03-02",
        precision="exact",
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


def test_event_year_month_within_120_days_corroborates(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2020, 8, 1)))

    fact = _fact(
        kind="event",
        statement="Hospitalization.",
        date_approx="2020-05",
        precision="approx",
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert len(updates) == 1
    assert updates[0].corroboration == "corroborated"


def test_event_year_only_within_366_days_corroborates_but_not_beyond(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2019, 6, 15)))

    within_year = _fact(
        id="f-within",
        kind="event",
        statement="Surgery.",
        date_approx="2019",
        precision="approx",
    )
    updates = corroborate_facts([within_year], db, repo, today=TODAY)
    assert updates[0].corroboration == "corroborated"

    far_doc_repo = DataRepo.init_at(tmp_path / "data2")
    far_db = LabsDb(":memory:")
    far_db.upsert_document(_doc("b" * 64, date(2021, 6, 15)))
    too_far = _fact(
        id="f-too-far",
        kind="event",
        statement="Surgery.",
        date_approx="2019",
        precision="approx",
    )
    far_updates = corroborate_facts([too_far], far_db, far_doc_repo, today=TODAY)
    assert far_updates == []


def test_event_relative_year_phrase_corroborates_within_120_days(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    # today=2026-08-24, "6 years ago" -> ~2020-01-01; 2020-03-01 is 60 days
    # away, within the +/-120 day relative-year tolerance.
    db.upsert_document(_doc("a" * 64, date(2020, 3, 1)))

    fact = _fact(
        kind="event",
        statement="Diagnosis event.",
        date_approx="about 6 years ago",
        precision="approx",
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert len(updates) == 1
    assert updates[0].corroboration == "corroborated"


def test_event_no_match_is_unverified_never_contradicted(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    fact = _fact(
        kind="event", statement="An ER visit.", date_approx="2019-03-01", precision="exact"
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []  # no change from the default "unverified" state


def test_event_matches_encounter_file(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    encounter = Encounter(
        frontmatter=EncounterFrontmatter(date=date(2024, 3, 2), type="patient-report"),
        summary="ER visit",
    )
    write_encounter(repo.root / "case" / "encounters", encounter, "er-visit")

    fact = _fact(kind="event", statement="ER visit.", date_approx="2024-03-02", precision="exact")
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert len(updates) == 1
    assert updates[0].corroboration_source == "encounter:2024-03-02--er-visit.md"


def test_event_without_date_approx_is_never_processed(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 3, 2)))

    fact = _fact(kind="event", statement="Some event.", date_approx=None)
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


# --- diagnosis facts: period corroboration + future-year contradiction -----------------


def test_diagnosis_year_matches_clinical_note_within_one_year(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2019, 6, 1), doc_type="clinical_note"))

    fact = _fact(
        id="dx1",
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Diagnosed with lupus.",
        attribution="doctor_diagnosed",
        fields={"year": 2019, "by_whom": "Dr. Lee"},
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert len(updates) == 1
    assert updates[0].corroboration == "corroborated"
    assert "period corroboration only" in (updates[0].corroboration_note or "")


def test_diagnosis_year_ignores_non_clinical_documents(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2019, 6, 1), doc_type="lab_report"))

    fact = _fact(
        id="dx1",
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Diagnosed with lupus.",
        attribution="doctor_diagnosed",
        fields={"year": 2019, "by_whom": "Dr. Lee"},
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


def test_diagnosis_year_predating_all_documentation_stays_unverified(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 1, 1), doc_type="clinical_note"))

    fact = _fact(
        id="dx1",
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Diagnosed with lupus.",
        attribution="doctor_diagnosed",
        fields={"year": 2010, "by_whom": "Dr. Lee"},
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


def test_diagnosis_future_year_is_contradicted(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    fact = _fact(
        id="dx1",
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Diagnosed with lupus.",
        attribution="doctor_diagnosed",
        fields={"year": 2099, "by_whom": "Dr. Lee"},
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert len(updates) == 1
    assert updates[0].corroboration == "contradicted"
    assert updates[0].corroboration_source is None


def test_diagnosis_without_year_field_is_never_processed(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    fact = _fact(
        id="dx1",
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Diagnosed with lupus.",
        attribution="doctor_diagnosed",
        fields={"by_whom": "Dr. Lee"},
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


# --- medication/supplement facts: always skipped (circular) ----------------------------


def test_medication_and_supplement_facts_are_never_processed(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 1, 1)))

    med = _fact(id="med1", section="medications", kind="medication", statement="Metformin.")
    supp = _fact(id="supp1", section="supplements", kind="supplement", statement="Vitamin D.")
    updates = corroborate_facts([med, supp], db, repo, today=TODAY)

    assert updates == []


# --- symptom facts: exact canonical analyte match only ----------------------------------


def test_symptom_referencing_canonical_analyte_with_rows_corroborates(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 1, 1)))
    db.insert_results([_lab_row("a" * 64, date(2024, 1, 1), name="vitamin D")])

    fact = _fact(
        id="sym1",
        section="symptoms",
        kind="symptom",
        statement="Patient reports low vitamin D and fatigue.",
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert len(updates) == 1
    assert updates[0].corroboration == "corroborated"
    assert updates[0].corroboration_source is not None
    assert updates[0].corroboration_source.startswith("labs:")


def test_symptom_with_no_analyte_reference_is_never_processed(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    fact = _fact(
        id="sym1", section="symptoms", kind="symptom", statement="Patient reports fatigue."
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


def test_symptom_matching_analyte_with_no_rows_is_never_processed(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")

    fact = _fact(
        id="sym1",
        section="symptoms",
        kind="symptom",
        statement="Patient reports low vitamin D and fatigue.",
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


# --- retracted facts are never processed ------------------------------------------------


def test_retracted_facts_are_never_processed(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 3, 2)))

    fact = _fact(
        kind="event",
        statement="ER visit.",
        date_approx="2024-03-02",
        precision="exact",
        status="retracted",
    )
    updates = corroborate_facts([fact], db, repo, today=TODAY)

    assert updates == []


# --- idempotency: no update when the computed state already matches --------------------


def test_idempotent_no_update_when_already_corroborated_with_same_state(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    db = LabsDb(":memory:")
    db.upsert_document(_doc("a" * 64, date(2024, 3, 10)))

    fact = _fact(
        kind="event",
        statement="ER visit for chest pain.",
        date_approx="2024-03-02",
        precision="exact",
    )
    first = corroborate_facts([fact], db, repo, today=TODAY)
    assert len(first) == 1

    already_corroborated = fact.model_copy(
        update={
            "corroboration": first[0].corroboration,
            "corroboration_source": first[0].corroboration_source,
            "corroboration_note": first[0].corroboration_note,
        }
    )
    second = corroborate_facts([already_corroborated], db, repo, today=TODAY)
    assert second == []


# --- old-facts-file backward compat: defaults load cleanly ------------------------------


def test_old_style_fact_without_new_fields_loads_with_defaults() -> None:
    old_style = {
        "id": "f1",
        "section": "events",
        "kind": "event",
        "statement": "An old fact with no corroboration fields on file.",
        "provenance": _provenance().model_dump(mode="json"),
    }
    fact = IntakeFact.model_validate(old_style)

    assert fact.corroboration == "unverified"
    assert fact.corroboration_source is None
    assert fact.corroboration_note is None
    assert fact.reported_on is None

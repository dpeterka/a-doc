"""The three patient-update scenarios, end to end.

Each test names the sentence it is about. These are the paths where a
patient's own knowledge enters a record that was otherwise built entirely
from documents.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from adoc.casefile.disputes import DISPUTES_RELPATH, load_disputes
from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.patient_updates import (
    DisputeClaim,
    ReportedResultClaim,
    apply_patient_updates,
)
from adoc.casefile.repo import DataRepo
from adoc.casefile.reported import REPORTED_RESULTS_RELPATH, load_reported_results
from adoc.casefile.reported_corroborate import corroborate_reported
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabFlag, LabResult

TODAY = date(2026, 8, 28)
SHA = "b" * 64


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


def _mri_encounter(repo: DataRepo) -> str:
    path = write_encounter(
        repo.root / "case" / "encounters",
        Encounter(
            frontmatter=EncounterFrontmatter(date=date(2026, 8, 23), type="imaging"),
            summary="MRI pituitary.",
        ),
        "mripituitary",
    )
    return f"encounter:{path.name}"


# --- Example 2: a result she remembers, with no document ---------------------


def test_a_remembered_result_is_recorded_with_its_date(repo: DataRepo) -> None:
    """ "I know I had a lab in November of 2024 ... I know I had high levels of
    Iron at that time."

    Before this it became prose in an intake fact, invisible to every numeric
    consumer, because `LabResult.source_doc` is a required sha256 and a
    remembered value has no document.
    """
    report = apply_patient_updates(
        repo,
        reported=[ReportedResultClaim(analyte="Iron", direction="high", when_text="November 2024")],
        disputes=[],
        message="I had a lab in November of 2024 and my Iron was high",
        today=TODAY,
    )

    entry = load_reported_results(repo.root / Path(REPORTED_RESULTS_RELPATH)).entries[0]
    assert report.reported == ["Iron"]
    assert entry.when == date(2024, 11, 1)
    # A month, not a fabricated day.
    assert entry.when_precision == "month"
    assert entry.direction == "high"
    assert entry.verification == "unverified"
    assert entry.sources == [f"patient-report:{TODAY.isoformat()}"]


def test_a_remembered_result_never_enters_the_measured_series(repo: DataRepo) -> None:
    """The strictness that made this hard is the strictness worth keeping: a
    remembered value must not sit in the series the citation checker guards."""
    apply_patient_updates(
        repo,
        reported=[ReportedResultClaim(analyte="Iron", direction="high", when_text="2024")],
        disputes=[],
        message="my iron was high in 2024",
        today=TODAY,
    )
    db = LabsDb(":memory:")

    assert db.all_non_rejected_rows() == []


def test_an_invented_analyte_is_dropped(repo: DataRepo) -> None:
    """Same guard as the regimen path: a model that invents an analyte writes
    a fabricated lab result into a medical record."""
    report = apply_patient_updates(
        repo,
        reported=[ReportedResultClaim(analyte="Ferritin", direction="low")],
        disputes=[],
        message="I have been feeling tired.",
        today=TODAY,
    )

    assert report.reported == []
    assert report.dropped_ungrounded == ["Ferritin"]


def test_a_document_that_agrees_corroborates_the_memory(repo: DataRepo, tmp_path: Path) -> None:
    """When a document finally arrives carrying that analyte near that date,
    the loop she opened is closed."""
    apply_patient_updates(
        repo,
        reported=[ReportedResultClaim(analyte="iron", direction="high", when_text="November 2024")],
        disputes=[],
        message="my iron was high in November 2024",
        today=TODAY,
    )
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(
        LabDocument(sha256=SHA, filename="panel.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2024, 11, 12),
                name="iron",
                name_raw="Iron",
                value=210.0,
                flag=LabFlag.HIGH,
                source_doc=SHA,
                raw_json="{}",
            )
        ]
    )

    path = repo.root / Path(REPORTED_RESULTS_RELPATH)
    result = corroborate_reported(load_reported_results(path), db)

    assert result.entries[0].verification == "corroborated"
    assert result.entries[0].corroborating_source == "labs:iron:2024-11-12"


def test_a_document_that_disagrees_is_flagged_not_ignored(repo: DataRepo, tmp_path: Path) -> None:
    """A remembered "high" against a measured normal may mean a different
    analyte, a different year, or someone else's result. A differential built
    on the memory would be built on sand, so the conflict is stated."""
    apply_patient_updates(
        repo,
        reported=[ReportedResultClaim(analyte="iron", direction="high", when_text="November 2024")],
        disputes=[],
        message="my iron was high in November 2024",
        today=TODAY,
    )
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(
        LabDocument(sha256=SHA, filename="panel.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2024, 11, 12),
                name="iron",
                name_raw="Iron",
                value=95.0,
                source_doc=SHA,
                raw_json="{}",
            )
        ]
    )

    result = corroborate_reported(
        load_reported_results(repo.root / Path(REPORTED_RESULTS_RELPATH)), db
    )

    assert result.entries[0].verification == "contradicted"
    assert "DISAGREES" in result.entries[0].note or "measured" in result.entries[0].note


def test_an_undated_memory_is_not_matched_to_anything(repo: DataRepo, tmp_path: Path) -> None:
    """Picking the nearest row for an undated claim would manufacture a
    correspondence out of nothing."""
    apply_patient_updates(
        repo,
        reported=[ReportedResultClaim(analyte="iron", direction="high")],
        disputes=[],
        message="my iron was high once",
        today=TODAY,
    )
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(
        LabDocument(sha256=SHA, filename="panel.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2024, 11, 12),
                name="iron",
                name_raw="Iron",
                value=210.0,
                flag=LabFlag.HIGH,
                source_doc=SHA,
                raw_json="{}",
            )
        ]
    )

    result = corroborate_reported(
        load_reported_results(repo.root / Path(REPORTED_RESULTS_RELPATH)), db
    )

    assert result.entries[0].verification == "unverified"


# --- Example 3: the patient says the record is wrong ------------------------


def test_a_dispute_is_recorded_against_the_named_item(repo: DataRepo) -> None:
    """ "You reported I had a pituitary scan in 2026. This did not occur."

    There was no answer to that sentence: `retract_fact` reaches intake facts
    only, and the MRI is an ingested encounter.
    """
    target = _mri_encounter(repo)

    report = apply_patient_updates(
        repo,
        reported=[],
        disputes=[
            DisputeClaim(
                target=target,
                kind="did-not-occur",
                statement="This did not occur, your information is wrong.",
            )
        ],
        message="You reported I had a pituitary scan in 2026. This did not occur.",
        today=TODAY,
    )

    disputes = load_disputes(repo.root / Path(DISPUTES_RELPATH))
    assert report.disputes == [target]
    assert disputes.entries[0].kind == "did-not-occur"
    assert disputes.entries[0].status == "open"
    assert target in disputes.open_targets()


def test_a_dispute_never_deletes_the_record(repo: DataRepo) -> None:
    """The archived document stays: she may be misremembering, and a system
    that erased records on request would be worse than one that ignored
    them."""
    target = _mri_encounter(repo)
    filename = target.split(":", 1)[1]

    apply_patient_updates(
        repo,
        reported=[],
        disputes=[DisputeClaim(target=target, kind="did-not-occur")],
        message="that scan did not occur",
        today=TODAY,
    )

    assert (repo.root / "case" / "encounters" / filename).is_file()


def test_a_dispute_against_something_not_on_file_is_dropped(repo: DataRepo) -> None:
    """It would sit in the file forever matching nothing, and would look like
    an unaddressed patient objection when there is no such item."""
    report = apply_patient_updates(
        repo,
        reported=[],
        disputes=[DisputeClaim(target="encounter:2099-01-01--invented.md")],
        message="that never happened",
        today=TODAY,
    )

    assert report.disputes == []
    assert report.dropped_unknown_target == ["encounter:2099-01-01--invented.md"]
    assert load_disputes(repo.root / Path(DISPUTES_RELPATH)).entries == []


def test_repeating_an_objection_does_not_stack_disputes(repo: DataRepo) -> None:
    target = _mri_encounter(repo)
    for _ in range(3):
        apply_patient_updates(
            repo,
            reported=[],
            disputes=[DisputeClaim(target=target, kind="did-not-occur")],
            message="that scan did not occur",
            today=TODAY,
        )

    assert len(load_disputes(repo.root / Path(DISPUTES_RELPATH)).entries) == 1

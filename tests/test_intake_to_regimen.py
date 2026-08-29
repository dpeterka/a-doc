"""Tests for adoc.intake.to_regimen — medications converge on the regimen.

Intake wrote `case/medications.md` (1,549 bytes on the live case file) and
nothing ever read it. Adding that prose to the context pack would make it
visible but leave it the wrong shape: a list of names cannot answer whether
she was taking something when a specimen was drawn.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from adoc.casefile.regimen import Regimen, RegimenEntry
from adoc.casefile.schema import Provenance
from adoc.intake.facts import IntakeFact
from adoc.intake.to_regimen import facts_to_regimen_entries, merge_intake_medications

TODAY = date(2026, 8, 29)


def _fact(kind: str, name: str, **fields: object) -> IntakeFact:
    return IntakeFact(
        id=f"{kind}-{name.lower().replace(' ', '-')}",
        section="medications" if kind == "medication" else "supplements",
        kind=kind,  # type: ignore[arg-type]
        statement=name,
        fields={"name": name, **fields},
        provenance=Provenance(
            app_version="test",
            prompt_template_version="t@v1",
            model_id="m",
            dag_node="intake",
            timestamp=datetime(2026, 8, 29, tzinfo=UTC),
        ),
    )


def test_a_current_medication_becomes_an_attested_open_interval() -> None:
    """She said she takes it — not when she began. Recording a start date
    would be invention; attesting the date she said it is what is true."""
    entry = facts_to_regimen_entries(
        [_fact("medication", "Levothyroxine", dose="125 mcg", still_taking=True)],
        reported_on=TODAY,
    )[0]

    assert entry.kind == "medication"
    assert entry.dose == "125 mcg"
    assert entry.started is None
    assert entry.stopped is None
    assert entry.attested_on == [TODAY]
    assert entry.overlaps(TODAY) == "active"


def test_a_stopped_medication_leaves_both_endpoints_unknown() -> None:
    """`still_taking=False` says she is not on it now and nothing more.

    Recording today as the stop would claim she stopped during the
    conversation; recording any start would be invention. `overlaps` reports
    `unknown`, which is what is actually known.
    """
    entry = facts_to_regimen_entries(
        [_fact("medication", "Prednisone", still_taking=False)], reported_on=TODAY
    )[0]

    assert entry.started is None and entry.stopped is None
    assert entry.attested_on == []
    assert entry.overlaps(TODAY) == "unknown"


def test_attribution_is_never_guessed_from_the_kind() -> None:
    """Plenty of supplements are advised by a clinician and plenty of
    medications are not current, so the kind says nothing about who started
    it."""
    entries = facts_to_regimen_entries(
        [_fact("medication", "A"), _fact("supplement", "B")], reported_on=TODAY
    )

    assert {e.attribution for e in entries} == {"unknown"}


def test_a_substance_already_on_file_gains_detail_instead_of_duplicating() -> None:
    """`merge_entries` updates an open interval, so a regimen document and an
    intake mention do not produce two entries under two spellings."""
    existing = Regimen(entries=[RegimenEntry(name="levothyroxine", started=date(2026, 1, 1))])

    merged = merge_intake_medications(
        existing,
        [_fact("medication", "Levothyroxine", dose="125 mcg", still_taking=True)],
        reported_on=TODAY,
    )

    assert len(merged.entries) == 1
    assert merged.entries[0].dose == "125 mcg"
    assert merged.entries[0].started == date(2026, 1, 1)


def test_non_medication_facts_are_ignored() -> None:
    assert facts_to_regimen_entries([_fact("symptom", "fatigue")], reported_on=TODAY) == []

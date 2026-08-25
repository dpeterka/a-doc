"""Tests for adoc.intake.facts: the intake fact store and its deterministic
completion gates — the core safety mechanism of conversational onboarding
(docs/adr/0011-conversational-agentic-onboarding.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from adoc.intake.facts import (
    AddFact,
    IntakeFact,
    IntakeFactsStore,
    NewFact,
    RetractFact,
    UpdateFact,
    load_intake_facts,
    save_intake_facts,
    section_completion_blockers,
)


def _provenance(model_id: str = "fake-model") -> object:
    from adoc.casefile.schema import Provenance

    return Provenance(
        app_version="0.0.0-test",
        prompt_template_version="1",
        model_id=model_id,
        dag_node="intake-agent",
        timestamp=datetime.now(UTC),
    )


def _new_fact(**overrides: object) -> NewFact:
    data: dict[str, object] = {
        "id": "fact-1",
        "section": "allergies",
        "kind": "allergy",
        "statement": "Patient reports a penicillin allergy.",
        "fields": {"allergen": "penicillin"},
    }
    data.update(overrides)
    return NewFact.model_validate(data)


# --- op application: add / update / retract -----------------------------------------


def test_add_fact_persists_and_shows_up_active(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    result = store.apply_ops([AddFact(fact=_new_fact())], _provenance())

    assert result.added == ["fact-1"]
    active = store.active_facts()
    assert len(active) == 1
    assert active[0].statement == "Patient reports a penicillin allergy."
    assert active[0].status == "active"
    assert active[0].history == []


def test_add_fact_with_duplicate_id_is_rejected_not_raised_and_leaves_store_untouched(
    tmp_path: Path,
) -> None:
    """Defect fix (live blocker): a bad op must never cost the whole batch --
    `apply_ops` collects it in `.rejected` and keeps going, it never raises."""
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact())], _provenance())

    result = store.apply_ops([AddFact(fact=_new_fact())], _provenance())

    assert result.added == []
    assert len(result.rejected) == 1
    assert "duplicate fact id" in result.rejected[0]
    assert len(store.active_facts()) == 1  # unchanged


def test_new_fact_section_is_a_literal_rejected_at_model_validation_time() -> None:
    """Defect fix (live blocker): the live incident was a model emitting
    `section="note"` -- a valid `kind`, not a section -- which used to sail
    through as a bare `str` and only blow up deep inside `apply_ops`,
    losing the whole turn. `section` is now a `Literal` derived from
    `SECTIONS`, so the provider's own structured-output validation rejects
    the mistake before it ever reaches this module."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        _new_fact(id="fact-x", section="note")

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        _new_fact(id="fact-x", section="not-a-real-section")


def test_update_fact_merges_fields_and_appends_history(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact(fields={"allergen": "penicillin"}))], _provenance())

    store.apply_ops(
        [
            UpdateFact(
                id="fact-1",
                fields={"reaction": "hives"},
                note="patient clarified the reaction after a follow-up question",
            )
        ],
        _provenance(),
    )

    fact = store.get("fact-1")
    assert fact is not None
    assert fact.fields == {"allergen": "penicillin", "reaction": "hives"}
    assert len(fact.history) == 1
    assert "follow-up" in fact.history[0].change
    assert fact.history[0].prior_statement == "Patient reports a penicillin allergy."


def test_update_fact_unknown_id_is_rejected_not_raised(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    result = store.apply_ops(
        [UpdateFact(id="does-not-exist", note="this should be rejected, id is unknown")],
        _provenance(),
    )

    assert result.updated == []
    assert len(result.rejected) == 1
    assert "unknown fact id" in result.rejected[0]


def test_update_fact_note_too_short_is_rejected_by_pydantic() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        UpdateFact(id="fact-1", note="short")


# --- follow_up: docs/adr/0018-intake-clinical-progression-and-continuity.md ----------


def test_add_fact_can_be_flagged_follow_up(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact(id="follow-me", follow_up=True))], _provenance())

    fact = store.get("follow-me")
    assert fact is not None
    assert fact.follow_up is True


def test_add_fact_defaults_follow_up_to_false(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact())], _provenance())

    fact = store.get("fact-1")
    assert fact is not None
    assert fact.follow_up is False


def test_update_fact_sets_and_clears_follow_up(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact())], _provenance())

    store.apply_ops(
        [UpdateFact(id="fact-1", follow_up=True, note="worth checking back on next visit")],
        _provenance(),
    )
    assert store.get("fact-1").follow_up is True  # type: ignore[union-attr]

    store.apply_ops(
        [UpdateFact(id="fact-1", follow_up=False, note="revisited this on the next visit")],
        _provenance(),
    )
    assert store.get("fact-1").follow_up is False  # type: ignore[union-attr]


def test_update_fact_omitting_follow_up_leaves_it_unchanged(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact(follow_up=True))], _provenance())

    store.apply_ops(
        [UpdateFact(id="fact-1", fields={"reaction": "hives"}, note="added the reaction detail")],
        _provenance(),
    )

    assert store.get("fact-1").follow_up is True  # type: ignore[union-attr]


def test_retract_fact_marks_retracted_and_keeps_history(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact())], _provenance())

    store.apply_ops(
        [RetractFact(id="fact-1", reason="patient says this was a duplicate entry")], _provenance()
    )

    assert store.active_facts() == []
    fact = store.get("fact-1")
    assert fact is not None
    assert fact.status == "retracted"
    assert "retracted" in fact.history[0].change


def test_retract_fact_unknown_id_is_rejected_not_raised(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    result = store.apply_ops([RetractFact(id="nope", reason="n/a")], _provenance())

    assert result.retracted == []
    assert len(result.rejected) == 1
    assert "unknown fact id" in result.rejected[0]


# --- persistence round-trip -----------------------------------------------------------


def test_save_and_load_round_trips(tmp_path: Path) -> None:
    store = IntakeFactsStore(tmp_path)
    store.apply_ops([AddFact(fact=_new_fact())], _provenance())
    store.save()

    path = tmp_path / "case" / "intake-facts.yaml"
    loaded = load_intake_facts(path)
    assert len(loaded) == 1
    assert loaded[0].id == "fact-1"

    reloaded_store = IntakeFactsStore(tmp_path)
    assert len(reloaded_store.facts) == 1


def test_save_intake_facts_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "intake-facts.yaml"
    save_intake_facts(path, [])
    assert load_intake_facts(path) == []


def test_load_intake_facts_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_intake_facts(tmp_path / "does-not-exist.yaml") == []


# --- completion gates: the owner's named examples --------------------------------------


def _fact(**overrides: object) -> IntakeFact:
    data: dict[str, object] = {
        "id": "f1",
        "section": "family_history",
        "kind": "relative",
        "statement": "placeholder",
        "provenance": _provenance(),
    }
    data.update(overrides)
    return IntakeFact.model_validate(data)


def test_vague_family_allergy_needs_probe_blocks_and_clears() -> None:
    """ "My dad has allergies" -> needs_probe blocks; once resolved, clears."""
    vague = _fact(
        section="family_history",
        kind="relative",
        statement="Patient's dad has allergies.",
        clarification_status="needs_probe",
    )
    blockers = section_completion_blockers([vague], "family_history")
    assert len(blockers) == 1
    assert "needs a follow-up" in blockers[0] or "follow-up" in blockers[0]

    resolved = vague.model_copy(update={"clarification_status": "resolved"})
    assert section_completion_blockers([resolved], "family_history") == []


def test_doctor_diagnosed_without_by_whom_or_year_blocks() -> None:
    """ "I have cancer" recorded as doctor_diagnosed with neither by_whom nor
    year blocks; either one present clears it."""
    fact = _fact(
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Patient says they have cancer.",
        attribution="doctor_diagnosed",
        fields={},
        precision="unknown_after_probe",  # isolate rule (b) from rule (d)
    )
    blockers = section_completion_blockers([fact], "prior_diagnoses")
    assert len(blockers) == 1
    assert "diagnosed-by unclear" in blockers[0]

    with_year = fact.model_copy(update={"fields": {"year": 2020}})
    assert section_completion_blockers([with_year], "prior_diagnoses") == []

    with_by_whom = fact.model_copy(update={"fields": {"by_whom": "Dr. Lee"}})
    assert section_completion_blockers([with_by_whom], "prior_diagnoses") == []


def test_patient_assumption_without_reasoning_blocks_and_clears() -> None:
    fact = _fact(
        section="prior_diagnoses",
        kind="diagnosis",
        statement="Patient suspects they have lupus.",
        attribution="patient_assumption",
        fields={},
        precision="unknown_after_probe",
    )
    blockers = section_completion_blockers([fact], "prior_diagnoses")
    assert any("reasoning" in b for b in blockers)

    resolved = fact.model_copy(update={"fields": {"reasoning": "joint pain and fatigue"}})
    assert section_completion_blockers([resolved], "prior_diagnoses") == []


def test_event_with_precision_unasked_blocks_and_unknown_after_probe_passes() -> None:
    unasked = _fact(
        section="events",
        kind="event",
        statement="ER visit for chest pain.",
        precision="unasked",
    )
    blockers = section_completion_blockers([unasked], "events")
    assert any("timing never asked" in b for b in blockers)

    asked_but_unknown = unasked.model_copy(update={"precision": "unknown_after_probe"})
    assert section_completion_blockers([asked_but_unknown], "events") == []

    exact = unasked.model_copy(update={"precision": "exact", "date_approx": "2019-03-02"})
    assert section_completion_blockers([exact], "events") == []


def test_retracted_facts_never_block() -> None:
    fact = _fact(
        section="events",
        kind="event",
        statement="Old event.",
        precision="unasked",
        status="retracted",
    )
    assert section_completion_blockers([fact], "events") == []


def test_facts_in_other_sections_never_block() -> None:
    fact = _fact(
        section="events",
        kind="event",
        statement="ER visit.",
        precision="unasked",
    )
    assert section_completion_blockers([fact], "allergies") == []


def test_no_facts_means_no_blockers() -> None:
    assert section_completion_blockers([], "events") == []


def test_add_fact_accepts_the_flat_shape_the_model_sometimes_emits() -> None:
    """Observed live: the model wrote an `add_fact` op's fields flat beside
    `op` instead of nested under `fact`, which failed structured-output
    validation and cost the patient a whole message of family history.
    The nested form is still what the prompt asks for; this only lifts an
    unambiguous flat payload rather than losing the turn."""
    flat = {
        "op": "add_fact",
        "id": "sister-hypertension",
        "section": "family_history",
        "kind": "relative",
        "statement": "Half-sister (same father) had high blood pressure; died at 26.",
        "fields": {"relation": "half-sister", "age_at_death": 26},
    }
    op = AddFact.model_validate(flat)
    assert op.fact.id == "sister-hypertension"
    assert op.fact.section == "family_history"
    assert op.fact.fields["age_at_death"] == 26


def test_add_fact_nested_shape_is_unchanged() -> None:
    nested = {
        "op": "add_fact",
        "fact": {
            "id": "f-1",
            "section": "family_history",
            "kind": "relative",
            "statement": "Mother died when the patient was 11.",
        },
    }
    assert AddFact.model_validate(nested).fact.id == "f-1"


def test_add_fact_still_rejects_a_payload_that_is_malformed_after_lifting() -> None:
    """Lifting must not turn a genuinely broken op into a silently accepted
    one — an unknown section is still a validation error."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        AddFact.model_validate(
            {
                "op": "add_fact",
                "id": "x",
                "section": "not-a-topic",
                "kind": "note",
                "statement": "s",
            }
        )

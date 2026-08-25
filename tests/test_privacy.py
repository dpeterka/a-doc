"""Tests for adoc.privacy: PatientIdentifiers loading and Scrubber edge cases."""

from __future__ import annotations

from pathlib import Path

from adoc.privacy import (
    PatientIdentifiers,
    Scrubber,
    add_identifier,
    remove_identifier,
    scaffold_identifiers_file,
)


def _identifiers(**kwargs: object) -> PatientIdentifiers:
    return PatientIdentifiers.model_validate(kwargs)


def test_name_inside_a_word_is_not_matched() -> None:
    scrubber = Scrubber(_identifiers(names=["Ann"]))

    text, count = scrubber.scrub("Her Anniversary is in June.")

    assert count == 0
    assert text == "Her Anniversary is in June."


def test_name_as_a_whole_word_is_matched_case_insensitively() -> None:
    scrubber = Scrubber(_identifiers(names=["Jane Doe"]))

    text, count = scrubber.scrub("jane doe called about her results.")

    assert count == 1
    assert text == "[NAME] called about her results."


def test_dob_and_mrn_are_scrubbed() -> None:
    scrubber = Scrubber(_identifiers(dob="1980-05-12", mrn=["A123456"]))

    text, count = scrubber.scrub("DOB 1980-05-12, MRN A123456 on file.")

    assert count == 2
    assert "[DOB]" in text
    assert "[MRN]" in text
    assert "1980-05-12" not in text
    assert "A123456" not in text


def test_address_fragment_is_scrubbed() -> None:
    scrubber = Scrubber(_identifiers(address_fragments=["742 Evergreen Terrace"]))

    text, count = scrubber.scrub("Lives at 742 Evergreen Terrace, apt 2.")

    assert count == 1
    assert text == "Lives at [ADDRESS], apt 2."


def test_explicit_phone_and_email_are_scrubbed() -> None:
    scrubber = Scrubber(_identifiers(phone=["555-123-4567"], email=["pat@example.com"]))

    text, count = scrubber.scrub("Call 555-123-4567 or email pat@example.com.")

    assert count == 2
    assert "[PHONE]" in text
    assert "[EMAIL]" in text


def test_regex_classes_apply_even_with_no_identifiers_configured() -> None:
    scrubber = Scrubber()  # no PatientIdentifiers at all

    text, count = scrubber.scrub(
        "SSN 123-45-6789, phone (555) 867-5309, email a@b.com, MRN 9988776."
    )

    assert count == 4
    assert "[SSN]" in text
    assert "[PHONE]" in text
    assert "[EMAIL]" in text
    assert "[MRN]" in text


def test_clinical_values_are_never_scrubbed() -> None:
    scrubber = Scrubber(_identifiers(names=["Al"]))

    clinical_text = (
        "ANA titer 1:640 homogeneous. BP 120/80. CRP 3-5 reference range. "
        "Alkaline phosphatase 98 U/L. Temp 98.6F."
    )
    text, count = scrubber.scrub(clinical_text)

    assert count == 0
    assert text == clinical_text


def test_noop_scrubber_never_modifies_text() -> None:
    scrubber = Scrubber.noop()

    text, count = scrubber.scrub("SSN 123-45-6789 for Jane Doe.")

    assert count == 0
    assert text == "SSN 123-45-6789 for Jane Doe."


def test_load_returns_empty_identifiers_when_path_is_none_or_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"

    assert PatientIdentifiers.load(None) == PatientIdentifiers.empty()
    assert PatientIdentifiers.load(missing) == PatientIdentifiers.empty()


def test_load_reads_identifiers_from_a_data_repo_file(tmp_path: Path) -> None:
    identifiers_file = tmp_path / "identifiers.yaml"
    identifiers_file.write_text(
        "names: ['Jane Doe']\ndob: '1980-05-12'\nmrn: ['A123456']\n",
        encoding="utf-8",
    )

    identifiers = PatientIdentifiers.load(identifiers_file)

    assert identifiers.names == ["Jane Doe"]
    assert identifiers.dob == "1980-05-12"
    assert identifiers.mrn == ["A123456"]


def test_from_file_builds_a_working_scrubber(tmp_path: Path) -> None:
    identifiers_file = tmp_path / "identifiers.yaml"
    identifiers_file.write_text("names: ['Jane Doe']\n", encoding="utf-8")

    scrubber = Scrubber.from_file(identifiers_file)
    text, count = scrubber.scrub("Jane Doe reports fatigue.")

    assert count == 1
    assert text == "[NAME] reports fatigue."


def test_longer_identifier_is_preferred_over_a_shorter_substring_identifier() -> None:
    scrubber = Scrubber(_identifiers(names=["Jane", "Jane Doe"]))

    text, count = scrubber.scrub("Jane Doe called.")

    # The full-name pattern consumes "Jane Doe" before the bare "Jane"
    # pattern gets a chance to match, so there is exactly one replacement.
    assert count == 1
    assert text == "[NAME] called."


def test_clinical_text_survives_scrubbing_intact_lab_value_analyte_and_diagnosis() -> None:
    """The important guard: over-scrubbing would silently degrade every
    diagnosis, so a lab value, an analyte name, and a diagnosis must never
    be altered by a scrubber that also has real patient identifiers loaded
    (not just the empty-identifiers case `test_clinical_values_are_never_
    scrubbed` already covers)."""
    scrubber = Scrubber(
        _identifiers(
            names=["Jane Q. Public", "Jane Public", "Janie"],
            dob="1980-05-12",
            address_fragments=["123 Main St"],
            phone=["217-555-0134"],
            email=["jane.public@example.com"],
            mrn=["A123456"],
        )
    )
    clinical_text = (
        "Assessment: findings are consistent with systemic lupus erythematosus. "
        "CRP 8.5 mg/L (ref 0.0-3.0). ANA titer 1:640 homogeneous pattern. "
        "Sodium 140 mmol/L. BP 120/80. Alkaline phosphatase 98 U/L. Temp 98.6F. "
        "Continue home monitoring; follow up in 6 weeks."
    )

    text, count = scrubber.scrub(clinical_text)

    assert count == 0
    assert text == clinical_text


# --------------------------------------------------------------------------
# Scrubber.coverage_warning — the loud-failure surface
# --------------------------------------------------------------------------


def test_coverage_warning_is_none_for_an_explicit_noop() -> None:
    assert Scrubber.noop().coverage_warning is None


def test_coverage_warning_is_none_for_a_scrubber_built_without_a_source_file() -> None:
    # Constructed directly from in-memory identifiers (e.g. tests, or a
    # future caller with no file involved) - no file to warn about.
    scrubber = Scrubber(_identifiers())
    assert scrubber.coverage_warning is None


def test_coverage_warning_names_the_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "case" / "identifiers.yaml"

    scrubber = Scrubber.from_file(missing)

    warning = scrubber.coverage_warning
    assert warning is not None
    assert str(missing) in warning
    assert "does not exist" in warning


def test_coverage_warning_fires_when_the_file_exists_but_has_no_names(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"
    path.write_text("dob: '1980-05-12'\n", encoding="utf-8")

    scrubber = Scrubber.from_file(path)

    warning = scrubber.coverage_warning
    assert warning is not None
    assert str(path) in warning
    assert "no 'names' entries" in warning


def test_coverage_warning_is_none_once_a_name_is_configured(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"
    path.write_text("names: ['Jane Doe']\n", encoding="utf-8")

    scrubber = Scrubber.from_file(path)

    assert scrubber.coverage_warning is None


# --------------------------------------------------------------------------
# case/identifiers.yaml scaffolding + CLI-facing add/remove helpers
# --------------------------------------------------------------------------


def test_scaffold_identifiers_file_creates_a_commented_template(tmp_path: Path) -> None:
    path = tmp_path / "case" / "identifiers.yaml"

    created = scaffold_identifiers_file(path)

    assert created is True
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    # Every field add_identifier/remove_identifier support is documented.
    assert "names:" in content
    assert "dob:" in content
    assert "mrn:" in content
    assert "address_fragments:" in content
    assert "phone:" in content
    assert "email:" in content
    assert "adoc identifiers add" in content
    # Loading the freshly-scaffolded template back gives empty (safe)
    # identifiers, not a parse error.
    assert PatientIdentifiers.load(path) == PatientIdentifiers.empty()


def test_scaffold_identifiers_file_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"
    path.write_text("names: ['Real Patient Name']\n", encoding="utf-8")

    created = scaffold_identifiers_file(path)

    assert created is False
    assert "Real Patient Name" in path.read_text(encoding="utf-8")


def test_add_identifier_appends_to_a_list_field_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"

    add_identifier(path, "name", "Jane Q. Public")
    add_identifier(path, "name", "Janie")
    add_identifier(path, "name", "Jane Q. Public")  # duplicate: no-op

    identifiers = PatientIdentifiers.load(path)
    assert identifiers.names == ["Jane Q. Public", "Janie"]


def test_add_identifier_dob_replaces_any_existing_value(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"

    add_identifier(path, "dob", "1980-05-12")
    add_identifier(path, "dob", "1980-05-13")

    assert PatientIdentifiers.load(path).dob == "1980-05-13"


def test_add_identifier_maps_address_field_to_address_fragments(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"

    add_identifier(path, "address", "123 Main St")

    assert PatientIdentifiers.load(path).address_fragments == ["123 Main St"]


def test_add_identifier_rejects_an_unknown_field(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"

    try:
        add_identifier(path, "ssn", "123-45-6789")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_remove_identifier_removes_a_matching_value_and_reports_it(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"
    add_identifier(path, "name", "Jane Q. Public")

    identifiers, removed = remove_identifier(path, "name", "Jane Q. Public")

    assert removed is True
    assert identifiers.names == []
    assert PatientIdentifiers.load(path).names == []


def test_remove_identifier_reports_false_for_a_non_matching_value(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"
    add_identifier(path, "name", "Jane Q. Public")

    _identifiers, removed = remove_identifier(path, "name", "Someone Else")

    assert removed is False
    assert PatientIdentifiers.load(path).names == ["Jane Q. Public"]


def test_remove_identifier_dob_clears_regardless_of_value(tmp_path: Path) -> None:
    path = tmp_path / "identifiers.yaml"
    add_identifier(path, "dob", "1980-05-12")

    identifiers, removed = remove_identifier(path, "dob", None)

    assert removed is True
    assert identifiers.dob is None


def test_add_then_remove_round_trip_changes_scrubbing_behavior(tmp_path: Path) -> None:
    """End-to-end: a name added via `add_identifier` is scrubbed by a fresh
    `Scrubber.from_file`; once removed, it is not."""
    path = tmp_path / "identifiers.yaml"
    add_identifier(path, "name", "Jane Q. Public")

    scrubbed_text, count = Scrubber.from_file(path).scrub("Jane Q. Public reports fatigue.")
    assert count == 1
    assert scrubbed_text == "[NAME] reports fatigue."

    remove_identifier(path, "name", "Jane Q. Public")

    unscrubbed_text, count2 = Scrubber.from_file(path).scrub("Jane Q. Public reports fatigue.")
    assert count2 == 0
    assert unscrubbed_text == "Jane Q. Public reports fatigue."

"""Tests for adoc.privacy: PatientIdentifiers loading and Scrubber edge cases."""

from __future__ import annotations

from pathlib import Path

from adoc.privacy import PatientIdentifiers, Scrubber


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

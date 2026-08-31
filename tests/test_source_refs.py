"""Source-ref salvage and containment.

A live Challenger turn failed with two validation errors and lost the WHOLE
verdict — every valid op in it — because one hypothesis carried two evidence
items whose source refs did not match the grammar:

    intake:symptoms (ear eczema history; worsening after Zoryve/Opzelura ...)
    patient-report:2026-09-20 (as referenced in proposed diff; not yet ...)

The second is a perfectly valid ref with commentary appended. The first is not
a ref at all. Neither should have been able to discard the rest of the payload
— ADR 0028: no single field of one item may fail a payload.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from adoc.casefile.schema import Evidence, Hypothesis, normalize_source_ref, validate_source_ref

_ANNOTATED = (
    "patient-report:2026-09-20 (as referenced in proposed diff; "
    "not yet corroborated by clinician exam)"
)
_NOT_A_REF = (
    "intake:symptoms (ear eczema history; worsening after Zoryve/Opzelura preceding urgent care)"
)


def _hypothesis(**kwargs) -> dict:
    return {
        "id": "relapsing-polychondritis",
        "name": "Relapsing polychondritis",
        "tier": "cant-miss",
        "probability": "low",
        "status": "active",
        "origin": "challenger",
        "first_proposed": date(2026, 9, 20),
        **kwargs,
    }


# -- salvage ----------------------------------------------------------------


def test_a_trailing_annotation_is_stripped_not_rejected() -> None:
    """A model that has just written a ref tends to want to explain it. The
    ref is valid; only the commentary is not, and discarding a real citation
    over punctuation is the wrong trade."""
    assert normalize_source_ref(_ANNOTATED) == "patient-report:2026-09-20"
    assert validate_source_ref(_ANNOTATED) == "patient-report:2026-09-20"


def test_salvage_does_not_invent_a_ref() -> None:
    """Stripping a parenthetical is salvage. Rewriting a scheme would be
    guesswork, and a guessed citation is worse than none."""
    assert normalize_source_ref(_NOT_A_REF) is None
    assert normalize_source_ref("intake:symptoms") is None
    assert normalize_source_ref("(just a comment)") is None
    assert normalize_source_ref("patient-report:not-a-date") is None


def test_a_filename_containing_parentheses_is_untouched() -> None:
    """Salvage runs only after a plain match has already failed, so a real
    filename with parentheses in it never gets truncated."""
    ref = "doc:Lab Report (final).pdf"

    assert normalize_source_ref(ref) == ref


def test_surrounding_whitespace_is_tolerated() -> None:
    assert normalize_source_ref("  pmid:12345  ") == "pmid:12345"


# -- containment ------------------------------------------------------------


def test_one_unciteable_claim_does_not_fail_the_hypothesis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production failure. Two bad refs raised out of a nested model and
    took an entire ChallengerVerdict with them; one unciteable claim must cost
    itself and nothing else."""
    with caplog.at_level(logging.WARNING):
        hypothesis = Hypothesis.model_validate(
            _hypothesis(
                evidence_for=[
                    {"claim": "ear eczema history", "source": _NOT_A_REF, "strength": "weak"},
                    {"claim": "canal pain", "source": _ANNOTATED, "strength": "moderate"},
                    {
                        "claim": "urgent care visit",
                        "source": "encounter:2026-08-09--urgent-care.md",
                        "strength": "moderate",
                    },
                ]
            )
        )

    assert [e.source for e in hypothesis.evidence_for] == [
        "patient-report:2026-09-20",
        "encounter:2026-08-09--urgent-care.md",
    ]
    assert "unciteable source" in caplog.text


def test_a_dropped_claim_is_logged_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """A silently dropped claim is a claim nobody knows was made."""
    with caplog.at_level(logging.WARNING):
        Hypothesis.model_validate(
            _hypothesis(
                evidence_against=[{"claim": "nope", "source": "made-up:thing", "strength": "weak"}]
            )
        )

    assert "made-up:thing" in caplog.text
    assert "relapsing-polychondritis" in caplog.text


def test_evidence_against_is_filtered_too() -> None:
    hypothesis = Hypothesis.model_validate(
        _hypothesis(
            evidence_against=[
                {"claim": "bad", "source": "intake:whatever", "strength": "weak"},
                {"claim": "good", "source": "pmid:12345", "strength": "strong"},
            ]
        )
    )

    assert [e.source for e in hypothesis.evidence_against] == ["pmid:12345"]


def test_a_hypothesis_with_only_bad_evidence_still_builds() -> None:
    """It survives with no evidence; the ledger invariants — not this filter —
    decide whether it may stand."""
    hypothesis = Hypothesis.model_validate(
        _hypothesis(evidence_for=[{"claim": "x", "source": "intake:y", "strength": "weak"}])
    )

    assert hypothesis.evidence_for == []


def test_direct_evidence_construction_still_rejects_a_bad_ref() -> None:
    """The filter relaxes the HYPOTHESIS, not the grammar. Building an
    `Evidence` with an unsalvageable source is still an error — code that
    constructs one directly has no excuse."""
    with pytest.raises(ValueError, match="invalid source ref"):
        Evidence(claim="x", source="intake:y", strength="weak")


def test_good_evidence_is_untouched() -> None:
    hypothesis = Hypothesis.model_validate(
        _hypothesis(
            evidence_for=[
                {"claim": "a", "source": "labs:ana-titer:2026-07-15", "strength": "strong"},
                {"claim": "b", "source": "doc:report.pdf#p3", "strength": "moderate"},
            ]
        )
    )

    assert len(hypothesis.evidence_for) == 2


# --- the `engine:` scheme (phenotype-engine verdicts) ------------------------


@pytest.mark.parametrize(
    "ref",
    ["engine:lirical:2026-08-31", "engine:semsim:2026-01-01"],
)
def test_an_engine_ref_is_citable(ref: str) -> None:
    """A hypothesis that exists BECAUSE an engine ranked it has to be able to
    say so. Before this scheme there was nowhere for that evidence to point:
    `doc:` and `encounter:` describe files that do not exist for a
    computation, and a `pmid:` for the engine's method would attribute a claim
    about this patient to a paper that never saw her."""
    assert validate_source_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "engine:liricl:2026-08-31",  # typo'd engine name
        "engine:monarch:2026-08-31",  # an engine this system does not run
        "engine:lirical:2026-8-31",  # not an ISO date
        "engine:lirical",  # no date at all
    ],
)
def test_an_unknown_engine_or_bad_date_is_refused(ref: str) -> None:
    """Deliberately a CLOSED set, unlike every other slug in the grammar.

    The other schemes name things the patient's record already contains, so
    they must accept whatever is on file. An engine ref names a component of
    this system, and the list of engines is known at build time — a typo
    should be a validation error, not a citation that resolves to nothing.
    """
    with pytest.raises(ValueError, match="unciteable source|source ref"):
        validate_source_ref(ref)

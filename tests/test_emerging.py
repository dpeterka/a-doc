"""Tests for `casefile.emerging` — ADR 0050.

A new finding is tracked before it becomes a lead. The two literatures only
reconcile if it stays visible: watchful waiting lowers testing "without
missing serious pathology", while premature closure is most common exactly
when a new finding is folded into the existing story.
"""

from __future__ import annotations

from datetime import date

from adoc.casefile.emerging import (
    EMERGING_WINDOW_DAYS,
    MAX_SOURCES_TO_STAY_EMERGING,
    is_emerging,
    split_emerging,
)
from adoc.casefile.schema import Evidence, Hypothesis

TODAY = date(2026, 9, 4)


def _hyp(
    hid: str,
    *,
    days_ago: list[int],
    tier: str = "expanded",
    origin: str = "challenger",
) -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=f"Lead {hid}",
        tier=tier,  # type: ignore[arg-type]
        probability="low",
        status="active",
        origin=origin,  # type: ignore[arg-type]
        first_proposed=date(2026, 8, 1),
        evidence_for=[
            Evidence(
                claim=f"finding {i}",
                source=f"labs:analyte-{i}:{date.fromordinal(TODAY.toordinal() - d).isoformat()}",
                strength="moderate",
            )
            for i, d in enumerate(days_ago)
        ],
    )


def test_a_lead_resting_only_on_recent_findings_is_emerging() -> None:
    """The itchy-ear case: two weeks of history, three hypotheses, competing
    with leads built on years of serology."""
    assert is_emerging(_hyp("new", days_ago=[14, 10]), today=TODAY) is True


def test_one_old_citation_is_enough_to_make_it_a_lead() -> None:
    """The criterion is the OLDEST evidence, not the newest. A finding with
    any history behind it is not new, however recently it was last seen."""
    assert is_emerging(_hyp("has-history", days_ago=[400, 10]), today=TODAY) is False


def test_a_cant_miss_lead_is_never_emerging() -> None:
    """Deferring a dangerous possibility is the premature-closure failure the
    literature names, and the safety checklist (ADR 0039) exists for exactly
    that case. A new symptom that might be something serious is the LAST
    thing to set aside."""
    assert is_emerging(_hyp("cm", days_ago=[3], tier="cant-miss"), today=TODAY) is False


def test_a_patient_raised_lead_is_never_emerging() -> None:
    """Her own theories are not the system's to defer."""
    assert is_emerging(_hyp("hers", days_ago=[3], origin="patient"), today=TODAY) is False


def test_a_lead_with_no_dated_evidence_is_not_emerging() -> None:
    """Unknown age is not the same as new. Defaulting the other way would
    quietly defer the OLDEST findings in the record — the exact inversion of
    the intent."""
    hypothesis = _hyp("undated", days_ago=[])
    hypothesis.evidence_for = [
        Evidence(claim="something", source="encounter:notes.md", strength="moderate")
    ]

    assert is_emerging(hypothesis, today=TODAY) is False


def test_corroboration_promotes_it_regardless_of_age() -> None:
    """ADR 0050's second promotion route. A lead cited by several independent
    sources inside a fortnight is not a passing mention — the system has
    looked at it from more than one direction and it held up."""
    thin = _hyp("thin", days_ago=[5, 3])
    corroborated = _hyp("corroborated", days_ago=[5, 4, 3, 2])

    assert is_emerging(thin, today=TODAY) is True
    assert len({e.source for e in corroborated.evidence_for}) > MAX_SOURCES_TO_STAY_EMERGING
    assert is_emerging(corroborated, today=TODAY) is False


def test_duration_promotes_it_as_the_window_passes() -> None:
    """Nothing is written and nothing is edited: the same hypothesis
    reclassifies itself as the dates move."""
    hypothesis = _hyp("aging", days_ago=[EMERGING_WINDOW_DAYS - 1])

    assert is_emerging(hypothesis, today=TODAY) is True
    later = date.fromordinal(TODAY.toordinal() + 2)
    assert is_emerging(hypothesis, today=later) is False


def test_the_window_is_configurable() -> None:
    """3 months is the NCHS convention, not a universal: chronic cough is 8
    weeks and the CDC uses a year."""
    hypothesis = _hyp("h", days_ago=[45])

    assert is_emerging(hypothesis, today=TODAY, window_days=90) is True
    assert is_emerging(hypothesis, today=TODAY, window_days=30) is False


def test_split_preserves_the_callers_order() -> None:
    """An existing sort must not be silently re-ranked."""
    leads = [_hyp("a", days_ago=[400]), _hyp("new", days_ago=[3]), _hyp("b", days_ago=[500])]

    differential, emerging = split_emerging(leads, today=TODAY)

    assert [h.id for h in differential] == ["a", "b"]
    assert [h.id for h in emerging] == ["new"]

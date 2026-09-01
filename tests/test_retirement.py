"""Retiring hypotheses (ADR 0035).

Measured on the live ledger at version 12: 50 hypotheses, every one `active`,
none ever retired across twelve versions. `ruled-out` appeared in no prompt
and no logic — reachable in the type system, unreachable in practice.

The two exclusions are the reason this can be automatic at all, and they are
pinned first.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from adoc.casefile.retirement import (
    STALE_DAYS,
    RetirementReport,
    is_protected,
    propose_retirements,
    render_retirements,
)
from adoc.casefile.schema import Evidence, Hypothesis, Ledger

_TODAY = date(2026, 8, 30)


def _ev(claim: str = "c", *, strength: str = "moderate") -> Evidence:
    return Evidence(claim=claim, source="pmid:12345", strength=strength)  # type: ignore[arg-type]


def _h(
    hid: str,
    *,
    tier: str = "expanded",
    probability: str = "low",
    origin: str = "model",
    evidence_for: list | None = None,
    evidence_against: list | None = None,
    first_proposed: date = _TODAY,
    last_challenged: date | None = None,
) -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=hid.replace("-", " ").title(),
        tier=tier,  # type: ignore[arg-type]
        probability=probability,  # type: ignore[arg-type]
        status="active",
        origin=origin,  # type: ignore[arg-type]
        first_proposed=first_proposed,
        last_challenged=last_challenged,
        evidence_for=evidence_for or [],
        evidence_against=evidence_against or [],
    )


def _ledger(*hypotheses: Hypothesis) -> Ledger:
    return Ledger(
        version=12, updated=datetime(2026, 8, 30, tzinfo=UTC), hypotheses=list(hypotheses)
    )


# -- the exclusions ---------------------------------------------------------


def test_a_cant_miss_hypothesis_is_never_retired() -> None:
    """The entire point of that tier is that the cost of missing one is
    catastrophic and asymmetric. A rule that could silently drop a pulmonary
    embolism to tidy a list is not a rule worth having."""
    report = propose_retirements(_ledger(_h("pulmonary-embolism", tier="cant-miss")), today=_TODAY)

    assert report.retirements == []
    assert report.protected_count == 1


def test_a_patient_raised_hypothesis_is_never_retired() -> None:
    """ADR 0032 makes patient-reported material first-class. Her theory is
    hers to withdraw; the machine binning it quietly is the wrong
    behaviour."""
    report = propose_retirements(_ledger(_h("her-own-theory", origin="patient")), today=_TODAY)

    assert report.retirements == []
    assert report.protected_count == 1


def test_protection_holds_even_with_no_evidence_at_all() -> None:
    """The exclusions are absolute, not a tiebreak."""
    assert is_protected(_h("x", tier="cant-miss"))
    assert is_protected(_h("x", origin="patient"))
    assert not is_protected(_h("x"))


# -- the rules --------------------------------------------------------------


def test_a_hypothesis_nothing_supports_is_set_aside() -> None:
    """Not a judgement about the disease — a judgement about the entry. A
    differential is a set of claims about THIS patient, and a claim with no
    cited support is speculation that was never withdrawn. Eight of fifty on
    the live ledger."""
    report = propose_retirements(_ledger(_h("unsupported")), today=_TODAY)

    assert report.count == 1
    assert report.retirements[0].to_status == "parked"
    assert "nothing on file supports this" in report.retirements[0].reason


def test_counter_evidence_outweighing_support_rules_it_out() -> None:
    report = propose_retirements(
        _ledger(_h("outweighed", evidence_for=[_ev()], evidence_against=[_ev(), _ev()])),
        today=_TODAY,
    )

    assert report.retirements[0].to_status == "ruled-out"


def test_strong_evidence_counts_double() -> None:
    """Three weak observations do not outweigh one strong contradicting
    result; treating them as equal would let volume beat quality."""
    survives = _h(
        "survives",
        evidence_for=[_ev(strength="strong")],
        evidence_against=[_ev(strength="weak")],
    )
    falls = _h(
        "falls",
        evidence_for=[_ev(strength="weak")],
        evidence_against=[_ev(strength="strong")],
    )

    report = propose_retirements(_ledger(survives, falls), today=_TODAY)

    assert [r.hypothesis_id for r in report.retirements] == ["falls"]


def test_a_stale_low_value_lead_is_set_aside() -> None:
    old = _TODAY.replace(year=_TODAY.year - 1)
    report = propose_retirements(
        _ledger(_h("forgotten", evidence_for=[_ev()], first_proposed=old)), today=_TODAY
    )

    assert report.count == 1
    assert "untouched" in report.retirements[0].reason


def test_a_high_probability_lead_is_never_retired_for_being_stale() -> None:
    """A high or moderate hypothesis that has gone quiet is one nobody has
    tested. That is work for the test-chooser, not grounds for dropping it."""
    old = _TODAY.replace(year=_TODAY.year - 1)
    report = propose_retirements(
        _ledger(_h("untested", probability="high", evidence_for=[_ev()], first_proposed=old)),
        today=_TODAY,
    )

    assert report.retirements == []


def test_a_recently_challenged_lead_is_not_stale() -> None:
    """`last_challenged` is the freshness clock; something looked at last week
    is not forgotten however old it is."""
    old = _TODAY.replace(year=_TODAY.year - 1)
    report = propose_retirements(
        _ledger(
            _h(
                "revisited",
                evidence_for=[_ev()],
                first_proposed=old,
                last_challenged=_TODAY,
            )
        ),
        today=_TODAY,
    )

    assert report.retirements == []


def test_one_hypothesis_gets_one_reason() -> None:
    """Rules are tried in order and the first match wins. "Nothing supports
    this" reads better than three overlapping verdicts."""
    old = _TODAY.replace(year=_TODAY.year - 1)
    report = propose_retirements(
        _ledger(_h("everything-wrong", first_proposed=old, evidence_against=[_ev()])),
        today=_TODAY,
    )

    assert report.count == 1
    assert "nothing on file supports this" in report.retirements[0].reason


def test_an_already_retired_hypothesis_is_not_reconsidered() -> None:
    retired = _h("done").model_copy(update={"status": "ruled-out"})

    assert propose_retirements(_ledger(retired), today=_TODAY).count == 0


def test_a_fresh_hypothesis_with_support_survives() -> None:
    """The common case must be left alone. Every eligible hypothesis on the
    live ledger was one day old."""
    report = propose_retirements(_ledger(_h("new", evidence_for=[_ev()])), today=_TODAY)

    assert report.retirements == []


def test_stale_days_is_configurable_not_hardcoded_at_the_call_site() -> None:
    old = _TODAY - timedelta(days=STALE_DAYS + 1)
    aged = _ledger(_h("x", evidence_for=[_ev()], first_proposed=old))
    assert propose_retirements(aged, today=_TODAY).count == 1
    assert (
        propose_retirements(
            _ledger(_h("x", evidence_for=[_ev()], first_proposed=old)),
            today=_TODAY,
            stale_days=10_000,
        ).count
        == 0
    )


# -- rendering --------------------------------------------------------------


def test_the_report_says_what_was_left_alone() -> None:
    """Listing only what was cut would imply everything else was assessed."""
    report = propose_retirements(
        _ledger(_h("unsupported"), _h("pe", tier="cant-miss"), _h("hers", origin="patient")),
        today=_TODAY,
    )

    text = "\n".join(render_retirements(report))

    assert "Unsupported" in text
    assert "2 were not assessed" in text
    assert "can't-miss" in text


def test_the_report_says_nothing_is_deleted() -> None:
    """A retirement is a status change, reversible by the next review that
    finds new support."""
    report = propose_retirements(_ledger(_h("unsupported")), today=_TODAY)

    assert "Nothing is deleted" in "\n".join(render_retirements(report))


def test_an_empty_pass_renders_plainly() -> None:
    assert "Nothing was retired" in "\n".join(
        render_retirements(propose_retirements(_ledger(), today=_TODAY))
    )


def test_a_failed_apply_reads_as_a_failure_not_a_quiet_week() -> None:
    """`RetirementReport.error` is set ONLY when a retirement was proposed
    but the write to disk failed — the report used to render "nothing was
    retired" identically whether nothing was ever proposed or a proposal
    existed and the apply genuinely failed (a lock, an IO error), reading a
    real operational failure as an unremarkable clean week."""
    failed = RetirementReport(protected_count=1, error="OSError: disk full")

    text = "\n".join(render_retirements(failed))

    assert "could not be saved" in text
    assert "disk full" in text
    assert "Nothing was retired" not in text

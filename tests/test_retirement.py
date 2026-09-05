"""Retiring hypotheses (ADR 0035).

Measured on the live ledger at version 12: 50 hypotheses, every one `active`,
none ever retired across twelve versions. `ruled-out` appeared in no prompt
and no logic — reachable in the type system, unreachable in practice.

The two exclusions are the reason this can be automatic at all, and they are
pinned first.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from adoc.casefile.retirement import (
    STALE_DAYS,
    LabFact,
    RetirementReport,
    evaluate_rule_out,
    is_protected,
    propose_retirements,
    render_retirements,
)
from adoc.casefile.schema import Evidence, Hypothesis, Ledger, RuleOutCheck

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


# -- ADR 0038: how a hypothesis ends ----------------------------------------


def _lab(**kw: object) -> LabFact:
    return LabFact(**kw)  # type: ignore[arg-type]


def _excluded(source: str, claim: str = "Serum metanephrines normal") -> Evidence:
    return Evidence(claim=claim, source=source, strength="definitive-exclusion")


def test_a_definitive_exclusion_ends_a_cant_miss_lead() -> None:
    """The narrowing ADR 0038 makes to ADR 0035's absolute protection.

    Pheochromocytoma is a can't-miss lead AND the textbook case of a
    diagnosis one negative test excludes. A protection that cannot tell those
    apart guarantees the bloat it was meant to be worth paying for: 10
    can't-miss leads in production, none ever retired.
    """
    hypothesis = _h("pheochromocytoma", tier="cant-miss")
    hypothesis.evidence_against = [_excluded("labs:metanephrines:2026-08-01")]

    report = propose_retirements(_ledger(hypothesis), today=_TODAY)

    assert [r.to_status for r in report.retirements] == ["ruled-out"]
    assert report.protected_count == 0


def test_a_definitive_exclusion_ends_a_patient_origin_lead() -> None:
    """Same narrowing, other protected class. Her own theory is still hers —
    but an objective result settles it the same way it settles any other."""
    hypothesis = _h("her-theory", origin="patient")
    hypothesis.evidence_against = [_excluded("encounter:2026-08-01--rheum.md")]

    report = propose_retirements(_ledger(hypothesis), today=_TODAY)

    assert [r.hypothesis_id for r in report.retirements] == ["her-theory"]


@pytest.mark.parametrize("source", ["pmid:12345", "engine:lirical:2026-08-31"])
def test_a_definitive_exclusion_from_a_refused_source_ends_nothing(source: str) -> None:
    """The restriction IS the safety property.

    The Challenger writes `evidence_against`; without this it would have a
    one-word route to retiring a can't-miss lead. Literature knows nothing
    about this patient, and a phenotype engine that never ranked something has
    not refuted it — ADR 0036's entire `neutral` argument.
    """
    hypothesis = _h("aortic-dissection", tier="cant-miss")
    hypothesis.evidence_against = [_excluded(source, claim="not ranked")]

    report = propose_retirements(_ledger(hypothesis), today=_TODAY)

    assert report.retirements == []
    assert report.protected_count == 1
    assert report.refused_exclusions, "a refused exclusion must be reported, not swallowed"


def test_a_refused_exclusion_is_visible_in_the_report() -> None:
    """A model reaching for the one strength that bypasses the balance scale
    is worth seeing."""
    hypothesis = _h("aortic-dissection", tier="cant-miss")
    hypothesis.evidence_against = [_excluded("pmid:12345", claim="a paper says otherwise")]

    text = "\n".join(render_retirements(propose_retirements(_ledger(hypothesis), today=_TODAY)))

    assert "cannot settle it" in text
    assert "a paper says otherwise" in text


def test_a_met_rule_out_ends_the_hypothesis_that_stated_it() -> None:
    """`rule_out` was required at creation and never read again: 46 active
    hypotheses in production, 0 rule-outs ever evaluated. This is the stage
    that checks."""
    hypothesis = _h("pulmonary-embolism", tier="cant-miss")
    hypothesis.rule_out = "a normal d-dimer"
    hypothesis.rule_out_check = RuleOutCheck(analyte="ddimer", operator="normal")

    report = propose_retirements(
        _ledger(hypothesis), today=_TODAY, labs={"ddimer": _lab(value=0.3, unit="mg/L")}
    )

    assert [r.to_status for r in report.retirements] == ["ruled-out"]
    assert "rule-out condition is now met" in report.retirements[0].reason


def test_an_unmeasured_analyte_never_ends_a_hypothesis() -> None:
    """Cannot-tell is not met. Absence of a test is the ordinary state of
    every differential and reads nothing like a negative result; conflating
    them is the one failure this evaluator must not have."""
    hypothesis = _h("pulmonary-embolism", tier="cant-miss")
    hypothesis.rule_out_check = RuleOutCheck(analyte="ddimer", operator="normal")

    report = propose_retirements(_ledger(hypothesis), today=_TODAY, labs={})

    assert report.retirements == []
    assert report.protected_count == 1


def test_a_flagged_result_does_not_satisfy_a_normal_rule_out() -> None:
    # Given supporting evidence so `_no_supporting_evidence` cannot fire and
    # retire it for an unrelated reason — this test is about the rule-out.
    hypothesis = _h("pulmonary-embolism", evidence_for=[_ev()])
    hypothesis.rule_out_check = RuleOutCheck(analyte="ddimer", operator="normal")

    report = propose_retirements(
        _ledger(hypothesis), today=_TODAY, labs={"ddimer": _lab(value=4.0, flag="H")}
    )

    assert report.retirements == []


def test_negation_is_read_before_the_positive_substring() -> None:
    """ "Not detected" contains "detected". The substring order is the whole
    correctness of the qualitative operator."""
    met, _ = evaluate_rule_out(
        RuleOutCheck(analyte="ana", operator="negative"), {"ana": _lab(value_text="Not Detected")}
    )
    positive, _ = evaluate_rule_out(
        RuleOutCheck(analyte="ana", operator="negative"), {"ana": _lab(value_text="Detected")}
    )

    assert met is True
    assert positive is False


def test_a_threshold_is_never_compared_across_units() -> None:
    """Eosinophils are stored both as `4.5 %` and `320 cells/uL`. The same
    analyte, incomparable numbers — the bug
    `knowledge.criteria._count_threshold_item` exists to prevent."""
    met, why = evaluate_rule_out(
        RuleOutCheck(analyte="eos", operator="below", threshold=1000.0, unit="cells/uL"),
        {"eos": _lab(value=4.5, unit="%")},
    )

    assert met is False
    assert "not the" in why


def test_an_unevaluatable_rule_out_leaves_the_old_rules_in_charge() -> None:
    """A rule-out no lab can settle — "a negative cartilage biopsy" — has no
    check to write. Absent means never evaluated automatically, which is the
    prior behaviour for every hypothesis on the ledger."""
    hypothesis = _h("relapsing-polychondritis", tier="cant-miss")
    hypothesis.rule_out = "a negative cartilage biopsy"

    report = propose_retirements(_ledger(hypothesis), today=_TODAY)

    assert report.retirements == []
    assert report.protected_count == 1


# --- the analyte-normalisation bug ---------------------------------------------------------------


def test_a_multi_word_analyte_resolves_against_a_normalized_lookup() -> None:
    """Measured on the real case file: of 16 machine-checkable rule-outs
    proposed against 461 stored analytes, **15 answered "no result on file"
    and 1 matched** — the one whose analyte was `ferritin`.

    `review.build_lab_lookup` keys on the normalized name, so `Vitamin B12`
    is stored under `vitaminb12`; `evaluate_rule_out` looked it up RAW. Only
    a single lowercase word could ever match.

    Same shape as `criteria._RA_RF` — a check that cannot fire looks exactly
    like a check that fires and finds nothing. Here it would have made ADR
    0038's evaluator and ADR 0047's writer both correct in isolation and
    jointly useless.
    """
    from adoc.casefile.retirement import LabFact, evaluate_rule_out
    from adoc.casefile.schema import RuleOutCheck

    # Keyed the way `build_lab_lookup` keys it.
    labs = {
        "vitaminb12": LabFact(
            value=410.0, value_text="", flag="", unit="pg/mL", ref="labs:vitamin-b12:2026-05-02"
        )
    }

    met, why = evaluate_rule_out(RuleOutCheck(analyte="Vitamin B12", operator="normal"), labs)

    assert met is True, why


def test_every_spelling_of_one_analyte_resolves_to_the_same_row() -> None:
    from adoc.casefile.retirement import LabFact, evaluate_rule_out
    from adoc.casefile.schema import RuleOutCheck

    labs = {
        "complementc3": LabFact(
            value=110.0,
            value_text="",
            flag="",
            unit="mg/dL",
            ref="labs:complement-c3:2026-05-02",
        )
    }

    for spelling in ("Complement C3", "complement c3", "complementc3", "Complement-C3"):
        met, why = evaluate_rule_out(RuleOutCheck(analyte=spelling, operator="normal"), labs)
        assert met is True, f"{spelling!r}: {why}"


def test_an_analyte_genuinely_absent_is_still_not_met() -> None:
    """The property that must survive the fix. An analyte nobody has
    measured must never end a hypothesis — absence of a test reads nothing
    like a negative result, and conflating them is the one failure this
    evaluator must not have."""
    from adoc.casefile.retirement import LabFact, evaluate_rule_out
    from adoc.casefile.schema import RuleOutCheck

    labs = {
        "ferritin": LabFact(
            value=100.0, value_text="", flag="", unit="ng/mL", ref="labs:ferritin:2026-05-02"
        )
    }

    met, why = evaluate_rule_out(
        RuleOutCheck(analyte="Serum Metanephrines", operator="normal"), labs
    )

    assert met is False
    assert "no Serum Metanephrines result on file" in why


# --- the tier cap -----------------------------------------------------------------------------


def _cap_hyp(
    hid: str,
    *,
    tier: str = "expanded",
    probability: str = "low",
    evidence: int = 1,
    strong: bool = False,
    origin: str = "challenger",
    status: str = "active",
) -> Hypothesis:
    from adoc.casefile.schema import Evidence

    return Hypothesis(
        id=hid,
        name=f"Lead {hid}",
        tier=tier,  # type: ignore[arg-type]
        probability=probability,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
        first_proposed=date(2026, 8, 1),
        evidence_for=[
            Evidence(
                claim=f"finding {i}",
                source="labs:crp:2026-05-02",
                strength="strong" if strong else "moderate",
            )
            for i in range(evidence)
        ],
    )


def _cap_ledger(*hyps: Hypothesis) -> Ledger:
    return Ledger(version=1, updated=date(2026, 9, 4), hypotheses=list(hyps))


def test_a_tier_under_its_cap_folds_nothing() -> None:
    from adoc.casefile.retirement import propose_tier_folds

    ledger = _cap_ledger(*[_cap_hyp(f"h{i}") for i in range(3)])

    assert propose_tier_folds(ledger, today=date(2026, 9, 4), caps={"expanded": 12}) == []


def test_the_weakest_fold_first_and_are_parked_not_ruled_out() -> None:
    """Nothing is deleted or refuted. A parked lead keeps its evidence and
    returns the moment it earns its way back."""
    from adoc.casefile.retirement import propose_tier_folds

    ledger = _cap_ledger(
        _cap_hyp("strong-lead", evidence=4, strong=True, probability="high"),
        _cap_hyp("weak-lead", evidence=1, probability="minimal"),
    )

    folds = propose_tier_folds(ledger, today=date(2026, 9, 4), caps={"expanded": 1})

    assert [f.hypothesis_id for f in folds] == ["weak-lead"]
    assert folds[0].to_status == "parked"
    assert "not ruled out" in folds[0].reason


def test_an_uncited_lead_folds_before_any_cited_one() -> None:
    """A lead with no evidence at all is a placeholder, not a lead.

    Achieved by evidence weight rather than a separate key: every evidence
    item contributes at least 1, so an uncited lead scores 0 and is the
    unique minimum. `_fold_rank` briefly carried an explicit uncited-first
    key and it was removed as decoration — a negative control showed no
    input could make the two keys disagree.
    """
    from adoc.casefile.retirement import propose_tier_folds

    ledger = _cap_ledger(
        _cap_hyp("uncited", evidence=0, probability="high"),
        _cap_hyp("cited-but-minimal", evidence=1, probability="minimal"),
    )

    folds = propose_tier_folds(ledger, today=date(2026, 9, 4), caps={"expanded": 1})

    assert [f.hypothesis_id for f in folds] == ["uncited"]


def test_evidence_weight_outranks_the_models_own_probability() -> None:
    """Probability is model-assigned and is deliberately the LAST
    substantive key. A lead the model called `high` on one weak citation
    folds before one it called `low` with four strong ones."""
    from adoc.casefile.retirement import propose_tier_folds

    ledger = _cap_ledger(
        _cap_hyp("thin-but-confident", evidence=1, probability="high"),
        _cap_hyp("well-evidenced", evidence=4, strong=True, probability="low"),
    )

    folds = propose_tier_folds(ledger, today=date(2026, 9, 4), caps={"expanded": 1})

    assert [f.hypothesis_id for f in folds] == ["thin-but-confident"]


def test_the_safety_checklist_is_never_capped() -> None:
    """`cant-miss` exists so a dangerous possibility stays visible however
    unlikely (ADR 0039). Capping it would delete the mechanism to tidy the
    page — and all 10 on the real ledger are `minimal` or `low`, so a
    probability-ordered cap would take them first."""
    from adoc.casefile.retirement import TIER_CAPS, propose_tier_folds

    ledger = _cap_ledger(
        *[_cap_hyp(f"cm{i}", tier="cant-miss", probability="minimal") for i in range(20)]
    )

    assert "cant-miss" not in TIER_CAPS
    assert propose_tier_folds(ledger, today=date(2026, 9, 4)) == []


def test_a_patient_raised_lead_is_never_folded() -> None:
    """Her own theories are exactly what a tidying rule must not quietly
    remove."""
    from adoc.casefile.retirement import propose_tier_folds

    ledger = _cap_ledger(
        _cap_hyp("hers", origin="patient", evidence=0, probability="minimal"),
        _cap_hyp("strong", evidence=4, strong=True, probability="high"),
    )

    folds = propose_tier_folds(ledger, today=date(2026, 9, 4), caps={"expanded": 1})

    assert [f.hypothesis_id for f in folds] == ["strong"]


def test_a_protected_lead_still_occupies_its_slot() -> None:
    """The cap is about how much the page shows, and a protected lead is on
    the page. Exempting it from the count would let a tier grow without
    limit by accumulating protected leads."""
    from adoc.casefile.retirement import propose_tier_folds

    ledger = _cap_ledger(
        _cap_hyp("hers", origin="patient"),
        _cap_hyp("a"),
        _cap_hyp("b"),
    )

    folds = propose_tier_folds(ledger, today=date(2026, 9, 4), caps={"expanded": 2})

    assert len(folds) == 1
    assert folds[0].hypothesis_id != "hers"


def test_a_parked_lead_does_not_count_toward_the_cap() -> None:
    """Otherwise the cap would fold the same tier repeatedly, once per
    review, until nothing was left."""
    from adoc.casefile.retirement import propose_tier_folds

    ledger = _cap_ledger(
        _cap_hyp("live-a"),
        _cap_hyp("live-b"),
        *[_cap_hyp(f"gone{i}", status="parked") for i in range(10)],
    )

    assert propose_tier_folds(ledger, today=date(2026, 9, 4), caps={"expanded": 2}) == []


def test_a_clinical_reason_beats_the_cap_for_the_same_lead() -> None:
    """A lead the rules can end should end for its own stated reason, not be
    swept away as one of the weakest in an overfull tier. A reader deserves
    the real reason when there is one."""
    from adoc.casefile.retirement import LabFact, propose_retirements
    from adoc.casefile.schema import RuleOutCheck

    endable = _cap_hyp("endable", evidence=1, probability="minimal")
    endable.rule_out_check = RuleOutCheck(analyte="ferritin", operator="normal")
    ledger = _cap_ledger(endable, *[_cap_hyp(f"h{i}", evidence=4, strong=True) for i in range(20)])
    labs = {
        "ferritin": LabFact(
            value=100.0, value_text="", flag="", unit="ng/mL", ref="labs:ferritin:2026-05-02"
        )
    }

    report = propose_retirements(ledger, today=date(2026, 9, 4), labs=labs)
    by_id = {r.hypothesis_id: r for r in report.retirements}

    assert "endable" in by_id
    assert by_id["endable"].to_status == "ruled-out"
    assert "rule-out condition is now met" in by_id["endable"].reason


def test_no_lead_is_retired_twice_in_one_pass() -> None:
    """The cap runs over the ledger as it stands, so a lead the rules just
    ended is still in the tier. It must not also be folded."""
    from adoc.casefile.retirement import LabFact, propose_retirements
    from adoc.casefile.schema import RuleOutCheck

    endable = _cap_hyp("endable", evidence=1, probability="minimal")
    endable.rule_out_check = RuleOutCheck(analyte="ferritin", operator="normal")
    ledger = _cap_ledger(endable, *[_cap_hyp(f"h{i}") for i in range(20)])
    labs = {
        "ferritin": LabFact(
            value=100.0, value_text="", flag="", unit="ng/mL", ref="labs:ferritin:2026-05-02"
        )
    }

    report = propose_retirements(ledger, today=date(2026, 9, 4), labs=labs)
    ids = [r.hypothesis_id for r in report.retirements]

    assert len(ids) == len(set(ids)), "a lead was retired twice in one pass"


def test_the_default_cap_on_the_real_distribution() -> None:
    """Pins the CONSEQUENCE of the default, not the constant.

    Reproduces the production distribution measured 2026-09-04 — 36
    expanded, 3 of them patient-raised — so a change to `TIER_CAPS` shows up
    as a change in how many leads a reader stops seeing, which is the thing
    worth being deliberate about. 20 is the owner's call, not derived.
    """
    from adoc.casefile.retirement import propose_tier_folds

    expanded = [_cap_hyp(f"h{i}", evidence=(i % 4) + 1) for i in range(33)]
    hers = [_cap_hyp(f"hers{i}", origin="patient") for i in range(3)]
    cant_miss = [_cap_hyp(f"cm{i}", tier="cant-miss", probability="minimal") for i in range(10)]
    ledger = _cap_ledger(*expanded, *hers, *cant_miss)

    folds = propose_tier_folds(ledger, today=date(2026, 9, 4))

    assert len(folds) == 16, "36 expanded at a cap of 20 should fold 16"
    assert all(f.to_status == "parked" for f in folds)
    # None of hers, and none of the safety checklist.
    folded = {f.hypothesis_id for f in folds}
    assert not folded & {h.id for h in hers}
    assert not folded & {h.id for h in cant_miss}

"""ADR 0049: a lead can end because the cause was removed.

The case this exists for is on this ledger. Selenium was high, fell steadily
toward normal across draws, and the hypothesis "selenium excess from
supplementation" was CORRECT — a functional-medicine doctor had prescribed a
supplement, the dose was too much, and she stopped taking it.

The system could not say that. `ruled-out` claims the hypothesis was false;
`parked` claims nobody is looking. So the lead sat active forever with a
falling analyte underneath it.

What these tests mostly pin is the boundary: detecting the trend is
arithmetic, naming the cause is not, and no code path may cross from one to
the other on its own.
"""

from __future__ import annotations

from datetime import date

from adoc.casefile.questions import question_id
from adoc.casefile.regimen import Regimen, RegimenEntry
from adoc.casefile.resolution import (
    MIN_SERIES_POINTS,
    ResolutionSignal,
    classify_series,
    hypotheses_citing,
    match_regimen_stop,
    propose_resolution_questions,
    render_resolution_questions,
)
from adoc.casefile.schema import Evidence, Hypothesis, Ledger

_TODAY = date(2026, 9, 8)


def _signal(
    analyte: str = "Selenium",
    *,
    direction: str = "toward-reference",
    side: str = "high",
    last_date: date = date(2026, 8, 1),
    closed: float = 0.8,
) -> ResolutionSignal:
    return ResolutionSignal(
        analyte=analyte,
        direction=direction,  # type: ignore[arg-type]
        first_date=date(2026, 1, 1),
        first_value=250.0,
        last_date=last_date,
        last_value=160.0,
        unit="ug/L",
        bound=150.0,
        side=side,  # type: ignore[arg-type]
        fraction_closed=closed,
    )


def _hypothesis(hid: str, *, status: str = "active", source: str = "labs:selenium:2026-01-01"):
    return Hypothesis(
        id=hid,
        name="Selenium excess from supplementation",
        tier="expanded",
        probability="moderate",
        status=status,  # type: ignore[arg-type]
        origin="model",
        first_proposed=date(2026, 1, 1),
        evidence_for=[Evidence(claim="Selenium 250 ug/L", source=source, strength="moderate")],
    )


def _ledger(*hypotheses: Hypothesis) -> Ledger:
    return Ledger(version=1, updated=_TODAY, hypotheses=list(hypotheses))


# --- the arithmetic half ------------------------------------------------------


def test_a_falling_high_value_is_heading_back() -> None:
    series = [(date(2026, 1, 1), 250.0), (date(2026, 4, 1), 200.0), (date(2026, 8, 1), 160.0)]

    direction, closed, bound, side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction == "toward-reference"
    assert side == "high" and bound == 150.0
    assert 0.85 < closed < 0.95


def test_a_rising_low_value_is_heading_back() -> None:
    series = [(date(2026, 1, 1), 4.0), (date(2026, 4, 1), 7.0), (date(2026, 8, 1), 9.5)]

    direction, closed, _bound, side = classify_series(series, ref_low=10.0, ref_high=None)

    assert direction == "toward-reference" and side == "low"
    assert closed > 0.9


def test_a_worsening_value_is_not_a_resolution() -> None:
    series = [(date(2026, 1, 1), 200.0), (date(2026, 4, 1), 260.0), (date(2026, 8, 1), 300.0)]

    direction, _closed, _bound, _side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction == "away-from-reference"


def test_a_small_drift_is_flat_not_a_resolution() -> None:
    """A 3% move is noise. Asking her what changed on noise trains her to
    ignore the question, which costs the mechanism its one asset."""
    series = [(date(2026, 1, 1), 250.0), (date(2026, 4, 1), 248.0), (date(2026, 8, 1), 247.0)]

    direction, _closed, _bound, _side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction == "flat"


def test_no_usable_range_is_unknown_not_flat() -> None:
    """`unknown` is not `flat`. Calling an unmeasurable analyte stable is the
    reading ADR 0051 spent a release removing from the abnormal check — a
    value nobody could judge coming back as "not abnormal"."""
    series = [(date(2026, 1, 1), 250.0), (date(2026, 4, 1), 200.0), (date(2026, 8, 1), 160.0)]

    direction, _closed, _bound, side = classify_series(series, ref_low=None, ref_high=None)

    assert direction == "unknown"
    assert side is None


def test_two_points_are_a_line_not_a_trend() -> None:
    series = [(date(2026, 1, 1), 250.0), (date(2026, 8, 1), 160.0)]
    assert len(series) < MIN_SERIES_POINTS

    direction, _closed, _bound, _side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction == "unknown"


def test_a_value_that_never_left_the_range_yields_nothing() -> None:
    series = [(date(2026, 1, 1), 100.0), (date(2026, 4, 1), 110.0), (date(2026, 8, 1), 105.0)]

    direction, _closed, _bound, side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction == "unknown" and side is None


# --- matching a candidate cause -----------------------------------------------


def test_a_stopped_supplement_matching_the_analyte_is_offered() -> None:
    regimen = Regimen(
        entries=[
            RegimenEntry(name="Selenium 200mcg", started=date(2025, 6, 1), stopped=date(2026, 5, 1))
        ]
    )

    match = match_regimen_stop(_signal(), regimen, today=_TODAY)

    assert match is not None and match.name == "Selenium 200mcg"


def test_something_still_being_taken_is_not_an_explanation() -> None:
    regimen = Regimen(entries=[RegimenEntry(name="Selenium 200mcg", started=date(2025, 6, 1))])

    assert match_regimen_stop(_signal(), regimen, today=_TODAY) is None


def test_an_unrelated_substance_is_not_offered() -> None:
    regimen = Regimen(
        entries=[
            RegimenEntry(name="Biotin 10mg", started=date(2025, 6, 1), stopped=date(2026, 5, 1))
        ]
    )

    assert match_regimen_stop(_signal(), regimen, today=_TODAY) is None


def test_the_most_recent_qualifying_stop_wins() -> None:
    """An older interval for the same substance is real history but a worse
    explanation for this particular fall."""
    regimen = Regimen(
        entries=[
            RegimenEntry(name="Selenium", started=date(2023, 1, 1), stopped=date(2023, 6, 1)),
            RegimenEntry(
                name="Selenium 200mcg", started=date(2025, 6, 1), stopped=date(2026, 5, 1)
            ),
        ]
    )

    match = match_regimen_stop(_signal(), regimen, today=_TODAY)

    assert match is not None and match.stopped == date(2026, 5, 1)


def test_an_unmatched_signal_still_earns_a_question() -> None:
    """The record not knowing why is exactly when asking her is worth most."""
    questions = propose_resolution_questions(
        [_signal()], _ledger(_hypothesis("se-01")), Regimen(), today=_TODAY
    )

    assert len(questions) == 1
    assert questions[0].candidate is None
    assert "what changed" in questions[0].question.ask.lower()


# --- what gets asked ----------------------------------------------------------


def test_the_question_names_the_candidate_when_there_is_one() -> None:
    """ "Did you stop a selenium supplement around May" is answerable; "did
    anything change" is not."""
    regimen = Regimen(
        entries=[
            RegimenEntry(name="Selenium 200mcg", started=date(2025, 6, 1), stopped=date(2026, 5, 1))
        ]
    )

    questions = propose_resolution_questions(
        [_signal()], _ledger(_hypothesis("se-01")), regimen, today=_TODAY
    )

    ask = questions[0].question.ask
    assert "Selenium 200mcg" in ask
    assert "May 2026" in ask
    assert ask.rstrip().endswith("?"), "the candidate must be offered, never asserted"


def test_the_question_is_hers_to_answer() -> None:
    questions = propose_resolution_questions(
        [_signal()], _ledger(_hypothesis("se-01")), Regimen(), today=_TODAY
    )

    assert questions[0].question.audience == "you"


def test_the_question_carries_the_lead_it_would_close() -> None:
    questions = propose_resolution_questions(
        [_signal()], _ledger(_hypothesis("se-01")), Regimen(), today=_TODAY
    )

    assert questions[0].question.hypothesis_ids == ["se-01"]


def test_an_analyte_under_no_live_lead_is_not_asked_about() -> None:
    """An analyte quietly normalising under nothing is good news, not a
    question worth her time."""
    assert propose_resolution_questions([_signal()], _ledger(), Regimen(), today=_TODAY) == []


def test_an_analyte_under_only_an_ENDED_lead_is_not_asked_about() -> None:
    ended = _hypothesis("se-01", status="ruled-out")

    assert propose_resolution_questions([_signal()], _ledger(ended), Regimen(), today=_TODAY) == []


def test_a_worsening_signal_produces_no_question() -> None:
    worse = _signal(direction="away-from-reference")

    assert (
        propose_resolution_questions(
            [worse], _ledger(_hypothesis("se-01")), Regimen(), today=_TODAY
        )
        == []
    )


def test_the_id_is_stable_so_the_next_review_does_not_ask_twice() -> None:
    """Constraint 3 of ADR 0048, inherited: one store, one id. A second id for
    the same topic re-asks something she may already have answered."""
    first = propose_resolution_questions(
        [_signal()], _ledger(_hypothesis("se-01")), Regimen(), today=_TODAY
    )
    later = propose_resolution_questions(
        [_signal()], _ledger(_hypothesis("se-01")), Regimen(), today=date(2026, 12, 1)
    )

    assert first[0].question.id == later[0].question.id
    assert first[0].question.id == question_id("What changed with your selenium")


def test_matching_is_by_cited_analyte_not_by_prose() -> None:
    """Evidence claims name the CONDITION; only the `labs:` source names the
    measurement. Matching prose would tie every lead mentioning "selenium
    excess" to every selenium row."""
    prose_only = Hypothesis(
        id="prose-01",
        name="Selenium excess",
        tier="expanded",
        probability="moderate",
        status="active",
        origin="model",
        first_proposed=date(2026, 1, 1),
        evidence_for=[
            Evidence(claim="selenium selenium selenium", source="pmid:12345", strength="moderate")
        ],
    )

    assert hypotheses_citing("Selenium", [prose_only]) == []
    assert hypotheses_citing("Selenium", [_hypothesis("se-01")]) == ["se-01"]


# --- the boundary that must not be crossed ------------------------------------


def test_nothing_here_changes_a_status() -> None:
    """The correlation proposes; her answer disposes. A stopped supplement
    preceding a falling level is temporal coincidence, and a code path that
    turned it into `resolved` would be the system deciding a hypothesis is
    over on that coincidence. ADR 0042 drew the same line for suppressed
    markers.
    """
    import inspect

    from adoc.casefile import resolution as module

    source = inspect.getsource(module)

    assert '"resolved"' not in source, (
        "this module assigns the status it is only allowed to ask for"
    )
    assert "UpdateHypothesis" not in source, "this module writes a ledger op"


def test_only_a_person_can_mark_a_lead_resolved() -> None:
    """The status has exactly one writer, and it is a form she submits."""
    import subprocess

    hits = sorted(
        path
        for path in subprocess.run(
            # The ledger op, specifically. `intake.agent` sets a
            # `clarification_status="resolved"` on an intake fact, which is a
            # different field on a different object.
            ["grep", "-rln", "--include=*.py", 'UpdateHypothesis(.*status="resolved"', "src/adoc/"],
            capture_output=True,
            text=True,
        ).stdout.split()
    )

    assert hits == ["src/adoc/web/routes/ledger.py"], f"unexpected writers of `resolved`: {hits}"


# --- what the reader sees -----------------------------------------------------


def test_the_report_shows_both_ends_of_the_move() -> None:
    questions = propose_resolution_questions(
        [_signal()], _ledger(_hypothesis("se-01")), Regimen(), today=_TODAY
    )

    rendered = "\n".join(render_resolution_questions(questions))

    assert "250" in rendered and "160" in rendered
    assert "2026-01-01" in rendered and "2026-08-01" in rendered


def test_nothing_found_renders_nothing() -> None:
    assert render_resolution_questions([]) == []


# --- every consumer of the new status has an explicit branch ------------------


def test_every_status_has_a_label() -> None:
    """ADR 0049: a status a renderer does not recognise must not silently
    disappear from a page. A label table total over the literal fails here
    instead of rendering an empty chip on a real lead."""
    from typing import get_args

    from adoc.casefile.schema import HypothesisStatus
    from adoc.web.templating import _STATUS_LABELS

    assert set(_STATUS_LABELS) == set(get_args(HypothesisStatus))


def test_a_resolved_lead_is_not_read_as_ruled_out() -> None:
    """The distinction is clinical. A doctor reading "selenium excess — ruled
    out" concludes it never happened and may re-prescribe."""
    from adoc.web.templating import safety_status, status_label

    resolved = _hypothesis("se-01", status="resolved")

    assert safety_status(resolved) == "Resolved"
    assert "ruled out" not in status_label("resolved").lower()


def test_a_resolved_lead_is_grouped_apart_from_open_leads() -> None:
    """Read among "worth discussing now" it says the opposite of what
    happened; read among the low-likelihood tail it buries a real finding."""
    from adoc.web.casefile_helpers import group_hypotheses

    open_lead = _hypothesis("open-01")
    done = _hypothesis("se-01", status="resolved")

    groups = group_hypotheses([open_lead, done])

    assert [h.id for h in groups["resolved"]] == ["se-01"]
    assert "se-01" not in [h.id for h in groups["leading"] + groups["secondary"]]
    assert "open-01" in [h.id for h in groups["leading"] + groups["secondary"]]


def test_no_hypothesis_is_dropped_by_the_grouping() -> None:
    """The failure this guards is silent: a status no branch matches vanishing
    off the page entirely."""
    from typing import get_args

    from adoc.casefile.schema import HypothesisStatus
    from adoc.web.casefile_helpers import group_hypotheses

    every = [_hypothesis(f"h-{s}", status=s) for s in get_args(HypothesisStatus)]

    groups = group_hypotheses(every)
    placed = [h.id for group in groups.values() for h in group]

    assert sorted(placed) == sorted(h.id for h in every)
    assert len(placed) == len(set(placed)), "a hypothesis appears in two groups"


def test_a_resolved_lead_is_not_retirement_eligible() -> None:
    """It is already ended. The retirement pass should skip it rather than
    reason about it — and re-retiring it would overwrite `resolved` with
    `parked`, losing the finding."""
    from adoc.casefile.retirement import propose_retirements

    done = Hypothesis(
        id="se-01",
        name="Selenium excess",
        tier="expanded",
        probability="low",
        status="resolved",
        origin="model",
        first_proposed=date(2024, 1, 1),
    )

    report = propose_retirements(_ledger(done), today=_TODAY)

    assert report.retirements == []


def test_a_resolved_lead_is_off_the_active_board() -> None:
    from adoc.casefile.ledger import ACTIVE_STATUSES

    assert "resolved" not in ACTIVE_STATUSES


def test_a_value_that_spiked_and_came_back_is_detected() -> None:
    """The shape ADR 0049 was actually written from: normal → supplement
    started → high → stopped → falling.

    Anchored on the FIRST reading, any pre-supplement draw on file made the
    whole series read `unknown` and the question was never asked. The detector
    would have missed the selenium case it exists for, whenever the record
    reached back far enough to show it starting normal — and silently, since
    "no signal" and "cannot judge" rendered the same.
    """
    series = [
        (date(2023, 1, 1), 120.0),  # before the supplement — in range
        (date(2025, 1, 1), 250.0),  # the excursion the lead was raised on
        (date(2025, 8, 1), 200.0),
        (date(2026, 8, 1), 160.0),  # coming back down
    ]

    direction, closed, bound, side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction == "toward-reference"
    assert side == "high" and bound == 150.0
    assert closed > 0.85


def test_a_low_value_that_recovered_is_detected_too() -> None:
    series = [(date(2023, 1, 1), 12.0), (date(2025, 1, 1), 4.0), (date(2026, 8, 1), 9.5)]

    direction, _closed, _bound, side = classify_series(series, ref_low=10.0, ref_high=None)

    assert direction == "toward-reference" and side == "low"


def test_a_peak_in_the_latest_draw_is_not_a_resolution() -> None:
    """The anchor is the worst reading the LAST one could have come down
    from. A value peaking today has not resolved; it is the finding."""
    series = [(date(2026, 1, 1), 160.0), (date(2026, 4, 1), 200.0), (date(2026, 8, 1), 300.0)]

    direction, _closed, _bound, _side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction != "toward-reference"


def test_a_series_that_never_left_the_range_is_still_unknown() -> None:
    """Anchoring on the peak must not turn ordinary variation inside the range
    into an excursion. The peak has to actually break a bound."""
    series = [(date(2026, 1, 1), 100.0), (date(2026, 4, 1), 148.0), (date(2026, 8, 1), 105.0)]

    direction, _closed, _bound, side = classify_series(series, ref_low=None, ref_high=150.0)

    assert direction == "unknown" and side is None

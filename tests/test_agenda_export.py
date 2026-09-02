"""Tests for `casefile.export`: the one-page appointment agenda (ADR 0041).

Three properties carry the module, and each has a negative control in the
suite below: the page fits one page, everything dropped is counted, and a
medication list can appear without opening a treatment-advice path.
"""

from __future__ import annotations

import json
from datetime import date

from adoc.casefile.export import (
    AGENDA_LINE_BUDGET,
    AGENDA_MAX_ASKS,
    AGENDA_MAX_LABS,
    AGENDA_MAX_LEADS,
    AGENDA_MAX_REGIMEN,
    DOSE_WITHHELD,
    Agenda,
    AgendaRegimenRow,
    agenda_gate_failures,
    build_agenda,
    render_agenda_markdown,
    rendered_lines,
)
from adoc.casefile.regimen import Regimen, RegimenEntry
from adoc.casefile.schema import Evidence, Hypothesis, Ledger, RuleOutCheck
from adoc.labs.models import LabResult


def _lab(name: str, **kw: object) -> LabResult:
    return LabResult(
        date=date(2026, 5, 2),
        name=name,
        name_raw=name,
        source_doc="a" * 64,
        raw_json=json.dumps({"name_raw": name}),
        **kw,  # type: ignore[arg-type]
    )


def _hyp(hid: str, *, cited: bool = True, **kw: object) -> Hypothesis:
    evidence = (
        [
            Evidence(
                claim=f"Something the record says about {hid}",
                source="labs:crp:2026-05-02",
                strength="strong",
            )
        ]
        if cited
        else []  # `Evidence.source` is schema-validated, so uncited means NO evidence
    )
    defaults: dict[str, object] = {
        "tier": "most-likely",
        "probability": "high",
        "status": "active",
        "origin": "model",
    }
    defaults.update(kw)
    return Hypothesis(
        id=hid,
        name=f"Condition {hid}",
        first_proposed=date(2026, 8, 1),
        evidence_for=evidence,
        **defaults,  # type: ignore[arg-type]
    )


def _ledger(*hypotheses: Hypothesis) -> Ledger:
    return Ledger(version=1, updated=date(2026, 9, 1), hypotheses=list(hypotheses))


def _maximal() -> Agenda:
    """More of everything than any real case file holds."""
    long_claim = "A claim that keeps going " * 12
    leads = [
        Hypothesis(
            id=f"h{i}",
            name=f"A long condition name number {i} with a parenthetical qualifier (subtype B)",
            tier="most-likely",
            probability="high",
            status="active",
            origin="model",
            first_proposed=date(2026, 8, 1),
            evidence_for=[
                Evidence(claim=long_claim, source="labs:crp:2026-05-02", strength="strong")
                for _ in range(5)
            ],
            rule_out_check=RuleOutCheck(analyte="metanephrines", operator="normal"),
        )
        for i in range(30)
    ]
    regimen = Regimen(
        entries=[
            RegimenEntry(
                name=f"A substance with a long name number {i}",
                dose="200 mg",
                frequency="twice daily",
                started=date(2025, 1, 1),
            )
            for i in range(25)
        ]
    )
    return build_agenda(
        ledger=_ledger(*leads),
        abnormal=[
            _lab(f"An analyte with a long name {i}", value=float(i), flag="H") for i in range(40)
        ],
        regimen=regimen,
        asks=["A question phrased at length for the doctor " * 4 for _ in range(9)],
        today=date(2026, 9, 2),
    )


def test_the_one_page_bound_holds_for_a_maximal_case_file() -> None:
    """The whole point of the artifact. Two earlier attempts overflowed:
    caps of 8/3/10/3 rendered 57 lines against a 46-line budget, and counting
    newlines instead of wrapped lines hid another 10."""
    lines = rendered_lines(render_agenda_markdown(_maximal()))

    assert lines <= AGENDA_LINE_BUDGET, f"{lines} rendered lines exceeds {AGENDA_LINE_BUDGET}"


def test_the_bound_counts_wrapped_lines_not_newlines() -> None:
    """A 200-character claim is one newline and three printed lines."""
    assert rendered_lines("x" * 250) == 3
    assert rendered_lines("short\nshort") == 2


def test_everything_dropped_is_counted() -> None:
    """Truncating to fit is honest only if the reader is told what did not
    fit. Silence would make a partial page look complete."""
    agenda = _maximal()
    notes = " ".join(agenda.omitted)

    assert len(agenda.labs) == AGENDA_MAX_LABS
    assert len(agenda.leads) == AGENDA_MAX_LEADS
    assert len(agenda.regimen) == AGENDA_MAX_REGIMEN
    assert len(agenda.asks) == AGENDA_MAX_ASKS
    assert "further abnormal result" in notes
    assert "further lead" in notes
    assert "further current medication" in notes
    assert "Not shown on this page" in render_agenda_markdown(agenda)


def test_an_empty_case_file_says_so_rather_than_rendering_blank_sections() -> None:
    agenda = build_agenda(
        ledger=_ledger(), abnormal=[], regimen=None, asks=[], today=date(2026, 9, 2)
    )
    text = render_agenda_markdown(agenda)

    assert "No abnormal result is flagged" in " ".join(agenda.omitted)
    assert "No lead currently carries cited evidence" in text
    assert "Nothing specific is being requested" in text


# --- the safety properties -----------------------------------------------------------------------


def test_a_medication_list_renders_and_still_passes_the_gate() -> None:
    """`treatment_gate` blocks every phrasing of a medication list, including
    a names-only one ("taking: hydroxychloroquine" reads as an imperative).
    The regimen block uses the `recording_only` scribe exemption instead —
    the single reason the most clinically useful section on the page can
    exist at all."""
    agenda = build_agenda(
        ledger=_ledger(_hyp("a")),
        abnormal=[],
        regimen=Regimen(
            entries=[
                RegimenEntry(name="Hydroxychloroquine", dose="200 mg", frequency="twice daily"),
                RegimenEntry(name="Biotin", dose="10000 mcg", frequency="daily"),
            ]
        ),
        asks=[],
        today=date(2026, 9, 2),
    )

    assert agenda_gate_failures(agenda) == []
    text = render_agenda_markdown(agenda)
    assert "Hydroxychloroquine" in text and "200 mg" in text
    assert "10000 mcg" in text


def test_an_instruction_in_a_regimen_field_is_still_caught() -> None:
    """`recording_only` drops the bare-dosage rule and KEEPS the imperative
    rule. Rule 5 is about instructions, not quantities."""
    agenda = Agenda(
        generated=date(2026, 9, 2),
        regimen=[AgendaRegimenRow(name="start taking prednisone", dose="20 mg")],
    )

    failures = agenda_gate_failures(agenda)

    assert failures
    assert any("imperative" in f for f in failures)


def test_a_verbless_instruction_cannot_hide_in_a_dose_cell() -> None:
    """Measured, not assumed: `recording_only` PASSES "increase to 400 mg",
    because the imperative rule needs a drug-like token near the verb. The
    dose cell is therefore also shape-checked — a quantity and a unit,
    nothing else. No instruction fits that grammar."""
    from adoc.reason.safety import treatment_gate

    # The gap this guards is real.
    assert treatment_gate("increase to 400 mg", recording_only=True).passed

    agenda = build_agenda(
        ledger=_ledger(),
        abnormal=[],
        regimen=Regimen(entries=[RegimenEntry(name="Prednisone", dose="increase to 400 mg")]),
        asks=[],
        today=date(2026, 9, 2),
    )

    assert agenda.regimen[0].dose == DOSE_WITHHELD
    assert "increase to 400" not in render_agenda_markdown(agenda)
    # The drug itself is NOT dropped: knowing she takes it matters more than
    # the amount, and a silently missing row hides a drug from a doctor.
    assert "Prednisone" in render_agenda_markdown(agenda)


def test_a_well_formed_dose_is_not_withheld() -> None:
    """The other half of the shape check: over-withholding would empty the
    column and make the section useless."""
    agenda = build_agenda(
        ledger=_ledger(),
        abnormal=[],
        regimen=Regimen(
            entries=[
                RegimenEntry(name="A", dose="200 mg"),
                RegimenEntry(name="B", dose="5000 IU"),
                RegimenEntry(name="C", dose="1-2 tablets"),
                RegimenEntry(name="D", dose="0.5 ml"),
            ]
        ),
        asks=[],
        today=date(2026, 9, 2),
    )

    assert [r.dose for r in agenda.regimen] == ["200 mg", "5000 IU", "1-2 tablets", "0.5 ml"]


def test_no_patient_identifier_is_printed() -> None:
    """`case/identifiers.yaml` exists to define what gets SCRUBBED
    (ADR 0017). Reading it to render PII would invert the one file whose
    purpose is removal — so the page carries a blank line instead."""
    text = render_agenda_markdown(_maximal())

    assert "Name / date of birth: ____" in text


def test_an_uncited_lead_never_reaches_the_page() -> None:
    """ADR 0037 for the patient view, and more sharply here: a doctor reading
    an unsourced claim on a patient-made page discounts the whole page.

    "Uncited" is an EMPTY `evidence_for`. `Evidence.source` is validated
    non-empty by the schema, so a filter on a truthy `source` would read as a
    citation check and never exclude anything — the first draft of
    `_support_lines` had exactly that vacuous filter."""
    agenda = build_agenda(
        ledger=_ledger(
            _hyp("cited"),
            _hyp("uncited", cited=False, tier="cant-miss", probability="low"),
        ),
        abnormal=[],
        regimen=None,
        asks=[],
        today=date(2026, 9, 2),
    )

    names = [lead.name for lead in agenda.leads]
    assert "Condition cited" in names
    assert "Condition uncited" not in names
    assert "no citation yet" in " ".join(agenda.omitted)


def test_a_retired_lead_never_reaches_the_page() -> None:
    """ADR 0038 ended it. Handing a doctor a lead the patient already
    excluded wastes the appointment it was built for."""
    agenda = build_agenda(
        ledger=_ledger(_hyp("gone", status="ruled-out"), _hyp("live")),
        abnormal=[],
        regimen=None,
        asks=[],
        today=date(2026, 9, 2),
    )

    assert [lead.name for lead in agenda.leads] == ["Condition live"]


# --- the numbers a doctor reads ------------------------------------------------------------------


def test_a_detection_limit_is_not_printed_as_a_measurement() -> None:
    """A `comparator` of `<` on 0.1 means the assay could not measure below
    0.1. Printing a bare `0.1` turns a detection limit into a measurement,
    which is a different clinical fact on a page a doctor will act on."""
    agenda = build_agenda(
        ledger=_ledger(),
        abnormal=[_lab("Free T4", value=0.1, comparator="<", flag="L")],
        regimen=None,
        asks=[],
        today=date(2026, 9, 2),
    )

    assert agenda.labs[0].value == "<0.1"
    assert "<0.1" in render_agenda_markdown(agenda)


def test_an_undated_regimen_entry_is_not_given_a_date() -> None:
    """`regimen.py` keeps `unknown` distinct from `no` because whether a lab
    result is real can depend on it. A printed page must not quietly turn an
    undated entry into a dated one."""
    agenda = build_agenda(
        ledger=_ledger(),
        abnormal=[],
        regimen=Regimen(
            entries=[
                RegimenEntry(name="Undated"),
                RegimenEntry(name="Attested", attested_on=[date(2026, 8, 1)]),
                RegimenEntry(name="Dated", started=date(2025, 3, 4)),
            ]
        ),
        asks=[],
        today=date(2026, 9, 2),
    )
    since = {row.name: row.since for row in agenda.regimen}

    assert since["Undated"] == "start date not on file"
    assert since["Attested"] == "reported on 2026-08-01"
    assert since["Dated"] == "2025-03-04"


def test_the_asks_fall_back_to_what_would_settle_a_lead() -> None:
    """A patient with an appointment tomorrow and no review this week still
    needs a usable page. ADR 0038's `rule_out_check` is the answer already on
    the record."""
    agenda = build_agenda(
        ledger=_ledger(
            _hyp("a", rule_out_check=RuleOutCheck(analyte="metanephrines", operator="normal"))
        ),
        abnormal=[],
        regimen=None,
        asks=[],
        today=date(2026, 9, 2),
    )

    assert agenda.asks
    assert "metanephrines" in agenda.asks[0]


def test_supplied_asks_win_over_the_derived_ones() -> None:
    """The review's test chooser reasoned across the whole differential; the
    fallback only looks at one lead at a time."""
    agenda = build_agenda(
        ledger=_ledger(
            _hyp("a", rule_out_check=RuleOutCheck(analyte="metanephrines", operator="normal"))
        ),
        abnormal=[],
        regimen=None,
        asks=["Could we run a complement C3/C4 panel?"],
        today=date(2026, 9, 2),
    )

    assert agenda.asks == ["Could we run a complement C3/C4 panel?"]


def test_a_missing_regimen_file_says_so_instead_of_looking_like_nothing_taken() -> None:
    """The failure mode this repo keeps hitting: absence looks exactly like
    working. A page with no medication table and no note reads to a doctor as
    "takes nothing", which is a wrong answer rather than a missing one."""
    agenda = build_agenda(
        ledger=_ledger(), abnormal=[], regimen=None, asks=[], today=date(2026, 9, 2)
    )

    assert agenda.regimen == []
    assert "not a statement that nothing is being taken" in " ".join(agenda.omitted)


def test_an_all_stopped_regimen_is_distinguished_from_a_missing_one() -> None:
    agenda = build_agenda(
        ledger=_ledger(),
        abnormal=[],
        regimen=Regimen(entries=[RegimenEntry(name="Was taking", stopped=date(2025, 1, 1))]),
        asks=[],
        today=date(2026, 9, 2),
    )
    notes = " ".join(agenda.omitted)

    assert "recorded as stopped" in notes
    assert "not a statement that nothing is being taken" not in notes

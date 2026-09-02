"""One page a doctor will actually read — the appointment agenda (ADR 0041).

The weekly review is the right artifact for the patient and the wrong one
for a 15-minute consultation. The last real report was 52,969 characters;
handed to a specialist on a phone screen it reads as a patient who has been
reading the internet, which is the outcome this system exists to avoid.

Three rules this module enforces, none of them a matter of taste:

**One page is a bound, not an aspiration.** Every section has a hard cap and
the whole render has a line budget. A "one-page" export that runs to three
pages has failed at the only thing it was for.

**Everything dropped is counted.** Truncating to fit is honest only if the
reader is told what did not fit; `Agenda.omitted` carries a line per capped
section, and it renders.

**The regimen is a record, not advice.** A medication list is the single most
useful thing on the page — the whole reason `regimen.py` exists is that
whether a lab result is real depends on what she was taking when it was
drawn — and `safety.treatment_gate` blocks every phrasing of one, including
a names-only list. It is gated with `recording_only=True` instead: the same
scribe exemption ADR 0020 built for intake, which drops the bare-dosage rule
and keeps the imperative rule that CLAUDE.md rule 5 actually exists to
enforce. "Hydroxychloroquine 200 mg daily" passes; "start taking 50 mcg"
does not.

Nothing here calls a model. Every field is copied from the ledger, the labs
database or `regimen.yaml`.

**No PII is printed.** Not the name, not the date of birth, not an MRN —
`case/identifiers.yaml` exists to define what gets *scrubbed* (ADR 0017),
and reading it to render PII would inverse the one file whose purpose is
removal. The page carries a blank line to fill in by hand. She is standing
in front of the doctor holding it; her name is the one fact in the room that
is not in doubt.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, Field

from adoc.casefile.ledger import ACTIVE_STATUSES
from adoc.casefile.regimen import Regimen, RegimenEntry
from adoc.casefile.schema import Hypothesis, Ledger
from adoc.labs.models import LabResult
from adoc.reason.safety import treatment_gate

# The budget is derived from the print stylesheet, then checked — not fitted
# to whatever the render happened to produce:
#
#   US Letter 11in − 2 × 0.5in margins  = 720pt printable height
#   9.5pt font × 1.25 line-height       = 11.875pt per line
#   720 / 11.875                        = 60 lines
#   less 17 worst-case table rows × 2pt = −2.9 lines
#   ------------------------------------------------------
#   capacity                            ≈ 57 lines
#
# `export_agenda.html` is written to those exact numbers, so the derivation
# is checkable against the CSS rather than being a claim about a page nobody
# has printed. Worst-case render measures 54, leaving 3 lines of slack.
#
# The caps below were TUNED to that budget, not chosen and hoped over. Two
# earlier attempts overflowed it: 8/3/10/3 rendered 57 against a 46-line
# budget, and counting newlines instead of WRAPPED lines hid another 10.
# That is the entire reason this is a test and not a comment.
AGENDA_MAX_LABS = 6
AGENDA_MAX_LEADS = 3
AGENDA_MAX_SUPPORT = 2
AGENDA_MAX_REGIMEN = 7
AGENDA_MAX_ASKS = 3
AGENDA_LINE_BUDGET = 57

AGENDA_CHARS_PER_LINE = 100
"""How many characters fit on one printed line at 9.5pt across the
stylesheet's 7.5in column. A budget counted in newlines is not a budget: a
200-character claim is one newline and three printed lines. `rendered_lines`
counts what the page will actually show."""

AGENDA_MAX_CLAIM_CHARS = 96
"""Longest evidence claim, lead name or ask rendered before elision — just
under one printed line, so every content row costs exactly one line and the
budget arithmetic holds.

Without a cap the page's height depends on how verbose a model was on the day
it wrote the ledger, which is not a bound at all: at 150 characters the same
worst case rendered 60 lines against a 52-line budget. Short is also right
for the artifact — a doctor scanning a page needs a scannable line, and the
full claim is one click away in the case file."""

# What a dose cell may contain. The `recording_only` gate keeps the
# imperative rule, but that rule needs a drug-like token near the verb, so a
# verbless fragment ("increase to 400 mg") passes it — measured, not
# assumed. A dose cell is therefore also shape-checked: a quantity and a
# unit, nothing else. No instruction fits that grammar.
_DOSE_SHAPE_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:\s*[-–/]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:mg|mcg|µg|ug|g|kg|iu|units?|ml|l|tablets?|tabs?|capsules?|caps?|"
    r"drops?|puffs?|sprays?|patch(?:es)?|scoops?|%)$",
    re.IGNORECASE,
)
DOSE_WITHHELD = "as reported — see case file"
"""What renders in place of a dose that fails `_DOSE_SHAPE_RE`. The entry
still appears: knowing she takes a thing matters more than the amount, and
dropping the row silently would hide a drug from a doctor."""


class AgendaLabRow(BaseModel):
    """One abnormal result, with the date a doctor needs to judge it by."""

    analyte: str
    value: str
    unit: str = ""
    flag: str = ""
    on: date
    source: str = ""


class AgendaRegimenRow(BaseModel):
    """One thing she takes. `dose` is shape-checked, never free text."""

    name: str
    dose: str = ""
    frequency: str = ""
    since: str = ""


class AgendaLead(BaseModel):
    """One lead, with what supports it and what would settle it."""

    name: str
    tier: str
    probability: str
    support: list[str] = Field(default_factory=list)
    would_settle: str = ""


class Agenda(BaseModel):
    """A whole page, before rendering. Every field is copied, none derived
    by a model."""

    generated: date
    labs: list[AgendaLabRow] = Field(default_factory=list)
    regimen: list[AgendaRegimenRow] = Field(default_factory=list)
    leads: list[AgendaLead] = Field(default_factory=list)
    asks: list[str] = Field(default_factory=list)
    omitted: list[str] = Field(default_factory=list)
    """One line per section that hit its cap, and one per section that is
    empty and would otherwise look merely forgotten."""


def rendered_lines(markdown: str) -> int:
    """Printed lines, not newlines — long lines wrap and a bound must know it.

    Deliberately an over-estimate at the margins: a wrapped line breaks at a
    word boundary, so the true count is never higher than this and is usually
    the same or lower.
    """
    total = 0
    for line in markdown.splitlines():
        width = len(line)
        total += 1 if width <= AGENDA_CHARS_PER_LINE else -(-width // AGENDA_CHARS_PER_LINE)
    return total


def _elide(text: str, limit: int = AGENDA_MAX_CLAIM_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _dose_cell(dose: str | None) -> str:
    if not dose:
        return ""
    text = dose.strip()
    return text if _DOSE_SHAPE_RE.match(text) else DOSE_WITHHELD


def _since(entry: RegimenEntry) -> str:
    """When she started, in the words the record can support. `regimen.py`
    keeps `unknown` distinct from `no` on purpose; a printed page must not
    quietly turn an undated entry into a dated one."""
    if entry.started is not None:
        return entry.started.isoformat()
    if entry.attested_on:
        return f"reported on {max(entry.attested_on).isoformat()}"
    return "start date not on file"


def _lab_value(row: LabResult) -> str:
    """The value as the record holds it, comparator included.

    A `comparator` of `<` on a value of 0.1 means the assay could not measure
    below 0.1 — printing a bare `0.1` for a doctor turns a detection limit
    into a measurement, which is a different clinical fact."""
    if row.value is not None:
        return f"{row.comparator or ''}{row.value:g}"
    return (row.value_text or "").strip()


def _support_lines(hypothesis: Hypothesis, *, limit: int = AGENDA_MAX_SUPPORT) -> list[str]:
    """The cited evidence for a lead, strongest first, capped.

    `Evidence.source` is validated non-empty by the schema, so every evidence
    item on a hypothesis is by construction cited — filtering on a truthy
    `source` here would read like a citation check and never exclude
    anything. What "uncited" actually means in this codebase is an EMPTY
    `evidence_for`, which is how `web.casefile_helpers` decides the same
    question, and it is handled by the caller."""
    strength_rank = {"definitive-exclusion": 0, "strong": 1, "moderate": 2, "weak": 3}
    cited = sorted(
        hypothesis.evidence_for,
        key=lambda e: (strength_rank.get(e.strength, 9), e.claim.lower()),
    )
    return [_elide(e.claim) for e in cited[:limit] if e.claim.strip()]


def _would_settle(hypothesis: Hypothesis) -> str:
    """ADR 0038's machine-checkable rule-out if there is one, else its prose."""
    check = getattr(hypothesis, "rule_out_check", None)
    if check is not None:
        operator = {
            "negative": "negative",
            "normal": "normal",
            "below": f"below {check.threshold}",
            "above": f"above {check.threshold}",
        }.get(check.operator, check.operator)
        unit = f" {check.unit}" if check.unit else ""
        return f"a {operator}{unit} {check.analyte}"
    return (getattr(hypothesis, "rule_out", "") or "").strip()


def build_agenda(
    *,
    ledger: Ledger,
    abnormal: Sequence[LabResult],
    regimen: Regimen | None = None,
    asks: Sequence[str] = (),
    today: date,
) -> Agenda:
    """Assemble the page. Deterministic, no model call, no I/O.

    `asks` comes from the review's test chooser when there has been a
    review. With none supplied, the asks are derived from what would settle
    the leads — so a patient with an appointment tomorrow and no review this
    week still gets a usable page.
    """
    omitted: list[str] = []

    lab_rows = [
        AgendaLabRow(
            analyte=row.name,
            value=_lab_value(row),
            unit=row.ucum_unit or "",
            flag=row.flag or "",
            on=row.date,
            source=row.source_doc[:12] if row.source_doc else "",
        )
        for row in abnormal[:AGENDA_MAX_LABS]
    ]
    if len(abnormal) > AGENDA_MAX_LABS:
        omitted.append(
            f"{len(abnormal) - AGENDA_MAX_LABS} further abnormal result(s) not shown — "
            "the full list is in the case file."
        )
    if not abnormal:
        omitted.append("No abnormal result is flagged in the record.")

    active = [h for h in ledger.hypotheses if h.status in ACTIVE_STATUSES]
    probability_rank = {"high": 0, "moderate": 1, "low": 2, "minimal": 3}
    tier_rank = {"most-likely": 0, "cant-miss": 1, "expanded": 2}
    # "Substantiated" is `evidence_for` being non-empty, not a truthiness
    # test on `source`: the schema validates every source ref, so a check on
    # `e.source` can never fail and would be a vacuous filter reading as a
    # safety property. Same definition `web.casefile_helpers` uses.
    substantiated = [h for h in active if h.evidence_for]
    substantiated.sort(
        key=lambda h: (
            probability_rank.get(h.probability, 9),
            tier_rank.get(h.tier, 9),
            h.name.lower(),
        )
    )
    leads = [
        AgendaLead(
            name=_elide(h.name),
            tier=h.tier,
            probability=h.probability,
            support=_support_lines(h),
            would_settle=_would_settle(h),
        )
        for h in substantiated[:AGENDA_MAX_LEADS]
    ]
    if len(substantiated) > AGENDA_MAX_LEADS:
        omitted.append(
            f"{len(substantiated) - AGENDA_MAX_LEADS} further lead(s) with cited "
            "evidence not shown."
        )
    unsubstantiated = len(active) - len(substantiated)
    if unsubstantiated > 0:
        omitted.append(
            f"{unsubstantiated} lead(s) are on the case file with no citation yet and "
            "are deliberately not listed here."
        )

    regimen_rows: list[AgendaRegimenRow] = []
    if regimen is None or not regimen.entries:
        # The recurring failure mode this repo keeps hitting (see
        # `docs/deployment-dependencies.md`): absence looks exactly like
        # working. A page with no medication table and no note reads as
        # "takes nothing", which for a doctor deciding whether a thyroid
        # panel is real is the wrong answer, not a missing one.
        omitted.append(
            "No medication or supplement list is on file, so none is shown — this is "
            "not a statement that nothing is being taken."
        )
    if regimen is not None:
        current = [e for e in regimen.entries if e.stopped is None]
        current.sort(key=lambda e: e.name.lower())
        regimen_rows = [
            AgendaRegimenRow(
                name=entry.name,
                dose=_dose_cell(entry.dose),
                frequency=(entry.frequency or "").strip(),
                since=_since(entry),
            )
            for entry in current[:AGENDA_MAX_REGIMEN]
        ]
        if regimen.entries and not current:
            omitted.append(
                "Every medication and supplement on file is recorded as stopped, so the "
                "current list is empty."
            )
        if len(current) > AGENDA_MAX_REGIMEN:
            omitted.append(
                f"{len(current) - AGENDA_MAX_REGIMEN} further current medication(s) or "
                "supplement(s) not shown."
            )

    chosen = [_elide(a) for a in asks if a.strip()][:AGENDA_MAX_ASKS]
    if not chosen:
        derived = [
            f"Could we check {lead.would_settle}? It would settle {lead.name}."
            for lead in leads
            if lead.would_settle
        ]
        chosen = derived[:AGENDA_MAX_ASKS]
    if not chosen:
        omitted.append(
            "No specific test is being requested yet — nothing on file names one that "
            "would settle a current lead."
        )

    return Agenda(
        generated=today,
        labs=lab_rows,
        regimen=regimen_rows,
        leads=leads,
        asks=chosen,
        omitted=omitted,
    )


def render_agenda_markdown(agenda: Agenda) -> str:
    """The page as markdown. The print route renders the same `Agenda`."""
    out: list[str] = [
        "# Appointment agenda",
        "",
        f"Name / date of birth: ____________________  ·  prepared "
        f"{agenda.generated.isoformat()} from a patient-maintained case file. Leads and "
        "questions only — not a diagnosis, and not produced by a clinician.",
        "",
    ]

    out += ["## What is abnormal", ""]
    if agenda.labs:
        out += ["| Result | Value | Flag | Date |", "|---|---|---|---|"]
        for row in agenda.labs:
            value = f"{row.value} {row.unit}".strip()
            out.append(f"| {row.analyte} | {value} | {row.flag} | {row.on.isoformat()} |")
    else:
        out.append("_Nothing flagged abnormal in the record._")
    out.append("")

    out += ["## Leads with cited evidence", ""]
    if agenda.leads:
        for lead in agenda.leads:
            out.append(f"**{lead.name}** — {lead.probability} probability, {lead.tier}")
            for claim in lead.support:
                out.append(f"  - {claim}")
            if lead.would_settle:
                out.append(f"  - _Would be settled by:_ {lead.would_settle}")
    else:
        out.append("_No lead currently carries cited evidence._")
    out.append("")

    if agenda.regimen:
        out += ["## Current medications and supplements (patient-reported)", ""]
        out += ["| Name | Dose | Frequency | Since |", "|---|---|---|---|"]
        for entry in agenda.regimen:
            out.append(f"| {entry.name} | {entry.dose} | {entry.frequency} | {entry.since} |")
        out.append("")

    out += ["## What I am asking for", ""]
    if agenda.asks:
        for ask in agenda.asks:
            out.append(f"- {ask}")
    else:
        out.append("_Nothing specific is being requested._")
    out.append("")

    if agenda.omitted:
        out += ["## Not shown on this page", ""]
        for note in agenda.omitted:
            out.append(f"- {note}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def agenda_gate_failures(agenda: Agenda) -> list[str]:
    """Every gate failure in a built agenda, as human-readable reasons.

    The narrative sections take the full `treatment_gate`. The regimen takes
    `recording_only=True` — the scribe exemption (ADR 0020), which is the
    only reason a medication list can appear at all. Empty means the page is
    safe to render.
    """
    failures: list[str] = []

    narrative = "\n".join(
        [
            *agenda.asks,
            *agenda.omitted,
            *(lead.name for lead in agenda.leads),
            *(claim for lead in agenda.leads for claim in lead.support),
            *(lead.would_settle for lead in agenda.leads),
        ]
    )
    gate = treatment_gate(narrative)
    failures += [f"narrative: {span.text!r} ({span.reason})" for span in gate.spans]

    record = "\n".join(
        f"{entry.name} {entry.dose} {entry.frequency}".strip() for entry in agenda.regimen
    )
    if record:
        record_gate = treatment_gate(record, recording_only=True)
        failures += [f"regimen: {span.text!r} ({span.reason})" for span in record_gate.spans]

    return failures

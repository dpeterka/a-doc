"""A lead can end because the cause was removed (ADR 0049).

The case this exists for:

> Selenium was high. Over subsequent draws it fell steadily toward normal.
> The hypothesis "selenium excess from supplementation" was **correct**. The
> reason it resolved is that a functional-medicine doctor prescribed a
> selenium supplement, the dose turned out to be too much, and the patient
> stopped taking it.

Nothing in the system could represent that. `ruled-out` says the hypothesis
was false — it was not. `parked` says nobody is looking, which loses the
finding. So the lead sat active forever with a falling analyte under it.

Two halves live here, and the boundary between them is the whole point:

- **Detecting the trend is deterministic.** An analyte that was out of range
  and is moving back into it is arithmetic over stored numbers, and it is
  computed in code (`labs.resolution` builds the signals; this module has no
  `labs` import, the one-directional rule `knowledge.criteria` states).
- **Naming the cause is not.** A stopped supplement that precedes a falling
  level is a CORRELATION. This module proposes it as a question and never as
  a transition. ADR 0042 drew the same line for suppressed markers: name the
  candidate, let a person make the causal claim.

The status change to `resolved` is what her ANSWER earns. It is never earned
by the correlation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from adoc.casefile.questions import Audience, OpenQuestion, question_id
from adoc.casefile.regimen import Regimen, RegimenEntry
from adoc.casefile.schema import Hypothesis, Ledger

TrendDirection = Literal["toward-reference", "away-from-reference", "flat", "unknown"]
"""`unknown` is not `flat`. A series with no usable reference range, or too
few points, cannot be judged — and calling that "flat" would report every
unmeasurable analyte as stable, which is the reading ADR 0051 spent a release
removing from the abnormal check."""

MIN_SERIES_POINTS = 3
"""Two points are a line, not a trend. A single repeat draw moving the right
way is ordinary variation and is not worth a question."""

MIN_CLOSED_FRACTION = 0.25
"""How much of the original excursion beyond the reference bound must have
closed before this counts as heading back. A 3% drift is noise; a quarter of
the way back is a direction."""


class ResolutionSignal(BaseModel):
    """One analyte observed heading back toward its reference range.

    Plain data on purpose: built in `labs.resolution`, consumed here, so
    `casefile` never imports `labs`.
    """

    analyte: str
    direction: TrendDirection = "unknown"
    first_date: date
    first_value: float
    last_date: date
    last_value: float
    unit: str = ""
    bound: float
    """The reference bound the series started outside of."""
    side: Literal["high", "low"]
    fraction_closed: float = 0.0
    """How much of the original excursion has closed, 0.0–1.0. Reported so a
    reader can tell "nearly normal" from "barely moved"."""

    @property
    def toward_reference(self) -> bool:
        return self.direction == "toward-reference"


class ResolutionQuestion(BaseModel):
    """A question to put to her about one signal, and the candidate cause it
    names — if the record offers one."""

    signal: ResolutionSignal
    candidate: RegimenEntry | None = None
    hypothesis_ids: list[str] = Field(default_factory=list)
    question: OpenQuestion


def classify_series(
    values: Sequence[tuple[date, float]],
    *,
    ref_low: float | None,
    ref_high: float | None,
) -> tuple[TrendDirection, float, float, Literal["high", "low"] | None]:
    """`(direction, fraction_closed, bound, side)` for one analyte's series.

    Judged from the FIRST reading's excursion beyond the bound it broke, not
    from the slope: a value that halves and then plateaus just inside the
    range has resolved, and a slope test would call the plateau flat.
    """
    if len(values) < MIN_SERIES_POINTS:
        return "unknown", 0.0, 0.0, None

    ordered = sorted(values)
    first_value = ordered[0][1]
    last_value = ordered[-1][1]

    if ref_high is not None and first_value > ref_high:
        bound, side = ref_high, "high"
        excursion = first_value - bound
        closed = (first_value - last_value) / excursion if excursion else 0.0
    elif ref_low is not None and first_value < ref_low:
        bound, side = ref_low, "low"
        excursion = bound - first_value
        closed = (last_value - first_value) / excursion if excursion else 0.0
    else:
        # It did not start outside a bound we can see, so there is no
        # excursion to close. Not flat — unjudgeable.
        return "unknown", 0.0, 0.0, None

    if closed >= MIN_CLOSED_FRACTION:
        return "toward-reference", min(closed, 1.0), bound, side  # type: ignore[return-value]
    if closed <= -MIN_CLOSED_FRACTION:
        return "away-from-reference", closed, bound, side  # type: ignore[return-value]
    return "flat", closed, bound, side  # type: ignore[return-value]


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def match_regimen_stop(
    signal: ResolutionSignal, regimen: Regimen, *, today: date
) -> RegimenEntry | None:
    """A closed regimen interval whose substance matches the analyte and whose
    `stopped` date precedes the fall.

    Matched on the analyte name appearing in the substance name, or the
    reverse — "Selenium" against "Selenium 200mcg". Deliberately crude: this
    is offered as a question, and the cost of naming the wrong supplement is
    that she says so. The cost of a clever matcher that fires silently would
    be higher.

    Returns `None` freely. An unmatched signal still earns a question; it just
    asks blind rather than naming a candidate.
    """
    analyte = _normalize(signal.analyte)
    if not analyte:
        return None
    best: RegimenEntry | None = None
    for entry in regimen.entries:
        if entry.stopped is None:
            continue
        if entry.stopped > signal.last_date or entry.stopped > today:
            continue
        name = _normalize(entry.name)
        if not name or (analyte not in name and name not in analyte):
            continue
        # The most recent qualifying stop. An older interval for the same
        # substance is real history but a worse explanation for this fall.
        if best is None or (best.stopped is not None and entry.stopped > best.stopped):
            best = entry
    return best


def hypotheses_citing(analyte: str, hypotheses: Iterable[Hypothesis]) -> list[str]:
    """Ids of hypotheses whose cited evidence names this analyte.

    Evidence sources are `labs:<slug>:<date>`, so the analyte is matched
    against the slug rather than against the claim prose — prose mentions the
    condition, not the measurement.
    """
    key = _normalize(analyte)
    if not key:
        return []
    found: list[str] = []
    for hypothesis in hypotheses:
        for item in list(hypothesis.evidence_for) + list(hypothesis.evidence_against):
            source = item.source or ""
            if not source.startswith("labs:"):
                continue
            if key in _normalize(source):
                found.append(hypothesis.id)
                break
    return found


def _ask_text(signal: ResolutionSignal, candidate: RegimenEntry | None) -> str:
    """Her question, in her words. One sentence, one thing asked.

    The candidate is named when the record offers one, because "did you stop
    a selenium supplement around May" is answerable and "did anything change"
    is not. It is phrased as a question in both cases — the record proposes,
    she decides.
    """
    direction = "high and has been coming down" if signal.side == "high" else "low and has come up"
    opening = f"Your {signal.analyte.lower()} was {direction}."
    if candidate is not None and candidate.stopped is not None:
        return (
            f"{opening} Your record shows you stopped {candidate.name} around "
            f"{candidate.stopped.strftime('%B %Y')} — is that what changed?"
        )
    return f"{opening} Do you know what changed — did you stop or change anything you take?"


def propose_resolution_questions(
    signals: Iterable[ResolutionSignal],
    ledger: Ledger,
    regimen: Regimen,
    *,
    today: date,
    audience: Audience = "you",
) -> list[ResolutionQuestion]:
    """One question per analyte heading back toward normal that a live lead
    rests on.

    Restricted to signals that bear on an ACTIVE hypothesis. An analyte
    quietly normalising under no lead at all is good news, not a question
    worth her time.
    """
    from adoc.casefile.ledger import ACTIVE_STATUSES

    active = [h for h in ledger.hypotheses if h.status in ACTIVE_STATUSES]
    out: list[ResolutionQuestion] = []
    for signal in signals:
        if not signal.toward_reference:
            continue
        ids = hypotheses_citing(signal.analyte, active)
        if not ids:
            continue
        candidate = match_regimen_stop(signal, regimen, today=today)
        panel = f"What changed with your {signal.analyte.lower()}"
        out.append(
            ResolutionQuestion(
                signal=signal,
                candidate=candidate,
                hypothesis_ids=ids,
                question=OpenQuestion(
                    id=question_id(panel),
                    panel=panel,
                    ask=_ask_text(signal, candidate),
                    why=(
                        f"{signal.analyte} has moved "
                        f"{signal.fraction_closed:.0%} of the way back into range since "
                        f"{signal.first_date.isoformat()}. If something you stopped explains "
                        "it, the lead resting on that result is resolved rather than open."
                    ),
                    audience=audience,
                    hypothesis_ids=ids,
                    first_asked_on=today,
                    last_asked_on=today,
                ),
            )
        )
    return out


def render_resolution_questions(items: Sequence[ResolutionQuestion]) -> list[str]:
    """The review section, in markdown."""
    if not items:
        return []
    lines = ["## Something here may already be sorted out", ""]
    lines.append(
        "One of your results has been heading back toward its normal range. If you know "
        "why, the lead resting on it can be closed as resolved instead of staying open."
    )
    lines.append("")
    for item in items:
        signal = item.signal
        lines.append(
            f"- **{signal.analyte}** — {signal.first_value:g} on "
            f"{signal.first_date.isoformat()} → {signal.last_value:g} on "
            f"{signal.last_date.isoformat()} ({signal.fraction_closed:.0%} of the way back)"
        )
        lines.append(f"  - {item.question.ask}")
    lines.append("")
    return lines

"""A new finding is tracked before it becomes a lead (ADR 0050).

Itchy, inflamed ears appeared recently. The review generated hypotheses for
it — including necrotizing otitis externa / skull-base osteomyelitis — and
they entered a board of 46 competing with hypotheses built on years of
serology. Nothing in the system knew that a two-week-old symptom and a
four-year-old pattern are different kinds of evidence.

That is the wrong default for the problem this system solves. The goal is the
UNDERLYING condition. A new, isolated finding is far more likely to be a
manifestation of something already on the board — or a self-limiting thing
that resolves — than an independent disease deserving three leads of its own.

## Tracked, never hidden

The premature-closure literature is specific that folding a new finding into
the existing story is exactly how the new thing gets missed, and that this is
"particularly common when patients seem to be having an exacerbation of a
known disorder". The watchful-waiting evidence (VAMPIRE, ISRCTN55755886)
points the other way: deferring on unexplained complaints lowers both testing
and false positives "without missing serious pathology".

Those are only compatible if the finding stays VISIBLE. So this is a derived
view, computed at render time — never a status, never a write. Nothing is
deleted, nothing is edited, and a lead reclassifies itself every review as
the dates move.

## What ADR 0050 assumed, and what is actually available

The ADR said "a hypothesis whose supporting phenotype terms are all newer
than the window". **A `Hypothesis` has no link to phenotype terms.** What it
does have is cited evidence whose source refs carry dates, and measurement
says that is enough: on the real ledger, **618 of 618 evidence sources parse
a date** (`labs:` 521, `encounter:` 82, `patient-report:` 15).

So the criterion is the age of a hypothesis's OLDEST cited evidence — which
is the better question anyway, because it asks what the hypothesis actually
rests on rather than what a separate matcher happened to extract.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from adoc.casefile.schema import Hypothesis, is_protected

EMERGING_WINDOW_DAYS = 90
"""How recent "all of it" has to be for a lead to count as still emerging.

Ninety days is the US National Center for Health Statistics' chronic-disease
convention and the DSM-5 chronicity specifier. It is **not** universal:
chronic cough is 8 weeks, the CDC uses a year, chronic migraine is 15 days a
month for 3 months. So this is a stated default rather than a derived
threshold, and `config.Settings.emerging_window_days` overrides it.

Measured on the real ledger 2026-09-04, by age of oldest cited evidence:

    30d -> 8 hypotheses (3 can't-miss)
    60d -> 11 (3)
    90d -> 11 (3)
   180d -> 15 (3)
"""

MAX_SOURCES_TO_STAY_EMERGING = 2
"""Above this many distinct citations, a finding is corroborated rather than
merely recent, and it joins the differential regardless of age.

ADR 0050's second promotion route. A lead cited by three independent sources
inside a fortnight is not a passing mention — the system has looked at it
from more than one direction and it held up.
"""

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _cited_dates(hypothesis: Hypothesis) -> list[date]:
    """Every date recoverable from this hypothesis's evidence refs.

    Works across `labs:<slug>:<date>`, `patient-report:<date>` and dated
    encounter filenames — 618 of 618 on the real ledger.
    """
    found: list[date] = []
    for item in hypothesis.evidence_for:
        match = _DATE_RE.search(item.source)
        if match is None:
            continue
        try:
            found.append(date.fromisoformat(match.group(1)))
        except ValueError:  # pragma: no cover - a malformed ref is not a date
            continue
    return found


def is_emerging(
    hypothesis: Hypothesis,
    *,
    today: date,
    window_days: int = EMERGING_WINDOW_DAYS,
    max_sources: int = MAX_SOURCES_TO_STAY_EMERGING,
) -> bool:
    """Whether this lead rests only on findings too new to act on yet.

    Four exclusions, each load-bearing:

    - **Never a `cant-miss` lead.** The safety checklist exists precisely for
      the dangerous-but-unlikely case (ADR 0039), and deferring one is the
      premature-closure failure the literature names. A new symptom that
      might be something serious is the LAST thing to set aside.
    - **Never a protected lead.** Her own theories are not the system's to
      defer.
    - **Never one with no dated evidence.** Unknown age is not the same as
      new, and defaulting the other way would quietly defer the oldest
      findings in the record — the exact inversion of the intent.
    - **Never a corroborated one.** More than `max_sources` distinct
      citations means the finding has been seen from several directions.
    """
    if hypothesis.tier == "cant-miss" or is_protected(hypothesis):
        return False
    dates = _cited_dates(hypothesis)
    if not dates:
        return False
    if (today - min(dates)).days >= window_days:
        return False
    return len({item.source for item in hypothesis.evidence_for}) <= max_sources


def split_emerging(
    hypotheses: Iterable[Hypothesis],
    *,
    today: date,
    window_days: int = EMERGING_WINDOW_DAYS,
) -> tuple[list[Hypothesis], list[Hypothesis]]:
    """`(differential, emerging)` — the same leads, sorted into two views.

    Order within each is the caller's input order, so an existing sort is
    preserved rather than silently re-ranked.
    """
    differential: list[Hypothesis] = []
    emerging: list[Hypothesis] = []
    for hypothesis in hypotheses:
        target = (
            emerging
            if is_emerging(hypothesis, today=today, window_days=window_days)
            else differential
        )
        target.append(hypothesis)
    return differential, emerging

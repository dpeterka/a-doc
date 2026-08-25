"""Deterministic fact corroboration (`docs/adr/0013-fact-corroboration.md`).

PLAIN DETERMINISTIC CODE — NO LLM call anywhere in this module (CLAUDE.md:
"Deterministic logic ... is plain code with unit tests — never delegated to
a model", same rule that governs `intake.facts.section_completion_blockers`
and `casefile.ledger`'s invariants). Checks each active `IntakeFact` against
what has already been ingested (`db.documents_overview()`, encounter files
under `case/encounters/`, lab rows) and computes whether it should be
`"corroborated"`, `"contradicted"`, or left `"unverified"`.

Deliberately conservative throughout: a false "corroborated"/"contradicted"
is worse than a true "unverified" — this app never fabricates certainty
about a patient's own history. In particular, **absence of a match never
means "contradicted"** — a patient can genuinely report something no
document happens to mention yet (missing documentation is normal).
`"contradicted"` is reserved for a hard, internally-detectable conflict
(currently: a diagnosis year in the future relative to today).

## Per-kind rules

- **event** facts with a parseable `date_approx`: matched against every
  dated document (`db.documents_overview()`, genomic documents excluded —
  they never participate in any LLM-adjacent reasoning path, ADR 0010) and
  encounter file, within a tolerance window scaled by how precisely the
  date was given (`_classify_date_approx`): an ISO date -> +/-14 days; a
  `YYYY-MM` or a relative-year phrase ("about 5 years ago") -> +/-120 days;
  a bare year -> +/-366 days (the whole year). A hit corroborates with a
  `doc:`/`encounter:` ref; no hit leaves the fact `"unverified"` — never
  `"contradicted"` on absence.
- **diagnosis** facts with `fields.year`: this store never has document
  TEXT, only metadata, so full entailment ("this document actually
  discusses this diagnosis") is out of reach here — that is Phase 2's job
  (PLAN.md "Phase 2 — Grounding & anti-hallucination hardening"). What is
  checkable now is *period* corroboration: a clinical-note document
  (`doc_type == "clinical_note"`) or an encounter file dated within
  `fields.year` +/-1 corroborates, with a note that is honest about the
  limit ("records exist from that period ... period corroboration only —
  content entailment is Phase 2"). A `fields.year` in the future relative
  to today is the one hard conflict this module can detect on its own
  terms (impossible regardless of documentation) and is the only case that
  reaches `"contradicted"`.
- **medication**/**supplement** facts are deliberately skipped entirely
  (left `"unverified"`, note `None`, never computed). The only case-file
  artifact that could corroborate a medication is `case/medications.md` —
  and that file is itself *written from* these very facts
  (`intake.convert.facts_to_section_data`/`intake.wizard._write_medications`).
  Checking a fact against an artifact derived from that same fact is
  circular, not corroboration, so this module does not attempt it.
- **symptom** facts referencing a canonical lab analyte: if any n-gram of
  the fact's statement/`fields["description"]` canonicalizes
  (`labs.validate.canonicalize` — reused, not reinvented) to a known
  analyte with rows on file, corroborate with the nearest-dated row's
  `labs:<slug>:<date>` ref. Conservative in two ways: only an exact
  canonical-alias match counts (single-character aliases like "k"/"ca"
  are excluded — see `_MIN_CANDIDATE_LEN` — since a bare 1-2 letter word is
  far too likely to appear in ordinary English by coincidence), and a miss
  never contradicts.
- Every other fact kind (`basic`, `patient_theory`, `relative`, `allergy`,
  `provider`, `insurance`, `note`) is left untouched by this sweep.

`corroborate_facts` is idempotent and safe to re-run at any time: it always
recomputes a fact's corroboration state fresh from current data (so a fact
that no longer matches — a source was somehow removed — reverts to
`"unverified"` rather than staying stale) and only returns a
`CorroborationUpdate` where the computed state actually differs from what
is already stored.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.ingest.genomics import GENOMIC_DOC_TYPE
from adoc.intake.facts import CorroborationUpdate, IntakeFact
from adoc.labs.db import LabsDb
from adoc.labs.validate import canonicalize

_EXACT_TOLERANCE_DAYS = 14
_YEAR_MONTH_TOLERANCE_DAYS = 120
_RELATIVE_YEAR_TOLERANCE_DAYS = 120
_YEAR_ONLY_TOLERANCE_DAYS = 366

_DIAGNOSIS_YEAR_TOLERANCE = 1
_CLINICAL_NOTE_DOC_TYPE = "clinical_note"

# Below this length, a canonical-alias match is too likely to be a
# coincidental English word ("k", "ca", "na" are all real analyte aliases —
# see `labs.validate`'s CMP specs) to trust as a symptom<->analyte match.
_MIN_CANDIDATE_LEN = 3
_MAX_CANDIDATE_NGRAM = 4

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_RELATIVE_YEAR_RE = re.compile(r"(\d+)\s*years?\s*ago", re.IGNORECASE)
_BARE_YEAR_RE = re.compile(r"\d{4}")

DateGranularity = Literal["exact", "year-month", "relative-year", "year-only"]

_TOLERANCE_BY_GRANULARITY: dict[DateGranularity, int] = {
    "exact": _EXACT_TOLERANCE_DAYS,
    "year-month": _YEAR_MONTH_TOLERANCE_DAYS,
    "relative-year": _RELATIVE_YEAR_TOLERANCE_DAYS,
    "year-only": _YEAR_ONLY_TOLERANCE_DAYS,
}


def _classify_date_approx(text: str, *, today: date) -> tuple[date, DateGranularity] | None:
    """Extends `intake.wizard._parse_approx_date`'s convention with a
    granularity label, so callers can scale a tolerance window by how
    precisely the date was actually given. Returns `None` when no date can
    be extracted at all (an undatable event is real, just not checkable
    here)."""
    stripped = text.strip()
    try:
        return date.fromisoformat(stripped), "exact"
    except ValueError:
        pass
    match = _YEAR_MONTH_RE.fullmatch(stripped)
    if match:
        return date(int(match.group(1)), int(match.group(2)), 1), "year-month"
    match = _RELATIVE_YEAR_RE.search(stripped)
    if match:
        return date(today.year - int(match.group(1)), 1, 1), "relative-year"
    match = _BARE_YEAR_RE.search(stripped)
    if match:
        return date(int(match.group(0)), 1, 1), "year-only"
    return None


@dataclass(frozen=True)
class _DatedSource:
    ref: str
    when: date
    doc_type: str | None
    """`None` for an encounter file — encounters are inherently "encounter
    kind" for the diagnosis period-corroboration rule regardless of any
    `doc_type` vocabulary."""


def _dated_documents(db: LabsDb) -> list[_DatedSource]:
    sources: list[_DatedSource] = []
    for overview in db.documents_overview():
        doc = overview.document
        if doc.doc_type == GENOMIC_DOC_TYPE or doc.doc_date is None:
            continue
        sources.append(
            _DatedSource(ref=f"doc:{doc.filename}#p1", when=doc.doc_date, doc_type=doc.doc_type)
        )
    return sources


def _dated_encounters(repo: DataRepo) -> list[_DatedSource]:
    encounters_dir = repo.root / "case" / "encounters"
    if not encounters_dir.is_dir():
        return []
    sources: list[_DatedSource] = []
    for path in sorted(encounters_dir.iterdir()):
        if path.suffix != ".md":
            continue
        encounter = read_encounter(path)
        sources.append(
            _DatedSource(
                ref=f"encounter:{path.name}", when=encounter.frontmatter.date, doc_type=None
            )
        )
    return sources


def _nearest(sources: Sequence[_DatedSource], target: date) -> tuple[_DatedSource, int] | None:
    best: tuple[_DatedSource, int] | None = None
    for source in sources:
        delta = abs((source.when - target).days)
        if best is None or delta < best[1]:
            best = (source, delta)
    return best


def _maybe_update(
    fact: IntakeFact,
    corroboration: Literal["corroborated", "contradicted", "unverified"],
    source: str | None,
    note: str | None,
) -> CorroborationUpdate | None:
    if (
        fact.corroboration == corroboration
        and fact.corroboration_source == source
        and fact.corroboration_note == note
    ):
        return None
    return CorroborationUpdate(
        fact_id=fact.id,
        corroboration=corroboration,
        corroboration_source=source,
        corroboration_note=note,
    )


def _unverified(fact: IntakeFact) -> CorroborationUpdate | None:
    return _maybe_update(fact, "unverified", None, None)


def _corroborate_event(
    fact: IntakeFact, sources: Sequence[_DatedSource], *, today: date
) -> CorroborationUpdate | None:
    if not fact.date_approx:
        return None
    classified = _classify_date_approx(fact.date_approx, today=today)
    if classified is None:
        return None
    target, granularity = classified
    tolerance = _TOLERANCE_BY_GRANULARITY[granularity]

    within_tolerance = [s for s in sources if abs((s.when - target).days) <= tolerance]
    nearest = _nearest(within_tolerance, target)
    if nearest is None:
        return _unverified(fact)
    source, delta = nearest
    note = (
        f"matched a record dated {source.when.isoformat()}, {delta} day(s) from the reported "
        f"timing ({fact.date_approx!r})"
    )
    return _maybe_update(fact, "corroborated", source.ref, note)


def _corroborate_diagnosis(
    fact: IntakeFact, clinical_sources: Sequence[_DatedSource], *, today: date
) -> CorroborationUpdate | None:
    year = fact.fields.get("year")
    if year is None:
        return None
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        return None

    if year_int > today.year:
        note = f"reported diagnosis year {year_int} is in the future relative to today's date"
        return _maybe_update(fact, "contradicted", None, note)

    match = next(
        (s for s in clinical_sources if abs(s.when.year - year_int) <= _DIAGNOSIS_YEAR_TOLERANCE),
        None,
    )
    if match is None:
        return _unverified(fact)
    note = (
        f"records exist from around {year_int} ({match.when.isoformat()}) — period "
        "corroboration only, content entailment is Phase 2"
    )
    return _maybe_update(fact, "corroborated", match.ref, note)


def _candidate_analyte_phrases(fact: IntakeFact) -> list[str]:
    text_parts = [fact.statement]
    description = fact.fields.get("description")
    if description:
        text_parts.append(str(description))
    words: list[str] = []
    for part in text_parts:
        words.extend(_TOKEN_RE.findall(part))

    candidates: list[str] = []
    for n in range(_MAX_CANDIDATE_NGRAM, 0, -1):
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i : i + n])
            if len(phrase) >= _MIN_CANDIDATE_LEN:
                candidates.append(phrase)
    return candidates


def _corroborate_symptom(fact: IntakeFact, db: LabsDb) -> CorroborationUpdate | None:
    for candidate in _candidate_analyte_phrases(fact):
        canonical = canonicalize(candidate)
        if canonical is None:
            continue
        rows = db.series(canonical)
        if not rows:
            continue
        nearest_row = max(rows, key=lambda r: r.date)
        slug = canonical.lower().replace(" ", "-")
        note = (
            f"an analyte matching {canonical!r} has recorded lab results (most recent "
            f"{nearest_row.date.isoformat()})"
        )
        return _maybe_update(
            fact, "corroborated", f"labs:{slug}:{nearest_row.date.isoformat()}", note
        )
    return None


def corroborate_facts(
    facts: Sequence[IntakeFact], db: LabsDb, repo: DataRepo, *, today: date | None = None
) -> list[CorroborationUpdate]:
    """Compute corroboration updates for every active fact in `facts`.

    Returns only facts whose computed state differs from what is already
    stored (idempotent, re-runnable) — apply the result with
    `intake.facts.IntakeFactsStore.apply_corroboration`.
    """
    today = today if today is not None else datetime.now(UTC).date()
    documents = _dated_documents(db)
    encounters = _dated_encounters(repo)
    event_sources = [*documents, *encounters]
    clinical_sources = [d for d in documents if d.doc_type == _CLINICAL_NOTE_DOC_TYPE] + encounters

    updates: list[CorroborationUpdate] = []
    for fact in facts:
        if fact.status != "active":
            continue

        update: CorroborationUpdate | None
        if fact.kind == "event":
            update = _corroborate_event(fact, event_sources, today=today)
        elif fact.kind == "diagnosis":
            update = _corroborate_diagnosis(fact, clinical_sources, today=today)
        elif fact.kind == "symptom":
            update = _corroborate_symptom(fact, db)
        else:
            # medication/supplement (circular — see module docstring) and
            # every other kind (basic, patient_theory, relative, allergy,
            # provider, insurance, note): left untouched by this sweep.
            update = None

        if update is not None:
            updates.append(update)

    return updates

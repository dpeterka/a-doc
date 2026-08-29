"""The patient's phenotype profile — `case/phenotype.yaml`.

A list of HPO terms with dates and provenance. It is the input LIRICAL's
phenotype-only mode takes (ADR 0029) and the missing half of every criteria
scorer's clinical items: the SLE 2019 scorer reports 16 of 21 items
`not_assessed` today purely because nothing can answer "fever", "arthritis",
"oral ulcers".

Built deterministically from text already on disk — encounter bodies and
intake facts — by `knowledge.hpo`'s label/synonym matcher. No model proposes
term ids, because a model asked for a code it half-remembers produces
plausible ids that do not exist, and a wrong phenotype term propagates
straight into a differential engine that ranks diseases by exactly those
terms.

**Presence and absence are both recorded.** LIRICAL takes excluded phenotypes
as evidence in their own right, so "denies chest pain" is worth as much as a
positive finding and is stored as one, negated.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

PHENOTYPE_RELPATH = "case/phenotype.yaml"


class PhenotypeTerm(BaseModel):
    """One HPO term observed for this patient."""

    term_id: str
    label: str
    present: bool = True
    """`False` for an explicitly excluded finding — not "unknown". A term
    nobody has mentioned simply is not in this file."""
    first_seen: date | None = None
    """Earliest date any source attested it. Dates come from the encounter
    the phrase was found in, so a symptom recorded in 2024 is not silently
    presented as current."""
    last_seen: date | None = None
    sources: list[str] = Field(default_factory=list)
    """`encounter:<file>` / `patient-report:<date>` refs, so a phenotype
    claim is checkable by the same machinery as any other (ADR 0028)."""
    matched_text: list[str] = Field(default_factory=list)
    """The patient's actual words that produced the match. Kept because "how
    did this term get here" is the first question anyone asks of an
    automatically derived phenotype, and the answer must not require rerunning
    the matcher."""


class PhenotypeProfile(BaseModel):
    entries: list[PhenotypeTerm] = Field(default_factory=list)
    updated: date | None = None

    def present_terms(self) -> list[str]:
        """Term ids to pass to LIRICAL as observed."""
        return [e.term_id for e in self.entries if e.present]

    def excluded_terms(self) -> list[str]:
        """Term ids to pass to LIRICAL as negated."""
        return [e.term_id for e in self.entries if not e.present]

    def by_id(self, term_id: str) -> PhenotypeTerm | None:
        return next((e for e in self.entries if e.term_id == term_id), None)


def load_phenotype(path: Path) -> PhenotypeProfile:
    if not path.is_file():
        return PhenotypeProfile()
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    return PhenotypeProfile.model_validate(raw or {})


def save_phenotype(path: Path, profile: PhenotypeProfile) -> None:
    """Stable, human-diffable YAML — same convention as the ledger, so
    repeated saves of identical content produce identical bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    profile.entries.sort(key=lambda e: e.term_id)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(profile.model_dump(mode="json"), fh)


def merge_terms(profile: PhenotypeProfile, found: list[PhenotypeTerm]) -> PhenotypeProfile:
    """Fold newly matched terms into `profile`.

    A term already present widens its date range and gains the new source
    rather than appearing twice.

    A CONFLICT — the same term recorded once as present and once as excluded —
    resolves to present. A patient who reported a symptom on one date and
    denied it on another genuinely had it; dropping it because a later note
    says "denies" would erase a real finding, whereas keeping it costs at most
    one term a clinician can dismiss.
    """
    by_id = {e.term_id: e for e in profile.entries}
    for term in found:
        existing = by_id.get(term.term_id)
        if existing is None:
            by_id[term.term_id] = term
            continue
        existing.present = existing.present or term.present
        for source in term.sources:
            if source not in existing.sources:
                existing.sources.append(source)
        for text in term.matched_text:
            if text not in existing.matched_text:
                existing.matched_text.append(text)
        dates = [d for d in (existing.first_seen, term.first_seen) if d is not None]
        if dates:
            existing.first_seen = min(dates)
        seen = [d for d in (existing.last_seen, term.last_seen) if d is not None]
        if seen:
            existing.last_seen = max(seen)
    return PhenotypeProfile(entries=list(by_id.values()), updated=profile.updated)


ENGINE_TERM_LIMIT = 8
"""How many terms a phenotype-driven engine is given.

Chosen from a measured sweep, not intuition — the first draft of this
constant said 12 and the data contradicted it. LIRICAL's composite likelihood
ratio for its top-ranked disease, against this patient's real profile:

    terms | top-ranked disease                          | LR
        5 | Autoinflammatory disease, systemic, w/ vasc. |  +2.29
        6 | (same)                                       |  +4.69
        8 | (same)                                       |  +4.82
       10 | (same)                                       |  +2.12
       12 | Charge syndrome                              |  -0.42
       15 | Celiac disease, susceptibility to, 1         |  -0.69
       20 | (same)                                       |  -2.94
       30 | (same)                                       |  -7.01
       82 | Autoinflammatory disease, systemic, w/ vasc. | -25.97

Two things that sweep shows. The score declines monotonically with profile
size, because terms no single disease explains subtract without bound — so a
complete record makes a poor engine input, however good a record it is. And
five to ten terms is a STABLE region: four independent term sets converge on
the same disease, which is a stronger signal than any single run.

Past ten it degrades into noise: "Charge syndrome" at twelve is not a serious
candidate for this patient.

Caveat worth keeping visible: this is one profile from one patient, so eight
is a defensible point inside the stable region rather than a universally
optimal number. What generalises is the shape, not the constant.
"""

ENGINE_RECENCY_DAYS = 730
"""How recent a finding must be to count as current. Two years is long enough
to keep a chronic finding that is only mentioned at annual reviews, short
enough to drop a resolved 2021 episode from a differential about today."""


def select_for_engine(
    profile: PhenotypeProfile,
    *,
    today: date,
    limit: int = ENGINE_TERM_LIMIT,
    recency_days: int = ENGINE_RECENCY_DAYS,
) -> tuple[list[str], list[str]]:
    """`(observed, excluded)` term ids for a phenotype-driven engine.

    The full profile is the RECORD; this is the QUERY. They are different
    artifacts and conflating them is what produced an unusable ranking.

    Selection, in order:

    1. **Current first.** A finding last seen inside `recency_days` outranks
       an older one. A 2021 episode that never recurred is history, and a
       differential about today should not be asked to explain it.
    2. **Then corroboration.** More independent sources means more confidence
       the term is real, which matters because these are matched from text
       and some matches are wrong.
    3. **Undated terms come last**, not never — a term with no date may still
       be current, so it fills remaining slots rather than being dropped.

    Excluded terms are NOT capped the same way. LIRICAL takes negated
    phenotypes as evidence, there are typically few of them, and each one is
    a deliberate clinical statement rather than an incidental mention.
    """
    present = [e for e in profile.entries if e.present]
    cutoff = today - timedelta(days=recency_days)

    def rank(entry: PhenotypeTerm) -> tuple[int, int, str]:
        seen = entry.last_seen or entry.first_seen
        current = 1 if (seen is not None and seen >= cutoff) else 0
        return (-current, -len(entry.sources), entry.term_id)

    chosen = sorted(present, key=rank)[:limit]
    return [e.term_id for e in chosen], profile.excluded_terms()

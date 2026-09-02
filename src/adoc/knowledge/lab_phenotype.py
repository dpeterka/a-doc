"""Lab findings as HPO terms, so the phenotype engines can see serology
(ADR 0044).

LIRICAL and the sem-sim index both take HPO term ids and nothing else. The
terms reaching them came only from `case/phenotype.yaml`, which is matched
from narrative text — so *arthralgia*, *fatigue* and *dry eyes* reached the
engines and an ANA of 1:640 did not.

Physical symptoms overlap heavily across common autoimmune disease and rare
congenital disease. In rheumatology the discriminating power is in the
serology. Asked to explain fatigue and joint pain with no antibody
information, a Mendelian phenotype engine ranks rare paediatric dysplasias —
and the measured outcome was exactly that: `engine_adjudication` returned
**66 of 66 neutral** on the last review, changing nothing, after LIRICAL had
run for 76.9 seconds.

HPO already has the vocabulary. `Antinuclear antibody positivity` is
HP:0003493; `Decreased circulating complement C3 concentration` is
HP:0005421. Nothing needed inventing — the terms were simply never derived.

## Labels, not ids

Every rule below names an HPO **label**, resolved to an id through the real
index at runtime. A hardcoded id typed wrong is silently wrong forever; a
label the ontology does not have lands in `unresolved` and renders in the
report. That mechanism is also how the gaps here were found rather than
guessed — searching all 19,119 terms showed HPO has **no anti-Smith
antibody term at all**, so the SLE criteria's `anti-dsDNA or anti-Sm` item
contributes only its dsDNA half here, and that is stated rather than
approximated with a neighbouring term.

## What is deliberately not derived

**Nothing from a normal result.** A negative ANA does not derive an
"absent" term. LIRICAL treats negated phenotypes as evidence *against* a
disease, so deriving one from a single normal draw would be a far stronger
claim than deriving a positive from a single abnormal one — and ADR 0042
established that under treatment a normal draw is frequently an expected
treatment effect. Excluded terms continue to come from the human record
only, where each one is a deliberate clinical statement.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from adoc.knowledge.hpo import HpoIndex
from adoc.labs.models import LabResult, flag_is_high, flag_is_low

Condition = Literal["positive", "high", "low", "titer_at_least"]

DERIVED_TERM_LIMIT = 10
"""Most derived terms admitted to one engine query.

`phenotype.select_for_engine` caps the human profile at 8 for a stated
reason — the full profile is the record, the query is a different artifact,
and an unbounded query produced an unusable ranking. Derived terms get their
own budget rather than competing for those 8, because the whole finding here
is that serology was absent from the query, and making it fight the symptoms
for slots would half-fix it. Ordered most recent first, so a current
abnormality outranks a resolved one."""


@dataclass(frozen=True)
class LabPhenotypeRule:
    """One lab finding worth telling a phenotype engine about."""

    patterns: tuple[str, ...]
    """Regexes over the NORMALISED analyte name (lowercase, non-alphanumerics
    stripped) — the same convention `knowledge.criteria` matches on, and for
    the same measured reason: the stored names are `Complement C4c` and
    `Smith (Sm) Antibody`, which no hand-written alias list contains."""
    condition: Condition
    hpo_label: str
    """The EXACT published label, resolved through the index at runtime."""
    threshold: float | None = None
    """For `titer_at_least`: the reciprocal titre, e.g. 80 for 1:80."""


# Ordered by how much a rheumatologist would weight it. Every label here was
# verified against the real index (19,119 terms) rather than recalled.
RULES: tuple[LabPhenotypeRule, ...] = (
    LabPhenotypeRule(
        patterns=(r"antinuclearantibod", r"^ana$", r"^anaifa$", r"antinuclearab"),
        condition="titer_at_least",
        hpo_label="Antinuclear antibody positivity",
        threshold=80.0,
    ),
    LabPhenotypeRule(
        patterns=(r"dsdna", r"doublestrandeddna", r"antidsdna"),
        condition="positive",
        hpo_label="Anti-dsDNA antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"ssa", r"^ro$", r"ssaro", r"antiro"),
        condition="positive",
        hpo_label="Anti-Ro/SS-A antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"ro52", r"trim21"),
        condition="positive",
        hpo_label="Anti-Ro52/TRIM21 antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"rheumatoidfactor", r"^rf$"),
        condition="positive",
        hpo_label="Rheumatoid factor positive",
    ),
    LabPhenotypeRule(
        patterns=(r"cyclidcitrullinated", r"anticcp", r"^ccp$", r"acpa"),
        condition="positive",
        hpo_label="Anti-citrullinated protein antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"anca", r"antineutrophilcytoplasmic"),
        condition="positive",
        hpo_label="Antineutrophil antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"anticardiolipinigg", r"cardiolipinigg"),
        condition="positive",
        hpo_label="Anticardiolipin IgG antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"anticardiolipinigm", r"cardiolipinigm"),
        condition="positive",
        hpo_label="Anticardiolipin IgM antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"lupusanticoagulant",),
        condition="positive",
        hpo_label="Lupus anticoagulant",
    ),
    LabPhenotypeRule(
        patterns=(r"beta2glycoprotein.*igg", r"b2glycoprotein.*igg"),
        condition="positive",
        hpo_label="Anti-beta-2-Glycoprotein I IgG antibody positivity",
    ),
    LabPhenotypeRule(
        patterns=(r"complementc3", r"^c3$", r"c3complement"),
        condition="low",
        hpo_label="Decreased circulating complement C3 concentration",
    ),
    LabPhenotypeRule(
        patterns=(r"complementc4", r"^c4$", r"c4complement"),
        condition="low",
        hpo_label="Decreased circulating complement C4 concentration",
    ),
    LabPhenotypeRule(
        patterns=(r"^wbc$", r"whitebloodcell", r"leukocytecount", r"^leukocytes$"),
        condition="low",
        hpo_label="Decreased total leukocyte count",
    ),
    LabPhenotypeRule(
        patterns=(r"^platelet", r"plateletcount"),
        condition="low",
        hpo_label="Thrombocytopenia",
    ),
    LabPhenotypeRule(
        patterns=(r"^lymphocyte", r"absolutelymphocyte"),
        condition="low",
        hpo_label="Decreased total lymphocyte count",
    ),
    LabPhenotypeRule(
        patterns=(r"^neutrophil", r"absoluteneutrophil"),
        condition="low",
        hpo_label="Decreased total neutrophil count",
    ),
    LabPhenotypeRule(
        patterns=(r"^eosinophil", r"absoluteeosinophil"),
        condition="high",
        hpo_label="Increased total eosinophil count",
    ),
    LabPhenotypeRule(
        patterns=(r"^hemoglobin$", r"^hgb$", r"^haemoglobin$"),
        condition="low",
        hpo_label="Anemia",
    ),
    LabPhenotypeRule(
        patterns=(r"creactiveprotein", r"^crp$", r"^hscrp$"),
        condition="high",
        hpo_label="Elevated circulating C-reactive protein concentration",
    ),
    LabPhenotypeRule(
        patterns=(r"sedimentationrate", r"^esr$", r"sedrate"),
        condition="high",
        hpo_label="Elevated erythrocyte sedimentation rate",
    ),
    LabPhenotypeRule(
        patterns=(r"creatinekinase", r"^ck$", r"^cpk$"),
        condition="high",
        hpo_label="Elevated circulating creatine kinase activity",
    ),
    LabPhenotypeRule(
        patterns=(r"thyroidstimulatinghormone", r"^tsh$"),
        condition="high",
        hpo_label="Elevated circulating thyroid-stimulating hormone concentration",
    ),
    LabPhenotypeRule(
        patterns=(r"^ferritin$",),
        condition="high",
        hpo_label="Increased circulating ferritin concentration",
    ),
    LabPhenotypeRule(
        patterns=(r"vitaminb12", r"cobalamin"),
        condition="low",
        hpo_label="Decreased circulating vitamin B12 concentration",
    ),
    LabPhenotypeRule(
        patterns=(r"partialthromboplastin", r"^aptt$", r"^ptt$"),
        condition="high",
        hpo_label="Prolonged partial thromboplastin time",
    ),
)

# HPO has no term for this, confirmed by searching every label in the index.
# Recorded so the gap is a documented fact rather than a silent omission —
# `knowledge.criteria`'s SLE item is "Anti-dsDNA or anti-Smith", and only
# the dsDNA half can reach an engine.
KNOWN_VOCABULARY_GAPS: tuple[str, ...] = ("anti-Smith (anti-Sm) antibody",)


class DerivedTerm(BaseModel):
    """One HPO term derived from a lab row, with the row it came from."""

    term_id: str
    label: str
    source: str
    """`labs:<slug>:<date>`, so the term is checkable by the same machinery
    as any other claim (ADR 0028)."""
    basis: str
    """The value and date, in words, for the report."""


class LabPhenotypeResult(BaseModel):
    """What the labs contributed to an engine query, and what they could not.

    A Pydantic model rather than a dataclass because it crosses into the
    review report and the `results` sink — CLAUDE.md's rule for every
    cross-boundary payload.
    """

    terms: list[DerivedTerm] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    """Rule labels the HPO index did not contain. Reported, never guessed at
    with a neighbouring term."""
    rows_considered: int = 0
    dropped_over_limit: int = 0
    index_available: bool = True

    @property
    def term_ids(self) -> list[str]:
        return [t.term_id for t in self.terms]


def _normalize(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


_TITER_RE = re.compile(r"1\s*[:/]\s*(\d+)")


def _titer_at_least(row: LabResult, threshold: float) -> bool:
    """Whether a titre reads at or above `1:threshold`.

    A titre is a ratio in text far more often than a number, and a higher
    reciprocal is a STRONGER result — the comparison people get backwards.
    """
    text = (row.value_text or "").strip()
    match = _TITER_RE.search(text)
    if match is not None:
        return float(match.group(1)) >= threshold
    if row.value is not None:
        return row.value >= threshold
    return False


def _is_positive(row: LabResult) -> bool:
    """Whether a qualitative row reads positive.

    Negation is checked FIRST because "not detected" contains "detected" —
    the same ordering `knowledge.criteria._is_positive` depends on, and the
    same reason.
    """
    text = (row.value_text or "").strip().lower()
    if text:
        negatives = (
            "not detected",
            "non-reactive",
            "nonreactive",
            "negative",
            "none seen",
            "absent",
            "not present",
        )
        if any(marker in text for marker in negatives):
            return False
        positives = ("positive", "reactive", "detected", "present", "abnormal")
        if any(marker in text for marker in positives):
            return True
    return flag_is_high(row.flag)


def _satisfies(rule: LabPhenotypeRule, row: LabResult) -> bool:
    if rule.condition == "positive":
        return _is_positive(row)
    if rule.condition == "high":
        return flag_is_high(row.flag)
    if rule.condition == "low":
        return flag_is_low(row.flag)
    if rule.condition == "titer_at_least":
        return _titer_at_least(row, rule.threshold or 0.0)
    return False


def _matches(rule: LabPhenotypeRule, row: LabResult) -> bool:
    keys = {_normalize(n) for n in (row.name, row.name_raw) if n}
    return any(re.search(pattern, key) for pattern in rule.patterns for key in keys)


def _shown(row: LabResult) -> str:
    if row.value is not None:
        return f"{row.comparator or ''}{row.value:g}"
    return (row.value_text or "").strip()


def _resolve(index: HpoIndex, label: str) -> tuple[str, str] | None:
    """`(term_id, label)` for an exact published label, or `None`.

    An exact lookup, and only exact: a near miss is a vocabulary gap to
    report, not a neighbouring term to substitute. `find_terms` is the wrong
    tool here and the reason is measured — its word tokens must begin with a
    letter, so `Anti-beta-2-Glycoprotein I IgG antibody positivity`
    tokenises without its `2` and matches nothing at all. Correct for
    scanning prose; useless for asking whether the ontology has a term.
    """
    term_id = index.term_id_for(label)
    if term_id is None:
        return None
    resolved = index.label(term_id)
    return term_id, resolved or label


def derive_lab_phenotype(
    rows: Sequence[LabResult], *, index: HpoIndex | None, limit: int = DERIVED_TERM_LIMIT
) -> LabPhenotypeResult:
    """HPO terms implied by the stored labs, for an engine query.

    `ever` semantics, consistent with ADR 0042: a marker that was positive at
    any point derives its term, because classification and the diseases these
    engines rank both treat serology cumulatively. Being inconsistent between
    the criteria scorers and the engines would be worse than either choice.

    The most recent satisfying row per rule supplies the citation, and terms
    are ordered most-recent-first so a current abnormality outranks a
    resolved one when the limit bites.
    """
    if index is None:
        return LabPhenotypeResult(rows_considered=len(rows), index_available=False)

    ordered = sorted(rows, key=lambda r: r.date, reverse=True)
    found: list[DerivedTerm] = []
    unresolved: list[str] = []
    seen_ids: set[str] = set()

    for rule in RULES:
        satisfying = [row for row in ordered if _matches(rule, row) and _satisfies(rule, row)]
        if not satisfying:
            continue
        resolved = _resolve(index, rule.hpo_label)
        if resolved is None:
            if rule.hpo_label not in unresolved:
                unresolved.append(rule.hpo_label)
            continue
        term_id, label = resolved
        if term_id in seen_ids:
            continue
        seen_ids.add(term_id)
        row = satisfying[0]
        slug = "-".join(part for part in re.split(r"[^a-z0-9]+", row.name.lower()) if part)
        found.append(
            DerivedTerm(
                term_id=term_id,
                label=label,
                source=f"labs:{slug}:{row.date.isoformat()}",
                basis=f"{row.name} {_shown(row)} on {row.date.isoformat()}",
            )
        )

    found.sort(key=lambda t: t.source.rsplit(":", 1)[-1], reverse=True)
    kept = found[:limit]
    return LabPhenotypeResult(
        terms=kept,
        unresolved=unresolved,
        rows_considered=len(rows),
        dropped_over_limit=len(found) - len(kept),
    )

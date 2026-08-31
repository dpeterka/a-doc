"""Phenotype semantic similarity — a second independent differential engine.

LIRICAL ranks by likelihood ratio against a curated disease model. This ranks
by how much *information* a patient's phenotype shares with each disease's
annotation set. The two disagree in useful ways: LIRICAL is sensitive to a
disease's prior and to how completely it is curated, while similarity only
asks how specific the overlap is. Where both rank a disease highly, that is
corroboration from genuinely different methods.

Like LIRICAL, this is NOT folded into a combined score
(`docs/research/scoring-across-engines.md`). Its output is a similarity, not a
probability, and averaging it against likelihood ratios and criteria points is
the unit-blindness that has already produced three wrong clinical conclusions
here.

## The measure

Resnik similarity with best-match average, symmetric.

Information content of a term is `-log(P(term))`, where `P` is the fraction of
annotated diseases that carry the term or any of its descendants. A term
almost every disease has ("abnormality of the nervous system") carries almost
no information; a rare specific one carries a lot. This is what stops a
patient's twenty generic findings from swamping their two specific ones.

Two phenotype terms are compared by the information content of their most
informative common ancestor — how specifically they agree, rather than whether
they are string-equal. "Aortic root aneurysm" and "dilated aortic sinus" are
different terms and nearly the same finding; a set-overlap measure scores that
zero and this does not.

Best-match average rather than plain average: each query term is scored
against its best partner in the disease, and vice versa, and the two means are
averaged. A one-directional average would rank a disease with two hundred
annotations above a precise match, because most of its terms would find
nothing and drag the mean down — or up, depending which direction you picked.
Symmetry removes that artefact.

Deliberately not Phenodigm or Lin: both normalise, which makes scores look
comparable across patients in a way they are not. A raw Resnik score is in
units of information and is only meaningful RELATIVE to other diseases scored
against the same query — which is exactly how it is used here, as a ranking.

## The known artefact

Rare diseases with few, highly specific annotations can outrank the obvious
answer on a short query, because a small annotation set has nothing to drag
its best-match average down. Measured on the real 2026-06-23 release with a
sicca/arthritis/fatigue query, the top six were:

    3.30  familial esophageal achalasia   <- the artefact
    2.73  Yao syndrome
    2.71  Sjogren syndrome
    2.57  Reynolds syndrome
    2.57  mixed connective tissue disease
    2.51  primary Sjogren disease

Four of six are the right disease family and the first is not. This is a
property of information-content similarity, not a bug to be tuned away, and it
is the concrete reason this engine reports DIVERGENCE rather than an answer:
rank 1 is a candidate to consider, never a conclusion. The same caution
applies to LIRICAL, for different reasons (ADR 0034).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# How many ranked diseases the engine returns. The tail is long and its scores
# are near-identical; past the top handful the ordering is noise, exactly as
# the LIRICAL sweep found (ADR 0034).
DEFAULT_TOP_N = 10

# A query term that appears in no disease annotation carries no information we
# can measure, and `-log(0)` is undefined. Such terms are dropped from scoring
# and REPORTED, so "we ignored four of your findings" is visible rather than
# silent.
_UNSEEN_IC = 0.0


class SemSimDisease(BaseModel):
    """One ranked disease."""

    disease_id: str
    """`OMIM:154700`, `ORPHA:558`."""
    name: str
    score: float
    """Resnik best-match average, in bits of information. Meaningful only
    relative to the other scores in the same run."""
    matched_terms: int
    """How many query terms found a partner with any shared information."""


class SemSimResult(BaseModel):
    """One similarity run."""

    diseases: list[SemSimDisease] = Field(default_factory=list)
    terms_used: list[str] = Field(default_factory=list)
    terms_unknown: list[str] = Field(default_factory=list)
    """Query terms absent from every disease annotation, so they could not be
    scored. Reported rather than dropped silently."""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class SemSimIndex:
    """The annotation corpus, with ancestor closure and information content.

    Closure and IC are computed once at construction rather than stored in the
    index file: they are derivable from ~40,000 parent edges in well under a
    second, so baking them in would trade real image size for nothing.
    """

    def __init__(self, parents: dict[str, list[str]], diseases: dict[str, dict]) -> None:
        self._parents = parents
        self._disease_names = {d: str(v.get("name", d)) for d, v in diseases.items()}
        self._disease_terms = {d: list(v.get("terms", [])) for d, v in diseases.items()}
        self._ancestors: dict[str, frozenset[str]] = {}
        self._ic: dict[str, float] = {}
        self._closed_terms: dict[str, frozenset[str]] = {}
        self._build()

    # -- construction ------------------------------------------------------

    def ancestors(self, term: str) -> frozenset[str]:
        """`term` and every is_a ancestor of it.

        Iterative rather than recursive: HPO is a DAG and a cycle in a bad
        release would blow the stack, which is a poor way to find out.
        """
        cached = self._ancestors.get(term)
        if cached is not None:
            return cached
        seen: set[str] = set()
        stack = [term]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._parents.get(current, ()))
        closure = frozenset(seen)
        self._ancestors[term] = closure
        return closure

    def _build(self) -> None:
        # A disease annotated with a term is implicitly annotated with every
        # ancestor of it. Without this propagation a specific annotation would
        # not count toward its general parent and the frequencies — and so the
        # information content — would be wrong.
        frequency: dict[str, int] = {}
        for disease, terms in self._disease_terms.items():
            closed: set[str] = set()
            for term in terms:
                closed |= self.ancestors(term)
            self._closed_terms[disease] = frozenset(closed)
            for term in closed:
                frequency[term] = frequency.get(term, 0) + 1

        total = len(self._disease_terms)
        if total:
            for term, count in frequency.items():
                self._ic[term] = -math.log(count / total)

    # -- accessors ---------------------------------------------------------

    @property
    def disease_count(self) -> int:
        return len(self._disease_terms)

    def information_content(self, term: str) -> float:
        return self._ic.get(term, _UNSEEN_IC)

    def is_known(self, term: str) -> bool:
        return term in self._ic

    def pairwise(self, a: str, b: str) -> float:
        """Information content of the most informative common ancestor.

        This is what makes the measure tolerant of near-synonyms: two distinct
        terms that share a specific ancestor score highly, where string
        equality scores nothing.
        """
        shared = self.ancestors(a) & self.ancestors(b)
        if not shared:
            return 0.0
        return max(self._ic.get(t, _UNSEEN_IC) for t in shared)

    # -- scoring -----------------------------------------------------------

    def _best_match_average(self, query: Sequence[str], target: Iterable[str]) -> tuple[float, int]:
        target_list = list(target)
        if not query or not target_list:
            return 0.0, 0
        best = [max(self.pairwise(q, t) for t in target_list) for q in query]
        matched = sum(1 for score in best if score > 0.0)
        return sum(best) / len(best), matched

    def score(self, query: Sequence[str], disease_id: str) -> tuple[float, int]:
        """`(similarity, matched_terms)` for one disease."""
        terms = self._disease_terms.get(disease_id)
        if not terms:
            return 0.0, 0
        forward, matched = self._best_match_average(query, terms)
        backward, _ = self._best_match_average(terms, query)
        return (forward + backward) / 2.0, matched

    def rank(self, query: Sequence[str], *, top_n: int = DEFAULT_TOP_N) -> SemSimResult:
        """Rank every disease against `query`.

        Unknown query terms are removed before scoring and reported, because a
        term nothing is annotated with contributes zero to every disease and
        would otherwise silently dilute the average for all of them equally —
        changing the scores without changing the ranking, which is the worst
        kind of noise.
        """
        known = [t for t in query if self.is_known(t)]
        unknown = [t for t in query if not self.is_known(t)]
        if not known:
            return SemSimResult(
                terms_unknown=unknown,
                error="no query term appears in any disease annotation",
            )

        scored: list[SemSimDisease] = []
        for disease_id in self._disease_terms:
            value, matched = self.score(known, disease_id)
            if value <= 0.0:
                continue
            scored.append(
                SemSimDisease(
                    disease_id=disease_id,
                    name=self._disease_names.get(disease_id, disease_id),
                    score=value,
                    matched_terms=matched,
                )
            )
        # Ties broken by id so a run is reproducible rather than dict-ordered.
        scored.sort(key=lambda d: (-d.score, d.disease_id))
        return SemSimResult(diseases=scored[:top_n], terms_used=known, terms_unknown=unknown)


@lru_cache(maxsize=2)
def load_index(path: Path) -> SemSimIndex | None:
    """Load and cache the index, or `None` if it is absent or unreadable.

    Absent is an ordinary state, not an error: the index is a build artifact
    baked into the image, and a local checkout will not have one. The caller
    reports "the similarity engine did not run" and the review completes.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        index = SemSimIndex(parents=data["parents"], diseases=data["diseases"])
    except FileNotFoundError:
        logger.info("semsim: no index at %s; similarity ranking is off", path)
        return None
    except Exception as exc:  # noqa: BLE001 - a bad index must not fail a review
        logger.warning("semsim: could not load index at %s: %s", path, exc)
        return None
    logger.info("semsim: loaded %d diseases from %s", index.disease_count, path)
    return index

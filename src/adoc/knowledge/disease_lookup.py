"""Disease lookup for the chat — what a condition is, and what it looks like.

The differential ledger names conditions the patient has never heard of.
"Relapsing polychondritis" and "primary ovarian insufficiency" are precise and
tell the person whose case file it is nothing at all, which is why
`Hypothesis.plain_language` exists. This answers the next question: *what would
I expect to see with that?*

Built entirely on indexes the image already carries — the phenotype-similarity
index for disease annotations and the HPO index for readable labels. No new
download, no network call, no model.

## Features are ranked by information content, not listed

A curated disease can carry forty annotations, most of them generic
("abnormality of the immune system"). Listing them in file order buries the
distinguishing ones. Ranking by information content puts the features that
actually characterise the disease first — the same measure the similarity
engine ranks on, used here to answer a different question.

## What this is not

It is a reference lookup, not a statement about the patient. Saying that
Sjögren syndrome characteristically involves dry eyes says nothing about
whether she has it, and the rendering is explicit about that. Nothing here
reads the case file.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, Field

from adoc.knowledge.hpo import HpoIndex
from adoc.knowledge.semsim import SemSimIndex

# How many characteristic features to show per disease. Enough to recognise a
# condition, short enough to read; past this the tail is generic.
MAX_FEATURES = 8

# How many diseases one lookup returns. A name like "arthritis" matches
# hundreds, and a wall of them is worse than none.
MAX_MATCHES = 3

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Words too generic to search on alone. "Syndrome" matches several thousand
# diseases and tells a reader nothing.
_TOO_GENERIC = frozenset(
    {
        "syndrome",
        "disease",
        "disorder",
        "deficiency",
        "type",
        "familial",
        "congenital",
        "chronic",
        "acute",
        "primary",
        "secondary",
        "idiopathic",
    }
)

_MIN_QUERY_CHARS = 4


class DiseaseCard(BaseModel):
    """One disease, as a reference entry."""

    disease_id: str
    name: str
    features: list[str] = Field(default_factory=list)
    """Characteristic phenotype labels, most distinctive first."""
    feature_count: int = 0
    """How many annotations the disease has in total, so "8 of 41 shown" is
    never mistaken for "these are all of them"."""


def _normalise(text: str) -> str:
    return " ".join(_NON_ALNUM_RE.sub(" ", text.lower()).split())


def find_diseases(index: SemSimIndex, query: str, *, limit: int = MAX_MATCHES) -> list[str]:
    """Disease ids whose name matches `query`, best first.

    Exact normalised match wins, then whole-phrase containment, then nothing.
    Deliberately NOT fuzzy: a near-miss that silently returns the wrong
    disease is worse than no answer, and the caller has no way to tell.
    """
    needle = _normalise(query)
    if len(needle) < _MIN_QUERY_CHARS or needle in _TOO_GENERIC:
        return []

    exact: list[str] = []
    contains: list[str] = []
    for disease_id, name in index.iter_diseases():
        normalised = _normalise(name)
        if normalised == needle:
            exact.append(disease_id)
        elif needle in normalised:
            contains.append(disease_id)

    # An exact name match is unambiguous, so return ONLY those. Mixing in
    # substring matches alongside one is how a question about Sjogren syndrome
    # came back with Marinesco-Sjogren syndrome — a cerebellar ataxia that
    # merely contains the string. Near-name noise beside a confident answer is
    # worse than no extra suggestions at all.
    if exact:
        return sorted(exact)[:limit]

    # With no exact match, containment is a reasonable "did you mean". Shorter
    # names first, so "Sjogren syndrome" beats
    # "Microcephaly-glomerulonephritis-marfanoid habitus syndrome" for a query
    # both happen to contain.
    contains.sort(key=lambda d: (len(index.disease_name(d) or ""), d))
    return contains[:limit]


def describe_disease(
    index: SemSimIndex,
    hpo: HpoIndex | None,
    disease_id: str,
    *,
    max_features: int = MAX_FEATURES,
) -> DiseaseCard | None:
    """A reference card for one disease, features most-distinctive first."""
    name = index.disease_name(disease_id)
    if name is None:
        return None

    terms = index.disease_terms(disease_id)
    ranked = sorted(terms, key=lambda t: (-index.information_content(t), t))

    features: list[str] = []
    for term in ranked:
        label = hpo.label(term) if hpo is not None else None
        # A term with no label in the HPO index is skipped rather than shown
        # as a bare `HP:0001250`, which tells a patient nothing.
        if label:
            features.append(label)
        if len(features) >= max_features:
            break

    return DiseaseCard(
        disease_id=disease_id,
        name=name,
        features=features,
        feature_count=len(terms),
    )


def lookup_diseases(
    index: SemSimIndex | None,
    hpo: HpoIndex | None,
    queries: Sequence[str],
    *,
    limit: int = MAX_MATCHES,
) -> list[DiseaseCard]:
    """Cards for every disease named in `queries`, deduplicated."""
    if index is None:
        return []
    cards: list[DiseaseCard] = []
    seen: set[str] = set()
    for query in queries:
        for disease_id in find_diseases(index, query, limit=limit):
            if disease_id in seen:
                continue
            seen.add(disease_id)
            card = describe_disease(index, hpo, disease_id)
            if card is not None:
                cards.append(card)
    return cards


def render_disease_cards(cards: Sequence[DiseaseCard]) -> str:
    """The retrieval block for the chat prompt.

    States plainly that this is a reference entry and not a claim about the
    patient — a model handed a list of features next to her case file will
    otherwise be tempted to treat the match as a finding.
    """
    if not cards:
        return "_No matching condition in the reference set._"

    lines = [
        "Reference entries from the Human Phenotype Ontology's disease "
        "annotations. These describe what a condition characteristically "
        "involves IN GENERAL — they say nothing about whether this patient "
        "has it, and must never be presented as findings about her.",
        "",
    ]
    for card in cards:
        lines.append(f"**{card.name}** (`{card.disease_id}`)")
        if card.features:
            shown = len(card.features)
            suffix = f" ({shown} of {card.feature_count} annotated features)"
            lines.append(f"- Characteristically involves{suffix}: {', '.join(card.features)}")
        else:
            lines.append("- No readable feature list for this entry.")
        lines.append("")
    return "\n".join(lines)

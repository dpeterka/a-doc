"""Disease lookup for the chat — what a condition is, and what it looks like.

The differential ledger names conditions the patient has never heard of.
"Relapsing polychondritis" and "primary ovarian insufficiency" are precise and
tell the person whose case file it is nothing at all, which is why
`Hypothesis.plain_language` exists. This answers the next question: *what would
I expect to see with that?*

Built entirely on indexes the image already carries — the phenotype-similarity
index for disease annotations and the HPO index for readable labels. No new
download, no network call, no model.

## Features are ranked by how CHARACTERISTIC they are

Two signals, in order: HPO's curated frequency band, then information content.

Frequency first, because ranking by information content alone answers the
wrong question. IC measures how SPECIFIC a finding is, not how characteristic:
on Marfan syndrome, pure IC put "spontaneous cerebrospinal fluid leak" and
"medial rotation of the medial malleolus" at the top — both genuinely rare,
therefore highly informative, and neither something anyone would recognise the
disease by. Specificity then breaks ties within a frequency band, so a common
finding that is also distinctive outranks a common generic one.

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
from adoc.knowledge.mondo import MondoIndex
from adoc.knowledge.orphadata import OrphaIndex, OrphaRecord, render_record
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

    orpha: OrphaRecord | None = None
    """Orphanet's curated definition, prevalence, onset and inheritance, when
    the disease resolves to an ORPHA code. This is the half a patient actually
    reads — a feature list is useful to a clinician and close to useless to
    the person whose case file it is."""


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
    orpha: OrphaIndex | None = None,
    mondo: MondoIndex | None = None,
) -> DiseaseCard | None:
    """A reference card for one disease, features most-distinctive first."""
    name = index.disease_name(disease_id)
    if name is None:
        return None

    terms = index.disease_terms(disease_id)
    # Frequency band FIRST, then specificity. Ranking by information content
    # alone answers the wrong question: IC measures how specific a finding is,
    # not how characteristic. On Marfan syndrome pure IC led with "spontaneous
    # cerebrospinal fluid leak" and "medial rotation of the medial malleolus"
    # — both genuinely rare, hence highly informative, and neither anything a
    # reader would recognise the disease by.
    ranked = sorted(
        terms,
        key=lambda t: (index.frequency_band(disease_id, t), -index.information_content(t), t),
    )

    features: list[str] = []
    for term in ranked:
        label = hpo.label(term) if hpo is not None else None
        # A term with no label in the HPO index is skipped rather than shown
        # as a bare `HP:0001250`, which tells a patient nothing.
        if label:
            features.append(label)
        if len(features) >= max_features:
            break

    # Orphanet is keyed by ORPHA; a disease found under an OMIM id reaches it
    # through Mondo. Without a Mondo index only an already-ORPHA id resolves,
    # which is graceful degradation rather than failure.
    record: OrphaRecord | None = None
    if orpha is not None:
        code = (
            mondo.orpha_code_for(curie=disease_id, name=name)
            if mondo is not None
            else (disease_id if disease_id.startswith("ORPHA:") else None)
        )
        if code:
            found = orpha.get(code)
            if found is not None and not found.is_empty:
                record = found

    return DiseaseCard(
        disease_id=disease_id,
        name=name,
        features=features,
        feature_count=len(terms),
        orpha=record,
    )


def lookup_diseases(
    index: SemSimIndex | None,
    hpo: HpoIndex | None,
    queries: Sequence[str],
    *,
    limit: int = MAX_MATCHES,
    orpha: OrphaIndex | None = None,
    mondo: MondoIndex | None = None,
) -> list[DiseaseCard]:
    """Cards for every disease named in `queries`, deduplicated."""
    if index is None:
        return []
    cards: list[DiseaseCard] = []
    seen: set[str] = set()
    for query in queries:
        for disease_id in find_diseases(index, query, limit=limit):
            # Deduplicate on IDENTITY, not on id. `OMIM:154700` and
            # `ORPHA:558` are both Marfan syndrome, and showing a reader the
            # same condition twice under two vocabularies — with two
            # different feature lists, because the two annotation sets differ
            # — is exactly the confusion Mondo exists to remove. Falls back to
            # the raw id when there is no Mondo index, which is the behaviour
            # that shipped before.
            identity = (
                mondo.resolve(curie=disease_id, name=index.disease_name(disease_id) or "")
                if mondo is not None
                else None
            ) or disease_id
            if identity in seen:
                continue
            seen.add(identity)
            card = describe_disease(index, hpo, disease_id, orpha=orpha, mondo=mondo)
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
        # Definition first: it is what a reader wants, and the feature list is
        # qualification on it.
        if card.orpha is not None:
            lines += render_record(card.orpha)
        if card.features:
            shown = len(card.features)
            suffix = f" ({shown} of {card.feature_count} annotated features)"
            lines.append(f"- Characteristically involves{suffix}: {', '.join(card.features)}")
        else:
            lines.append("- No readable feature list for this entry.")
        lines.append("")
    return "\n".join(lines)

"""Matching a patient's own words to HPO terms, deterministically.

LIRICAL's input is a list of HPO term ids, and the clinical items of every
criteria scorer need the same thing. Neither can run without a phenotype
profile, and this is how one gets built.

**No model is involved.** "Joint pain" is a listed synonym of
`HP:0002829 Arthralgia`, "night sweats" of `HP:0030166`, and the ontology
ships 26,237 synonyms precisely so that lay phrasing resolves. Asking a model
to guess term ids instead would invite it to invent plausible-looking codes
that do not exist, which is the failure this system keeps having to design
around. A phrase either matches the published vocabulary or it does not.

The index is compact by construction (`scripts/build_hpo_index.py`): 22MB of
ontology reduced to labels and synonyms, ~3MB, baked into the image. Phrases
reaching more than one term are dropped at build time — a wrong phenotype
term propagates straight into a differential engine that ranks diseases by
exactly those terms, so a miss costs recall while a wrong hit costs
correctness.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_CLAUSE_SPLIT_RE = re.compile(r"[.;\n]+")
"""Sentence-ish boundaries a negation cue must not cross. Commas and colons
are deliberately excluded — a list ("no fever, chills, or night sweats") and
a review-of-systems row ("Coma: no") both need the cue to reach across
them."""

MAX_PHRASE_WORDS = 6
"""Longest n-gram considered. The longest useful HPO synonyms run to about
six words; going further multiplies the scan for no additional matches."""

# Cues that the finding is being DENIED. LIRICAL treats an excluded phenotype
# as evidence in its own right, so reading "no joint pain" as arthralgia would
# not merely miss information — it would assert the opposite of what the
# patient said.
_NEGATION_CUES = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "denies",
        "denied",
        "negative",
        "absent",
        "ruled",
        "resolved",
        "free",
    }
)
_NEGATION_WINDOW = 4
"""How many preceding words are searched for a negation cue. Wide enough for
"she has no history of joint pain", narrow enough that a cue from an earlier
clause does not leak forward."""

_TRAILING_NEGATION_CUES = frozenset({"no", "none", "negative", "denied", "denies", "absent", "nil"})
_TRAILING_WINDOW = 2
"""Review-of-systems prose puts the denial AFTER the finding — "Coma: no",
"Chest pain - denied". A backward-only window reads those as positives, and
on the first real run that recorded `HP:0001259 Coma` five times from
checklist rows. Kept narrow: two words is enough for a colon and a "no",
short enough that the next sentence's "no" cannot reach back."""


@dataclass(frozen=True)
class HpoMatch:
    """One phenotype term found in a piece of text."""

    term_id: str
    label: str
    matched_text: str
    context: str = ""
    """A few words either side of the match.

    "Myxedema coma" is a real entity in this patient's history, and HPO has no
    term for the compound — so only "coma" matches and the modifier is lost.
    That is a genuine limitation of phrase matching against a fixed
    vocabulary, and the mitigation is to make it VISIBLE: a reader seeing
    `Coma` in the profile can see it came from "myxedema coma" and judge for
    themselves, instead of finding a bare term with no way back to the text.
    """
    present: bool = True
    """`False` when the surrounding words negate it."""


class HpoIndex:
    """Label/synonym lookup over the Human Phenotype Ontology."""

    def __init__(self, terms: dict[str, str], lookup: dict[str, str]) -> None:
        self._terms = terms
        self._lookup = lookup

    @property
    def size(self) -> int:
        return len(self._terms)

    @classmethod
    def load(cls, path: Path) -> HpoIndex | None:
        """The index at `path`, or `None` when it is absent or unreadable.

        Returning `None` rather than raising: the index is a build artifact,
        and a developer running without it should get phenotype features
        switched off with a warning, not a crashed chat turn.
        """
        if not path.is_file():
            logger.warning("hpo: no index at %s - phenotype matching is disabled", path)
            return None
        try:
            raw = json.loads(path.read_text())
            return cls(raw["terms"], raw["lookup"])
        except Exception as exc:  # noqa: BLE001 - a corrupt index disables the feature
            logger.warning("hpo: could not load index at %s: %s", path, exc)
            return None

    def label(self, term_id: str) -> str | None:
        return self._terms.get(term_id)

    def is_valid(self, term_id: str) -> bool:
        """Whether `term_id` exists in the published ontology."""
        return term_id in self._terms

    def find_terms(self, text: str) -> list[HpoMatch]:
        """Every HPO term named in `text`, longest match first.

        Longest-match-wins matters clinically: "joint pain" and "pain" would
        both match something, and the specific term is the one worth
        recording. Once a span is claimed by a longer phrase it is not
        re-matched by a shorter one inside it.

        Matching runs CLAUSE BY CLAUSE. Stripping punctuation before looking
        for negation lets a cue cross a sentence boundary: "ROS: Coma: no.
        Headache: yes." recorded headache as ABSENT, because "no" was three
        words back once the full stop vanished. Clause boundaries are `.`,
        `;` and newlines only — commas and colons stay inside, so "no fever,
        chills, or night sweats" still negates all three and "Coma: no" still
        negates the one.
        """
        matches: list[HpoMatch] = []
        for clause in _CLAUSE_SPLIT_RE.split(text):
            matches.extend(self._find_in_clause(clause))
        return matches

    def _find_in_clause(self, text: str) -> list[HpoMatch]:
        words = _NON_ALNUM.sub(" ", text.lower()).split()
        claimed: set[int] = set()
        matches: list[HpoMatch] = []

        for length in range(MAX_PHRASE_WORDS, 0, -1):
            for start in range(len(words) - length + 1):
                span = range(start, start + length)
                if any(i in claimed for i in span):
                    continue
                phrase = " ".join(words[start : start + length])
                term_id = self._lookup.get(phrase)
                if term_id is None:
                    continue
                claimed.update(span)
                matches.append(
                    HpoMatch(
                        term_id=term_id,
                        label=self._terms.get(term_id, phrase),
                        matched_text=phrase,
                        context=" ".join(
                            words[max(0, start - 3) : min(len(words), start + length + 3)]
                        ),
                        present=not _is_negated(words, start, start + length),
                    )
                )
        return matches


def _is_negated(words: list[str], start: int, end: int) -> bool:
    """Whether a negation cue sits just before OR just after the phrase."""
    before = words[max(0, start - _NEGATION_WINDOW) : start]
    if any(word in _NEGATION_CUES for word in before):
        return True
    after = words[end : end + _TRAILING_WINDOW]
    return any(word in _TRAILING_NEGATION_CUES for word in after)

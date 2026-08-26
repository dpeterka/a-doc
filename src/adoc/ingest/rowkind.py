"""Is an extracted row a measurement, or a sentence? (ADR 0025)

A narrative report — a DEXA/FRAX summary, an imaging impression — puts its
numbers inside prose: "Left total hip: A statistically significant decrease
of 6.7%". The extractor transcribed rows like that as lab results, so the
store ended up holding sentences as analyte names. Four separate downstream
guards were then narrowed to tolerate them (ADR 0016 x3, 0023, 0024, PR
#151) — every one of those changes made a *reader* more forgiving instead of
keeping the bad row out.

This is the gate that keeps it out. Deterministic and model-free, per
CLAUDE.md's rule that validation is plain code.

Conservative by construction: a row is diverted only when its name reads as
a SENTENCE, never merely because it is long. Real analyte names get long and
strange — "FRAX analysis shows 10-year probability of major osteoporotic
fracture (clinical spine, forearm, hip or shoulder)" is a genuine measure,
and "B. MIYAMOTOI AB (IGG)" and "% SATURATION" are genuine names. A false
divert silently loses a real result, which is worse than the tolerated
noise, so every rule here keys on positive evidence of prose.
"""

from __future__ import annotations

import re
from typing import Literal

RowKind = Literal["quantitative", "qualitative", "narrative", "empty"]

# A comparator-bearing result: "<20", ">= 150", "< 0.10". The numeric content
# is real and belongs in `value`; the comparator says it is a BOUND rather
# than a point measurement (ADR 0025).
COMPARATOR_VALUE_RE = re.compile(r"^\s*(<=|>=|=<|=>|<|>)\s*([\d,]*\.?\d+)\s*(.*?)\s*$")

_COMPARATOR_CANONICAL = {"<": "<", ">": ">", "<=": "<=", ">=": ">=", "=<": "<=", "=>": ">="}

# Finite reporting verbs and prose connectives. Their presence as a WORD in
# an analyte name is strong evidence the extractor captured a sentence: an
# analyte is a noun phrase, and noun phrases do not assert.
_PROSE_VERBS = frozenset(
    {
        "shows",
        "showed",
        "show",
        "indicates",
        "indicated",
        "suggests",
        "suggested",
        "demonstrates",
        "demonstrated",
        "reveals",
        "revealed",
        "measured",
        "measures",
        "compared",
        "increased",
        "decreased",
        "decrease",
        "increase",
        "remains",
        "remained",
        "represents",
        "consistent",
        "noted",
        "reported",
    }
)

# A name opening with an article is a sentence fragment ("The BMD measured"),
# never an analyte label.
_LEADING_ARTICLES = frozenset({"the", "a", "an", "this", "there"})

# Tokens that mark a genuine measurement even inside a long or odd name.
# Their presence VETOES a narrative verdict, so a real long measure name is
# never diverted.
_MEASURE_TOKENS = frozenset(
    {
        "bmd",
        "t-score",
        "z-score",
        "score",
        "probability",
        "ratio",
        "index",
        "titer",
        "count",
        "absolute",
        "igg",
        "igm",
        "ige",
        "iga",
        "ab",
        "antibody",
        "antigen",
        "level",
        "concentration",
        "saturation",
        "volume",
        "rate",
        "clearance",
        "gfr",
    }
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")

# A clause colon: prose on the right-hand side of a colon that itself
# contains an article or a prose verb ("Left total hip: A statistically
# significant decrease of"). A plain qualifier colon ("HDL: direct") is not
# prose and must survive.
_CLAUSE_COLON_RE = re.compile(r":\s*(.+)$")


def parse_comparator_value(text: str) -> tuple[str, float, str] | None:
    """Split a comparator-bearing result into `(comparator, value, rest)`.

    `"<20"` -> `("<", 20.0, "")`; `">= 150 mg/dL"` -> `(">=", 150.0,
    "mg/dL")`. Returns `None` when `text` is not comparator-shaped, so a
    qualitative result is never coerced into a number.
    """
    match = COMPARATOR_VALUE_RE.match(text)
    if match is None:
        return None
    raw_comparator, number, rest = match.groups()
    # A TITER is not a scalar. "<1:256" would otherwise parse as the number
    # 1 with a leftover ":256" — storing `value=1.0` for a titer of <1:256
    # is silent data corruption, and titers are exactly the shape a serology
    # panel is full of. Titer semantics live in `value_text` (and are
    # stripped by the readers' `_TITER_RE`), so leave them there.
    if rest.startswith(":"):
        return None
    canonical = _COMPARATOR_CANONICAL.get(raw_comparator)
    if canonical is None:  # pragma: no cover - regex only matches known tokens
        return None
    try:
        value = float(number.replace(",", ""))
    except ValueError:  # pragma: no cover - regex guarantees a numeric body
        return None
    return canonical, value, rest.strip()


def name_reads_as_prose(name: str) -> bool:
    """Does `name` read as a sentence rather than an analyte label?

    Checked AFTER `reconcile.clean_result_name` has stripped trailing
    fragments, so this is about names cleaning cannot rescue.
    """
    words = [w.lower() for w in _WORD_RE.findall(name)]
    if not words:
        return False

    # A recognized measure token means this is a measurement however oddly
    # it is phrased. Checked first so it can never be overridden below.
    if any(w in _MEASURE_TOKENS for w in words):
        return False

    if words[0] in _LEADING_ARTICLES:
        return True
    if any(w in _PROSE_VERBS for w in words):
        return True

    clause = _CLAUSE_COLON_RE.search(name)
    if clause is not None:
        tail_words = [w.lower() for w in _WORD_RE.findall(clause.group(1))]
        if tail_words and (
            tail_words[0] in _LEADING_ARTICLES or any(w in _PROSE_VERBS for w in tail_words)
        ):
            return True
    return False


def classify_extracted_row(name: str, value: float | None, value_text: str | None) -> RowKind:
    """One of `quantitative` / `qualitative` / `narrative` (ADR 0025).

    `narrative` rows are NOT lab results: the caller routes them to
    `DocumentExtraction.narrative_findings`, where they stay citable as
    `doc:<file>#p<n>` and available for retrieval, rather than becoming a
    row whose analyte name is a sentence.
    """
    text = (value_text or "").strip()
    # A row with NEITHER a number nor result text is not a result at all —
    # the extractor transcribed a label with nothing beside it (a section
    # heading, a blank table cell). Classifying it `qualitative` and keeping
    # it crashed a real 97-document backfill on document 100: `LabResult`
    # requires one of value/value_text, and the unhandled ValidationError
    # took every remaining document down with it.
    if value is None and not text:
        return "empty"
    if name_reads_as_prose(name):
        return "narrative"
    if value is not None:
        return "quantitative"
    if parse_comparator_value(text) is not None:
        return "quantitative"
    return "qualitative"

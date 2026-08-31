"""StatPearls lookup — a clinical review paragraph, verbatim and attributed.

The other knowledge sources each answer a narrow question. Orphanet gives a
one-paragraph definition; HPO gives characteristic features; PubMed gives real
citations. None of them explains a condition the way a clinical review does,
and that is the gap this fills.

Read-only FTS5 over an index built at image-build time
(`scripts/build_statpearls_index.py`), holding the title and lead sections of
roughly 12,500 articles. No network call at query time — the same reasoning
ADR 0029 applied to LIRICAL's data.

## The licence is a functional constraint, not a footnote

CC BY-NC-ND 4.0.

**Attribution** is required, so every rendered excerpt carries it and the
Bookshelf link. That is not politeness; omitting it would breach the terms.

**No-derivatives** is why excerpts are stored and rendered VERBATIM, with an
explicit instruction that they are quoted material. A model that paraphrases
quoted text into its own prose is producing adapted material, so the prompt
block says plainly that these are quotations to be attributed rather than
absorbed.

**Non-commercial** is satisfied by what this is: one patient's private tool.

## What it deliberately does not index

The build keeps introduction, etiology, epidemiology, pathophysiology, history
and physical, and evaluation. It drops treatment, management, nursing and quiz
sections — a read-only informational tool has no business surfacing those, and
`reason.safety`'s output gate would strip most of it anyway. Better not to
retrieve it at all than to retrieve it and gate it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import urllib.parse
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ATTRIBUTION = (
    "StatPearls, StatPearls Publishing. CC BY-NC-ND 4.0 "
    "(https://creativecommons.org/licenses/by-nc-nd/4.0/)"
)

# A Bookshelf SEARCH on the title, not a deep link. The archive's NXML carries
# no NBK id and none is derivable from the `article-NNNNN.nxml` filename, so a
# per-article URL is not available. A search on the exact title lands the
# reader on the article; inventing an id to build a direct URL would produce
# links that 404, which is worse than a search.
BOOKSHELF_SEARCH_URL = "https://www.ncbi.nlm.nih.gov/books/?term={query}"

# One article is a paragraph of orientation; three is a wall. The retrieval
# block competes with the case file for the model's attention, and the case
# file must win.
MAX_ARTICLES = 2

# Characters of body text per article. Enough to be useful, short enough that
# two of them do not crowd out everything else in the prompt.
MAX_EXCERPT_CHARS = 700

# Candidates fetched before re-ranking, over and above what the caller asked
# for. The relevance filter and the exact-title preference both run in Python,
# so applying the caller's LIMIT in SQL would hide the row they need: a query
# for "Granulomatosis with polyangiitis" with LIMIT 1 returned only the
# EOSINOPHILIC article — a different disease — leaving nothing to re-rank.
_CANDIDATE_POOL = 10


class StatPearlsArticle(BaseModel):
    """One matched article."""

    title: str
    excerpt: str
    """Verbatim text from the article. Never rewritten — see the licence note
    in the module docstring."""

    @property
    def url(self) -> str:
        """Where to read the whole thing. Satisfies the attribution term."""
        return BOOKSHELF_SEARCH_URL.format(query=urllib.parse.quote_plus(self.title))


class StatPearlsResult(BaseModel):
    articles: list[StatPearlsArticle] = Field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# Words that vary between sources and so must not be REQUIRED in a match.
# StatPearls titles its Sjogren article "Sjogren Disease" while the ledger and
# Orphanet both say "syndrome"; ANDing every word forced the match into body
# text, where "Physiology, Exocrine Gland" and "Shrinking Lung Syndrome"
# outranked the actual article. The distinctive word is the one to search on.
_GENERIC_WORDS = frozenset(
    {
        "syndrome",
        "disease",
        "disorder",
        "deficiency",
        "insufficiency",
        "primary",
        "secondary",
        "chronic",
        "acute",
        "idiopathic",
        "familial",
        "congenital",
        "type",
        "and",
        "the",
        "with",
    }
)

_MIN_WORD_CHARS = 4


def _words(text: str) -> list[str]:
    """Searchable words, punctuation removed.

    FTS5 treats `-`, `*`, `"`, `:` and `^` as operators, so "Sjögren's
    syndrome" or "alpha-gal" is a syntax error rather than a search. Stripping
    to alphanumerics and quoting each word sidesteps the grammar entirely —
    this takes a disease NAME, never a user-authored query language.
    """
    # Diacritics are folded here rather than relied on from the tokenizer.
    # "Sjögren's syndrome" found nothing while "Sjogren syndrome" found the
    # article, so the index and the query were not agreeing on the fold. Doing
    # it explicitly matches `knowledge.mondo` and `knowledge.lirical_
    # divergence`, and removes the dependency on FTS5 tokenizer options.
    folded = text.lower()
    for accented, plain in (("ö", "o"), ("é", "e"), ("è", "e"), ("ü", "u"), ("ä", "a"), ("ç", "c")):
        folded = folded.replace(accented, plain)
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in folded)
    return [w for w in cleaned.split() if len(w) >= _MIN_WORD_CHARS]


def build_queries(name: str) -> list[str]:
    """FTS5 queries to try in order, most precise first.

    Three passes, because one query cannot be both precise and forgiving:

    1. the distinctive words in the TITLE — an article about the condition
    2. the distinctive words anywhere — an article that covers it
    3. every word anywhere — the last resort before giving up

    Returning a list rather than one clever query keeps each pass readable and
    makes the fallback order explicit.
    """
    words = _words(name)
    if not words:
        return []
    distinctive = [w for w in words if w.lower() not in _GENERIC_WORDS] or words

    # MOST PRECISE FIRST. Ordering the loose pass first was a regression:
    # "insufficiency" is in the generic list, so "Primary ovarian
    # insufficiency" pruned to "ovarian" alone and `title:"ovarian"` matched
    # "Embryology, Ovarian Follicle Development" instead of the article that
    # carries the exact name. Trying the full name first gets that right, and
    # the looser passes still rescue "Sjogren syndrome" against StatPearls'
    # "Sjogren Disease".
    queries = [
        " AND ".join(f'title:"{w}"' for w in words),
        " AND ".join(f'title:"{w}"' for w in distinctive),
        " AND ".join(f'"{w}"' for w in words),
        " AND ".join(f'"{w}"' for w in distinctive),
    ]
    unique: list[str] = []
    for query in queries:
        if query and query not in unique:
            unique.append(query)
    return unique


def _prefer_exact(rows: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    """Move an exactly-named article to the front.

    bm25 ranks "Eosinophilic Granulomatosis With Polyangiitis" above
    "Granulomatosis With Polyangiitis" for a query naming the latter, because
    both titles contain every query word and the longer one scored higher.
    Those are DIFFERENT diseases — EGPA is Churg-Strauss — so returning one for
    the other is a clinical error, not a ranking nicety.

    An exact match on the normalised words wins outright; otherwise the
    shortest title wins, since a longer title carries qualifiers the query did
    not ask for.
    """
    target = _words(name)
    if not target:
        return rows

    def key(row: tuple[str, str]) -> tuple[int, int]:
        title_words = _words(row[0])
        return (0 if title_words == target else 1, len(row[0]))

    return sorted(rows, key=key)


def _is_relevant(title: str, name: str) -> bool:
    """Whether a matched title plausibly concerns `name`.

    A floor, not a ranking. Without it a nonsense query returned a
    confident-looking article: "Not A Real Disease" matched "Orthopedic
    Fluoroscopy", because the body says "real-time". Requiring at least one
    distinctive word of the query to appear in the TITLE costs nothing and
    turns that into an honest no-match.
    """
    words = _words(name)
    distinctive = [w for w in words if w.lower() not in _GENERIC_WORDS] or words
    lowered = title.lower()
    return any(w.lower() in lowered for w in distinctive)


class StatPearlsIndex:
    """Read-only FTS5 search over the lead-section index."""

    def __init__(self, path: Path) -> None:
        # Read-only URI so a query can never write to a build artifact, and
        # `immutable` because it never changes after the image is built.
        #
        # `check_same_thread=False` only DISABLES sqlite3's own same-thread
        # guard; it does not make sharing safe. The lock is what does that.
        # This index is an `lru_cache` singleton queried from sync FastAPI
        # routes, which run in a threadpool, so two requests can drive this
        # one connection at the same moment — and `labs.db.LabsDb` carries a
        # long comment about the production crash that exact situation
        # produced there (`sqlite3.InterfaceError: bad parameter or other API
        # misuse`, while three routes were served in the same second). Same
        # pattern, same guard.
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            f"file:{path}?immutable=1&mode=ro", uri=True, check_same_thread=False
        )

    def search(self, name: str, *, limit: int = MAX_ARTICLES) -> StatPearlsResult:
        """Articles matching a disease name, best match first.

        Never raises: a malformed query or a corrupt index degrades to no
        result, and a chat turn continues without this block.
        """
        queries = build_queries(name)
        if not queries:
            return StatPearlsResult(error="nothing searchable in that name")

        rows: list[tuple[str, str]] = []
        for query in queries:
            try:
                with self._lock:
                    rows = self._connection.execute(
                        # bm25 with the title weighted far above the body:
                        # an article ABOUT the condition beats one that merely
                        # mentions it in passing.
                        "SELECT title, body FROM articles "
                        "WHERE articles MATCH ? ORDER BY bm25(articles, 10.0, 1.0) LIMIT ?",
                        (query, limit + _CANDIDATE_POOL),
                    ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("statpearls: query failed for %r: %s", query, exc)
                continue
            rows = [r for r in rows if _is_relevant(r[0], name)]
            if rows:
                rows = _prefer_exact(rows, name)[:limit]
                break

        return StatPearlsResult(
            articles=[
                StatPearlsArticle(title=title, excerpt=body[:MAX_EXCERPT_CHARS])
                for title, body in rows
            ]
        )


@lru_cache(maxsize=2)
def load_statpearls_index(path: Path) -> StatPearlsIndex | None:
    """Open the index, or `None` if it is absent or unopenable.

    Absent is ordinary: it is a build artifact and a local checkout will not
    have one.
    """
    if not path.exists():
        logger.info("statpearls: no index at %s; review lookup is off", path)
        return None
    try:
        return StatPearlsIndex(path)
    except Exception as exc:  # noqa: BLE001 - never fail a turn over a reference index
        logger.warning("statpearls: could not open index at %s: %s", path, exc)
        return None


def render_articles(result: StatPearlsResult) -> str:
    """The retrieval block.

    Says explicitly that the text is quoted, because a model handed prose
    beside a case file will otherwise fold it into its own voice — which is
    both a licence problem under no-derivatives and a provenance problem, since
    the patient could no longer tell what came from a review and what came
    from her own record.
    """
    if not result.ok or not result.articles:
        return "_No clinical review article on file for that condition._"

    lines = [
        "QUOTED clinical review text. Reproduce or summarise it as a quotation "
        "attributed to StatPearls; do not restate it as your own conclusion, and "
        "do not present it as a finding about this patient.",
        "",
    ]
    for article in result.articles:
        lines.append(f"**{article.title}**")
        lines.append(f"> {article.excerpt}")
        if article.url:
            lines.append(f"- Full article: {article.url}")
        lines.append("")
    lines.append(f"_Source: {ATTRIBUTION}_")
    return "\n".join(lines)

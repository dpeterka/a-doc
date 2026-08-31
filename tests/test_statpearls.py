"""StatPearls clinical-review lookup.

Every test here corresponds to something that was actually wrong when the
index was first queried against the real 8,316-article corpus. Retrieval bugs
in this module are clinical errors, not ranking niceties — returning the
eosinophilic variant of a vasculitis for a query about the plain one names a
different disease.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from adoc.knowledge.statpearls import (
    ATTRIBUTION,
    StatPearlsIndex,
    StatPearlsResult,
    build_queries,
    load_statpearls_index,
    render_articles,
)

_ARTICLES = [
    ("Granulomatosis With Polyangiitis", "Introduction. A necrotising vasculitis."),
    (
        "Eosinophilic Granulomatosis With Polyangiitis (Churg-Strauss Syndrome)",
        "Introduction. Eosinophilic vasculitis with asthma.",
    ),
    ("Sjogren Disease", "Introduction. An autoimmune exocrinopathy causing dry eyes."),
    ("Primary Ovarian Insufficiency", "Introduction. Loss of ovarian function before 40."),
    ("Physiology, Exocrine Gland", "Introduction. Sjogren syndrome is one cause of dysfunction."),
    ("Orthopedic Fluoroscopy", "Introduction. Real-time imaging in the operating theatre."),
]


def _index(tmp_path: Path) -> StatPearlsIndex:
    path = tmp_path / "sp.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE VIRTUAL TABLE articles USING fts5(title, body, tokenize='porter unicode61')"
    )
    connection.executemany("INSERT INTO articles (title, body) VALUES (?, ?)", _ARTICLES)
    connection.commit()
    connection.close()
    return StatPearlsIndex(path)


# -- the clinical distinction ------------------------------------------------


def test_the_plain_vasculitis_is_not_answered_with_the_eosinophilic_one(tmp_path: Path) -> None:
    """EGPA is Churg-Strauss — a different disease. bm25 ranked the longer
    title higher because both contain every query word, and the SQL LIMIT then
    returned only that one, leaving nothing to re-rank."""
    result = _index(tmp_path).search("Granulomatosis with polyangiitis", limit=1)

    assert [a.title for a in result.articles] == ["Granulomatosis With Polyangiitis"]


def test_the_eosinophilic_variant_is_still_reachable_by_name(tmp_path: Path) -> None:
    """Preferring the exact title must not make the more specific article
    unreachable when it is what was asked for."""
    result = _index(tmp_path).search("Eosinophilic granulomatosis with polyangiitis", limit=1)

    assert "Eosinophilic" in result.articles[0].title


# -- name variation ---------------------------------------------------------


def test_a_generic_suffix_may_differ_between_sources(tmp_path: Path) -> None:
    """StatPearls titles it "Sjogren Disease"; the ledger and Orphanet both say
    "syndrome". Requiring every word forced the match into body text, where
    "Physiology, Exocrine Gland" outranked the actual article."""
    result = _index(tmp_path).search("Sjogren syndrome", limit=1)

    assert result.articles[0].title == "Sjogren Disease"


def test_diacritics_do_not_break_a_match(tmp_path: Path) -> None:
    """ "Sjögren's syndrome" found nothing while "Sjogren syndrome" found the
    article, so the index and query were not agreeing on the fold."""
    result = _index(tmp_path).search("Sjögren's syndrome, primary", limit=1)

    assert result.articles[0].title == "Sjogren Disease"


def test_a_name_whose_every_word_looks_generic_still_matches(tmp_path: Path) -> None:
    """ "insufficiency" and "primary" are both in the generic list, which left
    "Primary ovarian insufficiency" pruned to "ovarian" alone and matching the
    wrong article. Trying the full name first fixes it."""
    result = _index(tmp_path).search("Primary ovarian insufficiency", limit=1)

    assert result.articles[0].title == "Primary Ovarian Insufficiency"


# -- refusing to answer -----------------------------------------------------


def test_a_nonsense_query_returns_nothing(tmp_path: Path) -> None:
    """ "Not A Real Disease" matched "Orthopedic Fluoroscopy", because the body
    says "real-time". A confident-looking wrong article is worse than none."""
    assert _index(tmp_path).search("Not A Real Disease").articles == []


def test_an_unsearchable_query_is_reported(tmp_path: Path) -> None:
    result = _index(tmp_path).search("a b c")

    assert not result.ok
    assert result.articles == []


# -- query construction -----------------------------------------------------


def test_queries_run_most_precise_first() -> None:
    """Ordering the loose pass first was a regression; the full name is tried
    before the pruned one."""
    queries = build_queries("Primary ovarian insufficiency")

    assert queries[0].count("title:") == 3
    assert any(q == 'title:"ovarian"' for q in queries)


def test_punctuation_cannot_become_fts_syntax() -> None:
    """FTS5 treats `-`, `*`, `"` and `:` as operators, so an unescaped disease
    name is a syntax error rather than a search."""
    for name in ["alpha-gal syndrome", 'Sjögren"s', "a*b:c^d"]:
        for query in build_queries(name):
            assert "*" not in query
            assert "^" not in query


# -- rendering and licence --------------------------------------------------


def test_the_rendering_carries_attribution_and_a_link(tmp_path: Path) -> None:
    """CC BY-NC-ND: attribution is a licence condition, not politeness."""
    text = render_articles(_index(tmp_path).search("Sjogren syndrome", limit=1))

    assert ATTRIBUTION in text
    assert "ncbi.nlm.nih.gov/books" in text


def test_the_rendering_marks_the_text_as_quoted(tmp_path: Path) -> None:
    """No-derivatives: a model that folds quoted prose into its own voice is
    producing adapted material, and the patient can no longer tell what came
    from a review and what came from her own record."""
    text = render_articles(_index(tmp_path).search("Sjogren syndrome", limit=1))

    assert "QUOTED" in text
    assert "not present it as a finding about this patient" in text


def test_no_match_renders_plainly() -> None:
    assert "No clinical review article" in render_articles(StatPearlsResult())


# -- loading ----------------------------------------------------------------


def test_a_missing_index_is_not_an_error(tmp_path: Path) -> None:
    assert load_statpearls_index(tmp_path / "absent.sqlite") is None

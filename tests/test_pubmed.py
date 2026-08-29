"""PubMed search (`knowledge/pubmed.py`).

PMID verification already existed; search did not, which is why "every
literature claim carries a PMID" was unmet. A model asked for supporting
literature could only recall citations from training, and a recalled citation
is fabricated often enough that the citation checker exists to catch it. These
tests pin the properties that make search the fix: citations come from NCBI,
one bad record cannot cost the rest, and a failed call degrades rather than
raising into a review.

No test here touches the network — the transport is injected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adoc.knowledge.pubmed import (
    SEARCH_CACHE_TTL_DAYS,
    NcbiRateLimiter,
    PubMedClient,
    build_query,
    fetch_with_retry,
    parse_esearch,
    parse_esummary,
)


def _esearch(pmids: list[str], total: int) -> bytes:
    return json.dumps({"esearchresult": {"idlist": pmids, "count": str(total)}}).encode()


def _esummary(records: dict[str, dict]) -> bytes:
    return json.dumps({"result": {"uids": list(records), **records}}).encode()


_ARTICLE = {
    "title": "Sjogren syndrome: a review.",
    "fulljournalname": "The Lancet",
    "pubdate": "2021 Mar 15",
    "authors": [{"name": "Smith J"}, {"name": "Jones A"}],
    "elocationid": "doi: 10.1016/example",
}


def _client(tmp_path: Path, responses: list[bytes], calls: list[str] | None = None) -> PubMedClient:
    queue = list(responses)

    def transport(url: str) -> bytes:
        if calls is not None:
            calls.append(url)
        if not queue:
            raise AssertionError("transport called more times than the test expected")
        return queue.pop(0)

    return PubMedClient(
        tmp_path / "pubmed-cache.json",
        transport=transport,
        sleep=lambda _s: None,
    )


# -- query building ---------------------------------------------------------


def test_query_is_built_deterministically() -> None:
    """Query construction is plain code, not model-authored text: a model
    writing its own search string can quietly narrow a query until it returns
    only what it already believed, and nothing downstream could tell."""
    query = build_query("Sjogren syndrome")

    assert '"Sjogren syndrome"[Title/Abstract]' in query
    assert "review[Publication Type]" in query
    assert "humans[MeSH Terms]" in query


def test_an_empty_topic_makes_no_query() -> None:
    assert build_query("   ") == ""


# -- parsing ----------------------------------------------------------------


def test_esearch_parses_ids_and_total() -> None:
    """`total` is PubMed's own count and is usually far larger than the ids
    returned. "9 of 2,431 shown" and "9 results exist" are very different
    claims for a reasoning stage to make."""
    pmids, total = parse_esearch(_esearch(["111", "222"], 2431))

    assert pmids == ["111", "222"]
    assert total == 2431


def test_a_malformed_esearch_body_is_empty_not_an_exception() -> None:
    """NCBI returns a 200 with an error body often enough that raising would
    make transient upstream noise indistinguishable from a real bug."""
    assert parse_esearch(b"<html>service unavailable</html>") == ([], 0)
    assert parse_esearch(b'{"unexpected": true}') == ([], 0)


def test_esummary_keeps_the_search_ranking() -> None:
    """esearch returns by relevance and esummary keys by uid, so reading the
    response's own ordering would silently discard the ranking."""
    payload = _esummary({"111": dict(_ARTICLE), "222": dict(_ARTICLE)})

    articles = parse_esummary(payload, ["222", "111"])

    assert [a.pmid for a in articles] == ["222", "111"]


def test_one_bad_record_does_not_cost_the_others() -> None:
    """ADR 0028: no single item may fail a payload. One malformed summary must
    not throw away the other citations."""
    payload = _esummary({"111": {"error": "cannot get document summary"}, "222": dict(_ARTICLE)})

    articles = parse_esummary(payload, ["111", "222"])

    assert [a.pmid for a in articles] == ["222"]


def test_article_fields_are_extracted() -> None:
    articles = parse_esummary(_esummary({"111": dict(_ARTICLE)}), ["111"])

    article = articles[0]
    assert article.title == "Sjogren syndrome: a review."
    assert article.journal == "The Lancet"
    assert article.year == 2021
    assert article.authors == ["Smith J", "Jones A"]
    assert article.doi == "10.1016/example"
    assert article.citation_ref == "pmid:111"
    assert "Smith J et al." in article.short_citation()


# -- searching --------------------------------------------------------------


def test_search_returns_real_articles(tmp_path: Path) -> None:
    """The point of the module: citations come FROM PubMed, so a PMID is real
    by construction rather than recalled by a model."""
    client = _client(tmp_path, [_esearch(["111"], 12), _esummary({"111": dict(_ARTICLE)})])

    result = client.search(build_query("Sjogren syndrome"))

    assert result.ok
    assert result.total == 12
    assert [a.citation_ref for a in result.articles] == ["pmid:111"]


def test_a_failed_search_is_reported_not_raised(tmp_path: Path) -> None:
    """A literature refresh must degrade to "no citations" rather than take a
    whole review down with it."""

    def transport(_url: str) -> bytes:
        raise TimeoutError("ncbi is down")

    client = PubMedClient(tmp_path / "c.json", transport=transport, sleep=lambda _s: None)
    result = client.search(build_query("Sjogren syndrome"))

    assert not result.ok
    assert "TimeoutError" in result.error
    assert result.articles == []


def test_a_failed_search_is_never_cached(tmp_path: Path) -> None:
    """Same rule the PMID verifier follows: an NCBI outage must not freeze an
    empty result in place for a week."""
    cache = tmp_path / "c.json"
    calls: list[str] = []

    def flaky(url: str) -> bytes:
        calls.append(url)
        raise TimeoutError("down")

    PubMedClient(cache, transport=flaky, sleep=lambda _s: None).search("q[Title/Abstract]")
    assert not cache.exists() or "q[Title/Abstract]" not in cache.read_text()

    # A later, working call must actually go out rather than read a cached failure.
    client = _client(tmp_path, [_esearch(["111"], 1), _esummary({"111": dict(_ARTICLE)})])
    client._cache_path = cache
    assert client.search("q[Title/Abstract]").ok


def test_a_repeat_search_is_served_from_cache(tmp_path: Path) -> None:
    calls: list[str] = []
    client = _client(
        tmp_path, [_esearch(["111"], 1), _esummary({"111": dict(_ARTICLE)})], calls=calls
    )

    first = client.search("q[Title/Abstract]")
    second = client.search("q[Title/Abstract]")

    assert first.articles[0].pmid == second.articles[0].pmid
    assert len(calls) == 2  # esearch + esummary, once — not twice


def test_a_stale_cache_entry_is_refetched(tmp_path: Path) -> None:
    """A PMID that resolves once resolves forever; a QUERY gains new results as
    papers are indexed, so its answer goes stale."""
    cache = tmp_path / "c.json"
    stale = datetime.now(UTC) - timedelta(days=SEARCH_CACHE_TTL_DAYS + 1)
    cache.write_text(
        json.dumps(
            {
                "q[Title/Abstract]|5": {
                    "cached_at": stale.isoformat(),
                    "result": {"query": "q[Title/Abstract]", "total": 1, "articles": []},
                }
            }
        )
    )
    client = _client(tmp_path, [_esearch(["222"], 9), _esummary({"222": dict(_ARTICLE)})])
    client._cache_path = cache

    result = client.search("q[Title/Abstract]")

    assert result.total == 9
    assert [a.pmid for a in result.articles] == ["222"]


def test_zero_results_still_returns_cleanly(tmp_path: Path) -> None:
    client = _client(tmp_path, [_esearch([], 0)])

    result = client.search("nothing[Title/Abstract]")

    assert result.ok
    assert result.total == 0
    assert result.articles == []


def test_search_topic_falls_back_when_reviews_find_nothing(tmp_path: Path) -> None:
    """A rare disease can genuinely have no review articles. Returning zero
    citations for it while the primary literature exists would be the worst of
    both worlds."""
    client = _client(
        tmp_path,
        [
            _esearch([], 0),  # filtered search: no reviews
            _esearch(["333"], 4),  # unfiltered retry
            _esummary({"333": dict(_ARTICLE)}),
        ],
    )

    result = client.search_topic("Vexas syndrome")

    assert [a.pmid for a in result.articles] == ["333"]


def test_requests_identify_the_tool_to_ncbi(tmp_path: Path) -> None:
    """NCBI asks that requests identify themselves; this is a term of use, not
    a nicety."""
    calls: list[str] = []
    client = _client(tmp_path, [_esearch([], 0)], calls=calls)

    client.search("q[Title/Abstract]")

    assert "tool=a-doc" in calls[0]


def test_the_rate_limit_is_honoured(tmp_path: Path) -> None:
    """At most 3 requests/second without an API key."""
    slept: list[float] = []
    queue = [_esearch([], 0), _esearch([], 0)]

    client = PubMedClient(
        tmp_path / "c.json",
        transport=lambda _u: queue.pop(0),
        sleep=slept.append,
    )
    client.search("a[Title/Abstract]")
    client.search("b[Title/Abstract]")

    assert slept, "the second request went out with no throttle"
    assert slept[0] <= 1.0, "the throttle should pace requests, not stall them"


def test_a_citation_reads_cleanly() -> None:
    """A live NCBI response produced "Baldini C et al.." — author names and
    journal titles arrive with their own trailing punctuation, so the joiner
    has to strip before it adds."""
    articles = parse_esummary(
        _esummary({"111": {**_ARTICLE, "fulljournalname": "Nature reviews. Rheumatology"}}), ["111"]
    )

    citation = articles[0].short_citation()
    assert "et al.." not in citation
    assert citation.startswith("Smith J et al. ")
    assert citation.endswith("(2021)")


def test_search_and_verification_share_one_throttle() -> None:
    """Two independent 3/second limiters produce 6/second between them — over
    the published anonymous limit, which is how a real PMID ends up recorded
    `unverifiable` for no better reason than our own traffic. Observed live:
    one of two freshly-searched PMIDs came back "error" because the search
    that produced it had just spent the budget."""
    import adoc.reason.citations as citations

    assert citations.fetch_with_retry is fetch_with_retry


def test_the_limiter_waits_between_calls() -> None:
    slept: list[float] = []
    limiter = NcbiRateLimiter(sleep=slept.append)

    limiter.wait()
    limiter.wait()

    assert slept and 0 < slept[0] <= 1.0


def test_an_api_key_raises_the_allowed_rate() -> None:
    """NCBI permits 10/second with a key, 3/second without."""
    without: list[float] = []
    NcbiRateLimiter(sleep=without.append).wait()
    limiter_a = NcbiRateLimiter(sleep=without.append)
    limiter_a.wait()
    limiter_a.wait()

    with_key: list[float] = []
    limiter_b = NcbiRateLimiter(sleep=with_key.append)
    limiter_b.wait(has_api_key=True)
    limiter_b.wait(has_api_key=True)

    assert with_key[0] < without[-1]


def test_a_burst_rejection_is_retried_not_taken_at_face_value() -> None:
    """NCBI throttling is bursty. A live run at 3.1/second produced two
    spurious `error` verdicts and then an outright HTTPError — without a retry
    each of those costs a citation its verification, silently."""
    attempts: list[str] = []

    def flaky(url: str) -> bytes:
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("429 too many requests")
        return b"ok"

    body = fetch_with_retry(
        flaky,
        "https://example/x",
        limiter=NcbiRateLimiter(sleep=lambda _s: None),
        sleep=lambda _s: None,
    )

    assert body == b"ok"
    assert len(attempts) == 3


def test_a_persistent_failure_still_raises() -> None:
    """Retrying must not turn a real outage into a silent empty success — the
    caller decides whether that is `unverifiable` or an empty result."""

    def always_down(_url: str) -> bytes:
        raise OSError("down")

    try:
        fetch_with_retry(
            always_down,
            "https://example/x",
            limiter=NcbiRateLimiter(sleep=lambda _s: None),
            sleep=lambda _s: None,
        )
    except OSError:
        return
    raise AssertionError("a persistent failure must surface")


def test_the_pacing_leaves_headroom_under_the_published_ceiling() -> None:
    """3/second is NCBI's ceiling, not a target. Sitting exactly on it is what
    got us rejected."""
    slept: list[float] = []
    limiter = NcbiRateLimiter(sleep=slept.append)
    limiter.wait()
    limiter.wait()

    assert slept[0] > 1.0 / 3.0, "pacing at the ceiling leaves no headroom"

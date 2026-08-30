"""PubMed search over NCBI E-utilities (PLAN.md phase 3, `knowledge/pubmed.py`).

PMID *verification* already existed (`reason.citations.EutilsPmidVerifier`);
search did not, which is why the acceptance criterion "every literature claim
carries a PMID" was unmet. A model asked for supporting literature could only
recall citations from training, and a recalled citation is a fabricated one
often enough that the citation checker exists to catch it.

This module inverts that. Citations come FROM PubMed rather than from a model,
so a PMID is real by construction and the checker becomes a backstop rather
than the only defence. Nothing here asks a model for anything.

Stdlib `urllib` only, matching the verifier — CLAUDE.md's no-new-runtime-deps
rule. Transport is injectable so tests never touch the network.

NCBI's terms: at most 3 requests/second without an API key (10 with one), and
requests should identify themselves with `tool` and `email`. Both are honoured
below; the rate limit is enforced by this process, which is enough because the
web service runs a single task (CLAUDE.md "Infrastructure") and reviews run
one at a time.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

PubMedTransport = Callable[[str], bytes]

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_ESEARCH_URL = f"{_EUTILS_BASE}/esearch.fcgi"
_ESUMMARY_URL = f"{_EUTILS_BASE}/esummary.fcgi"

PUBMED_CACHE_RELPATH = "work/pubmed-cache.json"

# A search is not a PMID. A PMID that resolves once resolves forever, so the
# verifier caches "found" permanently; a QUERY gains new results as papers are
# indexed, so its answer goes stale. A week keeps a review's literature refresh
# from re-querying on every run while staying current enough to matter.
SEARCH_CACHE_TTL_DAYS = 7

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_RETMAX = 5

# NCBI asks that requests identify themselves. `tool` is fixed; `email` is
# configurable because it is the address NCBI would contact about excessive
# use, and hard-coding a personal address into a public repo is not on.
EUTILS_TOOL = "a-doc"

# NCBI publishes 3/second anonymous and 10/second with a key. Those are
# CEILINGS, and pacing exactly at one gets rejected: a live run at 3.1/second
# produced two spurious `error` verdicts and then an outright HTTPError on the
# third search. These leave deliberate headroom.
_MIN_INTERVAL_NO_KEY = 1.0 / 2.0
_MIN_INTERVAL_WITH_KEY = 1.0 / 7.0

# NCBI throttling is bursty, so a rejection is retried rather than taken at
# face value. Without this a transient 429 costs a citation its verification
# and a hypothesis its literature -- both silently.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0

_YEAR_RE = re.compile(r"(\d{4})")


class NcbiRateLimiter:
    """One process-wide throttle for every NCBI E-utilities call.

    Shared deliberately. Search and PMID verification both hit E-utilities,
    and two independent 3/second limiters produce 6/second between them —
    over the published anonymous limit, which is how a valid citation ends up
    recorded `unverifiable` for no better reason than our own traffic.

    Process-wide is sufficient here: the web service runs one task at a time
    (CLAUDE.md "Infrastructure") and reviews run one at a time, so there is no
    second process to coordinate with.
    """

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep
        self._last_at: float | None = None
        self._lock = threading.Lock()

    def wait(self, *, has_api_key: bool = False) -> None:
        interval = _MIN_INTERVAL_WITH_KEY if has_api_key else _MIN_INTERVAL_NO_KEY
        with self._lock:
            if self._last_at is not None:
                elapsed = time.monotonic() - self._last_at
                if elapsed < interval:
                    self._sleep(interval - elapsed)
            self._last_at = time.monotonic()


NCBI_RATE_LIMITER = NcbiRateLimiter()
"""The shared limiter. Both `PubMedClient` and `reason.citations`'s verifier
go through it."""


def fetch_with_retry(
    transport: PubMedTransport,
    url: str,
    *,
    limiter: NcbiRateLimiter | None = None,
    has_api_key: bool = False,
    attempts: int = RETRY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """One rate-limited E-utilities call, retried on failure.

    Shared by search and PMID verification so a burst rejection is handled the
    same way in both. Backoff is linear rather than exponential: NCBI's limit
    is per-second, so a short wait is the right shape and a long one just
    stalls a review.

    Raises the final exception if every attempt fails — callers decide whether
    that is `unverifiable` (verification) or an empty result (search).
    """
    gate = limiter or NCBI_RATE_LIMITER
    last: Exception | None = None
    for attempt in range(attempts):
        gate.wait(has_api_key=has_api_key)
        try:
            return transport(url)
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            last = exc
            if attempt < attempts - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last if last is not None else RuntimeError("no attempts made")


class PubMedArticle(BaseModel):
    """One article, as PubMed describes it. Every field comes from NCBI."""

    pmid: str
    title: str = ""
    journal: str = ""
    year: int | None = None
    authors: list[str] = Field(default_factory=list)
    doi: str = ""

    @property
    def citation_ref(self) -> str:
        """The `pmid:` ref this article would be cited by, in the form
        `reason.citations` already resolves."""
        return f"pmid:{self.pmid}"

    def short_citation(self) -> str:
        """A one-line human citation. Deliberately plain — a reasoning stage
        gets the structured fields, and this is what a person reads."""
        lead = self.authors[0].rstrip(".") if self.authors else ""
        if len(self.authors) > 1 and lead:
            lead = f"{lead} et al"
        bits = [b for b in (lead, self.title.rstrip("."), self.journal.rstrip(".")) if b]
        stem = ". ".join(bits)
        return f"{stem} ({self.year})" if self.year else stem


class PubMedSearchResult(BaseModel):
    """What one query returned.

    `total` is PubMed's own count of matching records, which is usually far
    larger than `articles`. Keeping it distinct matters: "9 of 2,431 shown" and
    "9 results exist" are very different claims for a reasoning stage to make.
    """

    query: str
    total: int = 0
    articles: list[PubMedArticle] = Field(default_factory=list)
    error: str = ""
    """Set when the search could not be completed. A literature refresh must
    degrade to "no citations" rather than fail a review, so callers check this
    instead of catching."""

    @property
    def ok(self) -> bool:
        return not self.error


def build_query(
    topic: str,
    *,
    reviews_only: bool = True,
    humans_only: bool = True,
    since_year: int | None = None,
) -> str:
    """Build a PubMed query string for a topic, deterministically.

    Query construction is plain code rather than model-authored text, for the
    same reason the rest of this system's deterministic logic is: a model
    writing its own search string can quietly narrow a query until it returns
    only what it already believed, and nothing downstream could tell.

    The filters are conservative on purpose. Review articles and human studies
    are what a differential wants; `since_year` is left to the caller because
    "recent" means something different for an established disease than for one
    described in the last decade.
    """
    cleaned = " ".join(topic.split())
    if not cleaned:
        return ""
    parts = [f'"{cleaned}"[Title/Abstract]']
    if reviews_only:
        parts.append("(review[Publication Type] OR systematic review[Publication Type])")
    if humans_only:
        parts.append("humans[MeSH Terms]")
    if since_year is not None:
        parts.append(f'("{since_year}"[Date - Publication] : "3000"[Date - Publication])')
    return " AND ".join(parts)


def parse_esearch(payload: bytes) -> tuple[list[str], int]:
    """`(pmids, total)` from an esearch JSON response.

    A malformed or unexpected payload yields `([], 0)` rather than raising:
    NCBI returns a 200 with an error body often enough that treating a parse
    failure as an exception would make transient upstream noise indistinguishable
    from a genuine bug.
    """
    try:
        data: Any = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return [], 0
    result = data.get("esearchresult") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return [], 0
    raw_ids = result.get("idlist")
    pmids = [str(i) for i in raw_ids if str(i).isdigit()] if isinstance(raw_ids, list) else []
    try:
        total = int(result.get("count", 0))
    except (TypeError, ValueError):
        total = 0
    return pmids, total


def _year_from(record: dict[str, Any]) -> int | None:
    for key in ("pubdate", "epubdate", "sortpubdate"):
        match = _YEAR_RE.search(str(record.get(key, "")))
        if match:
            return int(match.group(1))
    return None


def _doi_from(record: dict[str, Any]) -> str:
    if str(record.get("elocationid", "")).lower().startswith("doi:"):
        return str(record["elocationid"])[4:].strip()
    ids = record.get("articleids")
    if isinstance(ids, list):
        for entry in ids:
            if isinstance(entry, dict) and entry.get("idtype") == "doi":
                return str(entry.get("value", "")).strip()
    return ""


def parse_esummary(payload: bytes, pmids: Sequence[str]) -> list[PubMedArticle]:
    """Articles from an esummary JSON response, in the order `pmids` gives.

    Order is preserved from the search rather than taken from the response,
    because esearch returns by relevance and esummary keys by uid — reading the
    response's own ordering would silently discard the ranking.

    A record that fails to parse is SKIPPED, not fatal: ADR 0028's rule that no
    single item may cost a payload. One malformed summary must not throw away
    the other four citations.
    """
    try:
        data: Any = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return []

    articles: list[PubMedArticle] = []
    for pmid in pmids:
        record = result.get(str(pmid))
        if not isinstance(record, dict) or "error" in record:
            continue
        raw_authors = record.get("authors")
        authors = (
            [
                str(a.get("name", "")).strip()
                for a in raw_authors
                if isinstance(a, dict) and str(a.get("name", "")).strip()
            ]
            if isinstance(raw_authors, list)
            else []
        )
        try:
            articles.append(
                PubMedArticle(
                    pmid=str(pmid),
                    title=str(record.get("title", "")).strip(),
                    journal=str(
                        record.get("fulljournalname") or record.get("source") or ""
                    ).strip(),
                    year=_year_from(record),
                    authors=authors,
                    doi=_doi_from(record),
                )
            )
        except Exception:  # noqa: BLE001 - one bad record must not cost the rest
            continue
    return articles


class PubMedClient:
    """Searches PubMed and returns real, citable articles.

    Caches per query for `SEARCH_CACHE_TTL_DAYS`. A transport failure is NEVER
    cached — the same rule the PMID verifier follows, and for the same reason:
    an NCBI outage must not freeze an empty result in place for a week.
    """

    def __init__(
        self,
        cache_path: Path,
        *,
        transport: PubMedTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_key: str = "",
        email: str = "",
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        limiter: NcbiRateLimiter | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._timeout = timeout
        self._api_key = api_key
        self._email = email
        self._transport = transport or self._default_transport
        self._now = now
        self._retry_sleep = sleep
        self._limiter = limiter or NcbiRateLimiter(sleep=sleep)

    # -- transport ---------------------------------------------------------

    def _default_transport(self, url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=self._timeout) as response:  # noqa: S310
            body: bytes = response.read()
            return body

    def _url(self, base: str, params: dict[str, str]) -> str:
        full = {"db": "pubmed", "retmode": "json", "tool": EUTILS_TOOL, **params}
        if self._email:
            full["email"] = self._email
        if self._api_key:
            full["api_key"] = self._api_key
        return f"{base}?{urllib.parse.urlencode(full)}"

    def _fetch(self, url: str) -> bytes:
        return fetch_with_retry(
            self._transport,
            url,
            limiter=self._limiter,
            has_api_key=bool(self._api_key),
            sleep=self._retry_sleep,
        )

    # -- cache -------------------------------------------------------------

    def _load_cache(self) -> dict[str, Any]:
        if not self._cache_path.exists():
            return {}
        try:
            loaded: Any = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        except OSError:
            # A cache that cannot be written is a slower search, not a failure.
            pass

    def _cached(self, key: str) -> PubMedSearchResult | None:
        entry = self._load_cache().get(key)
        if not isinstance(entry, dict):
            return None
        try:
            cached_at = datetime.fromisoformat(entry["cached_at"])
        except (KeyError, ValueError, TypeError):
            return None
        if self._now() - cached_at >= timedelta(days=SEARCH_CACHE_TTL_DAYS):
            return None
        try:
            return PubMedSearchResult.model_validate(entry["result"])
        except Exception:  # noqa: BLE001 - a stale shape is a miss, not a crash
            return None

    def _store(self, key: str, result: PubMedSearchResult) -> None:
        cache = self._load_cache()
        cache[key] = {
            "cached_at": self._now().isoformat(),
            "result": result.model_dump(mode="json"),
        }
        self._save_cache(cache)

    # -- search ------------------------------------------------------------

    def search(self, query: str, *, retmax: int = DEFAULT_RETMAX) -> PubMedSearchResult:
        """Run one query and return its top `retmax` articles.

        Never raises. A caller doing a literature refresh must be able to
        degrade to "no citations found" without a failed NCBI call taking a
        whole review down with it.
        """
        if not query.strip():
            return PubMedSearchResult(query=query, error="empty query")

        key = f"{query}|{retmax}"
        cached = self._cached(key)
        if cached is not None:
            return cached

        try:
            search_url = self._url(
                _ESEARCH_URL, {"term": query, "retmax": str(retmax), "sort": "relevance"}
            )
            pmids, total = parse_esearch(self._fetch(search_url))
        except Exception as exc:  # noqa: BLE001 - transport/parse failure is reported, never raised
            return PubMedSearchResult(query=query, error=f"search failed: {type(exc).__name__}")

        if not pmids:
            result = PubMedSearchResult(query=query, total=total)
            self._store(key, result)
            return result

        try:
            summary_url = self._url(_ESUMMARY_URL, {"id": ",".join(pmids)})
            articles = parse_esummary(self._fetch(summary_url), pmids)
        except Exception as exc:  # noqa: BLE001
            return PubMedSearchResult(query=query, error=f"summary failed: {type(exc).__name__}")

        result = PubMedSearchResult(query=query, total=total, articles=articles)
        self._store(key, result)
        return result

    def search_topic(
        self,
        topic: str,
        *,
        retmax: int = DEFAULT_RETMAX,
        reviews_only: bool = True,
        since_year: int | None = None,
    ) -> PubMedSearchResult:
        """Search for a topic, building the query deterministically.

        Falls back to an unfiltered title/abstract search when the filtered one
        returns nothing. A rare disease can genuinely have no review articles,
        and returning zero citations for it while the primary literature exists
        would be the worst of both worlds.
        """
        filtered = build_query(topic, reviews_only=reviews_only, since_year=since_year)
        result = self.search(filtered, retmax=retmax)
        if result.ok and not result.articles and reviews_only:
            return self.search(
                build_query(topic, reviews_only=False, since_year=since_year), retmax=retmax
            )
        return result

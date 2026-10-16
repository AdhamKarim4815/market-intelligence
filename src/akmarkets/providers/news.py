"""News collection from Google News RSS.

Free, no key, and reliable enough to build on — which among free news
sources is rare. It is also an aggregator, so the same wire story arrives
many times with different outlet suffixes, and everything the deduplication
stage does exists because of that.

Google wraps the real article URL in a redirect and puts the outlet in the
`<source>` element rather than the link, so both are extracted rather than
parsed out of the URL.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx
from defusedxml import ElementTree

from akmarkets.domain.news import Article
from akmarkets.providers.base import DataState, Provenance, ProviderResult

_WHITESPACE = re.compile(r"\s+")

DEFAULT_TIMEOUT = 30.0

_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


# Google News puts the whole item in an HTML anchor, so a raw description
# is markup, not prose.
_TAGS = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&[a-zA-Z#0-9]+;")


def strip_markup(raw: str) -> str:
    """Text content of an RSS description, with the markup removed.

    Not cosmetic. The description arrives as `<a href="..." target="_blank">`,
    and every downstream keyword match sees `target`, `href` and a
    base64 URL as if they were words the journalist wrote. Measured on 123
    real articles, the un-stripped summary made the valuation term `target`
    match **123 of 123** — every single one — which quietly pinned the
    materiality score near its ceiling for the entire corpus and left it
    doing no discriminating at all.
    """
    without_tags = _TAGS.sub(" ", raw)
    return _WHITESPACE.sub(" ", _ENTITY.sub(" ", without_tags)).strip()


def _parse_when(raw: str | None) -> datetime:
    """RSS dates, or now.

    Falling back to now rather than skipping the item: a feed with a
    malformed date still carries a real story, and dropping it loses more
    than a slightly wrong timestamp costs. The freshness decay is the only
    thing that reads it, and being wrong by hours there changes a ranking,
    not a fact.
    """
    if not raw:
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class GoogleNewsProvider:
    """Headlines for a query, as RSS."""

    name = "google-news"
    BASE = "https://news.google.com/rss/search"

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        language: str = "en-US",
        country: str = "US",
    ) -> None:
        self._timeout = timeout
        self._client = client
        self._language = language
        self._country = country

    def _url(self, query: str, days: int) -> str:
        # `when:Nd` is Google's own recency operator. Filtering server-side
        # keeps the response small and means the pipeline is not handed a
        # year of history to date-filter itself.
        terms = quote_plus(f"{query} when:{days}d")
        return (
            f"{self.BASE}?q={terms}&hl={self._language}"
            f"&gl={self._country}&ceid={self._country}:{self._language.split('-')[0]}"
        )

    def fetch(
        self, query: str, symbols: Sequence[str] = (), days: int = 14, limit: int = 50
    ) -> ProviderResult[list[Article]]:
        url = self._url(query, days)
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            response = client.get(url, headers={"User-Agent": _AGENT})
            response.raise_for_status()
            root = ElementTree.fromstring(response.text)
        except Exception as exc:
            return ProviderResult.failed(self.name, f"{type(exc).__name__}: {exc}")

        articles: list[Article] = []
        for item in root.iterfind(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            source = (item.findtext("source") or "").strip() or "unknown"
            published = _parse_when(item.findtext("pubDate"))

            # The id is derived from the link so a re-run of the same feed
            # produces the same ids and the store can tell an already-seen
            # article from a new one without a second request.
            article_id = hashlib.sha256((link or title).encode()).hexdigest()[:32]
            articles.append(
                Article(
                    article_id=article_id,
                    title=title,
                    url=link,
                    source=source,
                    published_at=published,
                    summary=strip_markup(item.findtext("description") or "")[:500],
                    symbols=tuple(symbols),
                )
            )
            if len(articles) >= limit:
                break

        if not articles:
            return ProviderResult.missing(self.name, f"no items for {query!r}")
        return ProviderResult.healthy(
            articles,
            Provenance(
                source=self.name,
                retrieved_at=datetime.now(UTC),
                effective_at=max(article.published_at for article in articles),
                url=url,
            ),
        )

    def fetch_for_symbols(
        self, symbols: Sequence[str], days: int = 14, limit_per_symbol: int = 25
    ) -> ProviderResult[list[Article]]:
        """One query per symbol, so each article knows what it is about.

        A single combined query would be cheaper and would lose the
        attribution, and attribution is what makes portfolio weighting
        possible at all.
        """
        collected: list[Article] = []
        failures: list[str] = []
        for symbol in symbols:
            result = self.fetch(
                f"{symbol} stock", symbols=(symbol,), days=days, limit=limit_per_symbol
            )
            if result.value:
                collected.extend(result.value)
            elif result.state is DataState.FAILED:
                failures.append(f"{symbol}: {result.message[:60]}")

        if not collected:
            return ProviderResult.failed(
                self.name, "; ".join(failures) or "no articles for any symbol"
            )
        provenance = Provenance(source=self.name, retrieved_at=datetime.now(UTC))
        if failures:
            return ProviderResult(
                value=collected,
                state=DataState.DEGRADED,
                provenance=provenance,
                message=f"{len(failures)} symbol(s) failed: {'; '.join(failures[:3])}",
            )
        return ProviderResult.healthy(collected, provenance)

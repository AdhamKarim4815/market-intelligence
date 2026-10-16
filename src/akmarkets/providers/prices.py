"""Daily bars and delayed quotes from sources that need no account.

Two providers, tried in configured order: Nasdaq's public API and Yahoo's
chart endpoint. Both free, neither requiring a key, and neither
contractually obliged to keep working — which is exactly why they sit behind
a Protocol and a fallback chain rather than being called directly.

**Stooq was measured and rejected, 2026-08-10.** It was the obvious first
choice — plain CSV, no key, long history — and it is what many hobby
projects still use. It now answers every request with HTTP 200 and a
JavaScript proof-of-work challenge page instead of data. Solving that
challenge would be building on an access control the operator deliberately
put up, so the provider was dropped rather than worked around. This note
exists so the next person to think "why not Stooq?" gets the answer without
spending an afternoon on it.

Yahoo rate-limits aggressively by IP and returns 429 for minutes at a time
after a burst, which is why it is the fallback rather than the primary.

Every adapter returns a `ProviderResult`. None raise across the boundary: an
exception forces callers into a try/except that usually swallows it, and a
swallowed provider error becomes a blank on a dashboard that reads as real
data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

import httpx

from akmarkets.providers.base import (
    Bar,
    DataState,
    Provenance,
    ProviderResult,
    Quote,
)

DEFAULT_TIMEOUT = 30.0

# Several of these endpoints reject the default python-httpx agent outright.
# A browser-shaped agent is not evasion here -- these are public, unmetered
# endpoints and the header is what they expect -- it is the difference
# between a 200 and a 403.
_BROWSER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def _decimal(value: object) -> Decimal | None:
    """Parse a provider figure, or None. Never a zero standing in for absent.

    Free feeds routinely emit "", "N/A", "null" and "-" for a missing value.
    Every one of those must become None: a price of zero is a claim about a
    security, and a wrong one.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if text in {"", "N/A", "n/a", "null", "None", "-", "--"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _int(value: object) -> int | None:
    parsed = _decimal(value)
    return int(parsed) if parsed is not None else None


class YahooHistoryProvider:
    """Daily OHLCV from the chart endpoint, with split/dividend adjustment.

    Yahoo returns `null` inside the price arrays for halted or untraded
    sessions rather than omitting the timestamp, so the arrays are walked in
    parallel and any index with a null close is skipped.
    """

    name = "yahoo"
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, client: httpx.Client | None = None):
        self._timeout = timeout
        self._client = client

    def fetch_history(self, symbol: str, start: date, end: date) -> ProviderResult[list[Bar]]:
        params: dict[str, str | int] = {
            "period1": int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp()),
            "period2": int(datetime.combine(end, datetime.max.time(), tzinfo=UTC).timestamp()),
            "interval": "1d",
            "events": "div,split",
        }
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            response = client.get(
                f"{self.BASE}{symbol.upper()}",
                params=params,
                headers={"User-Agent": _BROWSER_AGENT},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return ProviderResult.failed(self.name, f"{type(exc).__name__}: {exc}")

        chart = payload.get("chart") or {}
        if chart.get("error"):
            return ProviderResult.missing(self.name, str(chart["error"]))
        results = chart.get("result") or []
        if not results:
            return ProviderResult.missing(self.name, f"no series for {symbol}")

        result = results[0]
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adjusted = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]

        bars: list[Bar] = []
        for index, stamp in enumerate(stamps):
            close = _decimal(_at(quote.get("close"), index))
            open_ = _decimal(_at(quote.get("open"), index))
            high = _decimal(_at(quote.get("high"), index))
            low = _decimal(_at(quote.get("low"), index))
            if close is None or open_ is None or high is None or low is None:
                continue
            bars.append(
                Bar(
                    on=datetime.fromtimestamp(stamp, tz=UTC).date(),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=_int(_at(quote.get("volume"), index)) or 0,
                    adjusted_close=_decimal(_at(adjusted.get("adjclose"), index)),
                )
            )

        if not bars:
            return ProviderResult.missing(self.name, f"no usable rows for {symbol}")
        return ProviderResult.healthy(
            bars,
            Provenance(
                source=self.name,
                retrieved_at=datetime.now(UTC),
                effective_at=datetime.combine(bars[-1].on, datetime.min.time(), tzinfo=UTC),
                url=str(response.url),
            ),
        )


class YahooQuoteProvider:
    """Delayed last prices, read from the same chart endpoint's metadata.

    Deliberately reuses the chart endpoint rather than the `v7/quote` one,
    which now requires a crumb and a cookie. Fewer moving parts, and one
    fewer thing to break silently.

    Quotes from here are delayed by fifteen minutes or so, and `is_delayed`
    says so on every one. The UI is required to show that rather than imply
    a live price.
    """

    name = "yahoo"
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, client: httpx.Client | None = None):
        self._timeout = timeout
        self._client = client

    def fetch_quotes(self, symbols: Sequence[str]) -> ProviderResult[dict[str, Quote]]:
        quotes: dict[str, Quote] = {}
        failures: list[str] = []
        client = self._client or httpx.Client(timeout=self._timeout)

        for symbol in symbols:
            try:
                response = client.get(
                    f"{self.BASE}{symbol.upper()}",
                    params={"interval": "1d", "range": "5d"},
                    headers={"User-Agent": _BROWSER_AGENT},
                )
                response.raise_for_status()
                meta = (
                    ((response.json().get("chart") or {}).get("result") or [{}])[0].get("meta")
                    or {}
                )
            except Exception as exc:
                failures.append(f"{symbol}: {type(exc).__name__}")
                continue

            price = _decimal(meta.get("regularMarketPrice"))
            if price is None:
                failures.append(f"{symbol}: no price in response")
                continue
            previous = _decimal(meta.get("chartPreviousClose")) or _decimal(
                meta.get("previousClose")
            )
            stamp = meta.get("regularMarketTime")
            change = price - previous if previous is not None else None
            quotes[symbol.upper()] = Quote(
                symbol=symbol.upper(),
                price=price,
                previous_close=previous,
                change=change,
                change_percent=(change / previous) if change is not None and previous else None,
                volume=_int(meta.get("regularMarketVolume")),
                currency=str(meta.get("currency") or "USD").upper(),
                as_of=datetime.fromtimestamp(stamp, tz=UTC) if stamp else None,
                is_delayed=True,
            )

        if not quotes:
            return ProviderResult.failed(self.name, "; ".join(failures) or "no quotes returned")

        provenance = Provenance(source=self.name, retrieved_at=datetime.now(UTC))
        if failures:
            # Some symbols priced, some not. Reporting this as healthy would
            # let the missing ones vanish silently.
            return ProviderResult(
                value=quotes,
                state=DataState.DEGRADED,
                provenance=provenance,
                message=f"{len(failures)} symbol(s) unpriced: {'; '.join(failures[:5])}",
            )
        return ProviderResult.healthy(quotes, provenance)


def _at(series: object, index: int) -> object:
    if not isinstance(series, list) or index >= len(series):
        return None
    return series[index]


class NasdaqHistoryProvider:
    """Daily OHLCV from Nasdaq's public API. Keyless and, so far, reliable.

    Two shapes in the response are worth naming because they are easy to
    get subtly wrong. Prices arrive as display strings -- `"$313.33"`, with
    volume as `"34,437,190"` -- so every figure goes through the same
    tolerant parser as the rest of this module. And rows come **newest
    first**, which reversed silently produces a price series that runs
    backwards through every indicator computed from it.

    The endpoint also requires an `assetclass`, and answers with zero rows
    rather than an error when it is wrong: `SPY` is `etf`, `AAPL` is
    `stocks`. There is no way to ask which, so the class is resolved by
    trying and remembered per symbol for the life of the provider.
    """

    name = "nasdaq"
    BASE = "https://api.nasdaq.com/api/quote"
    ASSET_CLASSES = ("stocks", "etf")

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, client: httpx.Client | None = None):
        self._timeout = timeout
        self._client = client
        self._asset_class: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _BROWSER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _classes_to_try(self, symbol: str) -> tuple[str, ...]:
        known = self._asset_class.get(symbol.upper())
        return (known,) if known else self.ASSET_CLASSES

    def fetch_history(self, symbol: str, start: date, end: date) -> ProviderResult[list[Bar]]:
        client = self._client or httpx.Client(timeout=self._timeout)
        upper = symbol.upper()
        # A generous ceiling: the endpoint truncates silently at its own
        # default, which would quietly shorten a five-year history to a
        # month and make every long moving average wrong.
        limit = max(32, (end - start).days + 8)
        last_error = ""

        for asset_class in self._classes_to_try(upper):
            try:
                response = client.get(
                    f"{self.BASE}/{upper}/historical",
                    params=cast(
                        "dict[str, str | int]",
                        {
                            "assetclass": asset_class,
                            "fromdate": start.isoformat(),
                            "todate": end.isoformat(),
                            "limit": limit,
                        },
                    ),
                    headers=self._headers(),
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
            bars = [bar for bar in (_nasdaq_bar(row) for row in rows) if bar is not None]
            if not bars:
                last_error = f"no rows for assetclass={asset_class}"
                continue

            self._asset_class[upper] = asset_class
            # Newest-first in the response; every consumer wants oldest-first.
            bars.sort(key=lambda bar: bar.on)
            return ProviderResult.healthy(
                bars,
                Provenance(
                    source=self.name,
                    retrieved_at=datetime.now(UTC),
                    effective_at=datetime.combine(
                        bars[-1].on, datetime.min.time(), tzinfo=UTC
                    ),
                    url=str(response.url),
                ),
            )

        # Every asset class returning zero rows means the symbol is not
        # listed here, which the next provider may still know about. A
        # transport error is a different problem and says so.
        if last_error.startswith("no rows"):
            return ProviderResult.missing(self.name, f"{symbol}: {last_error}")
        return ProviderResult.failed(self.name, f"{symbol}: {last_error or 'no response'}")


class NasdaqQuoteProvider:
    """Delayed last prices from the same public API.

    Nasdaq states its own delay in the payload (`isRealTime`), which is
    carried through to `Quote.is_delayed` rather than assumed, so the UI
    labels what the source actually claimed.
    """

    name = "nasdaq"
    BASE = "https://api.nasdaq.com/api/quote"
    ASSET_CLASSES = ("stocks", "etf")

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, client: httpx.Client | None = None):
        self._timeout = timeout
        self._client = client
        self._asset_class: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _BROWSER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _fetch_one(self, client: httpx.Client, symbol: str) -> Quote | None:
        known = self._asset_class.get(symbol)
        for asset_class in (known,) if known else self.ASSET_CLASSES:
            response = client.get(
                f"{self.BASE}/{symbol}/info",
                params={"assetclass": asset_class},
                headers=self._headers(),
            )
            response.raise_for_status()
            data = (response.json() or {}).get("data") or {}
            primary = data.get("primaryData") or {}
            price = _decimal(primary.get("lastSalePrice"))
            if price is None:
                continue

            self._asset_class[symbol] = asset_class
            change = _decimal(primary.get("netChange"))
            percent = _decimal(primary.get("percentageChange"))
            return Quote(
                symbol=symbol,
                price=price,
                previous_close=(price - change) if change is not None else None,
                change=change,
                # The feed gives a percentage; the domain carries a
                # proportion, so it is divided here rather than at every
                # display site.
                change_percent=(percent / Decimal(100)) if percent is not None else None,
                volume=_int((data.get("keyStats") or {}).get("Volume", {}).get("value")),
                currency="USD",
                is_delayed=not bool(primary.get("isRealTime")),
            )
        return None

    def fetch_quotes(self, symbols: Sequence[str]) -> ProviderResult[dict[str, Quote]]:
        client = self._client or httpx.Client(timeout=self._timeout)
        quotes: dict[str, Quote] = {}
        failures: list[str] = []

        for symbol in symbols:
            upper = symbol.upper()
            try:
                quote = self._fetch_one(client, upper)
            except Exception as exc:
                failures.append(f"{upper}: {type(exc).__name__}")
                continue
            if quote is None:
                failures.append(f"{upper}: no price in response")
            else:
                quotes[upper] = quote

        if not quotes:
            return ProviderResult.failed(self.name, "; ".join(failures) or "no quotes returned")

        provenance = Provenance(source=self.name, retrieved_at=datetime.now(UTC))
        if failures:
            return ProviderResult(
                value=quotes,
                state=DataState.DEGRADED,
                provenance=provenance,
                message=f"{len(failures)} symbol(s) unpriced: {'; '.join(failures[:5])}",
            )
        return ProviderResult.healthy(quotes, provenance)


def _nasdaq_bar(row: dict[str, object]) -> Bar | None:
    """One row to a Bar, or None if it is not usable.

    A row missing any of OHLC is dropped rather than zero-filled: an
    invented bar flows straight into a moving average and is invisible
    afterwards.
    """
    close = _decimal(row.get("close"))
    open_ = _decimal(row.get("open"))
    high = _decimal(row.get("high"))
    low = _decimal(row.get("low"))
    if close is None or open_ is None or high is None or low is None:
        return None
    try:
        on = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date()
    except (TypeError, ValueError):
        return None
    return Bar(
        on=on,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=_int(row.get("volume")) or 0,
    )

"""Providers, the fallback chain, and the price cache.

No test here reaches the network. Provider adapters are exercised against
recorded response shapes, and the chain against fakes, because the point is
how this code behaves when a provider is slow, wrong, empty or lying — none
of which a live endpoint will reliably do on demand.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from akmarkets.providers.base import (
    Bar,
    DataState,
    Provenance,
    ProviderResult,
    Quote,
)
from akmarkets.providers.cache import PriceCache, bars_to_frame
from akmarkets.providers.chain import HistoryChain, QuoteChain
from akmarkets.providers.prices import (
    NasdaqHistoryProvider,
    NasdaqQuoteProvider,
    YahooHistoryProvider,
    YahooQuoteProvider,
    _decimal,
)

# Nasdaq's real response shape: display strings for prices, comma-grouped
# volume, MM/DD/YYYY dates, and rows newest-first.
NASDAQ_HISTORY = {
    "data": {
        "symbol": "ACME",
        "tradesTable": {
            "rows": [
                {"date": "01/05/2026", "close": "$10.90", "volume": "220,000",
                 "open": "$10.20", "high": "$11.00", "low": "$10.10"},
                {"date": "01/02/2026", "close": "$10.20", "volume": "150,000",
                 "open": "$10.00", "high": "$10.50", "low": "$9.80"},
            ]
        },
    }
}

NASDAQ_INFO = {
    "data": {
        "symbol": "ACME",
        "primaryData": {
            "lastSalePrice": "$10.90",
            "netChange": "+0.70",
            "percentageChange": "+6.86%",
            "isRealTime": False,
        },
        "keyStats": {"Volume": {"value": "220,000"}},
    }
}

YAHOO_CHART = {
    "chart": {
        "error": None,
        "result": [
            {
                "meta": {
                    "regularMarketPrice": 10.9,
                    "chartPreviousClose": 10.2,
                    "regularMarketVolume": 220000,
                    "currency": "USD",
                    "regularMarketTime": 1767225600,
                },
                "timestamp": [1767312000, 1767571200],
                "indicators": {
                    "quote": [
                        {
                            "open": [10.0, 10.2],
                            "high": [10.5, 11.0],
                            "low": [9.8, 10.1],
                            "close": [10.2, 10.9],
                            "volume": [150000, 220000],
                        }
                    ],
                    "adjclose": [{"adjclose": [10.1, 10.8]}],
                },
            }
        ],
    }
}


def stub_client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# -- parsing ------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "N/A", "n/a", "null", "None", "-", "--", "wat"])
def test_absent_provider_figures_parse_to_none_not_zero(raw: str) -> None:
    """Free feeds emit all of these for a missing value. Every one must
    become None: a price of zero is a claim about a security, and a false
    one that would flow straight into a moving average."""
    assert _decimal(raw) is None


def test_provider_figures_tolerate_currency_and_thousands_formatting() -> None:
    assert _decimal("$1,234.56") == Decimal("1234.56")
    assert _decimal("12.5%") == Decimal("12.5")


# -- nasdaq ------------------------------------------------------------


def test_nasdaq_parses_display_formatted_prices() -> None:
    """Prices arrive as `"$313.33"` and volume as `"34,437,190"`. Parsing
    those naively yields None and a series that silently goes missing."""
    client = stub_client(lambda request: httpx.Response(200, json=NASDAQ_HISTORY))
    result = NasdaqHistoryProvider(client=client).fetch_history(
        "ACME", date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result.ok
    assert result.value is not None
    assert result.value[-1].close == Decimal("10.90")
    assert result.value[-1].volume == 220000


def test_nasdaq_rows_are_reversed_into_ascending_order() -> None:
    """The response is newest-first. Left as-is, every indicator computed
    from the series runs backwards through time."""
    client = stub_client(lambda request: httpx.Response(200, json=NASDAQ_HISTORY))
    result = NasdaqHistoryProvider(client=client).fetch_history(
        "ACME", date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result.value is not None
    assert [bar.on for bar in result.value] == [date(2026, 1, 2), date(2026, 1, 5)]


def test_nasdaq_resolves_the_asset_class_by_trying_it() -> None:
    """The endpoint answers with zero rows rather than an error when the
    asset class is wrong, and there is no way to ask which one a symbol is:
    SPY is `etf`, AAPL is `stocks`."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asset_class = request.url.params.get("assetclass")
        seen.append(asset_class)
        if asset_class == "stocks":
            return httpx.Response(200, json={"data": {"tradesTable": {"rows": []}}})
        return httpx.Response(200, json=NASDAQ_HISTORY)

    provider = NasdaqHistoryProvider(client=stub_client(handler))
    assert provider.fetch_history("SPY", date(2026, 1, 1), date(2026, 1, 31)).ok
    assert seen == ["stocks", "etf"]

    # Remembered, so the wrong class is not retried on every later call.
    provider.fetch_history("SPY", date(2026, 1, 1), date(2026, 1, 31))
    assert seen == ["stocks", "etf", "etf"]


def test_nasdaq_reports_an_unlisted_symbol_as_absent_not_failed() -> None:
    """Zero rows under every asset class means this source does not list
    it — which the next provider may still know about. A transport error is
    a different problem and must not be conflated."""
    client = stub_client(
        lambda request: httpx.Response(200, json={"data": {"tradesTable": {"rows": []}}})
    )
    result = NasdaqHistoryProvider(client=client).fetch_history(
        "NOPE", date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result.state is DataState.UNKNOWN


def test_nasdaq_reports_a_transport_failure_rather_than_raising() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    result = NasdaqHistoryProvider(client=stub_client(boom)).fetch_history(
        "ACME", date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result.state is DataState.FAILED
    assert "ConnectTimeout" in result.message


def test_nasdaq_drops_a_row_with_a_missing_price_rather_than_zero_filling() -> None:
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {"date": "01/06/2026", "close": "N/A", "volume": "",
                     "open": "N/A", "high": "N/A", "low": "N/A"},
                    *NASDAQ_HISTORY["data"]["tradesTable"]["rows"],
                ]
            }
        }
    }
    client = stub_client(lambda request: httpx.Response(200, json=payload))
    result = NasdaqHistoryProvider(client=client).fetch_history(
        "ACME", date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result.value is not None
    assert len(result.value) == 2


def test_nasdaq_quotes_carry_the_delay_the_source_declared() -> None:
    """`isRealTime` is read rather than assumed, so the UI labels what the
    source actually claimed."""
    client = stub_client(lambda request: httpx.Response(200, json=NASDAQ_INFO))
    result = NasdaqQuoteProvider(client=client).fetch_quotes(["ACME"])
    assert result.ok
    assert result.value is not None
    quote = result.value["ACME"]
    assert quote.is_delayed is True
    assert quote.price == Decimal("10.90")
    assert quote.previous_close == Decimal("10.20")
    # The feed gives a percentage; the domain carries a proportion.
    assert quote.change_percent == Decimal("0.0686")


# -- yahoo --------------------------------------------------------------


def test_yahoo_parses_history_with_adjusted_closes() -> None:
    client = stub_client(lambda request: httpx.Response(200, json=YAHOO_CHART))
    result = YahooHistoryProvider(client=client).fetch_history(
        "ACME", date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result.ok
    assert result.value is not None
    assert result.value[0].adjusted_close == Decimal("10.1")


def test_yahoo_skips_a_null_close_without_shifting_the_other_series() -> None:
    """Yahoo emits null inside the price arrays for a halted session rather
    than omitting the timestamp. Zipping the arrays without checking would
    pair one day's open with another day's close."""
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {},
                    "timestamp": [1767312000, 1767398400, 1767571200],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0, None, 10.2],
                                "high": [10.5, None, 11.0],
                                "low": [9.8, None, 10.1],
                                "close": [10.2, None, 10.9],
                                "volume": [150000, None, 220000],
                            }
                        ],
                        "adjclose": [{"adjclose": [10.1, None, 10.8]}],
                    },
                }
            ],
        }
    }
    client = stub_client(lambda request: httpx.Response(200, json=payload))
    result = YahooHistoryProvider(client=client).fetch_history(
        "ACME", date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result.value is not None
    assert len(result.value) == 2
    assert result.value[1].open == Decimal("10.2")
    assert result.value[1].close == Decimal("10.9")


def test_yahoo_quotes_are_always_labelled_delayed() -> None:
    """The UI is required to say so rather than imply a live price."""
    client = stub_client(lambda request: httpx.Response(200, json=YAHOO_CHART))
    result = YahooQuoteProvider(client=client).fetch_quotes(["ACME"])
    assert result.ok
    assert result.value is not None
    quote = result.value["ACME"]
    assert quote.is_delayed is True
    assert quote.price == Decimal("10.9")
    assert quote.change == Decimal("0.7")


def test_a_partly_priced_batch_is_degraded_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some symbols priced and some not must not report healthy, or the
    missing ones vanish without anyone noticing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "GOOD" in str(request.url):
            return httpx.Response(200, json=YAHOO_CHART)
        return httpx.Response(404, json={"chart": {"error": "not found", "result": None}})

    result = YahooQuoteProvider(client=stub_client(handler)).fetch_quotes(["GOOD", "BAD"])
    assert result.state is DataState.DEGRADED
    assert result.value is not None
    assert set(result.value) == {"GOOD"}
    assert "BAD" in result.message


# -- the chain ----------------------------------------------------------


class FakeHistory:
    def __init__(self, name: str, result: ProviderResult[list[Bar]]) -> None:
        self.name = name
        self._result = result
        self.calls = 0

    def fetch_history(self, symbol: str, start: date, end: date) -> ProviderResult[list[Bar]]:
        self.calls += 1
        return self._result


def _bars() -> list[Bar]:
    return [
        Bar(
            on=date(2026, 1, 2),
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=1000,
        )
    ]


def _healthy(name: str) -> ProviderResult[list[Bar]]:
    return ProviderResult.healthy(
        _bars(), Provenance(source=name, retrieved_at=datetime.now(UTC))
    )


def test_the_chain_stops_at_the_first_provider_that_answers() -> None:
    first = FakeHistory("a", _healthy("a"))
    second = FakeHistory("b", _healthy("b"))
    result = HistoryChain([first, second]).fetch_history("X", date(2026, 1, 1), date(2026, 2, 1))
    assert result.state is DataState.HEALTHY
    assert second.calls == 0


def test_falling_back_is_reported_as_degraded() -> None:
    """A chain that quietly runs on its backup for a month is a chain
    nobody knows is broken."""
    down = FakeHistory("a", ProviderResult.failed("a", "connection refused"))
    up = FakeHistory("b", _healthy("b"))
    result = HistoryChain([down, up]).fetch_history("X", date(2026, 1, 1), date(2026, 2, 1))
    assert result.value is not None
    assert result.state is DataState.DEGRADED
    assert "fell back to b" in result.message


def test_everyone_saying_no_data_is_unknown_but_everyone_down_is_failed() -> None:
    """A delisted ticker and an outage need different responses, and the
    health page has to be able to tell them apart."""
    absent = HistoryChain(
        [
            FakeHistory("a", ProviderResult.missing("a")),
            FakeHistory("b", ProviderResult.missing("b")),
        ]
    ).fetch_history("X", date(2026, 1, 1), date(2026, 2, 1))
    assert absent.state is DataState.UNKNOWN

    down = HistoryChain(
        [
            FakeHistory("a", ProviderResult.failed("a", "timeout")),
            FakeHistory("b", ProviderResult.failed("b", "timeout")),
        ]
    ).fetch_history("X", date(2026, 1, 1), date(2026, 2, 1))
    assert down.state is DataState.FAILED


def test_an_empty_chain_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        HistoryChain([])


class FakeQuotes:
    def __init__(self, name: str, prices: dict[str, str]) -> None:
        self.name = name
        self.prices = prices

    def fetch_quotes(self, symbols: Sequence[str]) -> ProviderResult[dict[str, Quote]]:
        return ProviderResult.healthy(
            {
                symbol: Quote(symbol=symbol, price=Decimal(self.prices[symbol]))
                for symbol in symbols
                if symbol in self.prices
            },
            Provenance(source=self.name, retrieved_at=datetime.now(UTC)),
        )


def test_providers_disagreeing_materially_is_recorded_not_resolved() -> None:
    """Silently preferring whichever answered first hides a data problem
    behind a number that looks fine."""
    chain = QuoteChain(
        [FakeQuotes("a", {"ACME": "10.00"}), FakeQuotes("b", {"ACME": "12.00"})],
        disagreement_tolerance_percent=Decimal("0.5"),
    )
    result, disagreements = chain.fetch_quotes(["ACME"], cross_check=True)
    assert result.value is not None
    assert result.value["ACME"].price == Decimal("10.00")  # primary still used
    assert result.state is DataState.DEGRADED
    assert len(disagreements) == 1
    assert disagreements[0].values == {
        "a": Decimal("10.00"),
        "b": Decimal("12.00"),
    }
    assert disagreements[0].spread_percent == Decimal("20")


def test_a_difference_inside_tolerance_is_not_a_disagreement() -> None:
    chain = QuoteChain(
        [FakeQuotes("a", {"ACME": "10.00"}), FakeQuotes("b", {"ACME": "10.02"})],
        disagreement_tolerance_percent=Decimal("0.5"),
    )
    result, disagreements = chain.fetch_quotes(["ACME"], cross_check=True)
    assert disagreements == []
    assert result.state is DataState.HEALTHY


def test_cross_checking_is_off_by_default_so_one_call_is_one_request() -> None:
    """Redundant providers everywhere would triple the request budget for
    data that rarely disagrees. Cross-checking is opt-in."""
    chain = QuoteChain([FakeQuotes("a", {"ACME": "10.00"}), FakeQuotes("b", {"ACME": "99.00"})])
    result, disagreements = chain.fetch_quotes(["ACME"])
    assert disagreements == []
    assert result.state is DataState.HEALTHY


# -- the cache ----------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> PriceCache:
    return PriceCache(
        root=tmp_path / "prices",
        chain=HistoryChain([FakeHistory("a", _healthy("a"))]),
    )


def test_bars_round_trip_through_parquet_as_decimals(cache: PriceCache) -> None:
    cache.store("ACME", _bars())
    restored = cache.bars("ACME")
    assert restored[0].close == Decimal("10.5")
    assert isinstance(restored[0].close, Decimal)


def test_a_ticker_with_a_dot_gets_one_file_not_a_directory(cache: PriceCache) -> None:
    """`BRK.B` written naively becomes `BRK.B.parquet`, whose extension a
    static host and pandas both read differently than intended."""
    cache.store("BRK.B", _bars())
    assert (cache.root / "BRK_DOT_B.parquet").exists()
    assert cache.bars("BRK.B")[0].close == Decimal("10.5")


def test_duplicate_sessions_keep_the_later_correction(cache: PriceCache) -> None:
    """Providers repeat a session across a paginated response. The last
    occurrence wins, so a corrected bar beats the original."""
    original = Bar(
        on=date(2026, 1, 2), open=Decimal("10"), high=Decimal("11"),
        low=Decimal("9"), close=Decimal("10.5"), volume=1000,
    )
    corrected = Bar(
        on=date(2026, 1, 2), open=Decimal("10"), high=Decimal("11"),
        low=Decimal("9"), close=Decimal("10.7"), volume=1200,
    )
    frame = bars_to_frame([original, corrected])
    assert len(frame) == 1
    assert frame["close"].iloc[0] == 10.7


def test_close_on_carries_backwards_over_a_weekend_but_never_forwards(
    cache: PriceCache,
) -> None:
    """A portfolio on a Sunday is worth what it was on Friday. Carrying
    forward from the future is what would let a historical replay cheat."""
    friday = Bar(
        on=date(2026, 1, 2), open=Decimal("10"), high=Decimal("11"),
        low=Decimal("9"), close=Decimal("10.5"), volume=1000,
    )
    monday = Bar(
        on=date(2026, 1, 5), open=Decimal("11"), high=Decimal("12"),
        low=Decimal("10"), close=Decimal("11.5"), volume=1000,
    )
    cache.store("ACME", [friday, monday])
    assert cache.close_on("ACME", date(2026, 1, 4)) == Decimal("10.5")
    assert cache.close_on("ACME", date(2026, 1, 5)) == Decimal("11.5")
    assert cache.close_on("ACME", date(2026, 1, 1)) is None


def test_a_corrupt_cache_file_is_discarded_rather_than_raising(cache: PriceCache) -> None:
    """A corrupt cache is a recoverable annoyance, not an outage."""
    cache.store("ACME", _bars())
    cache._memory.clear()
    (cache.root / "ACME.parquet").write_bytes(b"not parquet at all")
    assert cache.load("ACME") is None
    assert not (cache.root / "ACME.parquet").exists()


def test_an_unknown_symbol_reads_as_absent_not_an_error(cache: PriceCache) -> None:
    assert cache.load("NEVER") is None
    assert cache.bars("NEVER") == []
    assert cache.close_on("NEVER", date(2026, 1, 5)) is None
    assert cache.is_fresh("NEVER") is False


def test_ensure_fetches_only_what_is_stale(tmp_path: Path) -> None:
    provider = FakeHistory("a", _healthy("a"))
    cache = PriceCache(root=tmp_path / "p", chain=HistoryChain([provider]))
    cache.ensure(["ACME"])
    assert provider.calls == 1
    # The stub returns a bar from January 2026, which is not the last
    # completed session, so a second pass legitimately refetches.
    cache.ensure(["ACME"])
    assert provider.calls == 2


def test_ensure_reports_a_failure_per_symbol_without_failing_the_batch(
    tmp_path: Path,
) -> None:
    """One delisted or misspelled ticker must not take down a portfolio
    refresh."""
    cache = PriceCache(
        root=tmp_path / "p",
        chain=HistoryChain([FakeHistory("a", ProviderResult.failed("a", "gone"))]),
    )
    results = cache.ensure(["ACME", "BETA"])
    assert set(results) == {"ACME", "BETA"}
    assert all(result.state is DataState.FAILED for result in results.values())

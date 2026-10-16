"""Parquet cache for daily bars, with session-based freshness.

Daily history is large, expensive to refetch, and never changes once a
session has settled — which makes it exactly the wrong thing to keep in
operational Postgres and exactly the right thing to keep as columnar files
under `var/`. Losing the whole directory costs time, not data.

**Freshness is a calendar question, not a clock question.** The cache is
current if it holds the last completed session's close. A time-to-live gets
this wrong in both directions on a daily schedule: every file is always
older than any window you pick, so a short TTL refetches five years of
history every run, and a long one goes stale across a weekend without
noticing. See `akmarkets.domain.sessions`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pandas as pd

from akmarkets.domain.money import to_decimal
from akmarkets.domain.sessions import last_completed_session
from akmarkets.providers.base import Bar, DataState, ProviderResult
from akmarkets.providers.chain import HistoryChain

logger = logging.getLogger(__name__)

_COLUMNS = ["on", "open", "high", "low", "close", "volume", "adjusted_close"]


class PriceCache:
    """Daily bars on disk, refreshed only when a session has been missed.

    An in-memory layer sits in front of the files behind a lock, because a
    background refresh writes while web requests read. The lock guards the
    dictionary, not the filesystem: a partially written Parquet file is
    prevented by writing to a temporary name and renaming.
    """

    def __init__(
        self,
        root: Path,
        chain: HistoryChain,
        calendar: str = "NYSE",
        history_days: int = 1825,
        max_workers: int = 8,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.chain = chain
        self.calendar = calendar
        self.history_days = history_days
        self.max_workers = max_workers
        self._lock = threading.Lock()
        self._memory: dict[str, pd.DataFrame] = {}

    # -- storage --------------------------------------------------------

    def _path(self, symbol: str) -> Path:
        # Tickers contain characters that are awkward in filenames --
        # `BRK.B`, `BF/B`. Encoding them keeps one file per instrument and
        # avoids a directory nobody expected.
        safe = symbol.upper().replace("/", "_SLASH_").replace(".", "_DOT_")
        return self.root / f"{safe}.parquet"

    def load(self, symbol: str) -> pd.DataFrame | None:
        key = symbol.upper()
        with self._lock:
            cached = self._memory.get(key)
        if cached is not None:
            return cached

        path = self._path(symbol)
        if not path.exists():
            return None
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:
            # A corrupt cache file is a recoverable annoyance, not an
            # outage: drop it and let the next fetch rebuild.
            logger.warning("discarding unreadable price cache %s: %s", path, exc)
            path.unlink(missing_ok=True)
            return None
        with self._lock:
            self._memory[key] = frame
        return frame

    def store(self, symbol: str, bars: Sequence[Bar]) -> pd.DataFrame:
        frame = bars_to_frame(bars)
        path = self._path(symbol)
        # Write-then-rename: a reader must never see a half-written file.
        staging = path.with_suffix(".parquet.partial")
        frame.to_parquet(staging, index=False)
        staging.replace(path)
        with self._lock:
            self._memory[symbol.upper()] = frame
        return frame

    # -- freshness ------------------------------------------------------

    def latest_session(self, symbol: str) -> date | None:
        frame = self.load(symbol)
        if frame is None or frame.empty:
            return None
        # pandas types every column as object; these are dates we wrote.
        return cast(date, frame["on"].iloc[-1])

    def is_fresh(self, symbol: str, now: date | None = None) -> bool:
        """Whether the cache holds the last completed session's close."""
        latest = self.latest_session(symbol)
        if latest is None:
            return False
        return latest >= last_completed_session(calendar=self.calendar)

    # -- refresh --------------------------------------------------------

    def ensure(
        self, symbols: Sequence[str], force: bool = False
    ) -> dict[str, ProviderResult[list[Bar]]]:
        """Fetch only what is stale, in parallel, reporting each outcome.

        Returns a result per symbol rather than raising, so one delisted or
        misspelled ticker cannot take down a portfolio refresh. Concurrency
        is in the HTTP, which is where the time goes.
        """
        wanted = [symbol.upper() for symbol in dict.fromkeys(symbols)]
        stale = [s for s in wanted if force or not self.is_fresh(s)]
        results: dict[str, ProviderResult[list[Bar]]] = {
            s: ProviderResult(value=None, state=DataState.HEALTHY, message="cache is current")
            for s in wanted
            if s not in stale
        }
        if not stale:
            return results

        end = date.today()
        start = end - timedelta(days=self.history_days)

        def fetch(symbol: str) -> tuple[str, ProviderResult[list[Bar]]]:
            return symbol, self.chain.fetch_history(symbol, start, end)

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(stale))) as pool:
            for symbol, result in pool.map(fetch, stale):
                if result.value:
                    self.store(symbol, result.value)
                results[symbol] = result
        return results

    # -- reads ----------------------------------------------------------

    def close_on(self, symbol: str, on: date) -> Decimal | None:
        """The close for a date, or the most recent one before it.

        Carrying the previous close backwards is correct for a valuation
        as of a non-trading day -- a portfolio on a Sunday is worth what it
        was worth on Friday. It deliberately never carries *forward* from
        the future, which is what would make a historical replay cheat.
        """
        frame = self.load(symbol)
        if frame is None or frame.empty:
            return None
        eligible = frame[frame["on"] <= on]
        if eligible.empty:
            return None
        return to_decimal(eligible["close"].iloc[-1])

    def closes(self, symbol: str) -> dict[date, Decimal]:
        frame = self.load(symbol)
        if frame is None or frame.empty:
            return {}
        return {
            cast(date, row.on): to_decimal(row.close)
            for row in frame.itertuples(index=False)
        }

    def bars(self, symbol: str) -> list[Bar]:
        frame = self.load(symbol)
        if frame is None or frame.empty:
            return []
        return frame_to_bars(frame)


def bars_to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    """Bars to a frame, sorted and de-duplicated by date.

    Providers occasionally repeat a session across a paginated response.
    Keeping the last occurrence means a corrected bar wins over the original,
    which is the right way round.
    """
    if not bars:
        return pd.DataFrame(columns=_COLUMNS)
    frame = pd.DataFrame(
        [
            {
                "on": bar.on,
                # Stored as float because Parquet has no decimal type worth
                # the trouble here, and this is *market data*, not money in
                # a ledger: a price is already an approximation of a trade,
                # and nothing in the cache is summed into a cost basis.
                # Everything that reaches the accounting engine goes through
                # `to_decimal` on the way out.
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
                "adjusted_close": (
                    float(bar.adjusted_close) if bar.adjusted_close is not None else None
                ),
            }
            for bar in bars
        ]
    )
    frame = frame.drop_duplicates(subset=["on"], keep="last").sort_values("on")
    return frame.reset_index(drop=True)


def frame_to_bars(frame: pd.DataFrame) -> list[Bar]:
    return [
        Bar(
            on=cast(date, row.on),
            open=to_decimal(row.open),
            high=to_decimal(row.high),
            low=to_decimal(row.low),
            close=to_decimal(row.close),
            volume=int(cast(int, row.volume)) if not pd.isna(row.volume) else 0,
            adjusted_close=(
                to_decimal(row.adjusted_close) if not pd.isna(row.adjusted_close) else None
            ),
        )
        for row in frame.itertuples(index=False)
    ]

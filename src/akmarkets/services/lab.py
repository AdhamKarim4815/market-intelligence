"""Strategy Lab service: run a rule over real history and report honestly.

Research only. Nothing here places, suggests, or is wired to an order; the
lab surfaces evidence and the reader decides. That separation is why the
module has no dependency on anything that could execute.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.domain.backtest import (
    HORIZONS,
    AsOf,
    HorizonResult,
    Signal,
    evaluate,
    replay,
    summarise,
)
from akmarkets.domain.indicators import relative_volume, rsi, sma
from akmarkets.domain.market import Bar
from akmarkets.domain.money import ZERO
from akmarkets.services import market_data

Rule = Callable[[str, list[Bar], AsOf], bool]


@dataclass(frozen=True)
class LabResult:
    rule: str
    symbols: tuple[str, ...]
    signals: int
    results: list[HorizonResult]
    first: date | None
    last: date | None

    @property
    def is_meaningful(self) -> bool:
        return any(result.is_meaningful for result in self.results)

    def verdict(self) -> str:
        """A plain reading of whether this is worth anything.

        Deliberately blunt about small samples. The most useful output of a
        backtest on a four-symbol universe is the sentence saying it proves
        nothing.
        """
        if not self.signals:
            return "The rule never fired. Nothing to measure."
        if not self.is_meaningful:
            return (
                f"{self.signals} signal(s) — far too few to conclude anything. "
                f"This is an illustration of the machinery, not evidence."
            )
        primary = next(
            (result for result in self.results if result.horizon == 20), self.results[0]
        )
        if primary.mean_excess is None:
            return "No outcomes at the primary horizon."
        direction = "beat" if primary.mean_excess > ZERO else "lagged"
        return (
            f"Over {primary.count} signals the rule {direction} its benchmark by "
            f"{primary.mean_excess:+.2%} on average at 20 days, hit rate "
            f"{primary.beat_rate:.0%}."
        )


# Named rules. Each is a small, readable function over only the bars visible
# at the time — which is what makes them impossible to accidentally peek.
def rule_oversold(symbol: str, visible: list[Bar], cut: AsOf) -> bool:
    """RSI crossed below 30 today."""
    closes = [bar.close for bar in visible]
    if len(closes) < 20:
        return False
    today = rsi(closes)
    yesterday = rsi(closes[:-1])
    return today is not None and yesterday is not None and yesterday >= 30 > today


def rule_breakout(symbol: str, visible: list[Bar], cut: AsOf) -> bool:
    """Closed above the 50-day average on twice the usual volume."""
    if len(visible) < 55:
        return False
    average = sma([bar.close for bar in visible], 50)
    volume = relative_volume(visible)
    return (
        average is not None
        and volume is not None
        and visible[-1].close > average
        and visible[-2].close <= average
        and volume >= Decimal(2)
    )


def rule_golden_cross(symbol: str, visible: list[Bar], cut: AsOf) -> bool:
    """The 50-day average crossed above the 200-day today."""
    closes = [bar.close for bar in visible]
    if len(closes) < 205:
        return False
    fast_now, slow_now = sma(closes, 50), sma(closes, 200)
    fast_then, slow_then = sma(closes[:-1], 50), sma(closes[:-1], 200)
    if None in (fast_now, slow_now, fast_then, slow_then):
        return False
    return fast_then <= slow_then and fast_now > slow_now  # type: ignore[operator]


RULES: dict[str, Rule] = {
    "oversold": rule_oversold,
    "breakout": rule_breakout,
    "golden-cross": rule_golden_cross,
}


def run(
    session: Session,
    settings: Settings,
    rule_name: str,
    symbols: list[str],
    benchmark: str | None = None,
) -> LabResult:
    """Replay a named rule over the cached history and measure it."""
    rule = RULES[rule_name]
    cache = market_data.build_price_cache(settings)
    series = {symbol: cache.bars(symbol) for symbol in symbols}
    series = {symbol: bars for symbol, bars in series.items() if bars}
    if not series:
        return LabResult(rule_name, tuple(symbols), 0, summarise([]), None, None)

    dates = sorted({bar.on for bars in series.values() for bar in bars})
    steps = replay(dates, series, rule)
    signals: list[Signal] = [
        signal for step in steps for signal in step.signals
    ]

    market = cache.bars(benchmark or settings.portfolio.default_benchmark_symbol)
    outcomes = evaluate(signals, series, benchmark=market or None)

    return LabResult(
        rule=rule_name,
        symbols=tuple(sorted(series)),
        signals=len(signals),
        results=summarise(outcomes),
        first=dates[0] if dates else None,
        last=dates[-1] if dates else None,
    )


__all__ = ["HORIZONS", "RULES", "LabResult", "run"]

"""Performance: portfolio value over time, attribution, benchmarks.

Pure calculation over a transaction log and a price history. No I/O.

The central problem this module exists to solve correctly: **a portfolio's
return is not the change in its value.** Deposit £10,000 into an account
that has not moved and its value doubles while its return is zero. Every
naive performance number is wrong in exactly that way, and it is wrong in
the flattering direction, which is why it survives so long unnoticed.

So contributions and withdrawals are removed from the return. The method
here is a **time-weighted return**: the series is cut at every external cash
flow, a sub-period return is computed across each cut, and those are
chained. That is what makes a comparison with an index meaningful — the
index has no deposits either.

Money-weighted return (IRR) answers a different and also useful question
(*how did my money do, given when I added it?*) and is deliberately not
implemented yet rather than approximated.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from akmarkets.domain.lots import build_state
from akmarkets.domain.money import ZERO, quantize_money
from akmarkets.domain.records import (
    CostBasisMethod,
    Transaction,
    TransactionType,
)

# Transactions that move money in or out of the portfolio from outside it.
# These are what a time-weighted return has to neutralise; a buy or a sell
# only moves money *within* the portfolio and must not be treated as a flow.
_EXTERNAL_FLOWS = {
    TransactionType.DEPOSIT: Decimal(1),
    TransactionType.WITHDRAWAL: Decimal(-1),
    TransactionType.TRANSFER_IN: Decimal(1),
    TransactionType.TRANSFER_OUT: Decimal(-1),
}


@dataclass(frozen=True)
class ValuePoint:
    """A portfolio's value on one date, and any external flow that day."""

    on: date
    value: Decimal | None
    external_flow: Decimal = ZERO
    # True when at least one held position had no price that day. The value
    # is None in that case rather than a partial sum, and every figure
    # derived from the series has to propagate that rather than skip it.
    incomplete: bool = False


@dataclass(frozen=True)
class PeriodReturn:
    """A return over a window, with what it was computed from.

    `start_value`, `end_value` and `net_flow` are carried so the figure can
    be explained rather than merely asserted. A performance number nobody
    can reconstruct is a number nobody should act on.
    """

    start: date
    end: date
    start_value: Decimal | None
    end_value: Decimal | None
    net_flow: Decimal
    time_weighted: Decimal | None
    # Set when the series had a gap. A return computed across missing days
    # is not a return, and this says so instead of quietly producing one.
    incomplete: bool = False

    @property
    def change(self) -> Decimal | None:
        if self.start_value is None or self.end_value is None:
            return None
        return self.end_value - self.start_value - self.net_flow


@dataclass(frozen=True)
class Contribution:
    """One instrument's contribution to a period's profit or loss."""

    instrument_id: str
    profit: Decimal
    # Share of the total absolute movement, so a portfolio that netted to
    # nothing still shows what moved. None when nothing moved at all.
    weight: Decimal | None = None


@dataclass(frozen=True)
class BenchmarkComparison:
    portfolio: Decimal | None
    benchmark: Decimal | None
    symbol: str

    @property
    def relative(self) -> Decimal | None:
        """Excess return, or None if either side is unknown.

        Deliberately arithmetic difference rather than a ratio: over a
        single period the difference is what a reader means by "beat the
        index by", and a geometric excess would need compounding context
        this type does not carry.
        """
        if self.portfolio is None or self.benchmark is None:
            return None
        return self.portfolio - self.benchmark


def external_flow(transaction: Transaction) -> Decimal:
    """Money entering or leaving the portfolio from outside it.

    A buy is not a flow — cash became shares, both inside the portfolio. A
    deposit is. Getting this wrong is the single most common way a
    performance figure is inflated: counting a deposit as a gain.
    """
    direction = _EXTERNAL_FLOWS.get(transaction.transaction_type)
    if direction is None:
        return ZERO
    if transaction.transaction_type in {
        TransactionType.TRANSFER_IN,
        TransactionType.TRANSFER_OUT,
    }:
        return direction * transaction.gross_value
    return direction * transaction.amount


def value_series(
    portfolio_id: str,
    transactions: Sequence[Transaction],
    price_history: dict[str, dict[date, Decimal]],
    dates: Iterable[date],
    method: CostBasisMethod = CostBasisMethod.FIFO,
) -> list[ValuePoint]:
    """The portfolio's value on each requested date.

    `price_history` is per instrument and may be sparse. A date with no
    price for a held instrument produces `value=None` and `incomplete=True`
    rather than a total quietly missing that position, because a total that
    silently omits a holding looks right and is not.
    """
    ordered_dates = sorted(dates)
    flows: dict[date, Decimal] = {}
    for transaction in transactions:
        flow = external_flow(transaction)
        if flow != ZERO:
            flows[transaction.occurred_on] = flows.get(transaction.occurred_on, ZERO) + flow

    points: list[ValuePoint] = []
    for on in ordered_dates:
        prices = _prices_as_of(price_history, on)
        state = build_state(
            portfolio_id=portfolio_id,
            transactions=transactions,
            method=method,
            as_of=on,
            prices=prices,
        )
        total = state.total_value
        points.append(
            ValuePoint(
                on=on,
                value=None if total is None else quantize_money(total),
                external_flow=flows.get(on, ZERO),
                incomplete=total is None,
            )
        )
    return points


def _prices_as_of(
    price_history: dict[str, dict[date, Decimal]], on: date
) -> dict[str, Decimal]:
    """The most recent close at or before `on`, per instrument.

    Carrying the last known close forward across a weekend or a holiday is
    correct. Carrying a *future* price backward would be look-ahead bias, and
    the `<=` here is the only thing preventing it.
    """
    resolved: dict[str, Decimal] = {}
    for instrument_id, closes in price_history.items():
        eligible = [day for day in closes if day <= on]
        if eligible:
            resolved[instrument_id] = closes[max(eligible)]
    return resolved


def time_weighted_return(points: Sequence[ValuePoint]) -> Decimal | None:
    """Chain-linked return with external cash flows removed.

    Each sub-period runs from one valuation to the next:

        r = (closing value − flow during the period) / opening value − 1

    The flow is subtracted from the *closing* value rather than added to
    the opening one, and that choice is the whole correctness of the
    function. A portfolio is valued on the date a flow lands, so its
    closing value already contains that money; adding it to the opening
    side as well would count it twice and understate the return. Concretely
    — $10,000 that rises to $11,000 and then receives a $5,000 deposit —
    subtracting gives the true 10%, while adding to the opening side gives
    6.7%.

    Returns None if any point is missing a value, rather than skipping the
    gap. Chaining across a hole silently attributes a hole's worth of
    movement to the periods either side of it.
    """
    usable = [point for point in points if point.value is not None]
    if len(usable) != len(points) or len(usable) < 2:
        return None

    compounded = Decimal(1)
    for previous, current in pairwise(usable):
        opening = previous.value or ZERO
        if opening == ZERO:
            # A period starting from nothing has no defined return. Skipping
            # it is right: it carries no information, and dividing would
            # invent an infinity.
            continue
        compounded *= ((current.value or ZERO) - current.external_flow) / opening
    return compounded - Decimal(1)


def period_return(points: Sequence[ValuePoint]) -> PeriodReturn | None:
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    return PeriodReturn(
        start=first.on,
        end=last.on,
        start_value=first.value,
        end_value=last.value,
        net_flow=sum((point.external_flow for point in points[1:]), start=ZERO),
        time_weighted=time_weighted_return(points),
        incomplete=any(point.incomplete for point in points),
    )


def attribution(
    portfolio_id: str,
    transactions: Sequence[Transaction],
    price_history: dict[str, dict[date, Decimal]],
    start: date,
    end: date,
    method: CostBasisMethod = CostBasisMethod.FIFO,
) -> list[Contribution]:
    """What actually made or lost money over a window, per instrument.

    Profit is the change in a position's market value over the period, plus
    anything realised inside it, minus what was paid for shares bought
    during it. Without that last term, buying more of something that then
    did nothing would show up as a large gain.

    Positions with no price are omitted rather than counted as zero, so a
    missing quote cannot masquerade as a flat holding.
    """
    opening = build_state(
        portfolio_id, transactions, method=method, as_of=start,
        prices=_prices_as_of(price_history, start),
    )
    closing = build_state(
        portfolio_id, transactions, method=method, as_of=end,
        prices=_prices_as_of(price_history, end),
    )

    opening_by_id = {position.instrument_id: position for position in opening.positions}
    purchases = _net_purchases(transactions, start, end)

    contributions: list[Contribution] = []
    for position in closing.positions:
        before = opening_by_id.get(position.instrument_id)
        end_value = position.market_value
        start_value = before.market_value if before else ZERO

        if end_value is None or (before is not None and before.is_open and start_value is None):
            continue

        realized = position.realized_gain - (before.realized_gain if before else ZERO)
        dividends = position.dividends_received - (
            before.dividends_received if before else ZERO
        )
        profit = (
            end_value
            - (start_value or ZERO)
            - purchases.get(position.instrument_id, ZERO)
            + realized
            + dividends
        )
        contributions.append(
            Contribution(
                instrument_id=position.instrument_id, profit=quantize_money(profit)
            )
        )

    total_movement = sum((abs(item.profit) for item in contributions), start=ZERO)
    weighted = [
        Contribution(
            instrument_id=item.instrument_id,
            profit=item.profit,
            weight=(item.profit / total_movement) if total_movement else None,
        )
        for item in contributions
    ]
    return sorted(weighted, key=lambda item: item.profit, reverse=True)


def _net_purchases(
    transactions: Sequence[Transaction], start: date, end: date
) -> dict[str, Decimal]:
    """Cash spent acquiring shares inside the window, per instrument.

    Subtracted from the value change so that buying more of a flat holding
    does not read as a profit. Sales are excluded here because their effect
    already shows up as realised gain.
    """
    spend: dict[str, Decimal] = {}
    for transaction in transactions:
        if not (start < transaction.occurred_on <= end):
            continue
        if transaction.transaction_type is not TransactionType.BUY:
            continue
        if transaction.instrument_id is None:
            continue
        spend[transaction.instrument_id] = (
            spend.get(transaction.instrument_id, ZERO)
            + transaction.gross_value
            + transaction.fees
        )
    return spend


def benchmark_return(
    closes: dict[date, Decimal], start: date, end: date, symbol: str
) -> Decimal | None:
    """A buy-and-hold return for the index over the same window.

    Uses the last close at or before each endpoint, so a window ending on a
    weekend compares like with like rather than silently shortening.
    """
    opening = _close_at_or_before(closes, start)
    closing = _close_at_or_before(closes, end)
    if opening is None or closing is None or opening == ZERO:
        return None
    return (closing - opening) / opening


def _close_at_or_before(closes: dict[date, Decimal], on: date) -> Decimal | None:
    eligible = [day for day in closes if day <= on]
    return closes[max(eligible)] if eligible else None


def compare_to_benchmark(
    portfolio: Decimal | None, closes: dict[date, Decimal], start: date, end: date, symbol: str
) -> BenchmarkComparison:
    return BenchmarkComparison(
        portfolio=portfolio,
        benchmark=benchmark_return(closes, start, end, symbol),
        symbol=symbol,
    )

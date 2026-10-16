"""Performance: the ways a return number goes wrong, and that it does not."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from akmarkets.domain.money import ZERO
from akmarkets.domain.records import Transaction, TransactionType
from akmarkets.domain.returns import (
    attribution,
    benchmark_return,
    compare_to_benchmark,
    external_flow,
    period_return,
    time_weighted_return,
    value_series,
)

ACME = "acme"
BETA = "beta"
PORTFOLIO = "p1"

_seq = 0


def tx(
    kind: TransactionType,
    on: str,
    quantity: object = 0,
    price: object = 0,
    amount: object = 0,
    instrument: str | None = ACME,
) -> Transaction:
    global _seq
    _seq += 1
    return Transaction(
        transaction_id=f"t{_seq:03d}",
        portfolio_id=PORTFOLIO,
        instrument_id=instrument,
        transaction_type=kind,
        occurred_on=date.fromisoformat(on),
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        quantity=quantity,
        price=price,
        amount=amount,
    )


def closes(*pairs: tuple[str, str]) -> dict[date, Decimal]:
    return {date.fromisoformat(day): Decimal(price) for day, price in pairs}


# -- what counts as a flow ----------------------------------------------


def test_a_buy_is_not_an_external_flow() -> None:
    """Cash became shares; both are inside the portfolio. Counting a buy as
    a flow is how a return gets neutralised out of existence."""
    assert external_flow(tx(TransactionType.BUY, "2026-01-05", 100, 10)) == ZERO
    assert external_flow(tx(TransactionType.SELL, "2026-01-05", 100, 10)) == ZERO
    assert external_flow(tx(TransactionType.DIVIDEND, "2026-01-05", amount=50)) == ZERO


def test_deposits_and_withdrawals_are_flows_with_the_right_sign() -> None:
    deposit = tx(TransactionType.DEPOSIT, "2026-01-05", amount=1000, instrument=None)
    withdrawal = tx(TransactionType.WITHDRAWAL, "2026-01-05", amount=400, instrument=None)
    assert external_flow(deposit) == Decimal("1000")
    assert external_flow(withdrawal) == Decimal("-400")


def test_transfers_are_flows_valued_at_the_shares_moved() -> None:
    moved = tx(TransactionType.TRANSFER_IN, "2026-01-05", 10, 20)
    assert external_flow(moved) == Decimal("200")


# -- the central bug this module exists to prevent ----------------------


def test_a_deposit_into_a_flat_portfolio_is_a_zero_return() -> None:
    """The single most common way a performance figure is inflated.

    Deposit $10,000 into an account that has not moved: value doubles,
    return is zero. A naive change-in-value number reports +100%, and it is
    wrong in the flattering direction, which is why it survives unnoticed.
    """
    ledger = [
        tx(TransactionType.DEPOSIT, "2026-01-01", amount=10_000, instrument=None),
        tx(TransactionType.BUY, "2026-01-01", 1000, 10),
        tx(TransactionType.DEPOSIT, "2026-02-01", amount=10_000, instrument=None),
    ]
    history = {ACME: closes(("2026-01-01", "10"), ("2026-02-01", "10"), ("2026-03-01", "10"))}
    points = value_series(
        PORTFOLIO, ledger, history,
        [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)],
    )
    # Value really did double.
    assert points[0].value == Decimal("10000.0000")
    assert points[-1].value == Decimal("20000.0000")
    # The return did not.
    assert time_weighted_return(points) == ZERO


def test_a_gain_survives_a_deposit_made_the_same_period() -> None:
    """Neutralising flows must not neutralise the actual performance.

    Start with $10,000 of ACME. It rises 10% to $11,000, then $5,000 is
    deposited. The period return is 10%, not the 60% a change-in-value
    number would report.
    """
    ledger = [
        tx(TransactionType.DEPOSIT, "2026-01-01", amount=10_000, instrument=None),
        tx(TransactionType.BUY, "2026-01-01", 1000, 10),
        tx(TransactionType.DEPOSIT, "2026-02-01", amount=5_000, instrument=None),
    ]
    history = {ACME: closes(("2026-01-01", "10"), ("2026-02-01", "11"))}
    points = value_series(PORTFOLIO, ledger, history, [date(2026, 1, 1), date(2026, 2, 1)])
    assert points[-1].value == Decimal("16000.0000")
    assert time_weighted_return(points) == Decimal("0.1")


def test_sub_period_returns_are_chained_not_averaged() -> None:
    """+10% then +10% is +21%, not +20%. Averaging is the other classic
    way a multi-period number ends up wrong."""
    ledger = [
        tx(TransactionType.DEPOSIT, "2026-01-01", amount=1000, instrument=None),
        tx(TransactionType.BUY, "2026-01-01", 100, 10),
    ]
    history = {
        ACME: closes(("2026-01-01", "10"), ("2026-02-01", "11"), ("2026-03-01", "12.1"))
    }
    points = value_series(
        PORTFOLIO, ledger, history,
        [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)],
    )
    assert time_weighted_return(points) == Decimal("0.21")


def test_a_gap_in_the_series_makes_the_return_none_not_approximate() -> None:
    """Chaining across a hole silently attributes a hole's worth of
    movement to the periods either side."""
    ledger = [
        tx(TransactionType.DEPOSIT, "2026-01-01", amount=1000, instrument=None),
        tx(TransactionType.BUY, "2026-01-01", 100, 10),
    ]
    # No price at all: the position cannot be valued on any date.
    points = value_series(
        PORTFOLIO, ledger, {}, [date(2026, 1, 1), date(2026, 2, 1)]
    )
    assert all(point.incomplete for point in points)
    assert time_weighted_return(points) is None

    summary = period_return(points)
    assert summary is not None
    assert summary.incomplete is True
    assert summary.time_weighted is None


def test_a_period_return_carries_what_it_was_computed_from() -> None:
    """A performance number nobody can reconstruct is one nobody should
    act on."""
    ledger = [
        tx(TransactionType.DEPOSIT, "2026-01-01", amount=1000, instrument=None),
        tx(TransactionType.BUY, "2026-01-01", 100, 10),
        tx(TransactionType.DEPOSIT, "2026-02-01", amount=500, instrument=None),
    ]
    history = {ACME: closes(("2026-01-01", "10"), ("2026-02-01", "11"))}
    points = value_series(PORTFOLIO, ledger, history, [date(2026, 1, 1), date(2026, 2, 1)])
    summary = period_return(points)
    assert summary is not None
    assert summary.start_value == Decimal("1000.0000")
    assert summary.end_value == Decimal("1600.0000")
    assert summary.net_flow == Decimal("500")
    # Value rose 600, of which 500 was deposited: 100 was actually earned.
    assert summary.change == Decimal("100.0000")


# -- look-ahead ---------------------------------------------------------


def test_a_valuation_never_uses_a_price_from_the_future() -> None:
    """Carrying the last close forward over a weekend is correct. Carrying
    a future price backward is look-ahead bias, and would make every
    historical figure quietly optimistic."""
    ledger = [
        # Funded first: a portfolio total is holdings *plus cash*, so buying
        # on an unfunded account correctly nets to zero and would tell us
        # nothing about which price was used.
        tx(TransactionType.DEPOSIT, "2026-01-05", amount=1000, instrument=None),
        tx(TransactionType.BUY, "2026-01-05", 100, 10),
    ]
    history = {ACME: closes(("2026-01-05", "10"), ("2026-01-09", "99"))}
    points = value_series(
        PORTFOLIO, ledger, history, [date(2026, 1, 7), date(2026, 1, 9)]
    )
    # 7 January sees the 5 January close, not the spike three days later.
    assert points[0].value == Decimal("1000.0000")
    assert points[1].value == Decimal("9900.0000")


# -- attribution --------------------------------------------------------


def test_attribution_names_what_actually_moved() -> None:
    """ACME 100 shares 10 → 13 is +$300; BETA 50 shares 20 → 18 is −$100."""
    ledger = [
        tx(TransactionType.BUY, "2026-01-01", 100, 10, instrument=ACME),
        tx(TransactionType.BUY, "2026-01-01", 50, 20, instrument=BETA),
    ]
    history = {
        ACME: closes(("2026-01-01", "10"), ("2026-03-01", "13")),
        BETA: closes(("2026-01-01", "20"), ("2026-03-01", "18")),
    }
    results = attribution(PORTFOLIO, ledger, history, date(2026, 1, 1), date(2026, 3, 1))
    by_id = {item.instrument_id: item for item in results}
    assert by_id[ACME].profit == Decimal("300.0000")
    assert by_id[BETA].profit == Decimal("-100.0000")
    # Sorted best first, so the top of the list is what carried the period.
    assert [item.instrument_id for item in results] == [ACME, BETA]


def test_buying_more_of_a_flat_holding_is_not_a_profit() -> None:
    """Without subtracting in-period purchases, doubling a position that
    then does nothing reads as a 100% gain."""
    ledger = [
        tx(TransactionType.BUY, "2026-01-01", 100, 10),
        tx(TransactionType.BUY, "2026-02-01", 100, 10),
    ]
    history = {
        ACME: closes(("2026-01-01", "10"), ("2026-02-01", "10"), ("2026-03-01", "10"))
    }
    results = attribution(PORTFOLIO, ledger, history, date(2026, 1, 1), date(2026, 3, 1))
    assert results[0].profit == ZERO


def test_an_unpriced_position_is_omitted_rather_than_counted_as_flat() -> None:
    """A missing quote must not masquerade as a holding that did nothing."""
    ledger = [
        tx(TransactionType.BUY, "2026-01-01", 100, 10, instrument=ACME),
        tx(TransactionType.BUY, "2026-01-01", 50, 20, instrument=BETA),
    ]
    history = {ACME: closes(("2026-01-01", "10"), ("2026-03-01", "13"))}
    results = attribution(PORTFOLIO, ledger, history, date(2026, 1, 1), date(2026, 3, 1))
    assert [item.instrument_id for item in results] == [ACME]


def test_weights_are_shares_of_absolute_movement() -> None:
    """A portfolio that netted to nothing still has to show what moved."""
    ledger = [
        tx(TransactionType.BUY, "2026-01-01", 100, 10, instrument=ACME),
        tx(TransactionType.BUY, "2026-01-01", 100, 10, instrument=BETA),
    ]
    history = {
        ACME: closes(("2026-01-01", "10"), ("2026-03-01", "13")),
        BETA: closes(("2026-01-01", "10"), ("2026-03-01", "7")),
    }
    results = attribution(PORTFOLIO, ledger, history, date(2026, 1, 1), date(2026, 3, 1))
    assert sum(item.profit for item in results) == ZERO
    assert {item.weight for item in results} == {Decimal("0.5"), Decimal("-0.5")}


# -- benchmarks ---------------------------------------------------------


def test_a_benchmark_window_uses_the_last_close_at_or_before_each_end() -> None:
    """A window ending on a Saturday must compare like with like rather
    than silently shortening itself."""
    spy = closes(("2026-01-02", "100"), ("2026-03-06", "110"))
    assert benchmark_return(spy, date(2026, 1, 4), date(2026, 3, 8), "SPY") == Decimal("0.1")


def test_a_benchmark_with_no_data_in_range_is_none_not_zero() -> None:
    """Zero would read as "the index went nowhere", which is a claim."""
    spy = closes(("2026-05-01", "100"))
    assert benchmark_return(spy, date(2026, 1, 1), date(2026, 3, 1), "SPY") is None


def test_relative_return_is_none_when_either_side_is_unknown() -> None:
    spy = closes(("2026-01-02", "100"), ("2026-03-06", "110"))
    known = compare_to_benchmark(Decimal("0.15"), spy, date(2026, 1, 2), date(2026, 3, 6), "SPY")
    assert known.relative == Decimal("0.05")

    unknown = compare_to_benchmark(None, spy, date(2026, 1, 2), date(2026, 3, 6), "SPY")
    assert unknown.relative is None
    no_index = compare_to_benchmark(
        Decimal("0.15"), {}, date(2026, 1, 2), date(2026, 3, 6), "SPY"
    )
    assert no_index.relative is None

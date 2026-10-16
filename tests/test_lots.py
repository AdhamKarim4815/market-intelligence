"""Tax-lot accounting.

The acceptance gate for financial correctness. Every case the specification
names is here as its own test, plus the property tests that catch what
enumerated cases do not. Figures are asserted against values computed by
hand in the docstring, so a failure says which arithmetic is wrong rather
than merely that something changed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from akmarkets.domain.lots import build_state, ordered, replay
from akmarkets.domain.money import ZERO
from akmarkets.domain.records import CostBasisMethod, Transaction, TransactionType

ACME = "instrument-acme"
BETA = "instrument-beta"
PORTFOLIO = "portfolio-1"

_counter = 0


def tx(
    kind: TransactionType,
    on: str,
    quantity: object = 0,
    price: object = 0,
    fees: object = 0,
    amount: object = 0,
    instrument: str | None = ACME,
    split_ratio: object = None,
    transaction_id: str | None = None,
) -> Transaction:
    """Build a transaction with a deterministic id and recorded_at.

    Ids increment so same-day ordering is stable and reproducible, which
    matters because lot selection is asserted by id.
    """
    global _counter
    _counter += 1
    return Transaction(
        transaction_id=transaction_id or f"t{_counter:03d}",
        portfolio_id=PORTFOLIO,
        instrument_id=instrument,
        transaction_type=kind,
        occurred_on=date.fromisoformat(on),
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        quantity=quantity,
        price=price,
        fees=fees,
        amount=amount,
        split_ratio=split_ratio,
    )


def position(state: object, instrument: str = ACME) -> object:
    return next(p for p in state.positions if p.instrument_id == instrument)  # type: ignore[attr-defined]


# -- the specification's named cases ------------------------------------


def test_one_buy() -> None:
    """100 @ $10 with $5 fee → 100 shares, basis 100×10 + 5 = $1,005."""
    state = build_state(PORTFOLIO, [tx(TransactionType.BUY, "2026-01-05", 100, 10, fees=5)])
    held = position(state)
    assert held.quantity == Decimal("100")
    assert held.cost_basis == Decimal("1005.0000")
    assert held.average_cost == Decimal("10.05")
    assert state.cash_total == Decimal("-1005.0000")


def test_multiple_buys_average_cost() -> None:
    """100 @ $10 then 50 @ $16 → 150 shares, basis $1,800, average $12."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.BUY, "2026-02-05", 50, 16),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("150")
    assert held.cost_basis == Decimal("1800.0000")
    assert held.average_cost == Decimal("12")
    assert len(held.lots) == 2


def test_partial_sell_fifo_takes_the_oldest_lot() -> None:
    """Buy 100 @ $10, buy 50 @ $20, sell 120 @ $30.

    FIFO consumes all 100 of the first lot and 20 of the second:
    basis = 100×10 + 20×20 = $1,400. Proceeds 120×30 = $3,600.
    Realised gain = $2,200. Remaining 30 shares at $20 = $600 basis.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.BUY, "2026-02-05", 50, 20),
            tx(TransactionType.SELL, "2026-03-05", 120, 30),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("30")
    assert held.cost_basis == Decimal("600.0000")
    assert held.realized_gain == Decimal("2200.0000")
    assert state.disposals[0].cost_basis == Decimal("1400")


def test_partial_sell_lifo_takes_the_newest_lot() -> None:
    """Same trades under LIFO: 50 @ $20 then 70 @ $10 = $1,700 basis.

    Realised gain = 3,600 − 1,700 = $1,900, and the 30 shares left are the
    *oldest* ones at $10, not the newest at $20. LIFO and FIFO disagreeing
    here is the whole reason lots are preserved.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.BUY, "2026-02-05", 50, 20),
            tx(TransactionType.SELL, "2026-03-05", 120, 30),
        ],
        method=CostBasisMethod.LIFO,
    )
    held = position(state)
    assert held.quantity == Decimal("30")
    assert held.cost_basis == Decimal("300.0000")
    assert held.realized_gain == Decimal("1900.0000")


def test_partial_sell_average_slices_every_lot() -> None:
    """100 @ $10 plus 50 @ $16 → 150 shares, $1,800 basis, $12 average.

    Selling 120 takes basis 120×12 = $1,440 and realises 3,600 − 1,440 =
    $2,160, leaving 30 shares worth $360. Both lots survive, each reduced by
    the same 80%, so acquisition dates are not destroyed.

    Note this is the same trade set as the FIFO and LIFO cases above at a
    different second price, and all three produce different realised gains —
    which is why the lots are kept rather than a running average.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.BUY, "2026-02-05", 50, 16),
            tx(TransactionType.SELL, "2026-03-05", 120, 30),
        ],
        method=CostBasisMethod.AVERAGE,
    )
    held = position(state)
    assert held.quantity == Decimal("30")
    assert held.cost_basis == Decimal("360.0000")
    assert held.realized_gain == Decimal("2160.0000")
    assert len(held.lots) == 2
    assert [lot.quantity for lot in held.lots] == [Decimal("20"), Decimal("10")]


def test_average_cost_with_a_repeating_fraction_leaves_no_residue() -> None:
    """Selling a third of a holding gives a non-terminating fraction.

    100 @ $10 plus 50 @ $20 is $2,000 over 150 shares — an average of
    $13.33…. Selling all 150 must close the position exactly, with no
    sliver left by the repeating slice, and realise 150×30 − 2,000 = $2,500.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.BUY, "2026-02-05", 50, 20),
            tx(TransactionType.SELL, "2026-03-05", 50, 30),
            tx(TransactionType.SELL, "2026-04-05", 100, 30),
        ],
        method=CostBasisMethod.AVERAGE,
    )
    held = position(state)
    assert held.quantity == ZERO
    assert held.lots == ()
    assert held.realized_gain == Decimal("2500.0000")


def test_full_exit_closes_the_position_completely() -> None:
    """No dust left behind, and average cost is None rather than $0.00."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.SELL, "2026-03-05", 100, 15),
        ],
    )
    held = position(state)
    assert held.quantity == ZERO
    assert held.lots == ()
    assert held.is_open is False
    assert held.average_cost is None
    assert held.realized_gain == Decimal("500.0000")


def test_re_entry_after_a_full_exit_starts_a_fresh_lot() -> None:
    """Realised gain from the first round survives; basis does not.

    Re-entering must not resurrect the closed lot's cost. The new position
    is 40 @ $20; the $500 already realised stays realised.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.SELL, "2026-03-05", 100, 15),
            tx(TransactionType.BUY, "2026-06-05", 40, 20),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("40")
    assert held.cost_basis == Decimal("800.0000")
    assert held.realized_gain == Decimal("500.0000")
    assert len(held.lots) == 1


def test_backdated_trade_changes_which_lot_a_later_sale_consumed() -> None:
    """A trade entered late but dated early is not a late trade.

    Inserting a cheaper 1 February lot after the fact must make the 1 March
    FIFO sale consume it, which changes the realised gain retroactively.
    Replaying the ledger is what makes that correct without a rebuild.
    """
    without = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-03-01", 100, 20),
            tx(TransactionType.SELL, "2026-03-10", 50, 30),
        ],
    )
    with_backdated = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-03-01", 100, 20),
            tx(TransactionType.SELL, "2026-03-10", 50, 30),
            # Recorded last, dated first.
            tx(TransactionType.BUY, "2026-02-01", 50, 8),
        ],
    )
    assert position(without).realized_gain == Decimal("500.0000")
    # FIFO now takes the 50 cheap shares: 50×30 − 50×8 = $1,100.
    assert position(with_backdated).realized_gain == Decimal("1100.0000")


def test_fees_are_capitalised_into_basis_on_buy_and_deducted_on_sell() -> None:
    """Buy 100 @ $10 + $10 fee = $1,010 basis.

    Sell 100 @ $12 with a $10 fee: proceeds $1,200, gain
    1,200 − 1,010 − 10 = $180. Cash out 1,010, in 1,190, net $180.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10, fees=10),
            tx(TransactionType.SELL, "2026-03-05", 100, 12, fees=10),
        ],
    )
    held = position(state)
    assert held.realized_gain == Decimal("180.0000")
    assert held.fees_paid == Decimal("20.0000")
    assert state.cash_total == Decimal("180.0000")


def test_cash_dividend_adds_cash_without_touching_basis() -> None:
    """A dividend is income, not a return of capital in this model."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.DIVIDEND, "2026-02-05", amount=45),
        ],
    )
    held = position(state)
    assert held.cost_basis == Decimal("1000.0000")
    assert held.dividends_received == Decimal("45.0000")
    assert state.cash_total == Decimal("-955.0000")


def test_reinvested_dividend_adds_shares_that_carry_real_basis() -> None:
    """2 shares at $22.50 arrive with $45 of basis, not zero.

    Booking them at zero cost would make the eventual sale look like pure
    profit and overstate the return.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.DIVIDEND_REINVEST, "2026-02-05", 2, "22.50"),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("102")
    assert held.cost_basis == Decimal("1045.0000")
    assert held.dividends_received == Decimal("45.0000")
    # No net cash movement: credited and immediately spent.
    assert state.cash_total == Decimal("-1000.0000")


def test_forward_split_preserves_total_cost_basis() -> None:
    """3-for-1: 100 @ $30 becomes 300 @ $10. Basis stays $3,000.

    Multiplying quantity without dividing cost per share is how a split
    silently triples a portfolio's cost basis and destroys every return
    figure computed afterwards.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 30),
            tx(TransactionType.SPLIT, "2026-02-05", split_ratio=3),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("300")
    assert held.cost_basis == Decimal("3000.0000")
    assert held.average_cost == Decimal("10")


def test_reverse_split_preserves_total_cost_basis() -> None:
    """1-for-10: 1000 @ $0.50 becomes 100 @ $5. Basis stays $500."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 1000, "0.50"),
            tx(TransactionType.SPLIT, "2026-02-05", split_ratio="0.1"),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("100")
    assert held.cost_basis == Decimal("500.0000")


def test_split_on_the_same_day_as_a_sale_is_applied_first() -> None:
    """Otherwise the sale consumes pre-split shares at post-split prices.

    Buy 100 @ $30, split 3-for-1, then sell 150 @ $12 the same day. After
    the split there are 300 shares at $10; selling 150 realises
    150×12 − 150×10 = $300 and leaves 150 shares.
    """
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 30),
            tx(TransactionType.SELL, "2026-02-05", 150, 12),
            tx(TransactionType.SPLIT, "2026-02-05", split_ratio=3),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("150")
    assert held.realized_gain == Decimal("300.0000")


def test_same_day_buy_is_ordered_before_a_sell() -> None:
    """A day trade is ordinary and must not produce an oversell finding."""
    result = replay(
        [
            tx(TransactionType.SELL, "2026-01-05", 10, 12),
            tx(TransactionType.BUY, "2026-01-05", 10, 10),
        ]
    )
    assert result.findings == []
    assert result.books[ACME].realized_gain == Decimal("20")


def test_deposits_and_withdrawals_move_only_cash() -> None:
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.DEPOSIT, "2026-01-01", amount=10_000, instrument=None),
            tx(TransactionType.WITHDRAWAL, "2026-06-01", amount=2_500, instrument=None),
            tx(TransactionType.INTEREST, "2026-06-30", amount="12.34", instrument=None),
        ],
    )
    assert state.cash_total == Decimal("7512.3400")
    assert state.positions == ()


def test_transfer_out_realises_nothing() -> None:
    """The shares left; nobody was paid. Booking zero proceeds would
    realise the entire cost basis as a loss."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.TRANSFER_OUT, "2026-03-05", 40, 0),
        ],
    )
    held = position(state)
    assert held.quantity == Decimal("60")
    assert held.cost_basis == Decimal("600.0000")
    assert held.realized_gain == ZERO


def test_transfer_in_without_a_price_is_flagged_not_guessed() -> None:
    """Zero basis would overstate the gain on eventual sale, so it is
    recorded as a finding rather than quietly accepted."""
    result = replay([tx(TransactionType.TRANSFER_IN, "2026-01-05", 100, 0)])
    assert [finding.code for finding in result.findings] == ["transfer_without_basis"]


def test_multiple_instruments_are_accounted_independently() -> None:
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10, instrument=ACME),
            tx(TransactionType.BUY, "2026-01-05", 20, 50, instrument=BETA),
            tx(TransactionType.SELL, "2026-03-05", 100, 12, instrument=ACME),
        ],
    )
    assert position(state, ACME).quantity == ZERO
    assert position(state, BETA).quantity == Decimal("20")
    assert position(state, BETA).realized_gain == ZERO
    assert state.realized_gain == Decimal("200.0000")


# -- historical reconstruction ------------------------------------------


def test_as_of_reconstructs_the_portfolio_at_a_past_date() -> None:
    """"What did my portfolio look like on 1 February" without snapshots."""
    ledger = [
        tx(TransactionType.BUY, "2026-01-05", 100, 10),
        tx(TransactionType.BUY, "2026-03-05", 50, 20),
        tx(TransactionType.SELL, "2026-04-05", 60, 25),
    ]
    february = build_state(PORTFOLIO, ledger, as_of=date(2026, 2, 1))
    assert position(february).quantity == Decimal("100")
    assert position(february).realized_gain == ZERO

    april = build_state(PORTFOLIO, ledger, as_of=date(2026, 4, 30))
    assert position(april).quantity == Decimal("90")


def test_as_of_before_any_transaction_is_an_empty_portfolio() -> None:
    state = build_state(
        PORTFOLIO,
        [tx(TransactionType.BUY, "2026-01-05", 100, 10)],
        as_of=date(2025, 12, 31),
    )
    assert state.positions == ()
    assert state.cash_total == ZERO


# -- pricing, and refusing to fabricate a total -------------------------


def test_market_value_and_unrealised_gain_use_a_supplied_price() -> None:
    state = build_state(
        PORTFOLIO,
        [tx(TransactionType.BUY, "2026-01-05", 100, 10)],
        prices={ACME: Decimal("14.50")},
        price_dates={ACME: date(2026, 6, 1)},
    )
    held = position(state)
    assert held.market_value == Decimal("1450.00")
    assert held.unrealized_gain == Decimal("450.0000")
    assert state.total_value == Decimal("450.0000")


def test_an_unpriced_position_makes_the_total_none_rather_than_wrong() -> None:
    """A total that silently omits a holding is worse than no total,
    because it looks right."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10, instrument=ACME),
            tx(TransactionType.BUY, "2026-01-05", 20, 50, instrument=BETA),
        ],
        prices={ACME: Decimal("14.50")},
    )
    assert state.unpriced_instrument_ids == (BETA,)
    assert state.positions_value is None
    assert state.total_value is None
    assert state.unrealized_gain is None
    assert position(state, BETA).market_value is None


def test_a_closed_position_does_not_need_a_price() -> None:
    """Nothing is held, so nothing is unpriced."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.SELL, "2026-03-05", 100, 12),
        ],
    )
    assert state.unpriced_instrument_ids == ()
    assert state.total_value == Decimal("200.0000")


# -- data that does not add up ------------------------------------------


def test_overselling_is_reported_and_still_computes_what_it_can() -> None:
    """A portfolio rebuilt from partial history routinely has a sale with
    no matching purchase. Refusing to compute anything would make the tool
    useless exactly when it is most needed."""
    result = replay([tx(TransactionType.SELL, "2026-01-05", 50, 20)])
    assert [finding.code for finding in result.findings] == ["oversold"]
    assert result.books[ACME].realized_gain == Decimal("1000")


def test_a_position_transaction_without_an_instrument_is_rejected() -> None:
    result = replay([tx(TransactionType.BUY, "2026-01-05", 10, 10, instrument=None)])
    assert [finding.code for finding in result.findings] == ["missing_instrument"]
    assert result.books == {}


def test_a_split_without_a_ratio_is_reported_not_assumed() -> None:
    result = replay(
        [
            tx(TransactionType.BUY, "2026-01-05", 100, 10),
            tx(TransactionType.SPLIT, "2026-02-05"),
        ]
    )
    assert [finding.code for finding in result.findings] == ["invalid_split_ratio"]
    assert result.books[ACME].quantity == Decimal("100")


def test_negative_magnitudes_are_rejected_at_construction() -> None:
    """Direction comes from the type, never a sign. A signed quantity is one
    field carrying two meanings, and the resulting errors are silent."""
    with pytest.raises(ValueError, match="magnitudes"):
        tx(TransactionType.BUY, "2026-01-05", -10, 10)


# -- properties ---------------------------------------------------------

_buy_or_sell = st.sampled_from([TransactionType.BUY, TransactionType.SELL])


@hypothesis_settings(max_examples=200, deadline=None)
@given(
    trades=st.lists(
        st.tuples(
            _buy_or_sell,
            st.integers(min_value=1, max_value=500),
            st.integers(min_value=1, max_value=200),
            st.integers(min_value=0, max_value=400),
        ),
        min_size=1,
        max_size=25,
    )
)
def test_quantity_never_goes_negative_and_lots_sum_to_it(
    trades: list[tuple[TransactionType, int, int, int]],
) -> None:
    """Two invariants that must hold whatever the ledger contains.

    Holdings cannot be negative (this product has no short model), and the
    sum of open lot quantities must equal the reported position — a drift
    between those two is the signature of a lot-consumption bug.
    """
    ledger = [
        tx(kind, f"2026-01-{1 + (day % 28):02d}", quantity, price)
        for kind, quantity, price, day in trades
    ]
    state = build_state(PORTFOLIO, ledger)
    for held in state.positions:
        assert held.quantity >= ZERO
        assert sum((lot.quantity for lot in held.lots), start=ZERO) == held.quantity


@hypothesis_settings(max_examples=100, deadline=None)
@given(
    buys=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=100),
            st.integers(min_value=1, max_value=100),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_cost_basis_equals_what_was_paid_when_nothing_is_sold(
    buys: list[tuple[int, int]],
) -> None:
    """With no disposals, basis is exactly the sum of the purchases. Any
    divergence means a lot is being mutated when it should not be."""
    ledger = [
        tx(TransactionType.BUY, "2026-01-05", quantity, price) for quantity, price in buys
    ]
    expected = sum((Decimal(q) * Decimal(p) for q, p in buys), start=ZERO)
    assert position(build_state(PORTFOLIO, ledger)).cost_basis == expected


@hypothesis_settings(max_examples=100, deadline=None)
@given(ratio=st.sampled_from(["2", "3", "5", "0.5", "0.1", "1.5"]))
def test_a_split_never_changes_total_cost_basis(ratio: str) -> None:
    before = build_state(PORTFOLIO, [tx(TransactionType.BUY, "2026-01-05", 137, "13.37")])
    after = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", 137, "13.37"),
            tx(TransactionType.SPLIT, "2026-02-05", split_ratio=ratio),
        ],
    )
    assert position(after).cost_basis == position(before).cost_basis


def test_ordering_is_total_and_deterministic() -> None:
    """Same ledger, any input order, identical result. Without a total
    ordering the answer would depend on insertion order."""
    ledger = [
        tx(TransactionType.SELL, "2026-02-05", 30, 20, transaction_id="c"),
        tx(TransactionType.BUY, "2026-01-05", 100, 10, transaction_id="a"),
        tx(TransactionType.SPLIT, "2026-02-05", split_ratio=2, transaction_id="b"),
    ]
    forward = [t.transaction_id for t in ordered(ledger)]
    backward = [t.transaction_id for t in ordered(list(reversed(ledger)))]
    assert forward == backward == ["a", "b", "c"]


def test_a_full_round_trip_leaves_no_residual_dust() -> None:
    """Buying and selling thirds is where floating point leaves a closed
    position permanently open by 1e-17 shares."""
    state = build_state(
        PORTFOLIO,
        [
            tx(TransactionType.BUY, "2026-01-05", "33.33333333", "3.33"),
            tx(TransactionType.BUY, "2026-01-06", "33.33333333", "6.66"),
            tx(TransactionType.SELL, "2026-02-05", "66.66666666", "9.99"),
        ],
    )
    held = position(state)
    assert held.quantity == ZERO
    assert held.lots == ()
    assert held.is_open is False

"""The tax-lot engine: transactions in, positions and realised gains out.

This is the most consequential module in the application. Everything the
product says about performance is downstream of it, so it is written to be
read: pure functions over an immutable transaction log, no storage, no
network, no clock.

**Why lots rather than `shares × average price`.** Average cost cannot
answer the questions the product needs answered. It cannot say which shares
were sold, so it cannot produce a defensible realised gain; it cannot say
how long a parcel was held; and it cannot be re-derived under a different
method once the underlying detail has been discarded. Keeping lots preserves
every one of those, and average cost remains available as a *view* over
them. Discarding detail is a one-way door.

**Replay, not incremental update.** The state at any date is computed by
replaying the ledger from the beginning. That sounds wasteful and is not:
portfolios of this size replay in microseconds, and it makes backdated
entries and corrections correct for free. An incrementally maintained
position would need every historical figure recomputed anyway the moment a
trade from three months ago was edited — which is a normal operation here,
not an edge case.

**Ordering is explicit and total.** Transactions are sorted by
`occurred_on`, then by a per-type rank, then by `recorded_at`, then by id.
The type rank matters: a split recorded on the same day as a sale must be
applied before the sale, or the sale consumes shares at the wrong ratio.
Sorting only by date leaves that to insertion order, which is not stable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from akmarkets.domain.money import (
    ZERO,
    is_zero_quantity,
    quantize_money,
    quantize_quantity,
)
from akmarkets.domain.records import (
    CashBalance,
    CostBasisMethod,
    Disposal,
    Lot,
    PortfolioState,
    Position,
    Transaction,
    TransactionType,
    ValidationFinding,
)

# Same-day ordering. A split must be applied before any trade that day, or
# the trade uses pre-split share counts against post-split prices. Buys
# precede sells so a same-day round trip has something to sell -- the
# alternative is a spurious oversell finding on a perfectly ordinary day
# trade.
_TYPE_RANK: dict[TransactionType, int] = {
    TransactionType.SPLIT: 0,
    TransactionType.DEPOSIT: 1,
    TransactionType.TRANSFER_IN: 2,
    TransactionType.BUY: 3,
    TransactionType.DIVIDEND: 4,
    TransactionType.DIVIDEND_REINVEST: 5,
    TransactionType.INTEREST: 6,
    TransactionType.SELL: 7,
    TransactionType.TRANSFER_OUT: 8,
    TransactionType.FEE: 9,
    TransactionType.WITHDRAWAL: 10,
}


def sort_key(transaction: Transaction) -> tuple[date, int, str, str]:
    return (
        transaction.occurred_on,
        _TYPE_RANK.get(transaction.transaction_type, 50),
        transaction.recorded_at.isoformat(),
        transaction.transaction_id,
    )


def ordered(transactions: Iterable[Transaction]) -> list[Transaction]:
    return sorted(transactions, key=sort_key)


@dataclass
class _OpenLot:
    """Mutable working copy of a lot during replay."""

    lot_id: str
    instrument_id: str
    opened_on: date
    quantity: Decimal
    cost_per_share: Decimal
    fees: Decimal
    source_transaction_id: str

    def freeze(self) -> Lot:
        return Lot(
            lot_id=self.lot_id,
            instrument_id=self.instrument_id,
            opened_on=self.opened_on,
            quantity=quantize_quantity(self.quantity),
            cost_per_share=quantize_money(self.cost_per_share),
            fees=quantize_money(self.fees),
            source_transaction_id=self.source_transaction_id,
        )


@dataclass
class _InstrumentBook:
    """Everything accumulated for one instrument during replay."""

    lots: list[_OpenLot] = field(default_factory=list)
    disposals: list[Disposal] = field(default_factory=list)
    realized_gain: Decimal = ZERO
    dividends_received: Decimal = ZERO
    fees_paid: Decimal = ZERO

    @property
    def quantity(self) -> Decimal:
        return sum((lot.quantity for lot in self.lots), start=ZERO)

    @property
    def cost_basis(self) -> Decimal:
        return sum(
            (lot.quantity * lot.cost_per_share + lot.fees for lot in self.lots),
            start=ZERO,
        )


@dataclass
class ReplayResult:
    """The full outcome of a replay, including what looked wrong."""

    books: dict[str, _InstrumentBook]
    cash: dict[str, Decimal]
    findings: list[ValidationFinding]
    as_of: date | None


def _select_lots(
    lots: Sequence[_OpenLot], method: CostBasisMethod
) -> list[_OpenLot]:
    """Order lots for consumption by a sale.

    FIFO and LIFO differ only here. AVERAGE is handled at consumption time
    rather than by ordering, because it does not pick a lot at all -- it
    takes a proportional slice of every open one.
    """
    if method is CostBasisMethod.LIFO:
        return sorted(lots, key=lambda lot: (lot.opened_on, lot.lot_id), reverse=True)
    return sorted(lots, key=lambda lot: (lot.opened_on, lot.lot_id))


def _consume_average(
    book: _InstrumentBook, quantity: Decimal
) -> tuple[Decimal, list[str]]:
    """Consume the same fraction of every open lot.

    Average cost is not "pick the average-priced lot"; it is a claim that
    every share is interchangeable. Taking the fraction `sold / held` out of
    each lot is exactly that claim, and it is algebraically identical to
    charging `quantity × average` — with `f = quantity / total`,
    `Σ qᵢ·f·cᵢ = f·Σ qᵢcᵢ = quantity × average`. Slicing is preferred over
    re-basing every lot to the average because it leaves each parcel's
    acquisition date and original cost intact, which a later switch to FIFO
    and every holding-period question still need.

    Fees capitalised into a lot are consumed in the same proportion, so a
    partially sold lot keeps only the share of its fees that remains
    attached to the shares still held.
    """
    total = book.quantity
    if is_zero_quantity(total):
        return ZERO, []

    fraction = quantity / total
    outstanding = quantity
    basis = ZERO
    consumed: list[str] = []
    last = len(book.lots) - 1

    for index, lot in enumerate(book.lots):
        # The final lot absorbs the remainder rather than taking its own
        # proportional share. `quantity / total` is frequently a repeating
        # decimal (selling a third of a holding), and without this the
        # slices sum to fractionally less than the quantity sold, leaving a
        # sliver of a fully closed position permanently open.
        taken = min(lot.quantity, outstanding) if index == last else lot.quantity * fraction
        if is_zero_quantity(taken):
            continue
        share = taken / lot.quantity if lot.quantity else ZERO
        basis += taken * lot.cost_per_share + lot.fees * share
        lot.fees -= lot.fees * share
        lot.quantity -= taken
        outstanding -= taken
        consumed.append(lot.lot_id)

    book.lots = [lot for lot in book.lots if not is_zero_quantity(lot.quantity)]
    return basis, consumed


def _consume_ordered(
    book: _InstrumentBook, quantity: Decimal, method: CostBasisMethod
) -> tuple[Decimal, list[str], Decimal]:
    """Consume whole lots in order until the quantity is satisfied.

    Returns the basis consumed, which lots it came from, and any quantity
    that could not be covered. An uncovered remainder is *returned* rather
    than raised: a portfolio reconstructed from a partial history routinely
    has a sale with no matching purchase, and refusing to compute anything
    would make the tool useless exactly when it is most needed. The caller
    records a finding instead.
    """
    remaining = quantity
    basis = ZERO
    consumed: list[str] = []

    for lot in _select_lots(book.lots, method):
        if is_zero_quantity(remaining):
            break
        taken = min(lot.quantity, remaining)
        if is_zero_quantity(taken):
            continue
        share = taken / lot.quantity if lot.quantity else ZERO
        basis += taken * lot.cost_per_share + lot.fees * share
        lot.fees -= lot.fees * share
        lot.quantity -= taken
        remaining -= taken
        consumed.append(lot.lot_id)

    book.lots = [lot for lot in book.lots if not is_zero_quantity(lot.quantity)]
    return basis, consumed, remaining


def replay(
    transactions: Iterable[Transaction],
    method: CostBasisMethod = CostBasisMethod.FIFO,
    as_of: date | None = None,
) -> ReplayResult:
    """Replay a ledger into lots, disposals and cash.

    `as_of` truncates the ledger to what was known to have happened by that
    date. That is what makes "what did my portfolio look like on 1 January"
    answerable without storing snapshots.
    """
    books: dict[str, _InstrumentBook] = defaultdict(_InstrumentBook)
    cash: dict[str, Decimal] = defaultdict(lambda: ZERO)
    findings: list[ValidationFinding] = []
    latest: date | None = None

    for transaction in ordered(transactions):
        if as_of is not None and transaction.occurred_on > as_of:
            continue
        latest = transaction.occurred_on
        kind = transaction.transaction_type
        currency = transaction.currency

        if kind.requires_instrument and transaction.instrument_id is None:
            findings.append(
                ValidationFinding(
                    code="missing_instrument",
                    severity="error",
                    message=f"{kind.value} transaction has no instrument",
                    subject_id=transaction.transaction_id,
                )
            )
            continue

        instrument_id = transaction.instrument_id or ""
        book = books[instrument_id] if instrument_id else None

        match kind:
            case TransactionType.BUY | TransactionType.TRANSFER_IN:
                assert book is not None
                # A transfer in with no stated price carries no cost basis,
                # which would make the whole position look like pure profit
                # on sale. Flagged, not guessed.
                if kind is TransactionType.TRANSFER_IN and transaction.price == ZERO:
                    findings.append(
                        ValidationFinding(
                            code="transfer_without_basis",
                            severity="warn",
                            message=(
                                "shares transferred in with no cost basis; "
                                "realised gain on sale will be overstated"
                            ),
                            subject_id=transaction.transaction_id,
                        )
                    )
                book.lots.append(
                    _OpenLot(
                        lot_id=transaction.transaction_id,
                        instrument_id=instrument_id,
                        opened_on=transaction.occurred_on,
                        quantity=transaction.quantity,
                        cost_per_share=transaction.price,
                        fees=transaction.fees,
                        source_transaction_id=transaction.transaction_id,
                    )
                )
                book.fees_paid += transaction.fees
                if kind is TransactionType.BUY:
                    cash[currency] -= transaction.gross_value + transaction.fees

            case TransactionType.DIVIDEND_REINVEST:
                assert book is not None
                # Cash is credited by the dividend and immediately spent, so
                # the net cash effect is zero -- but the shares arrive with a
                # real cost basis equal to the dividend value, which is what
                # stops a later sale looking like a windfall.
                book.lots.append(
                    _OpenLot(
                        lot_id=transaction.transaction_id,
                        instrument_id=instrument_id,
                        opened_on=transaction.occurred_on,
                        quantity=transaction.quantity,
                        cost_per_share=transaction.price,
                        fees=transaction.fees,
                        source_transaction_id=transaction.transaction_id,
                    )
                )
                book.dividends_received += transaction.gross_value
                book.fees_paid += transaction.fees
                cash[currency] -= transaction.fees

            case TransactionType.SELL | TransactionType.TRANSFER_OUT:
                assert book is not None
                if method is CostBasisMethod.AVERAGE:
                    available = book.quantity
                    shortfall = max(ZERO, transaction.quantity - available)
                    taken = min(transaction.quantity, available)
                    basis, matched = _consume_average(book, taken)
                else:
                    basis, matched, shortfall = _consume_ordered(
                        book, transaction.quantity, method
                    )

                if not is_zero_quantity(shortfall):
                    findings.append(
                        ValidationFinding(
                            code="oversold",
                            severity="error",
                            message=(
                                f"sold {transaction.quantity} but only "
                                f"{transaction.quantity - shortfall} were held; "
                                f"cost basis for {shortfall} is unknown"
                            ),
                            subject_id=transaction.transaction_id,
                        )
                    )

                # A transfer out is not a sale: the shares left, nobody was
                # paid. Booking proceeds of zero against them would realise
                # the entire cost basis as a loss, so proceeds are set equal
                # to the basis and the realised gain is nil. The receiving
                # portfolio records a matching transfer in that carries the
                # basis across.
                proceeds = (
                    transaction.gross_value if kind is TransactionType.SELL else basis
                )
                disposal = Disposal(
                    disposal_id=transaction.transaction_id,
                    instrument_id=instrument_id,
                    occurred_on=transaction.occurred_on,
                    quantity=transaction.quantity,
                    proceeds=proceeds,
                    cost_basis=basis,
                    fees=transaction.fees,
                    source_transaction_id=transaction.transaction_id,
                    matched_lot_ids=tuple(matched),
                )
                book.disposals.append(disposal)
                book.realized_gain += disposal.realized_gain
                book.fees_paid += transaction.fees
                if kind is TransactionType.SELL:
                    cash[currency] += proceeds - transaction.fees

            case TransactionType.SPLIT:
                assert book is not None
                ratio = transaction.split_ratio
                if ratio is None or ratio <= ZERO:
                    findings.append(
                        ValidationFinding(
                            code="invalid_split_ratio",
                            severity="error",
                            message="split transaction has no usable ratio",
                            subject_id=transaction.transaction_id,
                        )
                    )
                    continue
                # Total cost basis is invariant across a split; only the
                # per-share figure changes. Multiplying quantity without
                # dividing cost per share is the classic way to silently
                # triple a portfolio's cost basis.
                for lot in book.lots:
                    lot.quantity = lot.quantity * ratio
                    lot.cost_per_share = lot.cost_per_share / ratio

            case TransactionType.DIVIDEND:
                assert book is not None
                book.dividends_received += transaction.amount
                cash[currency] += transaction.amount

            case TransactionType.FEE:
                if book is not None:
                    book.fees_paid += transaction.amount
                cash[currency] -= transaction.amount

            case TransactionType.DEPOSIT | TransactionType.INTEREST:
                cash[currency] += transaction.amount

            case TransactionType.WITHDRAWAL:
                cash[currency] -= transaction.amount

    return ReplayResult(
        books=dict(books),
        cash=dict(cash),
        findings=findings,
        as_of=as_of or latest,
    )


def build_state(
    portfolio_id: str,
    transactions: Iterable[Transaction],
    method: CostBasisMethod = CostBasisMethod.FIFO,
    as_of: date | None = None,
    prices: dict[str, Decimal] | None = None,
    price_dates: dict[str, date] | None = None,
    base_currency: str = "USD",
) -> PortfolioState:
    """Derive a full portfolio state from a ledger.

    `prices` is deliberately optional and partial. An instrument with no
    price produces a position with `last_price=None` and lands in
    `unpriced_instrument_ids`, which makes the portfolio total `None` rather
    than a number quietly missing that holding. A total that silently
    excludes a position is worse than no total, because it looks right.
    """
    result = replay(transactions, method=method, as_of=as_of)
    quotes = prices or {}
    quote_dates = price_dates or {}
    effective = as_of or result.as_of or date.min

    positions: list[Position] = []
    disposals: list[Disposal] = []
    unpriced: list[str] = []

    for instrument_id, book in sorted(result.books.items()):
        disposals.extend(book.disposals)
        quantity = quantize_quantity(book.quantity)
        price = quotes.get(instrument_id)
        if price is None and not is_zero_quantity(quantity):
            unpriced.append(instrument_id)
        positions.append(
            Position(
                instrument_id=instrument_id,
                as_of=effective,
                quantity=quantity,
                cost_basis=quantize_money(book.cost_basis),
                lots=tuple(lot.freeze() for lot in book.lots),
                realized_gain=quantize_money(book.realized_gain),
                dividends_received=quantize_money(book.dividends_received),
                fees_paid=quantize_money(book.fees_paid),
                last_price=price,
                price_as_of=quote_dates.get(instrument_id),
            )
        )

    balances = [
        CashBalance(currency=currency, amount=quantize_money(amount))
        for currency, amount in sorted(result.cash.items())
    ]
    if not balances:
        balances = [CashBalance(currency=base_currency, amount=ZERO)]

    return PortfolioState(
        portfolio_id=portfolio_id,
        as_of=effective,
        positions=tuple(positions),
        cash=tuple(balances),
        disposals=tuple(sorted(disposals, key=lambda d: (d.occurred_on, d.disposal_id))),
        unpriced_instrument_ids=tuple(sorted(unpriced)),
    )

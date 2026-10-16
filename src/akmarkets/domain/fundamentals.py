"""Fundamentals: normalising filed figures, and valuing what deserves it.

Two jobs. Turn a pile of XBRL facts into comparable annual and quarterly
series, and apply only the valuation methods that make sense for the kind of
company in question.

**Not every ratio applies to every company.** A pre-revenue miner has no
P/E and computing one from a loss produces a negative number that looks
meaningful and is not; a bank's EV/EBITDA is meaningless because debt is
its raw material, not its financing. Mechanically calculating every metric
for every security is the single most common way a screener produces
confident nonsense, so the archetype decides what is even attempted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from akmarkets.domain.market import ReportedFact
from akmarkets.domain.money import ZERO


class Archetype(StrEnum):
    """What kind of business this is, for the purpose of valuing it.

    Deliberately coarse and deliberately explicit. The alternative — one
    ratio set for everything — is what produces a screener confidently
    reporting a P/E for a company that has never earned anything.
    """

    PROFITABLE_OPERATING = "profitable_operating"
    UNPROFITABLE_GROWTH = "unprofitable_growth"
    PRE_REVENUE = "pre_revenue"
    FINANCIAL = "financial"
    FUND = "fund"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Period:
    """One reporting period's figures, normalised."""

    period_end: date
    fiscal_year: int | None
    fiscal_period: str | None
    is_annual: bool
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    eps_diluted: Decimal | None = None
    cash: Decimal | None = None
    total_debt: Decimal | None = None
    total_assets: Decimal | None = None
    total_equity: Decimal | None = None
    operating_cash_flow: Decimal | None = None
    capex: Decimal | None = None
    shares_diluted: Decimal | None = None

    @property
    def gross_margin(self) -> Decimal | None:
        if self.gross_profit is None or not self.revenue:
            return None
        return self.gross_profit / self.revenue

    @property
    def operating_margin(self) -> Decimal | None:
        if self.operating_income is None or not self.revenue:
            return None
        return self.operating_income / self.revenue

    @property
    def free_cash_flow(self) -> Decimal | None:
        """Operating cash flow less capital expenditure.

        Capex is reported as a positive outflow in the `Payments...`
        concept, so it is subtracted. Adding it — an easy mistake when the
        sign convention differs between filers — would turn a
        capital-hungry business into a cash machine.
        """
        if self.operating_cash_flow is None or self.capex is None:
            return None
        return self.operating_cash_flow - abs(self.capex)

    @property
    def net_debt(self) -> Decimal | None:
        if self.total_debt is None:
            return None
        return self.total_debt - (self.cash or ZERO)


def normalise(facts: Sequence[ReportedFact]) -> list[Period]:
    """Group facts into periods, preferring annual figures.

    A period is identified by its end date, because fiscal years do not
    align across companies and a calendar year is not a reporting year.
    Where the same metric appears twice for one period — restated in a
    later filing — the most recently *filed* value wins, since a restatement
    is a correction.
    """
    by_period: dict[tuple[date, bool], dict[str, tuple[Decimal, date | None]]] = {}

    for fact in facts:
        key = (fact.period_end, fact.is_annual)
        bucket = by_period.setdefault(key, {})
        existing = bucket.get(fact.metric)
        if existing is None or (
            fact.filed_on is not None
            and (existing[1] is None or fact.filed_on > existing[1])
        ):
            bucket[fact.metric] = (fact.value, fact.filed_on)

    periods = [
        Period(
            period_end=period_end,
            fiscal_year=period_end.year,
            fiscal_period="FY" if is_annual else None,
            is_annual=is_annual,
            **{metric: value for metric, (value, _) in metrics.items()},
        )
        for (period_end, is_annual), metrics in by_period.items()
    ]
    return sorted(periods, key=lambda period: (period.period_end, period.is_annual))


def annual(periods: Sequence[Period]) -> list[Period]:
    return [period for period in periods if period.is_annual]


def latest(periods: Sequence[Period], annual_only: bool = True) -> Period | None:
    candidates = annual(periods) if annual_only else list(periods)
    return candidates[-1] if candidates else None


def growth(periods: Sequence[Period], metric: str) -> Decimal | None:
    """Year-over-year growth in a metric, or None.

    None when the base is zero or negative as well as when it is missing:
    growth from a loss is not a percentage anyone can interpret, and
    rendering one would be worse than rendering nothing.
    """
    yearly = annual(periods)
    if len(yearly) < 2:
        return None
    current: Decimal | None = getattr(yearly[-1], metric, None)
    previous: Decimal | None = getattr(yearly[-2], metric, None)
    if current is None or previous is None or previous <= ZERO:
        return None
    return (current - previous) / previous


def classify(periods: Sequence[Period], sector: str | None = None) -> Archetype:
    """Decide which valuation methods are even applicable.

    Sector is consulted first because a bank is a bank whatever its margins
    look like, and applying EV/EBITDA to one is meaningless — its debt is
    raw material, not financing.
    """
    if sector:
        lowered = sector.lower()
        if any(word in lowered for word in ("bank", "financial", "insurance")):
            return Archetype.FINANCIAL
        if any(word in lowered for word in ("fund", "trust", "etf")):
            return Archetype.FUND

    current = latest(periods)
    if current is None:
        return Archetype.UNKNOWN
    if not current.revenue:
        return Archetype.PRE_REVENUE
    if current.net_income is not None and current.net_income > ZERO:
        return Archetype.PROFITABLE_OPERATING
    return Archetype.UNPROFITABLE_GROWTH


# Which ratios mean anything, per archetype. Absence from this map is the
# mechanism: a metric that is not applicable is never computed, so it can
# never be rendered.
APPLICABLE: dict[Archetype, frozenset[str]] = {
    Archetype.PROFITABLE_OPERATING: frozenset(
        {"pe", "ps", "pb", "ev_ebitda", "fcf_yield", "net_debt_to_equity"}
    ),
    Archetype.UNPROFITABLE_GROWTH: frozenset({"ps", "pb", "net_debt_to_equity"}),
    # A pre-revenue company has no sales to value and no earnings. What
    # matters is whether it can fund itself, so book value and cash are all
    # that is offered.
    Archetype.PRE_REVENUE: frozenset({"pb", "net_debt_to_equity"}),
    Archetype.FINANCIAL: frozenset({"pe", "pb"}),
    Archetype.FUND: frozenset({"pb"}),
    Archetype.UNKNOWN: frozenset(),
}


@dataclass(frozen=True)
class Valuation:
    """Ratios that apply, and an explicit record of what did not.

    `not_applicable` is returned rather than omitted so a page can say
    "P/E does not apply to a pre-revenue company" instead of leaving a
    blank the reader will assume is missing data.
    """

    archetype: Archetype
    pe: Decimal | None = None
    ps: Decimal | None = None
    pb: Decimal | None = None
    ev_ebitda: Decimal | None = None
    fcf_yield: Decimal | None = None
    net_debt_to_equity: Decimal | None = None
    not_applicable: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()


ALL_RATIOS = ("pe", "ps", "pb", "ev_ebitda", "fcf_yield", "net_debt_to_equity")


def value(
    periods: Sequence[Period],
    price: Decimal | None,
    shares_outstanding: Decimal | None = None,
    sector: str | None = None,
) -> Valuation:
    """Compute only the ratios this kind of company supports.

    A missing input produces `None` and lands in `unavailable`; an
    inapplicable ratio is never attempted and lands in `not_applicable`.
    Keeping those two apart is the whole point — one is a gap in our data
    and the other is a statement about the company.
    """
    archetype = classify(periods, sector)
    applicable = APPLICABLE[archetype]
    current = latest(periods)

    computed: dict[str, Decimal | None] = dict.fromkeys(ALL_RATIOS)
    if current is not None and price is not None:
        shares = shares_outstanding or current.shares_diluted
        market_cap = price * shares if shares else None

        if "pe" in applicable and current.eps_diluted and current.eps_diluted > ZERO:
            computed["pe"] = price / current.eps_diluted
        if "ps" in applicable and market_cap and current.revenue:
            computed["ps"] = market_cap / current.revenue
        # Negative equity is excluded rather than divided by: price to a
        # negative book value is a negative ratio that sorts like a cheap
        # stock.
        if "pb" in applicable and market_cap and (current.total_equity or ZERO) > ZERO:
            computed["pb"] = market_cap / current.total_equity  # type: ignore[operator]
        if "fcf_yield" in applicable and market_cap:
            free_cash = current.free_cash_flow
            if free_cash is not None and market_cap > ZERO:
                computed["fcf_yield"] = free_cash / market_cap
        if "ev_ebitda" in applicable and market_cap and current.operating_income:
            enterprise = market_cap + (current.net_debt or ZERO)
            if current.operating_income > ZERO:
                computed["ev_ebitda"] = enterprise / current.operating_income
        if "net_debt_to_equity" in applicable and current.total_equity:
            net_debt = current.net_debt
            if net_debt is not None and current.total_equity > ZERO:
                computed["net_debt_to_equity"] = net_debt / current.total_equity

    return Valuation(
        archetype=archetype,
        pe=computed["pe"],
        ps=computed["ps"],
        pb=computed["pb"],
        ev_ebitda=computed["ev_ebitda"],
        fcf_yield=computed["fcf_yield"],
        net_debt_to_equity=computed["net_debt_to_equity"],
        not_applicable=tuple(name for name in ALL_RATIOS if name not in applicable),
        unavailable=tuple(
            name
            for name in ALL_RATIOS
            if name in applicable and computed[name] is None
        ),
    )

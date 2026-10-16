"""Fundamentals: normalisation, and refusing to value what cannot be valued."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from akmarkets.domain.fundamentals import (
    Archetype,
    classify,
    growth,
    latest,
    normalise,
    value,
)
from akmarkets.providers.edgar import Fact


def fact(
    metric: str,
    amount: str,
    year: int,
    annual: bool = True,
    filed: str | None = None,
    concept: str = "Revenues",
) -> Fact:
    return Fact(
        metric=metric,
        concept=concept,
        value=Decimal(amount),
        period_end=date(year, 12, 31),
        fiscal_year=year,
        fiscal_period="FY" if annual else "Q3",
        form="10-K" if annual else "10-Q",
        filed_on=date.fromisoformat(filed) if filed else None,
    )


# -- normalisation ------------------------------------------------------


def test_facts_are_grouped_into_periods() -> None:
    periods = normalise(
        [
            fact("revenue", "1000", 2025),
            fact("net_income", "100", 2025),
            fact("revenue", "1200", 2026),
        ]
    )
    assert len(periods) == 2
    assert periods[-1].revenue == Decimal(1200)
    assert periods[0].net_income == Decimal(100)


def test_a_restatement_wins_over_the_original() -> None:
    """A later filing revising an earlier figure is a correction, and the
    correction is the number."""
    periods = normalise(
        [
            fact("revenue", "1000", 2025, filed="2026-02-01"),
            fact("revenue", "950", 2025, filed="2026-08-01"),
        ]
    )
    assert latest(periods) is not None
    assert latest(periods).revenue == Decimal(950)  # type: ignore[union-attr]


def test_annual_and_quarterly_periods_ending_the_same_day_stay_apart() -> None:
    """A Q4 figure and a full-year figure can share an end date and mean
    completely different things."""
    periods = normalise(
        [fact("revenue", "4000", 2026), fact("revenue", "1100", 2026, annual=False)]
    )
    assert len(periods) == 2
    assert {period.revenue for period in periods} == {Decimal(4000), Decimal(1100)}


# -- derived figures ----------------------------------------------------


def test_free_cash_flow_subtracts_capex_rather_than_adding_it() -> None:
    """Capex is reported as a positive outflow. Adding it would turn a
    capital-hungry business into a cash machine."""
    periods = normalise(
        [fact("operating_cash_flow", "500", 2026), fact("capex", "200", 2026)]
    )
    assert latest(periods).free_cash_flow == Decimal(300)  # type: ignore[union-attr]


def test_margins_are_none_rather_than_zero_without_revenue() -> None:
    periods = normalise([fact("gross_profit", "100", 2026)])
    assert latest(periods).gross_margin is None  # type: ignore[union-attr]


def test_net_debt_nets_off_cash() -> None:
    periods = normalise([fact("total_debt", "500", 2026), fact("cash", "200", 2026)])
    assert latest(periods).net_debt == Decimal(300)  # type: ignore[union-attr]


def test_growth_from_a_loss_is_none_not_a_percentage() -> None:
    """Growth from a negative base is not a number anyone can interpret,
    and rendering one is worse than rendering nothing."""
    periods = normalise(
        [fact("net_income", "-100", 2025), fact("net_income", "50", 2026)]
    )
    assert growth(periods, "net_income") is None


def test_growth_needs_two_years() -> None:
    assert growth(normalise([fact("revenue", "100", 2026)]), "revenue") is None


def test_growth_is_the_year_over_year_change() -> None:
    periods = normalise([fact("revenue", "1000", 2025), fact("revenue", "1200", 2026)])
    assert growth(periods, "revenue") == Decimal("0.2")


# -- archetypes ---------------------------------------------------------


def test_a_company_with_no_revenue_is_pre_revenue() -> None:
    assert classify(normalise([fact("total_equity", "500", 2026)])) is Archetype.PRE_REVENUE


def test_a_bank_is_classified_by_sector_whatever_its_margins_look_like() -> None:
    """Its debt is raw material, not financing, so EV/EBITDA is meaningless
    however the numbers come out."""
    periods = normalise([fact("revenue", "1000", 2026), fact("net_income", "200", 2026)])
    assert classify(periods, sector="Banks — Regional") is Archetype.FINANCIAL


def test_profitability_separates_operating_from_growth() -> None:
    profitable = normalise(
        [fact("revenue", "1000", 2026), fact("net_income", "100", 2026)]
    )
    lossmaking = normalise(
        [fact("revenue", "1000", 2026), fact("net_income", "-100", 2026)]
    )
    assert classify(profitable) is Archetype.PROFITABLE_OPERATING
    assert classify(lossmaking) is Archetype.UNPROFITABLE_GROWTH


# -- valuation ----------------------------------------------------------


def _profitable() -> list:
    return normalise(
        [
            fact("revenue", "1000", 2026),
            fact("net_income", "100", 2026),
            fact("eps_diluted", "2", 2026),
            fact("total_equity", "800", 2026),
            fact("shares_diluted", "50", 2026),
            fact("operating_income", "150", 2026),
            fact("operating_cash_flow", "200", 2026),
            fact("capex", "50", 2026),
            fact("total_debt", "300", 2026),
            fact("cash", "100", 2026),
        ]
    )


def test_a_profitable_company_gets_the_full_ratio_set() -> None:
    """Price $20, EPS $2 → P/E 10. 50 shares × $20 = $1,000 market cap;
    revenue $1,000 → P/S 1; equity $800 → P/B 1.25."""
    result = value(_profitable(), price=Decimal(20))
    assert result.archetype is Archetype.PROFITABLE_OPERATING
    assert result.pe == Decimal(10)
    assert result.ps == Decimal(1)
    assert result.pb == Decimal("1.25")
    # FCF 200 − 50 = 150 on a $1,000 cap → 15%.
    assert result.fcf_yield == Decimal("0.15")
    assert result.not_applicable == ()


def test_a_pre_revenue_company_is_never_given_a_pe() -> None:
    """Computing one from a loss produces a negative number that looks
    meaningful and is not. This is the single most common way a screener
    produces confident nonsense."""
    periods = normalise(
        [
            fact("net_income", "-500", 2026),
            fact("total_equity", "1000", 2026),
            fact("shares_diluted", "100", 2026),
        ]
    )
    result = value(periods, price=Decimal(5))
    assert result.archetype is Archetype.PRE_REVENUE
    assert result.pe is None
    assert "pe" in result.not_applicable
    assert "ps" in result.not_applicable
    # Book value still applies and is computed: 100 × $5 = $500 over $1,000.
    assert result.pb == Decimal("0.5")


def test_a_bank_is_not_given_an_ev_ebitda() -> None:
    result = value(_profitable(), price=Decimal(20), sector="Banks")
    assert "ev_ebitda" in result.not_applicable
    assert result.ev_ebitda is None
    assert result.pe == Decimal(10)


def test_inapplicable_and_unavailable_are_kept_apart() -> None:
    """One is a statement about the company, the other a gap in our data,
    and a page that conflates them tells the reader nothing."""
    thin = normalise([fact("revenue", "1000", 2026), fact("net_income", "50", 2026)])
    result = value(thin, price=Decimal(20))
    assert result.not_applicable == ()          # a profitable operating company
    assert "pe" in result.unavailable           # but no EPS was reported
    assert result.pe is None


def test_no_price_yields_no_ratios_rather_than_zeroes() -> None:
    result = value(_profitable(), price=None)
    assert result.pe is None
    assert set(result.unavailable) >= {"pe", "ps", "pb"}


def test_a_negative_book_value_does_not_produce_a_ratio() -> None:
    """Price-to-book on negative equity is a negative number that sorts
    like a cheap stock."""
    periods = normalise(
        [
            fact("revenue", "1000", 2026),
            fact("net_income", "100", 2026),
            fact("total_equity", "-200", 2026),
            fact("shares_diluted", "50", 2026),
        ]
    )
    assert value(periods, price=Decimal(20)).pb is None


@pytest.mark.parametrize("archetype", list(Archetype))
def test_every_archetype_declares_what_applies(archetype: Archetype) -> None:
    from akmarkets.domain.fundamentals import APPLICABLE

    assert archetype in APPLICABLE


# -- provider classification -------------------------------------------


def test_an_etf_that_files_no_xbrl_is_absent_not_failed() -> None:
    """Every ETF and unit trust has a CIK and answers 404 to company facts.
    Treating that as a failure marks the provider degraded every night
    because a benchmark is in the universe."""
    import httpx

    from akmarkets.providers.base import DataState
    from akmarkets.providers.edgar import EdgarProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in str(request.url):
            return httpx.Response(200, json={"0": {"ticker": "SPY", "cik_str": 884394}})
        return httpx.Response(404, text="not found")

    provider = EdgarProvider(
        user_agent="test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = provider.fetch_facts("SPY")
    assert result.state is DataState.UNKNOWN
    assert "files no XBRL" in result.message


def test_a_foreign_issuer_using_ifrs_reports_no_concepts_rather_than_failing() -> None:
    """XMPL files under IFRS on 40-F and uses no us-gaap concepts at all."""
    import httpx

    from akmarkets.providers.base import DataState
    from akmarkets.providers.edgar import EdgarProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in str(request.url):
            return httpx.Response(200, json={"0": {"ticker": "XMPL", "cik_str": 1234567}})
        return httpx.Response(200, json={"facts": {"ifrs-full": {"Revenue": {}}}})

    provider = EdgarProvider(
        user_agent="test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = provider.fetch_facts("XMPL")
    assert result.state is DataState.UNKNOWN
    assert "expected concepts" in result.message


def test_a_real_transport_error_is_still_a_failure() -> None:
    """The distinction only helps if genuine outages remain visible."""
    import httpx

    from akmarkets.providers.base import DataState
    from akmarkets.providers.edgar import EdgarProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in str(request.url):
            return httpx.Response(200, json={"0": {"ticker": "AAPL", "cik_str": 320193}})
        return httpx.Response(503, text="unavailable")

    provider = EdgarProvider(
        user_agent="test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert provider.fetch_facts("AAPL").state is DataState.FAILED

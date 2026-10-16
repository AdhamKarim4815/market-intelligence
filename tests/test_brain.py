"""The Market Brain: refusing to explain, and refusing to spend."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from akmarkets.domain.brain import (
    ABNORMAL_MOVE,
    Cause,
    ChangeKind,
    Driver,
    PositionDna,
    explain_move,
    thesis_warnings,
    what_changed,
)
from akmarkets.domain.news import Severity

ON = date(2026, 8, 7)


# -- why is it moving ---------------------------------------------------


def test_no_clear_cause_is_the_answer_when_there_is_none() -> None:
    """The answer most of the time, and the one every other tool refuses to
    give. A 6% fall is not explained by a 0.4% index move."""
    analysis = explain_move(
        "XMPL", ON, move=Decimal("-0.06"), market_move=Decimal("-0.004")
    )
    assert analysis.is_abnormal is True
    assert analysis.verdict is Cause.UNCLEAR
    assert analysis.summary == "No clear cause detected."


def test_a_filing_on_the_day_is_a_confirmed_cause() -> None:
    """The closest thing to a cause this system can observe: a disclosure
    with legal consequences for being wrong, timed to the move."""
    analysis = explain_move(
        "XMPL",
        ON,
        move=Decimal("-0.09"),
        market_move=Decimal("-0.002"),
        events=[("XMPL cuts production guidance", Severity.CRITICAL, True)],
    )
    assert analysis.verdict is Cause.CONFIRMED
    assert "guidance" in analysis.summary
    assert analysis.explanations[0].evidence == ("filing",)


def test_news_without_a_filing_is_probable_not_confirmed() -> None:
    analysis = explain_move(
        "XMPL",
        ON,
        move=Decimal("-0.09"),
        events=[("Analyst downgrades XMPL", Severity.IMPORTANT, False)],
    )
    assert analysis.verdict is Cause.PROBABLE
    assert analysis.explanations[0].cause is Cause.PROBABLE


def test_the_market_only_explains_a_move_it_could_have_caused() -> None:
    """Calling a 0.4% index move the cause of a 6% fall is the failure this
    threshold exists to prevent."""
    tiny = explain_move("X", ON, move=Decimal("-0.06"), market_move=Decimal("-0.004"))
    assert all(item.evidence != ("index",) for item in tiny.explanations)

    aligned = explain_move("X", ON, move=Decimal("-0.03"), market_move=Decimal("-0.025"))
    assert aligned.verdict is Cause.PROBABLE
    assert "market" in aligned.summary.lower()


def test_a_sector_move_outranks_a_market_move() -> None:
    """A sector explanation is more specific, so when both fit the sector
    is the better answer."""
    analysis = explain_move(
        "X",
        ON,
        move=Decimal("-0.04"),
        market_move=Decimal("-0.035"),
        sector_move=Decimal("-0.038"),
    )
    assert "sector" in analysis.explanations[0].headline.lower()


def test_peers_moving_together_suggests_a_group_move() -> None:
    analysis = explain_move(
        "XMPL",
        ON,
        move=Decimal("-0.05"),
        peer_moves={"ALB": Decimal("-0.04"), "PLL": Decimal("-0.045"), "LAC": Decimal("-0.03")},
    )
    assert analysis.verdict is Cause.PROBABLE
    assert "peers" in analysis.explanations[0].headline


def test_one_peer_moving_is_not_a_group() -> None:
    analysis = explain_move(
        "XMPL",
        ON,
        move=Decimal("-0.05"),
        peer_moves={"ALB": Decimal("-0.04"), "PLL": Decimal("0.02"), "LAC": Decimal("0.01")},
    )
    assert analysis.verdict is Cause.UNCLEAR


def test_volume_is_evidence_and_never_a_cause() -> None:
    """It says a move was participated in rather than a thin drift, which
    changes how much weight to give everything else."""
    analysis = explain_move(
        "X", ON, move=Decimal("-0.06"), relative_volume=Decimal("3.4")
    )
    volume = next(item for item in analysis.explanations if item.evidence == ("volume",))
    assert volume.cause is Cause.UNCLEAR
    assert volume.weight == Decimal(0)
    assert analysis.verdict is Cause.UNCLEAR


def test_a_normal_move_is_not_investigated() -> None:
    analysis = explain_move("X", ON, move=Decimal("0.004"))
    assert analysis.is_abnormal is False
    assert "normal daily range" in analysis.summary


def test_the_threshold_is_a_filter_not_a_significance_claim() -> None:
    assert Decimal("0.02") == ABNORMAL_MOVE
    assert explain_move("X", ON, move=Decimal("0.021")).is_abnormal is True


def test_relative_move_is_carried_so_the_reader_can_judge() -> None:
    analysis = explain_move("X", ON, move=Decimal("-0.06"), market_move=Decimal("-0.01"))
    assert analysis.relative_move == Decimal("-0.05")


# -- what changed -------------------------------------------------------


def test_a_quiet_day_reports_quiet() -> None:
    """The most important property here: it is what stops an expensive
    model being asked to explain a day on which nothing happened, which is
    most days."""
    result = what_changed(
        "AAPL",
        since=date(2026, 8, 6),
        until=ON,
        price_before=Decimal(310),
        price_after=Decimal(313),
        rsi_before=Decimal(48),
        rsi_after=Decimal(50),
        trend_before="uptrend",
        trend_after="uptrend",
    )
    assert result.is_quiet is True
    assert result.summary() == "Nothing material changed."


def test_a_large_price_move_is_material() -> None:
    result = what_changed(
        "XMPL",
        since=date(2026, 8, 1),
        until=ON,
        price_before=Decimal(10),
        price_after=Decimal(12),
    )
    assert result.is_quiet is False
    assert result.material[0].kind is ChangeKind.PRICE


def test_a_trend_flip_registers_even_on_a_small_price_move() -> None:
    """A change in structure that a percentage does not capture."""
    result = what_changed(
        "X",
        since=date(2026, 8, 1),
        until=ON,
        price_before=Decimal(100),
        price_after=Decimal(101),
        trend_before="uptrend",
        trend_after="downtrend",
    )
    assert any(change.kind is ChangeKind.TECHNICAL for change in result.material)


def test_rsi_crossing_a_band_matters_and_drifting_inside_it_does_not() -> None:
    crossed = what_changed(
        "X", since=date(2026, 8, 1), until=ON, rsi_before=Decimal(68), rsi_after=Decimal(74)
    )
    drifted = what_changed(
        "X", since=date(2026, 8, 1), until=ON, rsi_before=Decimal(45), rsi_after=Decimal(58)
    )
    assert any(change.kind is ChangeKind.TECHNICAL for change in crossed.material)
    assert drifted.is_quiet


def test_a_thesis_flag_is_always_material() -> None:
    """The most important thing this system can report."""
    result = what_changed(
        "XMPL",
        since=date(2026, 8, 1),
        until=ON,
        thesis_flags=["one assumption may no longer hold: lithium price above $12,000"],
    )
    assert result.material[0].kind is ChangeKind.THESIS
    assert result.material[0].significance == Decimal(1)


def test_a_critical_event_outranks_a_pile_of_ordinary_ones() -> None:
    critical = what_changed("X", since=date(2026, 8, 1), until=ON, critical_events=1)
    ordinary = what_changed("X", since=date(2026, 8, 1), until=ON, new_events=5)
    assert critical.material[0].significance > ordinary.material[0].significance


def test_changes_are_ranked_by_significance() -> None:
    result = what_changed(
        "X",
        since=date(2026, 8, 1),
        until=ON,
        price_before=Decimal(100),
        price_after=Decimal(106),
        critical_events=1,
    )
    scores = [change.significance for change in result.changes]
    assert scores == sorted(scores, reverse=True)


def test_a_fundamental_move_counts_for_more_than_the_same_price_move() -> None:
    """Fundamentals move rarely and mean more when they do."""
    revenue = what_changed(
        "X",
        since=date(2026, 1, 1),
        until=ON,
        revenue_before=Decimal(1000),
        revenue_after=Decimal(1060),
    )
    price = what_changed(
        "X",
        since=date(2026, 1, 1),
        until=ON,
        price_before=Decimal(1000),
        price_after=Decimal(1060),
    )
    assert revenue.material[0].significance > price.material[0].significance


# -- position DNA -------------------------------------------------------


def _xmpl() -> PositionDna:
    return PositionDna(
        symbol="XMPL",
        drivers=(
            Driver("lithium price", "positive", "realised pricing drives revenue"),
            Driver("China EV demand", "positive"),
            Driver("production volume", "positive"),
        ),
        thesis="Low-cost producer ramping into a recovering lithium market.",
        invalidation=(
            "lithium price sustained below $10,000 per tonne",
            "production ramp delayed beyond 2027",
        ),
    )


def test_dna_finds_indirect_news_that_never_names_the_company() -> None:
    """The whole reason indirect news is findable: a lithium price move is
    news for a lithium producer even when the article is about the market."""
    matched = _xmpl().matches("Lithium price rallies 12% on Chinese restocking")
    assert "lithium price" in matched


def test_a_sensitivity_is_labelled_stated_rather_than_measured() -> None:
    """A number that looks measured and is not is worse than a label."""
    assert all(driver.sensitivity == "stated" for driver in _xmpl().drivers)


def test_a_thesis_warning_says_may_rather_than_has() -> None:
    """Matching words in a headline is evidence a topic came up, not that a
    thesis broke — and the difference is a useful prompt versus an alarm
    nobody trusts."""
    warnings = thesis_warnings(
        _xmpl(),
        ["Lithium price sustained below $9,000 per tonne as oversupply persists"],
    )
    assert warnings
    assert "may no longer hold" in warnings[0]
    assert "has been invalidated" not in warnings[0]


def test_unrelated_coverage_raises_no_thesis_warning() -> None:
    assert thesis_warnings(_xmpl(), ["Company announces new head of investor relations"]) == []

"""News intelligence: each stage, and the trap it exists to avoid."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from akmarkets.domain.news import (
    Article,
    Confirmation,
    MaterialityDimension,
    Severity,
    SourceClass,
    attention,
    classify_confirmation,
    classify_source,
    cluster_events,
    decay,
    deduplicate,
    half_life,
    materiality,
    normalise_title,
    novelty,
    pipeline,
    score_event,
    sentiment,
    similarity,
)

WHEN = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def article(
    ident: str,
    title: str,
    source: str = "Reuters",
    minutes: int = 0,
    symbols: tuple[str, ...] = ("XMPL",),
    url: str = "https://example.com/a",
) -> Article:
    return Article(
        article_id=ident,
        title=title,
        url=url,
        source=source,
        published_at=WHEN + timedelta(minutes=minutes),
        symbols=symbols,
    )


# -- normalisation ------------------------------------------------------


def test_the_outlet_suffix_is_stripped_so_one_story_hashes_once() -> None:
    """The same wire copy arrives with three different outlet suffixes;
    without stripping them, one story becomes three signals."""
    assert normalise_title("XMPL starts production - Reuters") == normalise_title(
        "XMPL starts production | Mining.com"
    )


def test_curly_quotes_do_not_defeat_the_hash() -> None:
    assert normalise_title("XMPL’s output rises") == normalise_title("XMPL's output rises")


def test_similarity_is_zero_for_unrelated_and_one_for_identical() -> None:
    assert similarity("XMPL starts production", "XMPL starts production") == Decimal(1)
    assert similarity("XMPL starts production", "Fed holds rates steady") == Decimal(0)


# -- deduplication ------------------------------------------------------


def test_fifty_outlets_carrying_one_wire_story_become_one() -> None:
    """News volume is not news importance, and this is where that is
    enforced."""
    articles = [
        article(f"a{i}", "XMPL begins commercial lithium production", source=f"Outlet {i}")
        for i in range(50)
    ]
    survivors, duplicates = deduplicate(articles)
    assert len(survivors) == 1
    assert len(duplicates) == 49


def test_a_light_rewrite_is_caught_by_token_overlap() -> None:
    survivors, duplicates = deduplicate(
        [
            article("a", "XMPL begins commercial lithium production at Rio do Norte"),
            article("b", "XMPL begins commercial lithium production at Rio Norte", minutes=5),
        ]
    )
    assert len(survivors) == 1
    assert duplicates["b"] == "a"


def test_two_genuinely_different_stories_are_not_merged() -> None:
    """A false merge loses a story entirely, which is worse than a
    duplicate — hence the deliberately short stop list."""
    survivors, _ = deduplicate(
        [
            article("a", "XMPL begins commercial lithium production"),
            article("b", "XMPL chief executive resigns unexpectedly", minutes=10),
        ]
    )
    assert len(survivors) == 2


def test_nothing_is_dropped_silently() -> None:
    """The map is what lets the page say "fifty articles, eight stories"."""
    survivors, duplicates = deduplicate(
        [article("a", "XMPL output rises"), article("b", "XMPL output rises", minutes=1)]
    )
    assert set(duplicates) | {a.article_id for a in survivors} == {"a", "b"}


# -- clustering ---------------------------------------------------------


def test_related_articles_form_one_event() -> None:
    """The unit a human cares about is the occurrence, not the coverage."""
    events = cluster_events(
        [
            article("a", "XMPL lithium production beats guidance"),
            article("b", "XMPL lithium production ahead of guidance target", minutes=30),
        ]
    )
    assert len(events) == 1
    assert events[0].size == 2


def test_the_same_words_a_month_apart_are_two_events() -> None:
    """A company reports production every quarter. Without the time window
    those would collapse into one perpetual event."""
    later = Article(
        article_id="b",
        title="XMPL lithium production beats guidance",
        url="https://example.com/b",
        source="Reuters",
        published_at=WHEN + timedelta(days=40),
        symbols=("XMPL",),
    )
    events = cluster_events([article("a", "XMPL lithium production beats guidance"), later])
    assert len(events) == 2


def test_articles_about_different_companies_do_not_cluster() -> None:
    events = cluster_events(
        [
            article("a", "Quarterly production beats guidance", symbols=("XMPL",)),
            article("b", "Quarterly production beats guidance", symbols=("ALB",), minutes=5),
        ]
    )
    assert len(events) == 2


# -- sources ------------------------------------------------------------


@pytest.mark.parametrize(
    "source,url,expected",
    [
        ("SEC", "https://www.sec.gov/edgar/x", SourceClass.REGULATORY_FILING),
        ("Federal Reserve", "https://federalreserve.gov/x", SourceClass.GOVERNMENT),
        ("GlobeNewswire", "", SourceClass.COMPANY_DISCLOSURE),
        ("Reuters", "", SourceClass.MAJOR_FINANCIAL_NEWS),
        ("Mining.com", "", SourceClass.INDUSTRY_PUBLICATION),
        ("r/wallstreetbets", "https://reddit.com/x", SourceClass.SOCIAL_MEDIA),
    ],
)
def test_sources_are_classified(source: str, url: str, expected: SourceClass) -> None:
    assert classify_source(source, url) is expected


def test_an_unknown_outlet_is_uncatalogued_not_accused() -> None:
    """The default should be "an outlet we have not catalogued", not an
    accusation of unreliability."""
    assert classify_source("The Wexford Herald") is SourceClass.OTHER_PUBLICATION


def test_a_filing_confirms_and_a_forum_post_speculates() -> None:
    """A random social post must never be treated like a regulatory
    filing."""
    assert (
        classify_confirmation("XMPL reports Q2 production", SourceClass.REGULATORY_FILING)
        is Confirmation.CONFIRMED
    )
    assert (
        classify_confirmation("XMPL about to moon", SourceClass.SOCIAL_MEDIA)
        is Confirmation.SPECULATION
    )


def test_hedged_language_downgrades_even_a_good_source() -> None:
    assert (
        classify_confirmation(
            "XMPL reportedly weighing a sale, people familiar say",
            SourceClass.MAJOR_FINANCIAL_NEWS,
        )
        is Confirmation.RUMOUR
    )


# -- sentiment ----------------------------------------------------------


def test_negation_scoping_stops_an_inverted_reading() -> None:
    """"Avoids bankruptcy" and "files for bankruptcy" share a word and mean
    opposite things."""
    assert sentiment("XMPL avoids bankruptcy").score > 0
    assert sentiment("XMPL files for bankruptcy").score < 0


def test_the_lexicon_is_financial_not_general() -> None:
    """A general model reads "beat" and "miss" backwards in this domain."""
    assert sentiment("GOOGL beats revenue estimates").label == "positive"
    assert sentiment("GOOGL misses revenue estimates").label == "negative"


def test_no_signal_is_distinguishable_from_balanced() -> None:
    """A score of zero because nothing matched is not "neutral sentiment",
    it is "no signal", and the UI must not conflate them."""
    nothing = sentiment("XMPL publishes its annual report")
    assert nothing.score == 0
    assert nothing.is_meaningful is False

    balanced = sentiment("XMPL beats revenue but misses on margin")
    assert balanced.is_meaningful is True


def test_padding_a_headline_does_not_weaken_it() -> None:
    """Dividing by word count would make the wordier of two identical
    claims look less certain."""
    short = sentiment("XMPL bankruptcy")
    padded = sentiment("In a filing today the company known as XMPL disclosed bankruptcy")
    assert short.score == padded.score


# -- materiality --------------------------------------------------------


def test_materiality_names_dimensions_rather_than_one_number() -> None:
    """"Could affect the balance sheet" and "could affect today's price"
    call for different reactions."""
    impact = materiality("XMPL announces $300m equity offering to fund expansion")
    assert MaterialityDimension.BALANCE_SHEET in impact.dimensions
    assert MaterialityDimension.STRATEGY in impact.dimensions
    assert impact.is_material
    assert impact.is_structural


def test_a_headline_about_nothing_scores_zero() -> None:
    assert materiality("XMPL to attend an industry conference").score == 0


def test_touching_six_dimensions_is_not_twice_as_material_as_three() -> None:
    """It usually just means the headline is long."""
    assert materiality(
        "XMPL revenue margin cash debt rating acquisition regulatory ceo earnings"
    ).score == Decimal(1)


# -- decay and novelty --------------------------------------------------


def test_a_routine_story_decays_faster_than_a_regulatory_one() -> None:
    """Old news must not permanently distort current sentiment."""
    assert half_life((MaterialityDimension.SHORT_TERM_PRICE,)) == Decimal(3)
    assert half_life((MaterialityDimension.REGULATORY,)) == Decimal(45)


def test_the_slowest_dimension_governs_the_half_life() -> None:
    """A story that is both an earnings beat and a regulatory approval
    stops mattering when the approval does."""
    assert half_life(
        (MaterialityDimension.SHORT_TERM_PRICE, MaterialityDimension.REGULATORY)
    ) == Decimal(45)


def test_decay_halves_per_half_life_and_stays_bounded() -> None:
    assert decay(Decimal(0), Decimal(10)) == Decimal(1)
    assert decay(Decimal(10), Decimal(10)) == Decimal("0.5")
    assert decay(Decimal(20), Decimal(10)) == Decimal("0.25")
    assert Decimal(0) <= decay(Decimal(10_000), Decimal(2)) <= Decimal(1)


def test_a_restatement_is_worth_less_than_the_original() -> None:
    """An article rephrasing yesterday's announcement is not news."""
    first = article("a", "XMPL begins production")
    second = article("b", "XMPL production has begun, company says", minutes=60)
    event = cluster_events([first, second])[0]
    assert novelty(event, first) == Decimal(1)
    assert novelty(event, second) < Decimal(1)


# -- attention ----------------------------------------------------------


def test_big_news_about_a_tiny_holding_does_not_reach_the_inbox() -> None:
    """The failure mode a weighted sum would allow: one large factor
    carrying an item over the threshold on its own."""
    scored = attention(
        materiality_score=Decimal(1),
        portfolio_weight=Decimal("0.002"),
        confidence=Decimal(1),
        freshness=Decimal(1),
        novelty_score=Decimal(1),
    )
    assert scored.severity is Severity.INFO


def test_a_certain_fresh_novel_report_about_nothing_scores_nothing() -> None:
    assert attention(
        materiality_score=Decimal(0),
        portfolio_weight=Decimal("0.4"),
        confidence=Decimal(1),
        freshness=Decimal(1),
        novelty_score=Decimal(1),
    ).score == Decimal(0)


def test_a_material_event_in_a_large_position_is_critical() -> None:
    scored = attention(
        materiality_score=Decimal(1),
        portfolio_weight=Decimal("0.6"),
        confidence=Decimal(1),
        freshness=Decimal(1),
        novelty_score=Decimal(1),
    )
    assert scored.severity is Severity.CRITICAL


def test_every_component_survives_so_the_score_can_be_argued_with() -> None:
    """A score nobody can decompose is a score nobody can argue with, and
    this one will be wrong sometimes."""
    scored = attention(
        Decimal("0.6"), Decimal("0.5"), Decimal("0.8"), Decimal("0.9"), Decimal(1)
    )
    text = scored.explain()
    for part in ("materiality", "weight", "confidence", "freshness", "novelty"):
        assert part in text
    assert scored.materiality == Decimal("0.6")


def test_components_are_clamped_rather_than_trusted() -> None:
    scored = attention(Decimal(5), Decimal(-1), Decimal(2), Decimal(1), Decimal(1))
    assert scored.score == Decimal(0)
    assert scored.materiality == Decimal(1)


# -- the whole pipeline -------------------------------------------------


def test_the_pipeline_collapses_coverage_and_ranks_what_is_left() -> None:
    articles = [
        *[
            article(f"prod{i}", "XMPL begins commercial lithium production", source=f"O{i}")
            for i in range(8)
        ],
        article("conf", "XMPL to attend a lithium conference", minutes=20),
        article(
            "debt",
            "XMPL announces $300m equity offering, shares fall",
            source="SEC",
            url="https://sec.gov/edgar/1",
            minutes=40,
        ),
    ]
    scored, duplicates = pipeline(articles, weights={"XMPL": Decimal("0.4")}, now=WHEN)

    assert len(duplicates) == 7  # eight copies of one story
    titles = [item.event.key_title for item in scored]
    assert titles[0] == "XMPL announces $300m equity offering, shares fall"
    # The conference is present but bottom-ranked: nothing about it is
    # material, so it is not hidden, only deprioritised.
    assert "conference" in titles[-1]
    assert scored[-1].severity is Severity.INFO


def test_a_filing_outranks_a_forum_post_about_the_same_thing() -> None:
    filing = score_event(
        cluster_events(
            [article("a", "XMPL reports record quarterly revenue", source="SEC",
                     url="https://sec.gov/edgar/1")]
        )[0],
        portfolio_weight=Decimal("0.3"),
        now=WHEN,
    )
    forum = score_event(
        cluster_events(
            [article("b", "XMPL reports record quarterly revenue", source="reddit",
                     url="https://reddit.com/x")]
        )[0],
        portfolio_weight=Decimal("0.3"),
        now=WHEN,
    )
    assert filing.attention.score > forum.attention.score
    assert filing.confirmation is Confirmation.CONFIRMED
    assert forum.confirmation is Confirmation.SPECULATION


def test_syndication_is_not_corroboration() -> None:
    """Fifty outlets running one wire story is one source, so the
    corroboration bonus is capped low."""
    one = score_event(
        cluster_events([article("a", "XMPL revenue beats", source="Reuters")])[0],
        portfolio_weight=Decimal("0.5"), now=WHEN,
    )
    many = score_event(
        cluster_events(
            [
                article(f"a{i}", f"XMPL revenue beats estimates for the {i} quarter",
                        source=f"Outlet{i}", minutes=i)
                for i in range(20)
            ]
        )[0],
        portfolio_weight=Decimal("0.5"), now=WHEN,
    )
    assert many.attention.confidence - one.attention.confidence <= Decimal("0.15")


# ---------------------------------------------------------------------------
# markup leaking into the text pipeline
# ---------------------------------------------------------------------------


def test_rss_description_markup_is_not_read_as_prose() -> None:
    """The bug that made materiality score 1.00 on everything.

    Google News sets `<description>` to an HTML anchor. Stored verbatim,
    `target="_blank"` matched the VALUATION stem `target` on 123 of 123 real
    articles, so the score was pinned near its ceiling across the whole
    corpus and discriminated between nothing.

    Both defences are asserted, because either alone is fragile: the markup
    is stripped here, *and* the term that caught it was removed from the
    lexicon. A future lexicon addition like `link` or `image` would
    otherwise reintroduce exactly this failure silently.
    """
    from akmarkets.providers.news import strip_markup

    raw = (
        '<a href="https://news.google.com/rss/articles/CBMizgFBVV95cUxQS3RN" '
        'target="_blank">Apple names a new chief financial officer</a>'
        "&nbsp;&#8212;&nbsp;<font color=\"#6f6f6f\">Reuters</font>"
    )
    cleaned = strip_markup(raw)
    assert cleaned == "Apple names a new chief financial officer Reuters"

    # No markup token survives into anything that gets scored.
    for leaked in ("target", "href", "_blank", "CBMizgFBVV95cUxQS3RN", "#6f6f6f", "font"):
        assert leaked not in cleaned

    # The dimension it genuinely is, and nothing invented by the markup.
    scored = materiality("Apple names a new chief financial officer", cleaned)
    assert MaterialityDimension.MANAGEMENT in scored.dimensions
    assert MaterialityDimension.VALUATION not in scored.dimensions


def test_strip_markup_leaves_plain_text_alone() -> None:
    """A feed that sends prose must survive unchanged apart from spacing."""
    from akmarkets.providers.news import strip_markup

    assert strip_markup("Example Lithium Corp beats H1 production targets") == (
        "Example Lithium Corp beats H1 production targets"
    )
    assert strip_markup("  spaced   out \n text ") == "spaced out text"


def test_production_targets_are_revenue_not_valuation() -> None:
    """"Beats production targets" is an operating fact, not an analyst view.

    Bare `target` used to put it in VALUATION. `price target` still catches
    the analyst case, and `production` already carried the operating one.
    """
    operating = materiality("Example Lithium Corp beats H1 production targets", "")
    assert MaterialityDimension.REVENUE in operating.dimensions
    assert MaterialityDimension.VALUATION not in operating.dimensions

    analyst = materiality("Top analyst revamps Google stock price target", "")
    assert MaterialityDimension.VALUATION in analyst.dimensions

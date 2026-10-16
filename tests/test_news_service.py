"""News persistence, suppression, and the inbox."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from akmarkets.db.engine import Database
from akmarkets.db.models import AlertRow, NewsArticleRow
from akmarkets.domain.news import Article
from akmarkets.providers.base import ProviderResult
from akmarkets.services import news as news_service
from akmarkets.services import portfolio as portfolio_service
from akmarkets.web.app import create_app

WHEN = datetime.now(UTC) - timedelta(hours=2)


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


@pytest.fixture
def folio(session: Session) -> str:
    user = portfolio_service.get_or_create_user(session, "local@localhost")
    return portfolio_service.create_portfolio(session, user.user_id, "Long Term").portfolio_id


def article(ident: str, title: str, source: str = "Reuters", minutes: int = 0) -> Article:
    return Article(
        article_id=ident,
        title=title,
        url=f"https://example.com/{ident}",
        source=source,
        published_at=WHEN + timedelta(minutes=minutes),
        symbols=("XMPL",),
    )


class FakeFeed:
    name = "fake-news"

    def __init__(self, articles: Sequence[Article]) -> None:
        self._articles = list(articles)
        self.calls = 0

    def fetch_for_symbols(
        self, symbols: Sequence[str], days: int = 14, limit_per_symbol: int = 25
    ) -> ProviderResult[list[Article]]:
        self.calls += 1
        return ProviderResult.healthy(self._articles, None)


# -- storage ------------------------------------------------------------


def test_articles_seen_before_are_not_stored_twice(session: Session) -> None:
    """Storage spans runs so deduplication does: an article seen yesterday
    must not become a new event tonight because the process restarted."""
    batch = [article("a", "XMPL output rises")]
    assert news_service.store_articles(session, batch) == 1
    assert news_service.store_articles(session, batch) == 0


def test_one_story_returned_for_three_symbols_is_stored_once(session: Session) -> None:
    """The bug that widening the research universe detonated instantly.

    A feed is queried per symbol, so an analyst piece mentioning Apple, Google
    and Microsoft comes back three times carrying one article_id. store_articles
    checked the database for ids it already had and never added newly-seen ids
    to that set, so the repeat inside a single batch was inserted twice and the
    flush died on a UNIQUE violation -- taking the whole refresh down with it.

    With four symbols the collision was rare enough never to happen in a month
    of running. At twelve it happened on the first attempt, with 543 articles.
    """
    # One id, three arrivals -- which is what a per-symbol feed hands back.
    one_story = article("shared", "Chips rally broadens")
    same_again = article("shared", "Chips rally broadens")
    third_time = article("shared", "Chips rally broadens")

    stored = news_service.store_articles(session, [one_story, same_again, third_time])

    assert stored == 1, "one story is one row, however many symbols asked for it"
    session.flush()  # the UNIQUE violation surfaced here, not at add()


def test_a_collapsed_duplicate_keeps_its_row(session: Session) -> None:
    """The count of what was collapsed is what lets a page say "fifty
    articles, eight stories" rather than quietly showing eight."""
    news_service.store_articles(
        session, [article("a", "XMPL output rises"), article("b", "XMPL output rises", minutes=2)]
    )
    news_service.mark_duplicates(session, {"b": "a"})
    dropped = session.get(NewsArticleRow, "b")
    assert dropped is not None
    assert dropped.duplicate_of == "a"


# -- alerting -----------------------------------------------------------


def _scored(session: Session, folio_id: str, articles: Sequence[Article]) -> Any:
    from akmarkets.domain.news import pipeline

    news_service.store_articles(session, articles)
    stored = news_service.recent_articles(session)
    scored, duplicates = pipeline(stored, weights={"XMPL": Decimal("0.5")})
    news_service.mark_duplicates(session, duplicates)
    news_service.store_events(session, scored)
    return scored


def test_a_material_event_in_a_real_position_reaches_the_inbox(
    session: Session, folio: str
) -> None:
    scored = _scored(
        session, folio, [article("a", "XMPL announces $300m equity offering to fund expansion")]
    )
    raised = news_service.raise_alerts(session, folio, scored)
    assert len(raised) == 1
    assert raised[0].severity in {"watch", "important", "critical"}
    # The explanation travels with it, so the ranking can be argued with.
    assert "materiality" in raised[0].explanation


def test_an_immaterial_event_is_recorded_but_not_pushed(
    session: Session, folio: str
) -> None:
    """Below the floor an event is still readable — it is simply not pushed
    at anyone."""
    scored = _scored(session, folio, [article("a", "XMPL to attend a lithium conference")])
    assert news_service.raise_alerts(session, folio, scored) == []
    assert session.get(NewsArticleRow, "a") is not None


def test_the_same_event_does_not_alert_twice(session: Session, folio: str) -> None:
    """An inbox that repeats itself is an inbox nobody opens, which quietly
    destroys the value of everything else in it."""
    scored = _scored(session, folio, [article("a", "XMPL announces $300m equity offering")])
    assert len(news_service.raise_alerts(session, folio, scored)) == 1
    assert news_service.raise_alerts(session, folio, scored) == []


def test_a_muted_event_stays_muted(session: Session, folio: str) -> None:
    """Overriding a mute would teach the user the button does not work."""
    scored = _scored(session, folio, [article("a", "XMPL announces $300m equity offering")])
    raised = news_service.raise_alerts(session, folio, scored)
    news_service.set_alert_state(session, raised[0].alert_id, muted=True)

    # Age the alert past the suppression window so only the mute is left
    # holding it back.
    raised[0].created_at = raised[0].created_at - news_service.SUPPRESS_FOR * 2
    session.flush()
    assert news_service.raise_alerts(session, folio, scored) == []


def test_muting_also_marks_it_read(session: Session, folio: str) -> None:
    """Leaving it in an unread count the user just dismissed would be
    the same bug from the other direction."""
    scored = _scored(session, folio, [article("a", "XMPL announces $300m equity offering")])
    alert = news_service.raise_alerts(session, folio, scored)[0]
    news_service.set_alert_state(session, alert.alert_id, muted=True)
    assert alert.read_at is not None


def test_the_same_event_can_matter_to_one_portfolio_and_not_another(
    session: Session, folio: str
) -> None:
    """The entire point of weighting attention by position size."""
    user = portfolio_service.get_or_create_user(session, "local@localhost")
    other = portfolio_service.create_portfolio(session, user.user_id, "Tiny").portfolio_id

    from akmarkets.domain.news import pipeline

    news_service.store_articles(
        session, [article("a", "XMPL announces $300m equity offering to fund expansion")]
    )
    stored = news_service.recent_articles(session)

    big, _ = pipeline(stored, weights={"XMPL": Decimal("0.5")})
    small, _ = pipeline(stored, weights={"XMPL": Decimal("0.001")})
    news_service.store_events(session, big)

    assert len(news_service.raise_alerts(session, folio, big)) == 1
    assert news_service.raise_alerts(session, other, small) == []


# -- inbox state --------------------------------------------------------


def test_read_and_archived_items_leave_the_default_view(
    session: Session, folio: str
) -> None:
    scored = _scored(session, folio, [article("a", "XMPL announces $300m equity offering")])
    alert = news_service.raise_alerts(session, folio, scored)[0]

    assert len(news_service.inbox(session, folio)) == 1
    news_service.set_alert_state(session, alert.alert_id, read=True)
    assert news_service.inbox(session, folio) == []
    assert len(news_service.inbox(session, folio, include_read=True)) == 1


def test_unread_counts_are_computed_not_derived_from_a_list(
    session: Session, folio: str
) -> None:
    """The dashboard must be able to say "1 critical" without loading every
    alert."""
    scored = _scored(session, folio, [article("a", "XMPL announces $300m equity offering")])
    news_service.raise_alerts(session, folio, scored)
    counts = news_service.unread_counts(session, folio)
    assert sum(counts.values()) == 1


# -- watchlists ---------------------------------------------------------


def test_a_watched_symbol_is_monitored_without_being_owned(
    session: Session, folio: str
) -> None:
    """Something you are considering is exactly when news matters most."""
    news_service.add_to_watchlist(session, folio, "alb", tier="B", note="lithium peer")
    entries = news_service.watchlist(session, folio)
    assert [(row.symbol, row.tier) for row in entries] == [("ALB", "B")]
    assert "ALB" in news_service.monitored_symbols(session, folio)


def test_a_watched_symbol_gets_a_small_non_zero_weight(
    session: Session, folio: str, settings: Any
) -> None:
    """Zero would multiply its attention to nothing and it would never
    surface, which defeats the purpose of watching it."""
    news_service.add_to_watchlist(session, folio, "ALB", tier="B")
    weights = news_service.portfolio_weights(session, settings, folio)
    assert weights["ALB"] == news_service.WATCHLIST_WEIGHT["B"]
    assert Decimal(0) < weights["ALB"] < Decimal("0.5")


def test_removing_a_watch_stops_it_being_monitored(session: Session, folio: str) -> None:
    news_service.add_to_watchlist(session, folio, "ALB")
    news_service.remove_from_watchlist(session, folio, "alb")
    assert "ALB" not in news_service.monitored_symbols(session, folio)


# -- the refresh --------------------------------------------------------


def test_a_refresh_with_nothing_watched_does_not_call_the_provider(
    session: Session, folio: str, settings: Any
) -> None:
    """A portfolio holding nothing costs nothing to refresh."""
    feed = FakeFeed([])
    news_service.refresh_news(session, settings, provider=feed)
    assert feed.calls == 0


def test_a_refresh_collects_scores_and_alerts_in_one_pass(
    session: Session, folio: str, settings: Any
) -> None:
    news_service.add_to_watchlist(session, folio, "XMPL", tier="A")
    feed = FakeFeed(
        [
            article("a", "XMPL announces $300m equity offering to fund expansion"),
            *[
                article(f"d{i}", "XMPL begins commercial production", source=f"O{i}", minutes=i)
                for i in range(6)
            ],
        ]
    )
    result = news_service.refresh_news(session, settings, provider=feed)

    assert result["items"] == 7
    assert "alert" in result["detail"]
    # Six copies of one story collapsed into one event.
    events = {row.event_id for row in session.query(AlertRow).all()}
    assert len(events) <= 2


# -- the page -----------------------------------------------------------


def test_the_inbox_page_leads_with_what_needs_attention(
    migrated: Database, settings: Any
) -> None:
    with migrated.sessions.begin() as session:
        user = portfolio_service.get_or_create_user(session, "local@localhost")
        portfolio_id = portfolio_service.create_portfolio(
            session, user.user_id, "Long Term"
        ).portfolio_id
        scored = _scored(
            session, portfolio_id, [article("a", "XMPL announces $300m equity offering")]
        )
        news_service.raise_alerts(session, portfolio_id, scored)

    with TestClient(create_app(settings)) as client:
        body = client.get(f"/portfolios/{portfolio_id}/inbox").text

    assert "deserve" in body
    assert "equity offering" in body
    # The explanation is on the page, not just in the database.
    assert "materiality" in body


def test_an_empty_inbox_says_so_rather_than_showing_a_zero(
    migrated: Database, settings: Any
) -> None:
    with migrated.sessions.begin() as session:
        user = portfolio_service.get_or_create_user(session, "local@localhost")
        portfolio_id = portfolio_service.create_portfolio(
            session, user.user_id, "Empty"
        ).portfolio_id

    with TestClient(create_app(settings)) as client:
        body = client.get(f"/portfolios/{portfolio_id}/inbox").text
    assert "Nothing needs your attention" in body


def test_acting_on_an_alert_from_another_portfolio_is_refused(
    migrated: Database, settings: Any
) -> None:
    with migrated.sessions.begin() as session:
        user = portfolio_service.get_or_create_user(session, "local@localhost")
        first = portfolio_service.create_portfolio(session, user.user_id, "One").portfolio_id
        second = portfolio_service.create_portfolio(session, user.user_id, "Two").portfolio_id
        scored = _scored(session, first, [article("a", "XMPL announces $300m equity offering")])
        alert_id = news_service.raise_alerts(session, first, scored)[0].alert_id

    with TestClient(create_app(settings)) as client:
        response = client.post(f"/portfolios/{second}/inbox/{alert_id}/read")
    assert response.status_code == 404

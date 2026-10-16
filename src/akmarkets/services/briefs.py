"""Daily and weekly briefs, and the router that delivers them.

A brief answers one question — *what actually mattered?* — and its hardest
job is leaving things out. A brief that lists every holding every morning is
a brief nobody reads, and once it is unread it does not matter how good the
one important line in it was.

So a section with nothing in it is omitted entirely rather than rendered
empty, and a brief with no sections at all says "nothing needs your
attention" in one line rather than padding to look substantial.

**Alert generation and delivery are separate.** The router takes finished
notifications and hands them to channels; it does not decide what is worth
sending. That split is what lets iOS push be added later by writing one
channel, without touching a line of the logic that decides what matters.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.db.models import AlertRow, PortfolioRow
from akmarkets.domain.money import ZERO, format_money
from akmarkets.domain.news import Severity
from akmarkets.services import market_data, news, performance
from akmarkets.services import portfolio as portfolio_service


class BriefKind(StrEnum):
    MORNING = "morning"
    CLOSING = "closing"
    WEEKLY = "weekly"


@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.lines


@dataclass
class Brief:
    kind: BriefKind
    portfolio_id: str
    portfolio_name: str
    on: date
    headline: str
    # Always shown: the portfolio's value and return. Kept apart from
    # `sections` because it is context rather than news — a brief whose only
    # content is "your portfolio is worth what it was worth" is a quiet one,
    # and counting that line as a section would hide that.
    summary: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def add(self, title: str, lines: Sequence[str]) -> None:
        """Add a section, but only if it has something in it.

        The omission is the feature. A section rendered empty says "we
        looked and there was nothing", which is true but costs a reader
        the same attention as a section that matters.
        """
        if lines:
            self.sections.append(Section(title=title, lines=list(lines)))

    @property
    def is_quiet(self) -> bool:
        return not self.sections

    def render(self) -> str:
        parts = [self.headline]
        parts.extend(f"  · {line}" for line in self.summary)
        for section in self.sections:
            parts.append(f"\n{section.title}")
            parts.extend(f"  · {line}" for line in section.lines)
        if self.is_quiet:
            parts.append("\nNothing needs your attention.")
        return "\n".join(parts)


def _severity_counts(session: Session, portfolio_id: str) -> dict[str, int]:
    return news.unread_counts(session, portfolio_id)


def _attention_headline(counts: dict[str, int]) -> str:
    critical = counts.get(Severity.CRITICAL.value, 0)
    important = counts.get(Severity.IMPORTANT.value, 0)
    watch = counts.get(Severity.WATCH.value, 0)
    total = critical + important + watch
    if not total:
        return "Nothing deserves your attention."
    bits = []
    if critical:
        bits.append(f"{critical} critical")
    if important:
        bits.append(f"{important} important")
    if watch:
        bits.append(f"{watch} watch")
    plural = "" if total == 1 else "s"
    return f"{total} item{plural} deserve{'s' if total == 1 else ''} attention — {', '.join(bits)}."


def build_brief(
    session: Session,
    settings: Settings,
    portfolio_id: str,
    kind: BriefKind = BriefKind.CLOSING,
    on: date | None = None,
) -> Brief:
    """Assemble a brief from what the platform already computed.

    Nothing is calculated here. Every line quotes a figure produced by a
    module that can be tested against a hand-worked reference, which is why
    a brief can be trusted without re-deriving it.
    """
    folio = portfolio_service.get_portfolio(session, portfolio_id)
    today = on or date.today()
    counts = _severity_counts(session, portfolio_id)

    brief = Brief(
        kind=kind,
        portfolio_id=portfolio_id,
        portfolio_name=folio.name,
        on=today,
        headline=_attention_headline(counts),
    )

    cache = market_data.build_price_cache(settings)
    period = "1w" if kind is BriefKind.WEEKLY else "1d"
    report = performance.performance(session, cache, portfolio_id, period=period)

    value_lines: list[str] = []
    prices, _ = market_data.closing_prices(
        cache, performance.portfolio_symbols(session, portfolio_id)
    )
    instruments = portfolio_service.list_instruments(
        session,
        [
            t.instrument_id
            for t in portfolio_service.list_transactions(session, portfolio_id)
            if t.instrument_id
        ],
    )
    by_instrument = {
        instrument_id: prices[instrument.symbol]
        for instrument_id, instrument in instruments.items()
        if instrument.symbol in prices
    }
    state = portfolio_service.portfolio_state(
        session, portfolio_id, prices=by_instrument
    )
    if state.total_value is not None:
        value_lines.append(
            f"Portfolio {format_money(state.total_value, folio.base_currency)}"
        )
    if report.summary and report.summary.time_weighted is not None:
        value_lines.append(f"Return ({period}) {report.summary.time_weighted:+.2%}")
        if report.benchmark.relative is not None:
            value_lines.append(
                f"vs {report.benchmark.symbol} {report.benchmark.relative:+.2%}"
            )
    if state.unpriced_instrument_ids:
        value_lines.append(
            f"{len(state.unpriced_instrument_ids)} holding(s) unpriced — the total "
            f"is withheld rather than understated"
        )
    brief.summary = value_lines

    # Movers, biggest first in each direction. Capped at three a side: a
    # brief that lists everything is a spreadsheet.
    contributions = [item for item in report.contributions if item.profit != ZERO]
    symbol_of = {
        instrument_id: instrument.symbol
        for instrument_id, instrument in instruments.items()
    }
    gainers = [item for item in contributions if item.profit > ZERO][:3]
    losers = sorted(
        (item for item in contributions if item.profit < ZERO),
        key=lambda item: item.profit,
    )[:3]
    brief.add(
        "Contributors",
        [
            f"{symbol_of.get(item.instrument_id, '?')} "
            f"{format_money(item.profit, folio.base_currency)}"
            for item in gainers
        ],
    )
    brief.add(
        "Detractors",
        [
            f"{symbol_of.get(item.instrument_id, '?')} "
            f"{format_money(item.profit, folio.base_currency)}"
            for item in losers
        ],
    )

    # Only unread, and only above the noise floor. Repeating what was read
    # yesterday is how a brief becomes wallpaper.
    alerts = [
        alert
        for alert in news.inbox(session, portfolio_id, limit=20)
        if alert.severity in {Severity.CRITICAL.value, Severity.IMPORTANT.value}
    ]
    brief.add(
        "Needs attention",
        [f"[{alert.severity}] {alert.symbols or '—'} {alert.title}" for alert in alerts[:5]],
    )

    # Scheduled reports for what is actually held. Placed after the alerts
    # because it is a diary, not news: nothing here has happened yet, and a
    # brief that leads with next week's dates buries today's facts.
    from akmarkets.services import macro as macro_service

    symbols_held = news.monitored_symbols(session, portfolio_id)
    brief.add(
        "Coming up",
        [
            macro_service.describe_earnings(row)
            for row in macro_service.upcoming_earnings(
                session, symbols=symbols_held, within_days=7, today=today
            )
        ][:5],
    )

    if kind is BriefKind.WEEKLY:
        from akmarkets.services import brain as brain_service

        symbols = news.monitored_symbols(session, portfolio_id)
        changes = brain_service.portfolio_changes(
            session, settings, portfolio_id, symbols
        )
        brief.add(
            "What changed this week",
            [
                f"{change.symbol}: {change.material[0].description}"
                for change in changes
                if change.material
            ][:6],
        )

    return brief


# ---------------------------------------------------------------------------
# notification routing
# ---------------------------------------------------------------------------


class Channel(StrEnum):
    INBOX = "inbox"
    EMAIL = "email"
    PUSH = "push"


@dataclass(frozen=True)
class Notification:
    """A finished thing to deliver. Channel-independent by construction.

    It carries no formatting and no channel-specific fields, which is what
    lets an iOS push channel be added later by writing one function — the
    logic that decided this was worth sending never has to be touched.
    """

    portfolio_id: str
    severity: Severity
    title: str
    body: str
    symbols: tuple[str, ...] = ()
    # A stable identifier for the thing this is about, so a future deep
    # link opens the right page rather than the inbox.
    subject_id: str = ""
    subject_kind: str = "alert"

    @property
    def deep_link(self) -> str:
        return f"/portfolios/{self.portfolio_id}/inbox#{self.subject_id}"


Deliverer = Callable[[Notification], bool]


@dataclass
class Router:
    """Sends notifications to whichever channels are enabled.

    A channel that fails does not stop the others: a broken email
    configuration must not prevent the inbox — the one channel that always
    works — from receiving anything.
    """

    channels: dict[Channel, Deliverer] = field(default_factory=dict)

    def register(self, channel: Channel, deliverer: Deliverer) -> None:
        self.channels[channel] = deliverer

    def send(
        self, notification: Notification, minimum: Severity = Severity.IMPORTANT
    ) -> dict[Channel, bool]:
        """Deliver, filtering by severity per channel.

        The inbox receives everything regardless of the minimum: it is the
        durable record, and a user who lowers their push threshold later
        should still find the history there.
        """
        order = {
            Severity.INFO: 0,
            Severity.WATCH: 1,
            Severity.IMPORTANT: 2,
            Severity.CRITICAL: 3,
        }
        results: dict[Channel, bool] = {}
        for channel, deliverer in self.channels.items():
            if channel is not Channel.INBOX and order[notification.severity] < order[minimum]:
                continue
            try:
                results[channel] = deliverer(notification)
            except Exception:
                # Recorded as a failure rather than raised. One broken
                # channel must not take the others down with it.
                results[channel] = False
        return results


def notifications_for(session: Session, portfolio_id: str) -> list[Notification]:
    """Unread alerts as channel-independent notifications."""
    return [
        Notification(
            portfolio_id=portfolio_id,
            severity=Severity(alert.severity),
            title=alert.title,
            body=alert.body,
            symbols=tuple(part for part in alert.symbols.split(",") if part),
            subject_id=alert.alert_id,
        )
        for alert in news.inbox(session, portfolio_id, limit=50)
    ]


def all_portfolios(session: Session) -> list[PortfolioRow]:
    return list(
        session.scalars(
            select(PortfolioRow).where(PortfolioRow.is_archived.is_(False))
        ).all()
    )


def unread_since(session: Session, portfolio_id: str, days: int = 1) -> int:
    from akmarkets.db.base import utc_now

    cutoff = utc_now() - timedelta(days=days)
    return len(
        session.scalars(
            select(AlertRow).where(
                AlertRow.portfolio_id == portfolio_id,
                AlertRow.created_at >= cutoff,
                AlertRow.read_at.is_(None),
            )
        ).all()
    )


def weight_of(weights: dict[str, Decimal], symbol: str) -> Decimal:
    return weights.get(symbol, ZERO)

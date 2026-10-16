"""Ask Market Brain, and the cost ledger behind it.

The architecture that matters, and the one this refuses:

    question → intent → structured retrieval → deterministic calculation
             → minimal context → explanation

**Never** question → send the database → hope. Every number the model sees
was computed upstream by code that can be tested; the model's contribution
is language, not arithmetic. That is why `SYSTEM_PROMPT` forbids
recalculation and why the context is assembled fact by fact.

Intent classification is deterministic keyword matching rather than a model
call. Spending a model call to decide which model call to make is a cost
with no corresponding benefit, and a wrong classification here is cheap:
the fallback gathers a broad context and the answer is still grounded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from akmarkets.ai.context import Context
from akmarkets.ai.router import (
    AiDisabled,
    Budget,
    BudgetExhausted,
    Completion,
    NothingToExplain,
    Router,
    Tier,
    Usage,
    openai_completer,
)
from akmarkets.config import Settings
from akmarkets.db.base import new_id, utc_now
from akmarkets.db.models import AiCallRow, AlertRow
from akmarkets.domain.money import ZERO
from akmarkets.secrets import resolve_api_key
from akmarkets.services import brain, market_data, news, performance
from akmarkets.services import portfolio as portfolio_service
from akmarkets.services import security as security_service


class Intent(StrEnum):
    """What kind of question this is, which decides what to retrieve."""

    PORTFOLIO_PERFORMANCE = "portfolio_performance"
    WHY_MOVING = "why_moving"
    WHAT_CHANGED = "what_changed"
    POSITION_DETAIL = "position_detail"
    RISK = "risk"
    UPCOMING = "upcoming"
    GENERAL = "general"


_INTENT_TERMS: dict[Intent, tuple[str, ...]] = {
    Intent.WHY_MOVING: ("why", "down today", "up today", "moving", "dropped", "jumped"),
    Intent.WHAT_CHANGED: ("changed", "since", "new", "update", "different"),
    Intent.PORTFOLIO_PERFORMANCE: (
        "performance", "how am i", "return", "versus", "vs ", "beating", "doing",
    ),
    Intent.RISK: ("risk", "exposure", "concentrat", "correlat", "drawdown"),
    Intent.UPCOMING: ("upcoming", "earnings", "next week", "calendar", "catalyst"),
    Intent.POSITION_DETAIL: ("position", "holding", "shares", "thesis"),
}


def classify_intent(question: str) -> Intent:
    """Deterministic keyword matching.

    Spending a model call to decide which model call to make is a cost with
    no corresponding benefit. A wrong guess is cheap here: the general
    fallback gathers a broad context and the answer is still grounded in
    computed facts.
    """
    lowered = question.lower()
    for intent, terms in _INTENT_TERMS.items():
        if any(term in lowered for term in terms):
            return intent
    return Intent.GENERAL


def extract_symbols(question: str, known: Sequence[str]) -> list[str]:
    """Symbols the question mentions, from those the portfolio knows.

    Matched against known symbols rather than by pattern, because a regex
    for tickers matches ordinary words — `IT`, `ALL`, `ON` and `FOR` are all
    real listings and half of them appear in every sentence.

    Case is respected rather than folded, which is the other half of the
    same problem: upper-casing every word turns "is **it** on track?" into a
    mention of `IT`. A ticker written as a ticker is capitalised or
    `$`-prefixed, so a lower-case word is taken at face value as a word.
    """
    mentioned: set[str] = set()
    for raw in question.split():
        word = raw.strip(".,?!'\"()")
        if word.startswith("$"):
            mentioned.add(word[1:].upper())
        elif word.isupper():
            mentioned.add(word)
    return [symbol for symbol in known if symbol.upper() in mentioned]


# -- the ledger ---------------------------------------------------------


def spent_today(session: Session, on: date | None = None) -> Decimal:
    """What today's AI calls have cost, in UTC.

    **The day is UTC, deliberately, because the ledger is.** This used to
    take `date.today()` — the *local* date — and then build the window with
    `_start_of`, which is UTC. East of Greenwich the two disagree for the
    hours between local midnight and UTC midnight, and in that window the
    query looked for calls in a day that had not started yet: spend read
    zero however much had actually been spent, so the daily ceiling stopped
    holding exactly when the date rolled over. Worse, spend recorded in
    those hours was never counted on any day, because the following day's
    window began after it.

    A ceiling that is briefly blind every single day is not a ceiling.
    """
    day = on or datetime.now(UTC).date()
    rows = session.scalars(
        select(AiCallRow.cost_usd).where(
            AiCallRow.created_at >= _start_of(day),
            AiCallRow.created_at < _start_of(day + timedelta(days=1)),
        )
    ).all()
    return sum((Decimal(value) for value in rows), start=ZERO)


def _start_of(day: date) -> Any:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def cost_by_feature(session: Session, days: int = 30) -> list[tuple[str, Decimal, int]]:
    """Which features are consuming money, over a window.

    The whole reason the ledger records a feature. "AI cost $4 this month"
    is not actionable; "Ask Market Brain cost $3.60 of it" is.
    """
    since = utc_now() - timedelta(days=days)
    rows = session.scalars(select(AiCallRow).where(AiCallRow.created_at >= since)).all()
    totals: dict[str, tuple[Decimal, int]] = {}
    for row in rows:
        cost, count = totals.get(row.feature, (ZERO, 0))
        totals[row.feature] = (cost + Decimal(row.cost_usd), count + 1)
    return sorted(
        ((feature, cost, count) for feature, (cost, count) in totals.items()),
        key=lambda item: item[1],
        reverse=True,
    )


def build_router(session: Session, settings: Settings, run_id: str = "") -> Router:
    """Assemble a router with today's spend already loaded.

    The budget is seeded from the ledger rather than from process memory,
    so a ceiling survives a restart. A budget that resets when the web app
    reloads is not a ceiling.
    """
    budget = Budget(
        ceiling=Decimal(str(settings.ai.daily_cost_ceiling_usd)),
        spent=spent_today(session),
        on=datetime.now(UTC).date(),
    )

    def record(feature: str, tier: Tier, model: str, usage: Usage) -> None:
        session.add(
            AiCallRow(
                call_id=new_id(),
                feature=feature,
                tier=int(tier),
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=str(usage.cost),
                run_id=run_id,
                created_at=utc_now(),
            )
        )
        session.flush()

    key = resolve_api_key(settings.openai_keyring_name, "OPENAI_API_KEY")
    return Router(
        enabled=settings.ai.enabled and bool(key),
        economical_model=settings.ai.tier2_model,
        reasoning_model=settings.ai.tier3_model,
        budget=budget,
        complete=openai_completer(key) if key else None,
        recorder=record,
    )


# -- the answer ---------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    question: str
    intent: Intent
    context: Context
    completion: Completion | None
    # Set when no model was called, with the reason. A refusal and an empty
    # answer need completely different UI, so they are never conflated.
    refusal: str | None = None

    @property
    def text(self) -> str:
        return self.completion.text if self.completion else (self.refusal or "")

    @property
    def cost(self) -> Decimal:
        return self.completion.usage.cost if self.completion else ZERO


def build_context(
    session: Session,
    settings: Settings,
    portfolio_id: str,
    question: str,
    intent: Intent,
) -> Context:
    """Gather exactly what this intent needs, and nothing else.

    Every figure added here was computed by a deterministic module. Nothing
    is summarised into prose before it goes, and no raw amount leaves —
    position sizes are banded.
    """
    context = Context(subject="portfolio", question=question)
    folio = portfolio_service.get_portfolio(session, portfolio_id)
    symbols = news.monitored_symbols(session, portfolio_id)
    mentioned = extract_symbols(question, symbols)

    weights = news.portfolio_weights(session, settings, portfolio_id)
    cache = market_data.build_price_cache(settings)

    if intent is Intent.PORTFOLIO_PERFORMANCE:
        report = performance.performance(session, cache, portfolio_id, period="1m")
        context.subject = f"{folio.name} performance"
        if report.summary and report.summary.time_weighted is not None:
            context.add("portfolio return (1 month, time-weighted)",
                        f"{report.summary.time_weighted:.2%}")
        context.add(f"{report.benchmark.symbol} return (same window)",
                    f"{report.benchmark.benchmark:.2%}" if report.benchmark.benchmark else None)
        context.add("relative",
                    f"{report.benchmark.relative:.2%}" if report.benchmark.relative else None)
        # Named by ticker, not instrument id. A model handed a UUID cannot
        # say anything useful about it and will usually paper over that by
        # talking about the portfolio in general.
        instruments = portfolio_service.list_instruments(
            session, [item.instrument_id for item in report.contributions]
        )
        for item in report.contributions[:5]:
            named = instruments.get(item.instrument_id)
            context.events.append(
                f"contribution {named.symbol if named else 'unknown'}: "
                f"{item.profit:+.0f}"
            )
        if report.unpriced:
            context.notes.append(
                f"{len(report.unpriced)} holding(s) could not be priced, so the "
                f"total is incomplete: {', '.join(report.unpriced)}"
            )

    elif intent in {Intent.WHY_MOVING, Intent.WHAT_CHANGED, Intent.POSITION_DETAIL}:
        targets = mentioned or list(weights)[:3]
        for symbol in targets:
            view = brain.brain_view(session, settings, symbol, portfolio_id=portfolio_id)
            if view is None:
                continue
            context.subject = symbol if len(targets) == 1 else "portfolio"
            context.add_exposure(symbol, weights.get(symbol))
            if view.move:
                context.add(f"{symbol} last move", f"{view.move.move:+.2%}")
                context.add(f"{symbol} attribution", view.move.summary)
                context.add(f"{symbol} attribution confidence", view.move.verdict.value)
            for change in view.changes.material[:4]:
                context.events.append(f"{symbol}: {change.description}")
            for warning in view.warnings:
                context.notes.append(f"{symbol}: {warning}")

            detail = security_service.security_view(
                session, settings, symbol, portfolio_id=portfolio_id
            )
            if detail and detail.technical:
                context.add(f"{symbol} trend", detail.technical.trend)
                context.add(f"{symbol} RSI14",
                            f"{detail.technical.rsi14:.1f}" if detail.technical.rsi14 else None)

    elif intent is Intent.RISK:
        context.subject = f"{folio.name} risk"
        for symbol, weight in sorted(weights.items(), key=lambda x: x[1], reverse=True)[:8]:
            context.add_exposure(symbol, weight)

    else:
        context.subject = folio.name
        unread = news.unread_counts(session, portfolio_id)
        for severity, count in unread.items():
            if count:
                context.add(f"unread {severity} alerts", count)
        for alert in session.scalars(
            select(AlertRow)
            .where(AlertRow.portfolio_id == portfolio_id, AlertRow.read_at.is_(None))
            .order_by(AlertRow.created_at.desc())
            .limit(5)
        ).all():
            context.events.append(f"[{alert.severity}] {alert.title}")

    return context


def ask(
    session: Session,
    settings: Settings,
    portfolio_id: str,
    question: str,
    run_id: str = "",
    router: Router | None = None,
) -> Answer:
    """Answer a question, or explain why nothing was asked of a model.

    The context is built either way. When AI is off or the budget is spent,
    the caller still gets the computed facts — which is the whole point of
    the product working without a model at all.
    """
    intent = classify_intent(question)
    context = build_context(session, settings, portfolio_id, question, intent)
    engine = router or build_router(session, settings, run_id=run_id)

    try:
        completion = engine.complete(
            context,
            feature="ask-market-brain",
            # A user asked and is waiting, so this is always the strong
            # model. Everything automatic is judged on significance.
            wants_reasoning=True,
            max_context_chars=settings.ai.max_context_tokens * 4,
        )
    except (AiDisabled, BudgetExhausted, NothingToExplain) as exc:
        return Answer(
            question=question, intent=intent, context=context, completion=None,
            refusal=str(exc),
        )

    return Answer(question=question, intent=intent, context=context, completion=completion)

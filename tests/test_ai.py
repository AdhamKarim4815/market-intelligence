"""The AI layer: what it refuses to send, and what it refuses to spend.

No test here contacts a provider. The completer is injected, which is the
point of injecting it — the interesting behaviour is entirely in the
refusals, and a live model would make none of them observable.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from akmarkets.ai.context import (
    SYSTEM_PROMPT,
    Context,
    ExposureBand,
    band,
    contains_private_figures,
)
from akmarkets.ai.router import (
    AiDisabled,
    Budget,
    BudgetExhausted,
    Router,
    Tier,
    Usage,
    estimate_tokens,
)
from akmarkets.db.engine import Database
from akmarkets.services import ask as ask_service
from akmarkets.services import portfolio as portfolio_service


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


@pytest.fixture
def folio(session: Session) -> str:
    user = portfolio_service.get_or_create_user(session, "local@localhost")
    return portfolio_service.create_portfolio(session, user.user_id, "Long Term").portfolio_id


def fake_completer(text: str = "Answer.", prompt: int = 500, completion: int = 100) -> Any:
    calls: list[tuple[str, str, str]] = []

    def complete(model: str, system: str, body: str) -> tuple[str, int, int]:
        calls.append((model, system, body))
        return text, prompt, completion

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def router(
    enabled: bool = True, ceiling: str = "1.00", spent: str = "0", **kwargs: Any
) -> Router:
    return Router(
        enabled=enabled,
        economical_model="gpt-4o-mini",
        reasoning_model="gpt-4o",
        budget=Budget(ceiling=Decimal(ceiling), spent=Decimal(spent)),
        complete=kwargs.pop("complete", fake_completer()),
        **kwargs,
    )


# -- privacy ------------------------------------------------------------


def test_a_position_is_banded_not_disclosed() -> None:
    """A model does not need to know a position is $52,431.17. It needs to
    know it is large — and LARGE cannot be reversed into a number."""
    assert band(Decimal("0.32")) is ExposureBand.DOMINANT
    assert band(Decimal("0.14")) is ExposureBand.LARGE
    assert band(Decimal("0.05")) is ExposureBand.MODERATE
    assert band(Decimal("0.01")) is ExposureBand.SMALL


def test_not_held_is_none_rather_than_small() -> None:
    """A model told SMALL would reason about a position that does not
    exist."""
    assert band(None) is ExposureBand.NONE
    assert band(Decimal(0)) is ExposureBand.NONE


def test_no_raw_amount_reaches_the_rendered_context() -> None:
    """The banding is only worth anything if nothing leaks the figure by
    another route — and a number sent to a provider cannot be recalled."""
    context = Context(subject="XMPL")
    context.add_exposure("XMPL", Decimal("0.31"))
    context.add("XMPL RSI14", "47.6")
    body = context.render()

    assert "DOMINANT" in body
    assert contains_private_figures(body, ["52431.17", "0.31", "52,431"]) == []


def test_unknown_facts_are_omitted_rather_than_sent_as_unknown() -> None:
    """A model given a list of unknowns pattern-matches its way to a
    confident answer about the few things it does know."""
    context = Context(subject="X")
    context.add("revenue", None)
    context.add("RSI14", "")
    context.add("trend", "uptrend")
    assert len(context.observations) == 1


def test_the_system_prompt_forbids_the_model_producing_numbers() -> None:
    """The rule the whole architecture exists to enforce."""
    assert "Never recalculate" in SYSTEM_PROMPT
    assert "not financial advice" in SYSTEM_PROMPT
    assert "No clear cause" in SYSTEM_PROMPT


def test_a_long_context_is_truncated_at_item_boundaries() -> None:
    """A half-sentence of financial data is worse than an omission, because
    the model will complete it."""
    context = Context(subject="X")
    for index in range(500):
        context.add(f"fact {index}", "some value that takes up room")
    body = context.render(max_chars=1000)
    assert len(body) <= 1000
    assert "omitted to stay within the context budget" in body
    assert not body.endswith("some value that takes up")


# -- the ceiling --------------------------------------------------------


def test_ai_disabled_refuses_rather_than_returning_nothing() -> None:
    """A refusal and an empty answer need completely different UI."""
    with pytest.raises(AiDisabled, match="computed without it"):
        router(enabled=False).complete(Context(subject="X"), feature="test")


def test_the_daily_ceiling_is_a_hard_stop() -> None:
    """A budget that warns and proceeds is not a budget."""
    engine = router(ceiling="0.01", spent="0.0099")
    with pytest.raises(BudgetExhausted, match="ceiling would be exceeded"):
        engine.complete(Context(subject="X"), feature="test")


def test_an_expensive_call_is_refused_before_it_is_made_not_after() -> None:
    completer = fake_completer()
    engine = router(ceiling="0.000001", complete=completer)
    with pytest.raises(BudgetExhausted):
        engine.complete(Context(subject="X"), feature="test")
    assert completer.calls == []


def test_spend_is_recorded_from_the_providers_own_usage() -> None:
    """Never from the estimate — the estimate exists only to pre-check."""
    recorded: list[tuple[str, Tier, str, Usage]] = []
    engine = router(
        complete=fake_completer(prompt=1000, completion=200),
        recorder=lambda *args: recorded.append(args),  # type: ignore[arg-type]
    )
    result = engine.complete(Context(subject="X"), feature="ask", wants_reasoning=True)

    assert recorded[0][0] == "ask"
    assert recorded[0][3].prompt_tokens == 1000
    # gpt-4o: 1000 prompt @ $2.50/M + 200 completion @ $10/M = $0.0045
    assert result.usage.cost == Decimal("0.0045")
    assert engine.budget.spent == Decimal("0.0045")


def test_an_unknown_model_is_priced_at_the_most_expensive_rate() -> None:
    """An unknown model must not be able to spend without appearing to."""
    known = Usage(model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=0)
    unknown = Usage(model="some-new-model", prompt_tokens=1000, completion_tokens=0)
    assert unknown.cost > known.cost


# -- tier routing -------------------------------------------------------


def test_a_user_question_always_gets_the_strong_model() -> None:
    """They asked, and they are waiting."""
    engine = router()
    assert engine.choose_tier(significance=Decimal(0), wants_reasoning=True) is Tier.REASONING


def test_a_routine_change_is_summarised_cheaply() -> None:
    engine = router()
    assert (
        engine.choose_tier(significance=Decimal("0.2"), wants_reasoning=False)
        is Tier.ECONOMICAL
    )


def test_a_material_change_earns_the_strong_model() -> None:
    engine = router()
    assert (
        engine.choose_tier(significance=Decimal("0.8"), wants_reasoning=False)
        is Tier.REASONING
    )


def test_the_chosen_tier_selects_the_configured_model() -> None:
    completer = fake_completer()
    engine = router(complete=completer)
    engine.complete(Context(subject="X"), feature="t", wants_reasoning=True)
    assert completer.calls[0][0] == "gpt-4o"


def test_token_estimation_is_only_used_for_the_pre_check() -> None:
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("") == 1


# -- intent and retrieval -----------------------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Why am I down today?", ask_service.Intent.WHY_MOVING),
        ("What changed since yesterday?", ask_service.Intent.WHAT_CHANGED),
        ("How am I performing versus NASDAQ?", ask_service.Intent.PORTFOLIO_PERFORMANCE),
        ("Which position has the most risk?", ask_service.Intent.RISK),
        ("Which positions have upcoming earnings?", ask_service.Intent.UPCOMING),
    ],
)
def test_intent_is_classified_without_a_model_call(
    question: str, expected: ask_service.Intent
) -> None:
    """Spending a model call to decide which model call to make is a cost
    with no corresponding benefit."""
    assert ask_service.classify_intent(question) is expected


def test_symbols_are_matched_against_known_holdings_not_a_pattern() -> None:
    """A regex for tickers matches ordinary words — IT, ALL, ON and FOR are
    all real listings and half of them appear in every sentence."""
    known = ["XMPL", "GOOGL", "IT"]
    assert ask_service.extract_symbols("Anything on XMPL?", known) == ["XMPL"]
    assert ask_service.extract_symbols("How is $googl doing?", known) == ["GOOGL"]
    # Lower-case "it" is a word, not the ticker IT. Upper-casing every word
    # before matching — the obvious implementation — gets this wrong.
    assert ask_service.extract_symbols("Is it on track?", known) == []
    # But the ticker written as a ticker is still found.
    assert ask_service.extract_symbols("What about IT?", known) == ["IT"]


def test_a_question_still_returns_computed_facts_when_ai_is_off(
    session: Session, settings: Any, folio: str
) -> None:
    """The whole point of the product working without a model at all."""
    answer = ask_service.ask(
        session, settings, folio, "How am I performing?", router=router(enabled=False)
    )
    assert answer.completion is None
    assert answer.refusal is not None
    assert "computed without it" in answer.refusal
    # The context was still assembled, so a page can render the facts.
    assert answer.context.subject


def test_the_ledger_records_which_feature_spent(
    session: Session, settings: Any, folio: str
) -> None:
    """"AI cost $4 this month" is not actionable; "Ask cost $3.60 of it"
    is."""
    engine = ask_service.build_router(session, settings)
    engine.enabled = True
    engine._complete = fake_completer()

    ask_service.ask(session, settings, folio, "How am I performing?", router=engine)
    breakdown = ask_service.cost_by_feature(session)
    assert breakdown
    assert breakdown[0][0] == "ask-market-brain"
    assert breakdown[0][2] == 1


def test_todays_spend_is_read_from_the_ledger_not_process_memory(
    session: Session, settings: Any, folio: str
) -> None:
    """A ceiling that resets when the web app reloads is not a ceiling."""
    engine = ask_service.build_router(session, settings)
    engine.enabled = True
    engine._complete = fake_completer()
    ask_service.ask(session, settings, folio, "How am I performing?", router=engine)

    fresh = ask_service.build_router(session, settings)
    assert fresh.budget.spent > Decimal(0)
    assert fresh.budget.spent == ask_service.spent_today(session)


def test_ai_is_off_by_default(settings: Any) -> None:
    """The platform is required to be fully usable with no LLM at all."""
    assert settings.ai.enabled is False
    assert settings.ai.daily_cost_ceiling_usd == 1.0


def test_a_ceiling_survives_the_day_it_was_set(session: Session, settings: Any) -> None:
    assert ask_service.spent_today(session, on=date(2020, 1, 1)) == Decimal(0)


def test_the_daily_ceiling_is_not_blind_around_local_midnight(
    session: Session, settings: Any, folio: str
) -> None:
    """The spend window and the ledger must agree on which day it is.

    `spent_today` took the *local* date and then built its window in UTC.
    East of Greenwich those disagree between local midnight and UTC
    midnight, and in that gap the query asked for a day that had not begun:
    spend read zero however much had been spent, and money spent in those
    hours was never counted on any day. A ceiling that goes blind every
    single day is not a ceiling.

    Asserted by recording a call at a UTC instant that falls on the
    *previous* local day for a UTC+3 observer, and requiring that it still
    counts against the UTC day it belongs to.
    """
    from datetime import UTC, datetime

    from akmarkets.db.models import AiCallRow

    stamp = datetime(2026, 8, 10, 21, 45, tzinfo=UTC)
    session.add(
        AiCallRow(
            call_id="tz-guard",
            feature="ask",
            tier=2,
            model="gpt-4o-mini",
            cost_usd="0.25",
            created_at=stamp,
        )
    )
    session.flush()

    assert ask_service.spent_today(session, on=stamp.date()) == Decimal("0.25")
    # And the day after in UTC must not still be counting it.
    assert ask_service.spent_today(session, on=date(2026, 8, 11)) == Decimal("0")

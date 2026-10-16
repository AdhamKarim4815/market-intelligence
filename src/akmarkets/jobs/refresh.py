"""The stages a refresh actually runs, and the command that drives them.

Stages are small on purpose. Each one does a thing that can fail on its own,
records what it did, and hands anything a later stage needs through the
context. The split is by *failure domain*, not by convenience: prices and
news fail independently, so revaluing a portfolio must not be blocked by a
news feed timing out.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from akmarkets.db.models import PortfolioRow, WatchlistEntryRow
from akmarkets.jobs.pipeline import Stage, StageContext, run_stages
from akmarkets.services import jobs as job_service
from akmarkets.services import market_data, performance

logger = logging.getLogger(__name__)


def stage_universe(context: StageContext) -> dict[str, Any]:
    """Work out which symbols anything downstream needs.

    Everything else is scoped by this, so a portfolio nobody holds anything
    in costs nothing to refresh. Benchmarks are included: a comparison
    cannot render without one, and discovering that at display time is too
    late.
    """
    portfolios = context.session.scalars(
        select(PortfolioRow).where(PortfolioRow.is_archived.is_(False))
    ).all()
    held: set[str] = set()
    for folio in portfolios:
        held.update(performance.portfolio_symbols(context.session, folio.portfolio_id))

    # The watchlist, which until now only ordered an inbox.
    #
    # Everything downstream is scoped by this stage, so restricting it to
    # holdings meant the platform could only ever research what was already
    # owned -- and the phantom ledger, which scores the calls the brief makes,
    # could therefore only ever be scored on three stocks. A watchlist entry is
    # exactly the statement "this matters to me and I do not own it", and it
    # was already being collected through the web inbox and then ignored here.
    watched = {
        symbol.strip().upper()
        for symbol in context.session.scalars(select(WatchlistEntryRow.symbol)).all()
        if symbol and symbol.strip()
    }

    symbols = held | watched
    context.symbols = sorted(symbols)
    extra = sorted(watched - held)
    context.report.note(
        f"{len(context.symbols)} symbol(s): {len(held)} held across "
        f"{len(portfolios)} portfolio(s), {len(extra)} watched but not held"
    )

    # A configured system that resolves to nothing is a finding. An
    # unconfigured one is not.
    #
    # Every later stage skips when there is nothing to work on and each reports
    # itself done, so a run with no symbols is a completely green refresh that
    # collected nothing -- which is why nobody noticed research had stopped.
    # But `test_a_refresh_with_no_portfolios_is_clean_not_broken` is right that
    # a fresh install has genuinely nothing to do, and alarming on that teaches
    # people to ignore the alarm. So the condition is *portfolios exist and
    # still produced no symbols*, which is the case that can only be a fault.
    starved = bool(portfolios) and not context.symbols
    return {
        "items": len(context.symbols),
        "degraded": starved,
        "detail": (", ".join(context.symbols[:8]) if context.symbols
                   else f"{len(portfolios)} portfolio(s) and no symbols at all: "
                        "nothing held, nothing watched, so every later stage "
                        "will quietly do nothing"),
    }


def stage_prices(context: StageContext) -> dict[str, Any]:
    """Bring the daily-bar cache up to date.

    Reports itself degraded when some symbols failed rather than raising:
    forty symbols of which two are delisted is a successful refresh with a
    known gap, and treating it as a failure would mean never refreshing
    anything again.
    """
    if not context.symbols:
        return {"items": 0, "detail": "no symbols to price"}

    outcomes = market_data.refresh_prices(
        context.session,
        context.settings,
        context.symbols,
        run_id=context.run_id,
    )
    failed = [symbol for symbol, state in outcomes.items() if state in {"failed", "unknown"}]
    context.report.step(f"priced {len(outcomes) - len(failed)}/{len(outcomes)}")
    return {
        "items": len(outcomes) - len(failed),
        "degraded": bool(failed),
        "detail": f"{len(failed)} unpriced: {', '.join(failed[:5])}" if failed else "",
    }


def stage_quotes(context: StageContext) -> dict[str, Any]:
    """Delayed last prices, for the intraday view of the dashboard."""
    if not context.symbols:
        return {"items": 0, "detail": "no symbols to quote"}
    quotes = market_data.latest_quotes(
        context.session, context.settings, context.symbols, run_id=context.run_id
    )
    missing = [s for s in context.symbols if s not in quotes]
    return {
        "items": len(quotes),
        "degraded": bool(missing),
        "detail": f"{len(missing)} unquoted" if missing else "",
    }


def stage_fundamentals(context: StageContext) -> dict[str, Any]:
    """Filed figures from SEC EDGAR.

    Rate-limited to ten requests a second as a condition of use, so this is
    the slowest stage by some distance and is deliberately last among the
    data stages — a timeout here must not delay pricing.
    """
    from akmarkets.services import security as security_service

    if not context.symbols:
        return {"items": 0, "detail": "no symbols"}
    return security_service.refresh_fundamentals(
        context.session, context.settings, context.symbols, run_id=context.run_id
    )


def stage_news(context: StageContext) -> dict[str, Any]:
    """Collect, deduplicate, cluster and score news; fill the inbox.

    Depends on `universe` for the symbol list but deliberately not on
    `prices`: news is worth collecting on a night when every price provider
    is down, and it is often the reason they matter.
    """
    from akmarkets.services import news as news_service

    return news_service.refresh_news(
        context.session, context.settings, run_id=context.run_id
    )


def stage_macro(context: StageContext) -> dict[str, Any]:
    """The Treasury yield curve.

    Deliberately depends on nothing. Rates are not about any symbol you
    hold, so this is the one data stage worth running on a night when the
    universe is empty and every equity provider is down.
    """
    from akmarkets.services import macro as macro_service

    return macro_service.refresh_macro(
        context.session, context.settings, run_id=context.run_id
    )


def stage_catalysts(context: StageContext) -> dict[str, Any]:
    """Upcoming earnings dates for the symbols actually held.

    Scoped by `universe` rather than fetching the whole calendar: thousands
    of companies report each quarter and none of it matters unless it is
    something you own or watch.
    """
    from akmarkets.services import macro as macro_service

    if not context.symbols:
        return {"items": 0, "detail": "no symbols"}
    return macro_service.refresh_catalysts(
        context.session, context.settings, context.symbols, run_id=context.run_id
    )


def stage_intelligence(context: StageContext) -> dict[str, Any]:
    """Snapshot what the platform believes about each security today.

    Runs after the data stages so it reflects today's prices and filings
    rather than yesterday's — and depends on `prices` explicitly, because a
    snapshot taken from stale bars would silently become the "before" that
    tomorrow's what-changed compares against.
    """
    from akmarkets.services import brain as brain_service

    if not context.symbols:
        return {"items": 0, "detail": "no symbols"}
    return brain_service.refresh_snapshots(
        context.session, context.settings, context.symbols, run_id=context.run_id
    )


def stage_validate(context: StageContext) -> dict[str, Any]:
    """Check every ledger for things that do not add up.

    Runs last and depends on nothing, so a data problem is reported even on
    a night when every provider was down.
    """
    from akmarkets.services import portfolio as portfolio_service

    portfolios = context.session.scalars(
        select(PortfolioRow).where(PortfolioRow.is_archived.is_(False))
    ).all()
    total = 0
    for folio in portfolios:
        findings = portfolio_service.validate_portfolio(context.session, folio.portfolio_id)
        for finding in findings:
            market_data.record_finding(
                context.session,
                code=finding.code,
                severity=finding.severity,
                subject_type="transaction",
                subject_id=finding.subject_id or folio.portfolio_id,
                message=finding.message,
                run_id=context.run_id,
            )
        total += len(findings)
    return {"items": total, "detail": f"{total} ledger finding(s)"}


# Ordered. `requires` is what makes a skipped dependency skip its dependents
# rather than run them against data that never arrived.
STAGES: list[Stage] = [
    Stage("universe", stage_universe, description="which symbols anything needs"),
    Stage("prices", stage_prices, requires=("universe",), description="daily bars"),
    Stage("quotes", stage_quotes, requires=("universe",), description="delayed quotes"),
    Stage("news", stage_news, requires=("universe",), description="news intelligence"),
    Stage(
        "fundamentals",
        stage_fundamentals,
        requires=("universe",),
        description="filed financials from EDGAR",
    ),
    Stage("macro", stage_macro, description="treasury yield curve"),
    Stage(
        "catalysts",
        stage_catalysts,
        requires=("universe",),
        description="upcoming earnings dates",
    ),
    Stage(
        "intelligence",
        stage_intelligence,
        requires=("prices",),
        description="daily intelligence snapshot",
    ),
    Stage("validate", stage_validate, description="ledger plausibility"),
]

STAGE_NAMES = [stage.name for stage in STAGES]


def run_refresh(
    session: Any,
    settings: Any,
    job: str = "refresh",
    only: list[str] | None = None,
    report: Any = None,
    stages: list[Stage] | None = None,
) -> dict[str, Any]:
    """Take the lock, run the stages, record the outcome, release.

    The lock is released in a `finally` so a crash frees it. A job that
    failed should be retried, not blocked out by its own corpse.
    """
    handle = job_service.acquire(session, job)
    try:
        result = run_stages(
            stages or STAGES,
            session=session,
            settings=settings,
            run_id=handle.run_id,
            report=report,
            only=only,
        )
        job_service.release(
            session,
            handle,
            status="done",
            message=(
                f"{len(result.outcomes)} stage(s), {len(result.degraded)} degraded"
                if result.degraded
                else f"{len(result.outcomes)} stage(s) clean"
            ),
            degraded=result.degraded,
            items=result.items,
        )
        return result.as_dict()
    except Exception as exc:
        job_service.release(session, handle, status="error", error=f"{type(exc).__name__}: {exc}")
        raise

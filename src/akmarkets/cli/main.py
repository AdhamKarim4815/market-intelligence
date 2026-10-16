"""`ak-markets` — the command line interface.

A first-class surface, not a debugging aid. Every command here calls the
same application services the web routes call, so a figure printed by the
CLI and the same figure on a page cannot disagree: there is one
implementation and both are looking at it.
"""

from __future__ import annotations

import os
import sys
from typing import Annotated

import typer

from akmarkets import __version__
from akmarkets.cli import db as db_commands
from akmarkets.cli import secrets as secrets_commands
from akmarkets.config import get_settings

app = typer.Typer(
    help="Market Intelligence Platform — portfolio, monitoring, research.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(db_commands.app, name="db")
app.add_typer(secrets_commands.app, name="secrets")


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def config() -> None:
    """Show the resolved configuration, with no secret values.

    Prints where each runtime directory is and which database is targeted,
    because "which database am I actually pointed at" is the first question
    in almost every incident.
    """
    settings = get_settings()
    from akmarkets.db.engine import describe_database_url

    typer.echo(f"version          {__version__}")
    typer.echo(f"env              {settings.env}")
    typer.echo(f"database         {describe_database_url(settings.database_url)}")
    typer.echo(f"backend          {'postgres' if settings.is_postgres else 'sqlite'}")
    typer.echo(f"project root     {settings.project_root}")
    typer.echo(f"listen           {settings.host}:{settings.port}")
    typer.echo(f"cost basis       {settings.portfolio.default_cost_basis_method}")
    typer.echo(f"benchmark        {settings.portfolio.default_benchmark_symbol}")
    typer.echo(f"price providers  {', '.join(settings.prices.provider_priority)}")
    typer.echo(f"ai enabled       {settings.ai.enabled}")


@app.command()
def status() -> None:
    """Database connectivity and schema state. Exits non-zero when unhealthy."""
    from akmarkets.db.engine import Database
    from akmarkets.db.migrations import schema_status

    settings = get_settings()
    database = Database(settings.database_url)
    try:
        info = database.check_connection()
    except Exception as exc:
        typer.secho(f"database unreachable: {type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"database   {info['target']}")
    typer.echo(f"user       {info['user']}")

    state = schema_status(database.engine, settings.project_root)
    typer.echo(f"schema     {state.describe()}")
    database.dispose()
    if not state.ok:
        raise typer.Exit(code=1)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 0,
    reload: Annotated[bool, typer.Option(help="Reload on source changes.")] = False,
) -> None:
    """Run the web application.

    `PORT` from the environment wins over configuration when set, because
    container hosts assign it and expect the process to obey.
    """
    import uvicorn

    settings = get_settings()
    resolved_port = port or int(os.environ.get("PORT") or settings.port)
    resolved_host = host or ("0.0.0.0" if os.environ.get("PORT") else settings.host)
    uvicorn.run(
        "akmarkets.web.app:create_app",
        factory=True,
        host=resolved_host,
        port=resolved_port,
        reload=reload,
    )


def main() -> None:
    sys.exit(app())


if __name__ == "__main__":
    main()


@app.command()
def prices(
    symbols: Annotated[list[str], typer.Argument(help="Tickers to refresh.")],
    force: Annotated[bool, typer.Option("--force", help="Refetch even if current.")] = False,
) -> None:
    """Bring the local price cache up to date and report what happened.

    Prints a line per symbol rather than a single summary, because "the
    refresh succeeded" is not a useful thing to know when one ticker in a
    watchlist has been delisted.
    """
    from akmarkets.db.engine import Database
    from akmarkets.services import market_data

    settings = get_settings()
    database = Database(settings.database_url)
    cache = market_data.build_price_cache(settings)

    with database.sessions.begin() as session:
        outcomes = market_data.refresh_prices(
            session, settings, symbols, cache=cache, force=force
        )

    for symbol, state in sorted(outcomes.items()):
        latest = cache.latest_session(symbol)
        close = cache.close_on(symbol, latest) if latest else None
        detail = f"{latest} close {close}" if latest else "no data"
        typer.echo(f"{symbol:<10} {state:<10} {detail}")
    database.dispose()


@app.command()
def quote(
    symbols: Annotated[list[str], typer.Argument(help="Tickers to quote.")],
    cross_check: Annotated[
        bool, typer.Option("--cross-check", help="Ask every provider and compare.")
    ] = False,
) -> None:
    """Delayed quotes. Every line says whether the price is delayed."""
    from akmarkets.db.engine import Database
    from akmarkets.services import market_data

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions.begin() as session:
        quotes = market_data.latest_quotes(
            session, settings, symbols, cross_check=cross_check
        )
    database.dispose()

    missing = [s.upper() for s in symbols if s.upper() not in quotes]
    for symbol, value in sorted(quotes.items()):
        label = "delayed" if value.is_delayed else "live"
        typer.echo(
            f"{symbol:<10} {value.price:>12}  {value.change or '—':>10}  "
            f"{value.change_percent or '—':>10}  {label}"
        )
    for symbol in missing:
        typer.echo(f"{symbol:<10} {'—':>12}  no quote available")


@app.command()
def health() -> None:
    """Provider health and open data-quality findings."""
    from akmarkets.db.engine import Database
    from akmarkets.services import market_data

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions() as session:
        providers = market_data.provider_health(session)
        findings = market_data.open_findings(session, limit=20)
    database.dispose()

    typer.echo("providers")
    if not providers:
        typer.echo("  (none have reported yet)")
    for row in providers:
        typer.echo(
            f"  {row['category']:<12} {row['provider']:<18} {row['state']:<10} "
            f"{str(row['message'])[:50]}"
        )
    typer.echo(f"\nopen findings: {len(findings)}")
    for finding in findings:
        typer.echo(
            f"  {finding.severity:<6} {finding.code:<28} "
            f"{finding.subject_id:<10} {finding.message[:50]}"
        )


@app.command()
def export(
    out: Annotated[str, typer.Option("--out", help="Directory to publish into.")] = "dist",
    owner: Annotated[
        str, typer.Option("--owner", help="Email whose portfolios to include.")
    ] = "",
) -> None:
    """Build the static snapshot.

    Fails loudly rather than publishing a partial one: the previous snapshot
    staying in place is better than a half-rendered replacement that looks
    live.
    """
    from pathlib import Path

    from akmarkets.export.site import ExportError, export_site

    settings = get_settings()
    try:
        result = export_site(settings, Path(out), owner_email=owner or None)
    except ExportError as exc:
        typer.secho(f"export failed, nothing published: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"wrote {result.pages} page(s) to {result.out_dir}")
    for url in result.urls:
        typer.echo(f"  {url}")


@app.command()
def refresh(
    # Typer needs a concrete default for a repeatable option and cannot
    # take None here, so the empty-list default is deliberate. It is never
    # mutated -- `list(stage)` below copies before use.
    stage: Annotated[
        list[str], typer.Option("--stage", help="Run only these stages. Repeatable.")
    ] = [],  # noqa: B006
    job: Annotated[str, typer.Option(help="Job name to lock on.")] = "refresh",
) -> None:
    """Run the refresh pipeline.

    Exits 0 for a clean run, 2 when stages degraded, 1 when the run itself
    failed. Three codes rather than two because a scheduler needs to tell
    "nothing happened" from "most of it happened" — collapsing them means
    either alerting on every flaky provider or never alerting at all.
    """
    from akmarkets.db.engine import Database
    from akmarkets.jobs.refresh import STAGE_NAMES, run_refresh
    from akmarkets.services.jobs import JobBusy

    unknown = [name for name in stage if name not in STAGE_NAMES]
    if unknown:
        typer.secho(
            f"unknown stage(s) {unknown}; known: {', '.join(STAGE_NAMES)}", fg="red", err=True
        )
        raise typer.Exit(code=1)

    settings = get_settings()
    database = Database(settings.database_url)
    try:
        with database.sessions.begin() as session:
            result = run_refresh(session, settings, job=job, only=list(stage) or None)
    except JobBusy as exc:
        # Not an error: the schedule overlapped with a manual run, which is
        # exactly what the lock is for. Exit 0 so a cron does not alert.
        typer.echo(f"skipped: {exc}")
        raise typer.Exit(code=0) from exc
    finally:
        database.dispose()

    for entry in result["stages"]:
        marker = {"ok": " ", "degraded": "!", "skipped": "-"}.get(str(entry["state"]), "?")
        typer.echo(f"{marker} {entry['name']:<12} {entry['state']:<10} {entry['detail']}")

    if result["degraded"]:
        typer.secho(f"degraded: {', '.join(result['degraded'])}", fg="yellow")
        raise typer.Exit(code=2)
    typer.secho("clean", fg="green")


@app.command()
def jobs(limit: Annotated[int, typer.Option(help="How many runs to show.")] = 10) -> None:
    """Recent job runs and any lock currently held."""
    from akmarkets.db.engine import Database
    from akmarkets.db.models import JobLockRow
    from akmarkets.services import jobs as job_service

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions() as session:
        runs = job_service.recent_runs(session, limit=limit)
        locks = session.query(JobLockRow).all()
    database.dispose()

    if locks:
        for lock in locks:
            typer.echo(f"LOCK {lock.job} held by {lock.run_id[:8]} from {lock.source}")
        typer.echo("")
    if not runs:
        typer.echo("no runs recorded yet")
        return
    for run in runs:
        degraded = f" degraded={','.join(run.degraded_stages)}" if run.degraded else ""
        typer.echo(
            f"{run.started_at:%Y-%m-%d %H:%M}  {run.job:<10} {run.status:<10} "
            f"{run.source:<22} {run.items:>4} items{degraded}"
        )


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="What you want to know.")],
    portfolio: Annotated[str, typer.Option(help="Portfolio id.")] = "",
) -> None:
    """Ask the Market Brain.

    Prints the computed facts either way. When AI is off or the day's
    ceiling is spent it says so and shows the facts anyway — which is the
    product working as designed, not a degraded mode.
    """
    from akmarkets.db.engine import Database
    from akmarkets.services import ask as ask_service
    from akmarkets.services import portfolio as portfolio_service

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions.begin() as session:
        target = portfolio
        if not target:
            user = portfolio_service.get_or_create_user(session, "local@localhost")
            folios = portfolio_service.list_portfolios(session, user.user_id)
            if not folios:
                typer.secho("no portfolios yet", fg="red", err=True)
                raise typer.Exit(code=1)
            target = folios[0].portfolio_id
        answer = ask_service.ask(session, settings, target, question)
    database.dispose()

    typer.echo(f"intent: {answer.intent.value}\n")
    typer.echo(answer.context.render())
    typer.echo("")
    if answer.completion is None:
        typer.secho(f"[no model call: {answer.refusal}]", fg="yellow")
    else:
        typer.echo(answer.text)
        typer.secho(
            f"\n[{answer.completion.model} · ${answer.cost:.5f}]", fg="green"
        )


@app.command("ai-cost")
def ai_cost(days: Annotated[int, typer.Option(help="Window in days.")] = 30) -> None:
    """Which features are consuming money."""
    from akmarkets.db.engine import Database
    from akmarkets.services import ask as ask_service

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions() as session:
        breakdown = ask_service.cost_by_feature(session, days=days)
        today = ask_service.spent_today(session)
    database.dispose()

    typer.echo(f"today        ${today:.4f} of ${settings.ai.daily_cost_ceiling_usd} ceiling")
    if not breakdown:
        typer.echo("no model calls recorded")
        return
    typer.echo(f"\nlast {days} days:")
    for feature, cost, count in breakdown:
        typer.echo(f"  {feature:<24} ${cost:.4f}  {count} call(s)")


@app.command()
def backtest(
    rule: Annotated[str, typer.Argument(help="oversold | breakout | golden-cross")],
    symbols: Annotated[list[str], typer.Argument(help="Tickers to test.")],
    benchmark: Annotated[str, typer.Option(help="Benchmark symbol.")] = "",
) -> None:
    """Replay a rule over cached history and report what happened.

    Research only. Nothing here is wired to an order, and the verdict is
    deliberately blunt about small samples — the most useful output of a
    backtest on four symbols is the sentence saying it proves nothing.
    """
    from akmarkets.db.engine import Database
    from akmarkets.services import lab

    if rule not in lab.RULES:
        typer.secho(
            f"unknown rule {rule!r}; known: {', '.join(lab.RULES)}", fg="red", err=True
        )
        raise typer.Exit(code=1)

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions() as session:
        result = lab.run(session, settings, rule, symbols, benchmark or None)
    database.dispose()

    typer.echo(f"rule       {result.rule}")
    typer.echo(f"universe   {', '.join(result.symbols) or '(nothing cached)'}")
    typer.echo(f"window     {result.first} → {result.last}")
    typer.echo(f"signals    {result.signals}\n")
    for row in result.results:
        typer.echo(f"  {row.describe()}")
    typer.echo("")
    typer.secho(result.verdict(), fg="green" if result.is_meaningful else "yellow")


@app.command()
def brief(
    kind: Annotated[str, typer.Option(help="morning | closing | weekly")] = "closing",
    portfolio: Annotated[str, typer.Option(help="Portfolio id.")] = "",
) -> None:
    """Print a brief. Its hardest job is leaving things out."""
    from akmarkets.db.engine import Database
    from akmarkets.services import briefs
    from akmarkets.services import portfolio as portfolio_service

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions.begin() as session:
        user = portfolio_service.get_or_create_user(session, "local@localhost")
        targets = (
            [portfolio]
            if portfolio
            else [f.portfolio_id for f in portfolio_service.list_portfolios(session, user.user_id)]
        )
        rendered = [
            briefs.build_brief(
                session, settings, target, kind=briefs.BriefKind(kind)
            ).render()
            for target in targets
        ]
    database.dispose()

    if not rendered:
        typer.echo("no portfolios yet")
        return
    typer.echo("\n\n".join(rendered))


@app.command()
def rates() -> None:
    """The Treasury yield curve, as stored."""
    from akmarkets.db.engine import Database
    from akmarkets.services import macro as macro_service

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions() as session:
        snapshots = macro_service.latest_rates(session)
    database.dispose()

    if not snapshots:
        typer.echo("no rate history stored — run `ak-markets refresh --only macro`")
        return
    for snapshot in snapshots:
        typer.echo(macro_service.describe_rate(snapshot))


@app.command()
def catalysts(
    days: int = typer.Option(14, help="how far ahead to look"),
    symbol: list[str] = typer.Option([], help="limit to these symbols"),
) -> None:
    """Scheduled earnings dates for what you hold."""
    from akmarkets.db.engine import Database
    from akmarkets.services import macro as macro_service

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions() as session:
        events = macro_service.upcoming_earnings(
            session, symbols=symbol, within_days=days
        )
    database.dispose()

    if not events:
        typer.echo(f"nothing scheduled in the next {days} days")
        return
    for event in events:
        typer.echo(macro_service.describe_earnings(event))

@app.command()
def watch(
    add: Annotated[
        list[str], typer.Option("--add", help="Symbol to start watching. Repeatable.")
    ] = [],  # noqa: B006 -- Typer needs a concrete default; never mutated
    drop: Annotated[
        list[str], typer.Option("--drop", help="Symbol to stop watching. Repeatable.")
    ] = [],  # noqa: B006
    tier: Annotated[str, typer.Option(help="B for priority, C for normal.")] = "C",
    note: Annotated[str, typer.Option(help="Why it is worth watching.")] = "",
    portfolio: Annotated[str, typer.Option(help="Portfolio id.")] = "",
) -> None:
    """Securities worth researching that are not held.

    The refresh universe is holdings plus this list, so anything added here
    gets prices, news and fundamentals collected for it from the next run --
    which is the whole point. Without it the platform can only ever study what
    is already owned, and a record of how good its calls are is then a record
    of how good its calls about three stocks are.
    """
    from sqlalchemy import delete, select

    from akmarkets.db.engine import Database
    from akmarkets.db.models import WatchlistEntryRow
    from akmarkets.services import portfolio as portfolio_service

    settings = get_settings()
    database = Database(settings.database_url)
    with database.sessions.begin() as session:
        user = portfolio_service.get_or_create_user(session, "local@localhost")
        target = portfolio or next(
            (f.portfolio_id for f in portfolio_service.list_portfolios(session, user.user_id)),
            "",
        )
        if not target:
            # The CLI signs in as local@localhost, and on this deployment the
            # book belongs to a real address -- so the default user owns
            # nothing and the obvious lookup finds nothing. On a single-tenant
            # box "the portfolio" is unambiguous, so fall back to the only one
            # there is rather than asking for an id nobody has memorised. More
            # than one and it refuses, because then the question is real.
            from sqlalchemy import select as _select

            from akmarkets.db.models import PortfolioRow

            everything = session.scalars(
                _select(PortfolioRow).where(PortfolioRow.is_archived.is_(False))
            ).all()
            if len(everything) == 1:
                target = everything[0].portfolio_id
            elif not everything:
                typer.echo("no portfolio yet; create one first")
                raise typer.Exit(1)
            else:
                names = ", ".join(f"{f.name} ({f.portfolio_id})" for f in everything)
                typer.echo(f"more than one portfolio; pass --portfolio: {names}")
                raise typer.Exit(1)

        for symbol in (s.strip().upper() for s in drop if s.strip()):
            session.execute(
                delete(WatchlistEntryRow).where(
                    WatchlistEntryRow.portfolio_id == target,
                    WatchlistEntryRow.symbol == symbol,
                )
            )

        existing = {
            row.symbol
            for row in session.scalars(
                select(WatchlistEntryRow).where(WatchlistEntryRow.portfolio_id == target)
            ).all()
        }
        for symbol in (s.strip().upper() for s in add if s.strip()):
            # Adding something twice is a person being careful, not an error.
            if symbol in existing:
                continue
            session.add(
                WatchlistEntryRow(
                    portfolio_id=target, symbol=symbol, tier=tier, note=note or None
                )
            )
            existing.add(symbol)

        session.flush()
        watched = sorted(
            (row.symbol, row.tier, row.note or "")
            for row in session.scalars(
                select(WatchlistEntryRow).where(WatchlistEntryRow.portfolio_id == target)
            ).all()
        )
    database.dispose()

    if not watched:
        typer.echo("watching nothing")
        return
    for symbol, entry_tier, entry_note in watched:
        typer.echo(f"  {symbol:<8} {entry_tier:<3} {entry_note}")


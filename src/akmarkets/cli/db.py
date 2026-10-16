"""`ak-markets db` — connectivity and schema migration.

`migrate` needs an *owner* credential. The runtime role `akmarkets_app` has
no DDL privileges at all, so running this with the application's own URL
fails at the database rather than half-applying something. That is the
intended behaviour: the credential that can change the schema should not be
the one the web process holds.
"""

from __future__ import annotations

from typing import Annotated

import typer

from akmarkets.config import get_settings
from akmarkets.db.engine import Database, describe_database_url
from akmarkets.db.migrations import (
    MigrationError,
    apply_pending,
    load_migrations,
    schema_status,
)

app = typer.Typer(help="Database connectivity and schema migrations.", no_args_is_help=True)

_URL_OPTION = typer.Option(
    "--url",
    help="Override the database URL. Use an owner credential for `migrate`.",
    envvar="AKM_MIGRATION_DATABASE_URL",
)


@app.command()
def check(url: Annotated[str, _URL_OPTION] = "") -> None:
    """Prove the database is reachable and say who we connected as.

    Run first in CI, before anything that could otherwise be blamed on the
    application. Prints identity, never the password.
    """
    settings = get_settings()
    target = url or settings.database_url
    typer.echo(f"connecting to {describe_database_url(target)}")
    database = Database(target)
    try:
        info = database.check_connection()
    except Exception as exc:
        typer.secho(f"FAILED  {type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"ok      user={info['user']} database={info['database']}")
    typer.echo(f"        {info['version']}")
    database.dispose()


@app.command()
def status(url: Annotated[str, _URL_OPTION] = "") -> None:
    """Compare the migration files on disk against the database ledger."""
    settings = get_settings()
    database = Database(url or settings.database_url)
    state = schema_status(database.engine, settings.project_root)
    on_disk = load_migrations(settings.project_root)

    typer.echo(f"on disk    {len(on_disk)} migration(s)")
    typer.echo(f"applied    {len(state.applied)}")
    typer.echo(f"current    {state.current_version or '(none)'}")
    typer.echo(f"state      {state.describe()}")
    for migration in state.pending:
        typer.echo(f"  pending  {migration.version}_{migration.name}")
    database.dispose()
    if not state.ok:
        raise typer.Exit(code=1)


@app.command()
def migrate(
    url: Annotated[str, _URL_OPTION] = "",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would run without applying it.")
    ] = False,
) -> None:
    """Apply every pending migration, each in its own transaction."""
    settings = get_settings()
    target = url or settings.database_url
    typer.echo(f"target  {describe_database_url(target)}")
    database = Database(target)
    try:
        applied = apply_pending(database.engine, settings.project_root, dry_run=dry_run)
    except MigrationError as exc:
        typer.secho(f"FAILED  {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        database.dispose()

    if not applied:
        typer.echo("nothing to apply")
        return
    verb = "would apply" if dry_run else "applied"
    for migration in applied:
        typer.echo(f"{verb}  {migration.version}_{migration.name}")

"""`ak-markets secrets` — optional provider keys in the system keychain.

Every key is optional. Prices, filings, fundamentals and news all come from
sources needing no account; a key only ever adds coverage. `status`
therefore reports absence as a fact, not an error.
"""

from __future__ import annotations

from typing import Annotated

import typer

from akmarkets.config import get_settings
from akmarkets.secrets import (
    KeyringUnavailableError,
    delete_api_key,
    get_api_key,
    keyring_available,
    set_api_key,
)

app = typer.Typer(help="Store optional provider API keys in the system keychain.")


def _services() -> dict[str, tuple[str, str]]:
    """Known key names mapped to (keychain service, what it unlocks).

    `fred` and `finnhub` were removed on 2026-08-10 along with the
    providers that read them. Offering to store a key that nothing loads is
    worse than not offering it: it reads as "macro data needs this", and
    the user would set it and get exactly the same behaviour as before.
    Rates and the earnings calendar now come from US Treasury and Nasdaq,
    neither of which takes a credential.
    """
    settings = get_settings()
    return {
        "openai": (settings.openai_keyring_name, "Tier 2/3 synthesis (billed per token)"),
        "huggingface": (settings.huggingface_keyring_name, "faster local model downloads"),
    }


@app.command("set")
def set_key(
    name: Annotated[str, typer.Argument(help="openai | huggingface")],
    value: Annotated[str, typer.Option(prompt=True, hide_input=True, help="The API key.")],
) -> None:
    """Store a key. Fails loudly if this machine cannot keep secrets."""
    services = _services()
    if name not in services:
        typer.secho(f"unknown key {name!r}; expected one of {', '.join(services)}", fg="red")
        raise typer.Exit(code=1)
    try:
        set_api_key(services[name][0], value)
    except KeyringUnavailableError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"stored {name}")


@app.command("delete")
def delete_key(name: Annotated[str, typer.Argument(help="Key name to remove.")]) -> None:
    services = _services()
    if name not in services:
        typer.secho(f"unknown key {name!r}", fg="red")
        raise typer.Exit(code=1)
    delete_api_key(services[name][0])
    typer.echo(f"removed {name}")


@app.command()
def status() -> None:
    """Which optional keys are present. Absence is reported, not an error."""
    if not keyring_available():
        typer.echo("keyring   unavailable on this machine (env vars only)")
    for name, (service, purpose) in _services().items():
        present = "present" if get_api_key(service) else "absent"
        typer.echo(f"{name:<12} {present:<8} {purpose}")

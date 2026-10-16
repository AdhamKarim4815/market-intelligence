"""Configuration precedence and optional-key resolution."""

from __future__ import annotations

import pytest

from akmarkets.config import Settings
from akmarkets.secrets import get_api_key, resolve_api_key, set_api_key


def test_environment_overrides_a_nested_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AKM_PORTFOLIO__DEFAULT_BENCHMARK_SYMBOL", "QQQ")
    assert Settings().portfolio.default_benchmark_symbol == "QQQ"


def test_ai_is_off_unless_switched_on() -> None:
    """The platform is required to be fully usable with no LLM at all, so
    every tier defaults off and the cost ceiling is a hard stop."""
    settings = Settings()
    assert settings.ai.enabled is False
    assert settings.ai.tier1_local_enabled is False
    assert settings.ai.daily_cost_ceiling_usd == 1.0


def test_edgar_user_agent_uses_the_contact_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AKM_EDGAR_CONTACT", raising=False)
    assert Settings().edgar_user_agent == "akmarkets/0.1 (you@example.com)"
    monkeypatch.setenv("AKM_EDGAR_CONTACT", "ops@example.org")
    assert Settings().edgar_user_agent == "akmarkets/0.1 (ops@example.org)"


def test_local_default_needs_no_cloud_credentials() -> None:
    settings = Settings()
    assert settings.database_url.startswith("sqlite:///")
    assert settings.is_postgres is False


def test_missing_keyring_is_reported_as_no_key_not_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A headless runner has no keychain. Conflating that with an error once
    killed an entire pricing stage in an earlier version, because the
    price layer asked whether a key existed before building its chain."""
    import keyring.errors

    def explode(*_args: object, **_kwargs: object) -> None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr("keyring.get_password", explode)
    assert get_api_key("akmarkets-finnhub") is None


def test_environment_wins_over_the_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has secrets and no keychain; a laptop has a keychain and usually no
    exported variable. One code path serves both."""
    set_api_key("akmarkets-fred", "from-keychain")
    assert resolve_api_key("akmarkets-fred", "AKM_FRED_KEY") == "from-keychain"
    monkeypatch.setenv("AKM_FRED_KEY", "from-env")
    assert resolve_api_key("akmarkets-fred", "AKM_FRED_KEY") == "from-env"

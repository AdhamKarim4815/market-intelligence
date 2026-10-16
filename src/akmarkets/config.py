"""Runtime configuration.

Settings come from three layers, in increasing precedence: the model
defaults here, `config/default.toml`, then `AKM_`-prefixed environment
variables (`__` separates nesting, e.g. `AKM_PRICES__BENCHMARK_SYMBOL`).
Each section of the TOML file maps onto one sub-model below, and a missing
section leaves that sub-model on its defaults, so the application runs with
no config file at all.

Business decisions belong in this file or the TOML beside it, never in an
environment variable. The environment carries secrets and the one piece of
deployment truth that genuinely varies by machine -- the database URL.
"""

from __future__ import annotations

import secrets
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.toml"

CostBasisMethod = Literal["fifo", "lifo", "average"]


@lru_cache(maxsize=1)
def config_document(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _section(name: str) -> dict[str, Any]:
    section = config_document().get(name, {})
    return section if isinstance(section, dict) else {}


class PortfolioSettings(BaseModel):
    """How positions are accounted for.

    `default_cost_basis_method` is a *view* over the transaction log, not a
    property of it. Lots are always preserved individually, so changing this
    re-derives every figure rather than migrating any stored value.
    """

    default_cost_basis_method: CostBasisMethod = "fifo"
    default_currency: str = "USD"
    default_benchmark_symbol: str = "SPY"
    # Fractional shares are real; brokers report them to eight places.
    quantity_decimal_places: int = Field(default=8, ge=0, le=12)
    money_decimal_places: int = Field(default=4, ge=2, le=10)


class PriceSettings(BaseModel):
    """Daily bars and delayed quotes.

    The provider order is the fallback chain, tried left to right until one
    returns a non-empty frame. All of these are free and need no account.
    """

    provider_priority: list[str] = Field(
        default_factory=lambda: ["nasdaq", "yahoo"]
    )
    history_days: int = Field(default=1825, ge=90, le=10_000)
    exchange_calendar: str = "NYSE"
    max_workers: int = Field(default=8, ge=1, le=32)
    request_timeout_seconds: float = Field(default=30.0, ge=5.0, le=300.0)
    # Two providers disagreeing by more than this on a close is reported as a
    # data-quality finding rather than silently resolved by provider order.
    disagreement_tolerance_pct: float = Field(default=0.5, ge=0.0, le=25.0)


class FundamentalsSettings(BaseModel):
    """SEC EDGAR is the primary, and needs no key -- only a real user agent.

    EDGAR requires a contactable User-Agent and rate-limits to 10 requests a
    second. Both are conditions of use, not performance tuning. The contact
    address is not stored here: set `AKM_EDGAR_CONTACT` (see `Settings`).
    """

    edgar_requests_per_second: float = Field(default=8.0, gt=0, le=10.0)
    request_timeout_seconds: float = Field(default=45.0, ge=5.0, le=300.0)


class NewsSettings(BaseModel):
    lookback_days: int = Field(default=14, ge=1, le=365)
    max_items_per_feed: int = Field(default=50, ge=1, le=500)
    request_timeout_seconds: float = Field(default=30.0, ge=5.0, le=300.0)
    # Cosine similarity above which two articles are treated as the same
    # story. Calibrated in M5 against a labelled sample; until then the
    # pipeline falls back to exact-hash deduplication only.
    near_duplicate_threshold: float = Field(default=0.86, ge=0.5, le=0.999)


class MonitoringSettings(BaseModel):
    """Tier A/B/C/D from the product spec: how closely a security is watched.

    Frequencies are expressed relative to the job schedule rather than as
    poll intervals, because the scheduled jobs are what actually run.
    """

    owned_positions_tier: Literal["A"] = "A"
    priority_watchlist_tier: Literal["B"] = "B"
    normal_watchlist_tier: Literal["C"] = "C"
    universe_tier: Literal["D"] = "D"


class AISettings(BaseModel):
    """Tiered AI routing. Everything here is off until explicitly enabled.

    The platform is required to be fully usable with no LLM at all, so the
    default is disabled and each tier is opted into separately.
    """

    enabled: bool = False
    tier1_local_enabled: bool = False
    tier2_model: str = "gpt-4o-mini"
    tier3_model: str = "gpt-4o"
    # A hard stop, not a target. The router refuses a call that would take
    # the day's recorded spend past this.
    daily_cost_ceiling_usd: float = Field(default=1.0, ge=0.0, le=1000.0)
    max_context_tokens: int = Field(default=8000, ge=500, le=200_000)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AKM_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    env: str = "development"
    # Local development must need no cloud credentials at all: absent an
    # override this is a SQLite file under var/.
    database_url: str = "sqlite:///var/akmarkets.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8770
    session_secret: str = ""
    # Contact address placed in the SEC EDGAR User-Agent, which EDGAR requires
    # and uses to reach you if your client misbehaves. The default is a
    # placeholder: set AKM_EDGAR_CONTACT to an address you control before
    # fetching fundamentals.
    edgar_contact: str = "you@example.com"

    # Keychain service names for the optional provider keys. The values are
    # never stored here -- see `akmarkets.secrets`.
    fred_keyring_name: str = "akmarkets-fred"
    finnhub_keyring_name: str = "akmarkets-finnhub"
    openai_keyring_name: str = "akmarkets-openai"
    huggingface_keyring_name: str = "akmarkets-huggingface"

    portfolio: PortfolioSettings = Field(
        default_factory=lambda: PortfolioSettings(**_section("portfolio"))
    )
    prices: PriceSettings = Field(default_factory=lambda: PriceSettings(**_section("prices")))
    fundamentals: FundamentalsSettings = Field(
        default_factory=lambda: FundamentalsSettings(**_section("fundamentals"))
    )
    news: NewsSettings = Field(default_factory=lambda: NewsSettings(**_section("news")))
    monitoring: MonitoringSettings = Field(
        default_factory=lambda: MonitoringSettings(**_section("monitoring"))
    )
    ai: AISettings = Field(default_factory=lambda: AISettings(**_section("ai")))

    @property
    def effective_session_secret(self) -> str:
        return self.session_secret or secrets.token_urlsafe(32)

    @property
    def edgar_user_agent(self) -> str:
        return f"akmarkets/0.1 ({self.edgar_contact})"

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.strip().startswith(("postgresql", "postgres:"))

    def ensure_runtime_dirs(self) -> None:
        for relative in (
            "var",
            "var/prices",
            "var/fundamentals",
            "var/news",
            "var/reports",
            "var/models",
            "artifacts",
        ):
            (self.project_root / relative).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_dirs()
    return settings

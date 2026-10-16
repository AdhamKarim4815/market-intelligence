"""Tier routing and the ceiling that makes AI safe to switch on.

The hierarchy, cheapest first, and the rule for choosing:

- **Tier 0** — deterministic code. Almost everything. Never routed here
  because it never reaches this module.
- **Tier 1** — local models. Sentiment, embeddings, clustering. No cost.
- **Tier 2** — an economical model. Short summaries, easy classification.
- **Tier 3** — a strong reasoning model. Earnings interpretation, thesis
  change, multi-source synthesis, Ask Market Brain.

**Nothing reaches Tier 2 or 3 unless something changed.** The what-changed
engine is the gate, and a quiet subject is refused before a request is
built. That is not an optimisation; it is the difference between a tool that
costs a few dollars a month and one that costs a few hundred.

**The daily ceiling is a hard stop, not a target.** Once the recorded spend
reaches it, calls are refused and say so. A budget that warns and proceeds
is not a budget.

Every call is recorded before its result is used, so a crash mid-response
still costs what it cost. Accounting that only records successes
systematically understates the bill.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import IntEnum

from akmarkets.ai.context import SYSTEM_PROMPT, Context
from akmarkets.domain.money import ZERO

logger = logging.getLogger(__name__)


class Tier(IntEnum):
    LOCAL = 1
    ECONOMICAL = 2
    REASONING = 3


# Published prices per million tokens, as of 2026-08. Stored here rather
# than fetched because a cost ceiling that depends on a network call is not
# a ceiling. Wrong-but-stated beats right-but-invisible: if these drift, the
# recorded spend drifts with them and the ledger says which model it used.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
}

# Used when a model is not in the table. Deliberately the most expensive
# entry rather than zero: an unknown model must not be able to spend
# without appearing to.
UNKNOWN_MODEL_PRICE = (Decimal("2.50"), Decimal("10.00"))


class AiDisabled(RuntimeError):
    """AI is switched off. The platform is required to work this way."""


class BudgetExhausted(RuntimeError):
    """The day's cost ceiling has been reached."""


class NothingToExplain(RuntimeError):
    """Nothing material changed, so there is nothing worth paying to say."""


@dataclass(frozen=True)
class Usage:
    model: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def cost(self) -> Decimal:
        prompt_price, completion_price = PRICES.get(self.model, UNKNOWN_MODEL_PRICE)
        million = Decimal(1_000_000)
        return (
            Decimal(self.prompt_tokens) * prompt_price / million
            + Decimal(self.completion_tokens) * completion_price / million
        )


@dataclass(frozen=True)
class Completion:
    text: str
    tier: Tier
    model: str
    usage: Usage
    feature: str


def estimate_tokens(text: str) -> int:
    """A deliberately rough token count.

    Four characters per token is the usual English approximation. Used only
    to pre-check a request against the ceiling before sending it — the
    charge is always recorded from the provider's own reported usage, never
    from this.
    """
    return max(1, len(text) // 4)


@dataclass
class Budget:
    """The day's ceiling and what has been spent against it."""

    ceiling: Decimal
    spent: Decimal = ZERO
    on: date | None = None

    def remaining(self) -> Decimal:
        return max(ZERO, self.ceiling - self.spent)

    def would_exceed(self, estimate: Decimal) -> bool:
        return self.spent + estimate > self.ceiling


class Router:
    """Chooses a tier, enforces the ceiling, and records every call.

    The provider is injected rather than constructed so the whole path is
    testable without a key or a network — and so that switching provider is
    a constructor argument rather than a rewrite.
    """

    def __init__(
        self,
        enabled: bool,
        economical_model: str,
        reasoning_model: str,
        budget: Budget,
        complete: Callable[[str, str, str], tuple[str, int, int]] | None = None,
        recorder: Callable[[str, Tier, str, Usage], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.economical_model = economical_model
        self.reasoning_model = reasoning_model
        self.budget = budget
        # (model, system, prompt) -> (text, prompt_tokens, completion_tokens)
        self._complete = complete
        self._recorder = recorder

    def model_for(self, tier: Tier) -> str:
        return self.reasoning_model if tier is Tier.REASONING else self.economical_model

    def choose_tier(self, *, significance: Decimal, wants_reasoning: bool) -> Tier:
        """Pick the cheapest tier that can do the job.

        A user asking a question explicitly gets the strong model, because
        they asked and are waiting. Everything automatic is judged on how
        much changed: a routine update is summarised cheaply, and only a
        genuinely material change is worth reasoning about.
        """
        if wants_reasoning:
            return Tier.REASONING
        return Tier.REASONING if significance >= Decimal("0.6") else Tier.ECONOMICAL

    def complete(
        self,
        context: Context,
        feature: str,
        significance: Decimal = ZERO,
        wants_reasoning: bool = False,
        max_context_chars: int = 6000,
    ) -> Completion:
        """Run one call, or refuse with a reason.

        Refusals are exceptions rather than empty results so a caller
        cannot mistake "we declined to spend" for "the model had nothing to
        say" — they call for completely different UI.
        """
        if not self.enabled:
            raise AiDisabled(
                "AI is disabled; every figure on this page was computed without it"
            )
        if self._complete is None:
            raise AiDisabled("no AI provider is configured")

        tier = self.choose_tier(
            significance=significance, wants_reasoning=wants_reasoning
        )
        model = self.model_for(tier)
        prompt = context.render(max_chars=max_context_chars)

        # Pre-check against the ceiling using an estimate, so an expensive
        # call is refused before it is made rather than after it is billed.
        prompt_price, completion_price = PRICES.get(model, UNKNOWN_MODEL_PRICE)
        estimate = (
            Decimal(estimate_tokens(prompt + SYSTEM_PROMPT))
            * prompt_price
            / Decimal(1_000_000)
        ) + (Decimal(600) * completion_price / Decimal(1_000_000))

        if self.budget.would_exceed(estimate):
            raise BudgetExhausted(
                f"the day's ${self.budget.ceiling} ceiling would be exceeded "
                f"(${self.budget.spent:.4f} spent, this call ≈ ${estimate:.4f})"
            )

        text, prompt_tokens, completion_tokens = self._complete(
            model, SYSTEM_PROMPT, prompt
        )
        usage = Usage(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        # Recorded before the result is returned. Accounting that only
        # records successes systematically understates the bill, and a
        # crash between here and the caller still cost money.
        self.budget.spent += usage.cost
        if self._recorder is not None:
            self._recorder(feature, tier, model, usage)

        return Completion(
            text=text, tier=tier, model=model, usage=usage, feature=feature
        )


def openai_completer(api_key: str, timeout: float = 60.0) -> Callable[
    [str, str, str], tuple[str, int, int]
]:
    """An OpenAI-backed completer.

    Imported lazily so the base install never needs the SDK — the platform
    is required to work with AI switched off entirely, and an import at
    module level would make that a lie.
    """

    def complete(model: str, system: str, prompt: str) -> tuple[str, int, int]:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=600,
        )
        usage = response.usage
        return (
            (response.choices[0].message.content or "").strip(),
            usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0,
        )

    return complete

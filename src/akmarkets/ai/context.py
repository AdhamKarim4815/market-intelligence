"""Building the smallest honest context, and keeping the portfolio private.

Two jobs, both about what the model is *not* told.

**Banding.** A model asked to explain a portfolio does not need to know it
holds $52,431.17 of XMPL. It needs to know the position is large. So
exposures are banded before they leave, and the bands are coarse on
purpose: `HIGH` cannot be reversed into a number, and a percentage to one
decimal place very nearly can when combined with a total the user has
mentioned elsewhere.

**Minimisation.** Everything sent is a fact this system computed, phrased as
a fact. `RSI14 = 47.6` rather than "the stock looks weak". The model's job
is to explain and synthesise; every number it sees was calculated
deterministically upstream, and it is never asked to produce one.

There is no free-text passthrough of the user's own notes, no account
identifiers, and no raw ledger. If a future feature needs one of those, it
should have to add it here where the decision is visible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from akmarkets.domain.money import ZERO


class ExposureBand(StrEnum):
    """How large a position is, coarsely enough not to be reversible."""

    NONE = "NONE"
    SMALL = "SMALL"
    MODERATE = "MODERATE"
    LARGE = "LARGE"
    DOMINANT = "DOMINANT"


# Boundaries as a share of the portfolio. Chosen to match how people talk
# about position size rather than to be evenly spaced, and stated here so
# they can be argued with.
_BANDS: tuple[tuple[Decimal, ExposureBand], ...] = (
    (Decimal("0.25"), ExposureBand.DOMINANT),
    (Decimal("0.10"), ExposureBand.LARGE),
    (Decimal("0.03"), ExposureBand.MODERATE),
    (Decimal("0"), ExposureBand.SMALL),
)


def band(weight: Decimal | None) -> ExposureBand:
    """Band a portfolio weight, or `NONE` if it is not held.

    `None` and zero both mean not held. A model told `SMALL` for something
    the user does not own would reason about a position that does not
    exist.
    """
    if weight is None or weight <= ZERO:
        return ExposureBand.NONE
    for threshold, name in _BANDS:
        if weight >= threshold:
            return name
    return ExposureBand.SMALL


@dataclass(frozen=True)
class Observation:
    """One computed fact, phrased as a fact.

    `label` and `value` are separate so nothing has to be parsed back out
    of a sentence, and so the whole context can be counted and capped.
    """

    label: str
    value: str

    def render(self) -> str:
        return f"{self.label}: {self.value}"


@dataclass
class Context:
    """Everything a model is about to be told, and nothing else.

    Assembled explicitly rather than serialised from a domain object,
    because "what does the model see" must be answerable by reading one
    list rather than by tracing what a dataclass happened to contain.
    """

    subject: str
    question: str = ""
    observations: list[Observation] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, label: str, value: object) -> Context:
        """Add a fact, skipping anything unknown.

        Unknowns are omitted rather than sent as "unknown", because a model
        given a list of unknowns will pattern-match its way to a confident
        answer about the few things it does know.
        """
        if value is None or value == "":
            return self
        self.observations.append(Observation(label=label, value=str(value)))
        return self

    def add_exposure(self, symbol: str, weight: Decimal | None) -> Context:
        """Add a position's size as a band, never as a figure."""
        return self.add(f"{symbol} portfolio exposure", band(weight).value)

    def render(self, max_chars: int = 6000) -> str:
        """The prompt body, truncated at a stated ceiling.

        Truncation drops whole items from the end and says how many, rather
        than cutting mid-fact. A half-sentence of financial data is worse
        than an omission because the model will complete it.
        """
        lines = [f"Subject: {self.subject}"]
        if self.question:
            lines.append(f"Question: {self.question}")
        if self.observations:
            lines.append("\nComputed facts (do not recalculate these):")
            lines.extend(f"- {item.render()}" for item in self.observations)
        if self.events:
            lines.append("\nRecent events:")
            lines.extend(f"- {event}" for event in self.events)
        if self.notes:
            lines.append("\nNotes:")
            lines.extend(f"- {note}" for note in self.notes)

        body = "\n".join(lines)
        if len(body) <= max_chars:
            return body

        kept: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > max_chars - 80:
                break
            kept.append(line)
            size += len(line) + 1
        dropped = len(lines) - len(kept)
        kept.append(f"\n[{dropped} further item(s) omitted to stay within the context budget]")
        return "\n".join(kept)

    @property
    def size(self) -> int:
        return len(self.render())


def contains_private_figures(text: str, forbidden: Sequence[str]) -> list[str]:
    """Find anything in a rendered context that should not have left.

    Used as a guard in tests and before any call: the banding is only worth
    anything if nothing else leaks the number by another route. Checked
    rather than trusted, because the failure is silent and permanent — a
    figure sent to a provider cannot be recalled.
    """
    lowered = text.lower()
    return [item for item in forbidden if item and item.lower() in lowered]


SYSTEM_PROMPT = """You are a financial analyst assistant for a private \
portfolio monitoring tool.

Rules you must follow:
1. Every number you are given was computed deterministically. Use them as \
given. Never recalculate, estimate, or infer a figure that was not \
provided — if a number is absent, say it is not available.
2. Position sizes are given as bands (SMALL, MODERATE, LARGE, DOMINANT). \
Do not attempt to infer amounts.
3. Distinguish what is confirmed from what is probable, and say when there \
is no clear answer. "No clear cause" is an acceptable and often correct \
response.
4. Be brief. Two or three sentences unless asked for more.
5. This is not financial advice and must never be phrased as a \
recommendation to buy or sell."""

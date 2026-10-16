"""The Market Brain: what changed, and why is it moving.

Two engines, both deterministic, and both built around the same refusal:
**they do not fabricate causality.**

`what_changed` compares two structured snapshots and reports the material
differences. It is a product feature and simultaneously the primary AI cost
control — nothing expensive runs unless something here says it should.

`explain_move` is the flagship. When a price moves abnormally it assembles
the candidate explanations, ranks them by how much they actually explain,
and — crucially — labels each one. A confirmed cause is a company-specific
event with a filing behind it. A probable explanation is a correlation with
the sector or the market on the same day. And when neither is present it
says **no clear cause detected**, which is the answer most of the time and
the one every other tool refuses to give.

The distinction is not cosmetic. "XMPL fell 6% because lithium sentiment
weakened" reads as an explanation and is usually a sentence generated to
fill a space. This module would say: market −0.4%, sector unknown, no
material company news, no filing — no clear cause detected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from akmarkets.domain.money import ZERO
from akmarkets.domain.news import Severity

# A move smaller than this is noise for these purposes, whatever caused it.
# Two percent is roughly a typical daily range for a large-cap; the
# threshold is a *filter on what to investigate*, not a claim about
# significance, and the investigation reports its own confidence.
ABNORMAL_MOVE = Decimal("0.02")

# How much of a move the market has to account for before "it was the
# market" is worth saying. Below this the index move is mentioned but not
# offered as an explanation.
MARKET_EXPLAINS = Decimal("0.5")


class Cause(StrEnum):
    """How firmly a candidate explanation is held.

    The three levels exist so the product can decline to explain. Most
    moves on most days have no identifiable cause, and a tool that always
    produces one has simply learned to write sentences.
    """

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class Explanation:
    """One candidate reason, with its evidence and its strength."""

    cause: Cause
    headline: str
    detail: str
    # 0..1. How much of the observed move this candidate accounts for,
    # where that is measurable; otherwise how strong the evidence is.
    weight: Decimal = ZERO
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class MoveAnalysis:
    symbol: str
    on: date
    move: Decimal
    market_move: Decimal | None
    relative_move: Decimal | None
    is_abnormal: bool
    explanations: tuple[Explanation, ...]

    @property
    def verdict(self) -> Cause:
        """The strongest cause found, or `UNCLEAR`.

        Reporting the strongest rather than a blend, because "probably the
        market, possibly the earnings report" is not something anyone can
        act on.
        """
        for level in (Cause.CONFIRMED, Cause.PROBABLE):
            if any(item.cause is level for item in self.explanations):
                return level
        return Cause.UNCLEAR

    @property
    def summary(self) -> str:
        if not self.is_abnormal:
            return "Move within its normal daily range."
        if self.verdict is Cause.UNCLEAR:
            return "No clear cause detected."
        leading = max(self.explanations, key=lambda item: item.weight)
        return leading.headline


def explain_move(
    symbol: str,
    on: date,
    move: Decimal,
    market_move: Decimal | None = None,
    sector_move: Decimal | None = None,
    peer_moves: dict[str, Decimal] | None = None,
    events: Sequence[tuple[str, Severity, bool]] = (),
    relative_volume: Decimal | None = None,
    threshold: Decimal = ABNORMAL_MOVE,
) -> MoveAnalysis:
    """Rank the candidate reasons a price moved, and label each one.

    `events` are `(headline, severity, has_filing)` for the day. A filing
    is what upgrades an explanation from probable to confirmed: it is a
    disclosure with legal consequences for being wrong, timed to the move.

    Nothing here is generated text. Every explanation is a fact about
    another observable series or a document that exists.
    """
    explanations: list[Explanation] = []
    is_abnormal = abs(move) >= threshold

    relative = move - market_move if market_move is not None else None

    # Company-specific first: a filing on the day of a move is the closest
    # thing to a cause this system can observe.
    for headline, severity, has_filing in events:
        if severity in {Severity.CRITICAL, Severity.IMPORTANT} or has_filing:
            explanations.append(
                Explanation(
                    cause=Cause.CONFIRMED if has_filing else Cause.PROBABLE,
                    headline=headline,
                    detail=(
                        "company filing on the day of the move"
                        if has_filing
                        else f"{severity.value} company news on the day of the move"
                    ),
                    weight=Decimal("0.9") if has_filing else Decimal("0.6"),
                    evidence=("filing",) if has_filing else ("news",),
                )
            )

    # Then the market. Only offered as an explanation when it accounts for a
    # real share of the move; otherwise it is context, and calling it a
    # cause would explain a 6% fall by a 0.4% one.
    if market_move is not None and move != ZERO:
        share = market_move / move
        if share >= MARKET_EXPLAINS:
            explanations.append(
                Explanation(
                    cause=Cause.PROBABLE,
                    headline=f"Moved with the market ({market_move:+.2%})",
                    detail=(
                        f"the index accounts for roughly "
                        f"{min(share, Decimal(1)):.0%} of the move"
                    ),
                    weight=min(share, Decimal(1)) * Decimal("0.7"),
                    evidence=("index",),
                )
            )

    if sector_move is not None and move != ZERO:
        share = sector_move / move
        if share >= MARKET_EXPLAINS:
            explanations.append(
                Explanation(
                    cause=Cause.PROBABLE,
                    headline=f"Moved with its sector ({sector_move:+.2%})",
                    detail=f"the sector accounts for roughly {min(share, Decimal(1)):.0%}",
                    weight=min(share, Decimal(1)) * Decimal("0.75"),
                    evidence=("sector",),
                )
            )

    # Peers moving the same way is weaker evidence than a sector index but
    # is often all that exists for a small company with no clean comparator.
    if peer_moves:
        aligned = [
            peer
            for peer, peer_move in peer_moves.items()
            if peer_move != ZERO and (peer_move > 0) == (move > 0)
        ]
        if len(aligned) >= max(2, len(peer_moves) // 2):
            explanations.append(
                Explanation(
                    cause=Cause.PROBABLE,
                    headline=f"{len(aligned)} of {len(peer_moves)} peers moved the same way",
                    detail="consistent with a group move rather than company news",
                    weight=Decimal("0.5"),
                    evidence=tuple(sorted(aligned)[:5]),
                )
            )

    # Volume is never a cause. It is evidence that a move was participated
    # in rather than a thin drift, which changes how much weight to give
    # everything else — so it is recorded and explicitly not ranked.
    if relative_volume is not None and relative_volume >= Decimal(2) and is_abnormal:
        explanations.append(
            Explanation(
                cause=Cause.UNCLEAR,
                headline=f"Volume {relative_volume:.1f}× normal",
                detail=(
                    "unusual participation — not a cause, but the move was "
                    "not a thin drift"
                ),
                weight=ZERO,
                evidence=("volume",),
            )
        )

    ranked = tuple(sorted(explanations, key=lambda item: item.weight, reverse=True))
    return MoveAnalysis(
        symbol=symbol,
        on=on,
        move=move,
        market_move=market_move,
        relative_move=relative,
        is_abnormal=is_abnormal,
        explanations=ranked,
    )


# ---------------------------------------------------------------------------
# what changed
# ---------------------------------------------------------------------------


class ChangeKind(StrEnum):
    PRICE = "price"
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    VALUATION = "valuation"
    NEWS = "news"
    POSITION = "position"
    THESIS = "thesis"
    RISK = "risk"


@dataclass(frozen=True)
class Change:
    """One material difference between two points in time."""

    kind: ChangeKind
    subject: str
    description: str
    before: str | None = None
    after: str | None = None
    # 0..1, used to decide what to show and whether anything expensive is
    # worth running.
    significance: Decimal = ZERO

    @property
    def is_material(self) -> bool:
        return self.significance >= Decimal("0.3")


@dataclass(frozen=True)
class ChangeSet:
    symbol: str
    since: date
    until: date
    changes: tuple[Change, ...] = field(default_factory=tuple)

    @property
    def material(self) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.is_material)

    @property
    def is_quiet(self) -> bool:
        """Nothing material changed.

        The most important property in the module: it is what stops an
        expensive model being asked to explain a day on which nothing
        happened, which is most days.
        """
        return not self.material

    def summary(self) -> str:
        if self.is_quiet:
            return "Nothing material changed."
        return f"{len(self.material)} material change(s) since {self.since}."


def _pct_change(before: Decimal | None, after: Decimal | None) -> Decimal | None:
    if before is None or after is None or before == ZERO:
        return None
    return (after - before) / abs(before)


def what_changed(
    symbol: str,
    since: date,
    until: date,
    price_before: Decimal | None = None,
    price_after: Decimal | None = None,
    rsi_before: Decimal | None = None,
    rsi_after: Decimal | None = None,
    trend_before: str | None = None,
    trend_after: str | None = None,
    revenue_before: Decimal | None = None,
    revenue_after: Decimal | None = None,
    pe_before: Decimal | None = None,
    pe_after: Decimal | None = None,
    new_events: int = 0,
    critical_events: int = 0,
    weight_before: Decimal | None = None,
    weight_after: Decimal | None = None,
    thesis_flags: Sequence[str] = (),
) -> ChangeSet:
    """Compare two snapshots and report only what moved materially.

    Every threshold below is a stated judgement rather than a fitted
    parameter, and each is chosen to be *conservative* — this gate exists
    to avoid spending money on a quiet day, so it should err toward
    reporting quiet.
    """
    changes: list[Change] = []

    price_move = _pct_change(price_before, price_after)
    if price_move is not None and abs(price_move) >= Decimal("0.05"):
        changes.append(
            Change(
                kind=ChangeKind.PRICE,
                subject=symbol,
                description=f"price {price_move:+.1%}",
                before=str(price_before),
                after=str(price_after),
                significance=min(abs(price_move) * Decimal(5), Decimal(1)),
            )
        )

    # A trend flip is worth reporting even when the price barely moved: it
    # is a change in structure, which a percentage does not capture.
    if trend_before and trend_after and trend_before != trend_after:
        changes.append(
            Change(
                kind=ChangeKind.TECHNICAL,
                subject=symbol,
                description=f"trend {trend_before} → {trend_after}",
                before=trend_before,
                after=trend_after,
                significance=Decimal("0.6"),
            )
        )

    # RSI crossing 30 or 70 matters; drifting within the band does not.
    if rsi_before is not None and rsi_after is not None:
        for level, label in ((Decimal(70), "overbought"), (Decimal(30), "oversold")):
            crossed = (rsi_before < level <= rsi_after) or (rsi_after <= level < rsi_before)
            if crossed:
                changes.append(
                    Change(
                        kind=ChangeKind.TECHNICAL,
                        subject=symbol,
                        description=f"RSI crossed {level} ({label})",
                        before=f"{rsi_before:.1f}",
                        after=f"{rsi_after:.1f}",
                        significance=Decimal("0.45"),
                    )
                )

    revenue_move = _pct_change(revenue_before, revenue_after)
    if revenue_move is not None and abs(revenue_move) >= Decimal("0.02"):
        changes.append(
            Change(
                kind=ChangeKind.FUNDAMENTAL,
                subject=symbol,
                description=f"reported revenue {revenue_move:+.1%}",
                before=str(revenue_before),
                after=str(revenue_after),
                # Fundamentals move rarely and mean more when they do.
                significance=min(abs(revenue_move) * Decimal(8), Decimal(1)),
            )
        )

    pe_move = _pct_change(pe_before, pe_after)
    if pe_move is not None and abs(pe_move) >= Decimal("0.15"):
        changes.append(
            Change(
                kind=ChangeKind.VALUATION,
                subject=symbol,
                description=f"P/E {pe_move:+.0%}",
                before=str(pe_before),
                after=str(pe_after),
                significance=min(abs(pe_move) * Decimal(2), Decimal(1)),
            )
        )

    if critical_events:
        changes.append(
            Change(
                kind=ChangeKind.NEWS,
                subject=symbol,
                description=f"{critical_events} critical event(s)",
                significance=Decimal(1),
            )
        )
    elif new_events >= 3:
        changes.append(
            Change(
                kind=ChangeKind.NEWS,
                subject=symbol,
                description=f"{new_events} new event(s)",
                significance=Decimal("0.35"),
            )
        )

    weight_move = _pct_change(weight_before, weight_after)
    if weight_move is not None and abs(weight_move) >= Decimal("0.25"):
        changes.append(
            Change(
                kind=ChangeKind.POSITION,
                subject=symbol,
                description=f"portfolio weight {weight_move:+.0%}",
                before=f"{weight_before:.1%}" if weight_before else None,
                after=f"{weight_after:.1%}" if weight_after else None,
                significance=Decimal("0.5"),
            )
        )

    # A thesis assumption coming into question is the most important thing
    # this system can report, so it is always material.
    for flag in thesis_flags:
        changes.append(
            Change(
                kind=ChangeKind.THESIS,
                subject=symbol,
                description=flag,
                significance=Decimal(1),
            )
        )

    return ChangeSet(
        symbol=symbol,
        since=since,
        until=until,
        changes=tuple(sorted(changes, key=lambda c: c.significance, reverse=True)),
    )


# ---------------------------------------------------------------------------
# position DNA
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Driver:
    """One external factor a position depends on.

    `sensitivity` is a stated belief, not a measured beta — and the type
    says so, because a number that looks measured and is not is worse than
    a label. Measuring it is the risk engine's job.
    """

    name: str
    direction: str  # positive | negative
    note: str = ""
    sensitivity: str = "stated"  # stated | measured


@dataclass(frozen=True)
class PositionDna:
    """What this position is actually a bet on.

    Written down so the system can recognise that a lithium price move is
    news for a lithium producer even when the article never names the
    company — which is the whole reason indirect news is findable at all.
    """

    symbol: str
    drivers: tuple[Driver, ...] = ()
    thesis: str = ""
    invalidation: tuple[str, ...] = ()

    def driver_names(self) -> tuple[str, ...]:
        return tuple(driver.name for driver in self.drivers)

    def matches(self, text: str) -> tuple[str, ...]:
        """Which of this position's drivers a piece of text mentions."""
        lowered = text.lower()
        return tuple(
            driver.name
            for driver in self.drivers
            if driver.name.lower() in lowered
        )


def thesis_warnings(dna: PositionDna, recent_text: Sequence[str]) -> list[str]:
    """Invalidation criteria that recent coverage appears to touch.

    Deliberately phrased as *may no longer hold* rather than *has been
    invalidated*. Matching words in a headline is evidence that a topic
    came up, not that a thesis broke, and the difference is the difference
    between a useful prompt and an alarm nobody trusts.
    """
    warnings: list[str] = []
    haystack = " ".join(recent_text).lower()
    for criterion in dna.invalidation:
        terms = [word for word in criterion.lower().split() if len(word) > 4]
        if terms and sum(term in haystack for term in terms) >= max(1, len(terms) // 2):
            warnings.append(f"one assumption may no longer hold: {criterion}")
    return warnings

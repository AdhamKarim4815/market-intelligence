"""Jinja filters. Presentation only -- these never decide anything.

Each one renders a missing value as an em dash rather than as zero. "We do
not know this" and "this is zero" are different facts about a portfolio, and
a template that conflates them is how a dashboard ends up looking complete
while being wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from akmarkets.domain.money import format_money, format_quantity

MISSING = "—"


def money(value: Any, currency: str = "USD") -> str:
    if value is None:
        return MISSING
    return format_money(value if isinstance(value, Decimal) else Decimal(str(value)), currency)


def signed_money(value: Any, currency: str = "USD") -> str:
    """A gain or loss, always carrying its sign so direction is unmissable."""
    if value is None:
        return MISSING
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    rendered = format_money(amount, currency)
    return f"+{rendered}" if amount > 0 else rendered


def percent(value: Any, places: int = 2) -> str:
    """A proportion (0.0342) rendered as a percentage (+3.42%)."""
    if value is None:
        return MISSING
    amount = (value if isinstance(value, Decimal) else Decimal(str(value))) * 100
    sign = "+" if amount > 0 else ""
    return f"{sign}{amount:,.{places}f}%"


def quantity(value: Any) -> str:
    if value is None:
        return MISSING
    return format_quantity(value if isinstance(value, Decimal) else Decimal(str(value)))


def ago(value: datetime | None) -> str:
    """Elapsed time in the coarsest unit that is still honest.

    Freshness is a first-class fact in this product, so this is used
    everywhere a figure has an age. It never rounds up into a friendlier
    number: eighty-nine minutes is "1h", not "2h".
    """
    if value is None:
        return MISSING
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def rounded(value: Any, places: int = 2) -> str:
    """Render a Decimal to a fixed number of places.

    Jinja's built-in `round` goes through float, which would undo the
    exactness the rest of the stack maintains — and on an indicator that is
    then compared to a threshold, that is not merely untidy.
    """
    if value is None:
        return MISSING
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{amount.quantize(Decimal(1).scaleb(-places)):,}"


def rate(value: Any, unit: str = "percent") -> str:
    """A yield, or a change in a yield.

    Two places always, for percentages: 4.20 must not print as "4.2"
    beside 4.65 in the same column, and a reader scanning a curve is
    comparing shapes, not reading prose.

    Delegates the trailing-zero handling to `services.macro.trim` rather
    than reimplementing it, so the page and `ak-markets rates` cannot drift
    into disagreeing about the same number.
    """
    if value is None:
        return MISSING
    from akmarkets.services.macro import trim

    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    trimmed = trim(amount)
    if unit == "percent":
        return f"{trimmed.quantize(Decimal('0.01'))}"
    return f"{trimmed}"


def signed_rate(value: Any, unit: str = "percent") -> str:
    """A change in a yield, always carrying its sign."""
    if value is None:
        return MISSING
    rendered = rate(value, unit)
    return f"+{rendered}" if not rendered.startswith("-") else rendered


def register(templates: Jinja2Templates) -> None:
    templates.env.filters["money"] = money
    templates.env.filters["signed_money"] = signed_money
    templates.env.filters["percent"] = percent
    templates.env.filters["quantity"] = quantity
    templates.env.filters["ago"] = ago
    templates.env.filters["round"] = rounded
    templates.env.filters["rate"] = rate
    templates.env.filters["signed_rate"] = signed_rate
    templates.env.globals["MISSING"] = MISSING
    templates.env.globals["sparkline"] = sparkline
    templates.env.globals["bar"] = bar
    # Flipped true while the static snapshot is being rendered, so pages can
    # hide controls that would POST to a server the snapshot does not have.
    templates.env.globals.setdefault("static_export", False)
    templates.env.globals.setdefault("exported_at", None)


def sparkline(points: Any, height: int = 56) -> Markup:
    """A portfolio value series as inline SVG.

    Inline rather than a charting library, and it is not a lack of ambition:
    the entire frontend budget of this project is one stylesheet and one
    small script, and a value-over-time line needs a path and a viewBox.
    Adding a dependency here would also add a build step, which §8 of the
    specification rules out until something actually requires it.

    Gaps are drawn as gaps. A point with no value breaks the path rather
    than being interpolated across, because a straight line through a period
    we could not price is a claim we cannot support.
    """
    usable = [(index, point) for index, point in enumerate(points) if point.value is not None]
    if len(usable) < 2:
        return Markup("")

    values = [float(point.value) for _, point in usable]
    low, high = min(values), max(values)
    span = high - low
    width = 100.0
    step = width / max(len(points) - 1, 1)

    def y(value: float) -> float:
        if span == 0:
            return height / 2
        # Inverted: SVG's origin is top-left, and a rising line should rise.
        return height - ((value - low) / span) * (height - 4) - 2

    segments: list[str] = []
    current: list[str] = []
    previous_index: int | None = None
    for index, point in usable:
        if previous_index is not None and index != previous_index + 1:
            segments.append(" ".join(current))
            current = []
        command = "M" if not current else "L"
        current.append(f"{command}{index * step:.2f},{y(float(point.value)):.2f}")
        previous_index = index
    segments.append(" ".join(current))

    rising = values[-1] >= values[0]
    colour = "var(--up)" if rising else "var(--down)"
    paths = "".join(
        f'<path d="{segment}" fill="none" stroke="{colour}" stroke-width="1.5" '
        f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/>'
        for segment in segments
        if segment
    )
    return Markup(
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" '
        f'aria-label="Portfolio value over the selected period">{paths}</svg>'
    )


def bar(weight: Any) -> Markup:
    """A signed proportion as a bar that grows from the centre.

    Centre-anchored so a loss reads as a loss at a glance rather than
    needing the number to be read. Returns the em dash when the weight is
    undefined, which happens when nothing moved at all.
    """
    if weight is None:
        return Markup(f'<span class="muted">{MISSING}</span>')
    proportion = float(weight)
    magnitude = min(abs(proportion), 1.0) * 50
    side = "left: 50%;" if proportion >= 0 else f"left: {50 - magnitude}%;"
    colour = "var(--up)" if proportion >= 0 else "var(--down)"
    return Markup(
        f'<span class="bar" role="img" aria-label="{proportion:.0%} of movement">'
        f'<span class="bar-fill" style="{side} width: {magnitude}%; background: {colour};">'
        f"</span></span>"
    )

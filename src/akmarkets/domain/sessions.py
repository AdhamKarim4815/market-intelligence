"""Exchange sessions: what "current" means for market data.

Freshness for daily bars is not a clock question. On a daily schedule every
cached file is always older than any fixed window you pick, so a time-based
test either refetches everything on every run or never refetches at all. The
right question is whether the cache holds the *last completed session's*
close, which needs a real exchange calendar — including holidays, early
closes, and the fact that Saturday's "yesterday" is Friday.

Kept in `domain` because it is pure calculation over a calendar with no I/O.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as market_calendars

EASTERN = ZoneInfo("America/New_York")

# US equities settle their official close at 16:00 Eastern, but consolidated
# tape corrections trail it. Treating a session as complete a little after
# the bell avoids caching a close that is still being revised.
_SETTLE_AFTER = time(16, 30)


@lru_cache(maxsize=8)
def _calendar(name: str) -> object:
    return market_calendars.get_calendar(name)


@lru_cache(maxsize=512)
def _sessions_between(name: str, start: date, end: date) -> tuple[date, ...]:
    schedule = _calendar(name).schedule(start_date=start, end_date=end)  # type: ignore[attr-defined]
    return tuple(stamp.date() for stamp in schedule.index)


def is_session(on: date, calendar: str = "NYSE") -> bool:
    return on in _sessions_between(calendar, on, on)


def last_completed_session(
    now: datetime | None = None, calendar: str = "NYSE"
) -> date:
    """The most recent session whose close is final.

    Before the settle time on a trading day, that day is still in progress
    and the answer is the previous session. This is the function the price
    cache asks "am I current?" — get it wrong in the optimistic direction
    and the dashboard shows a partial close as the day's result.
    """
    moment = (now or datetime.now(UTC)).astimezone(EASTERN)
    today = moment.date()
    sessions = _sessions_between(calendar, today - timedelta(days=14), today)
    if not sessions:
        return today
    if sessions[-1] == today and moment.time() < _SETTLE_AFTER:
        return sessions[-2] if len(sessions) > 1 else today
    return sessions[-1]


def previous_session(before: date, calendar: str = "NYSE") -> date | None:
    sessions = _sessions_between(calendar, before - timedelta(days=14), before)
    earlier = [session for session in sessions if session < before]
    return earlier[-1] if earlier else None


def sessions_in_range(start: date, end: date, calendar: str = "NYSE") -> list[date]:
    return list(_sessions_between(calendar, start, end))


def is_market_open(now: datetime | None = None, calendar: str = "NYSE") -> bool:
    """Whether a regular session is in progress right now.

    Used to decide whether polling quotes is worth doing at all. Polling a
    closed market burns a rate limit to re-fetch a number that cannot have
    changed.
    """
    moment = (now or datetime.now(UTC)).astimezone(EASTERN)
    if not is_session(moment.date(), calendar):
        return False
    schedule = _calendar(calendar).schedule(  # type: ignore[attr-defined]
        start_date=moment.date(), end_date=moment.date()
    )
    if schedule.empty:
        return False
    row = schedule.iloc[0]
    opened = row["market_open"].to_pydatetime().astimezone(EASTERN)
    closed = row["market_close"].to_pydatetime().astimezone(EASTERN)
    return bool(opened <= moment <= closed)

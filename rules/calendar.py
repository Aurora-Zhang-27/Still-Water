"""US market calendar: NYSE holidays, trading-day counts, which trading day a moment belongs to.

Only regular holidays; one-off closures (such as national days of mourning) are not included.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from rules.config import WEEKDAYS

# after-hours trading ends at 20:00 ET; later moments belong to the next trading day
SESSION_END = time(20, 0)


def _easter(year: int) -> date:
    # Gregorian Easter (Anonymous Gregorian algorithm)
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """A Saturday holiday is observed on Friday, a Sunday holiday on Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset[date]:
    mon, thu = 0, 3
    days = {
        _nth_weekday(year, 1, mon, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, mon, 3),  # Presidents' Day
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, mon),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, mon, 1),  # Labor Day
        _nth_weekday(year, 11, thu, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        days.add(new_year + timedelta(days=1))
    elif new_year.weekday() < 5:
        days.add(new_year)  # NYSE does not observe a Saturday New Year's Day on the Friday before
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    return frozenset(days)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def next_trading_day(day: date) -> date:
    """Returns day itself if it is a trading day."""
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def previous_trading_day(day: date) -> date:
    """The last trading day before day (not including day)."""
    day -= timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def trading_days_between(start: date, end: date) -> int:
    """Number of trading days in [start, end)."""
    count, day = 0, start
    while day < end:
        count += is_trading_day(day)
        day += timedelta(days=1)
    return count


def trade_date_for(moment: datetime, tz: ZoneInfo) -> date:
    """Trading day an order (or fill) at this moment belongs to: on a non-trading day or after the
    after-hours session, it is the next trading day."""
    local = moment.astimezone(tz)
    if is_trading_day(local.date()) and local.time() < SESSION_END:
        return local.date()
    return next_trading_day(local.date() + timedelta(days=1))


def week_start(day: date, first_weekday: str) -> date:
    return day - timedelta(days=(day.weekday() - WEEKDAYS.index(first_weekday)) % 7)

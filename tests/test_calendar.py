from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from rules.calendar import (
    is_trading_day,
    next_trading_day,
    nyse_holidays,
    previous_trading_day,
    trade_date_for,
    trading_days_between,
    week_start,
)

ET = ZoneInfo("America/New_York")


def test_2026_holidays():
    assert nyse_holidays(2026) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    }


def test_new_year_on_saturday_is_not_observed_on_friday():
    assert date(2027, 12, 31) not in nyse_holidays(2027)
    assert date(2028, 1, 1) not in nyse_holidays(2028)


def test_trading_day_helpers():
    assert not is_trading_day(date(2026, 10, 17))  # Saturday
    assert next_trading_day(date(2026, 10, 17)) == date(2026, 10, 19)
    assert previous_trading_day(date(2026, 10, 19)) == date(2026, 10, 16)
    assert previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)  # skips Thanksgiving


def test_trading_days_between_skips_weekend_and_thanksgiving():
    assert trading_days_between(date(2026, 10, 21), date(2026, 10, 28)) == 5
    assert trading_days_between(date(2026, 11, 23), date(2026, 11, 30)) == 4


@pytest.mark.parametrize(
    "local, expected",
    [
        (datetime(2026, 10, 16, 4, 0), date(2026, 10, 16)),  # pre-market counts as that day
        (datetime(2026, 10, 16, 19, 59), date(2026, 10, 16)),  # after hours counts as that day
        (datetime(2026, 10, 16, 20, 30), date(2026, 10, 19)),  # after the after-hours session: the next trading day
        (datetime(2026, 10, 18, 12, 0), date(2026, 10, 19)),  # the weekend counts as Monday
    ],
)
def test_trade_date_for(local, expected):
    moment = local.replace(tzinfo=ET).astimezone(timezone.utc)
    assert trade_date_for(moment, ET) == expected


def test_week_start():
    assert week_start(date(2026, 10, 16), "monday") == date(2026, 10, 12)
    assert week_start(date(2026, 10, 12), "monday") == date(2026, 10, 12)

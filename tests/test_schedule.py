"""Generating the launchd configuration (nothing is installed)."""

from datetime import time
from zoneinfo import ZoneInfo

from audit.schedule import build_plist, to_local
from settings import PROJECT_ROOT

ET = ZoneInfo("America/New_York")
MOUNTAIN = ZoneInfo("America/Denver")


def test_daily_plist_in_mountain_time():
    plist = build_plist("daily", ET, MOUNTAIN, python="/venv/bin/python")
    assert plist["Label"] == "local.still-water.daily"
    assert plist["ProgramArguments"] == ["/venv/bin/python", "-m", "audit", "daily"]
    assert plist["WorkingDirectory"] == str(PROJECT_ROOT)
    times = {(i["Weekday"], i["Hour"], i["Minute"]) for i in plist["StartCalendarInterval"]}
    # 18:30 ET (Mon–Fri) = 16:30 Mountain; 07:00 ET (Tue–Sat) = 05:00 Mountain. launchd counts Sunday as 0
    assert {(d, 16, 30) for d in range(1, 6)} | {(d, 5, 0) for d in range(2, 7)} == times


def test_weekly_plist_is_sunday():
    plist = build_plist("weekly", ET, MOUNTAIN, python="/venv/bin/python")
    assert plist["StartCalendarInterval"] == [{"Weekday": 0, "Hour": 16, "Minute": 0}]
    assert plist["ProgramArguments"][-1] == "weekly"


def test_conversion_can_cross_midnight():
    # Monday 01:00 ET = Sunday 22:00 Pacific
    assert to_local(0, time(1, 0), ET, ZoneInfo("America/Los_Angeles")) == (6, time(22, 0))

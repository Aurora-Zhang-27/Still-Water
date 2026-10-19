"""Scheduled jobs via macOS launchd (LaunchAgent).

    daily job: 18:30 US Eastern on weekdays (after the close), plus a catch-up run at 07:00 the next
               morning (IBKR end-of-day data is sometimes complete only late at night)
    weekly report: Sunday 18:00 US Eastern

Times are written in US Eastern and converted to this Mac's local time at install (launchd only knows
local time). Every job can run again safely; a catch-up run never audits twice. If the Mac was asleep
at a scheduled time, launchd runs the job after it wakes.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from settings import PROJECT_ROOT

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "local.still-water"

# (weekday, US Eastern time). Python weekdays: 0 = Monday … 6 = Sunday. Run times, not trading rules
JOBS = {
    "daily": [(d, time(18, 30)) for d in range(0, 5)] + [(d, time(7, 0)) for d in range(1, 6)],
    "weekly": [(6, time(18, 0))],
}


def label(job: str) -> str:
    return f"{LABEL_PREFIX}.{job}"


def plist_path(job: str) -> Path:
    return AGENTS_DIR / f"{label(job)}.plist"


def to_local(weekday: int, at: time, market_tz: ZoneInfo, local_tz) -> tuple[int, time]:
    """US Eastern (weekday, time) → local (weekday, time), converted on the next such day so daylight
    saving time matches that day."""
    today = date.today()
    day = today + timedelta(days=(weekday - today.weekday()) % 7)
    local = datetime.combine(day, at, tzinfo=market_tz).astimezone(local_tz)
    return local.weekday(), local.time()


def build_plist(job: str, market_tz: ZoneInfo, local_tz=None, python: str | None = None) -> dict:
    local_tz = local_tz or datetime.now().astimezone().tzinfo
    logs = PROJECT_ROOT / "data" / "logs"
    intervals = []
    for weekday, at in JOBS[job]:
        wd, t = to_local(weekday, at, market_tz, local_tz)
        # launchd weekdays: 0 = Sunday, 1 = Monday … 6 = Saturday
        intervals.append({"Weekday": (wd + 1) % 7, "Hour": t.hour, "Minute": t.minute})
    return {
        "Label": label(job),
        "ProgramArguments": [python or sys.executable, "-m", "audit", job],
        "WorkingDirectory": str(PROJECT_ROOT),
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(logs / f"launchd_{job}.log"),
        "StandardErrorPath": str(logs / f"launchd_{job}.log"),
        "RunAtLoad": False,
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(market_tz: ZoneInfo) -> list[Path]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    written = []
    for job in JOBS:
        path = plist_path(job)
        path.write_bytes(plistlib.dumps(build_plist(job, market_tz)))
        _launchctl("bootout", f"{domain}/{label(job)}")  # unload any existing copy first; the error when there is none is ignored
        result = _launchctl("bootstrap", domain, str(path))
        if result.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap {label(job)} failed: {result.stderr.strip()}")
        written.append(path)
    return written


def uninstall() -> list[Path]:
    domain = f"gui/{os.getuid()}"
    removed = []
    for job in JOBS:
        _launchctl("bootout", f"{domain}/{label(job)}")
        path = plist_path(job)
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def status() -> dict[str, str]:
    out = {}
    for job in JOBS:
        result = _launchctl("print", f"gui/{os.getuid()}/{label(job)}")
        if result.returncode != 0:
            out[job] = "not installed"
            continue
        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if " = " in line:
                key, value = line.strip().split(" = ", 1)
                fields.setdefault(key, value)  # first occurrence only: nested sections further down also have a state
        out[job] = f"installed, state {fields.get('state', '?')}, last exit code {fields.get('last exit code', '—')}"
    return out

"""Command line:

    python -m audit daily                 fetch Flex → refresh market data → audit new fills (the scheduled job)
    python -m audit daily --no-fetch      skip Flex; audit with the data already stored
    python -m audit weekly                update valuation EPS, build this week's report into data/reports/
    python -m audit weekly --week 2026-10-12
    python -m audit schedule show         show the scheduled jobs (in local time) without installing
    python -m audit schedule install      install the scheduled jobs (launchd)
    python -m audit schedule status
    python -m audit schedule uninstall
"""

from __future__ import annotations

import argparse
import plistlib
import sys
from datetime import date, datetime, timezone

from audit import schedule
from audit.daily import close_on, run_daily
from db.records import review_due_hunches
from audit.notify import notify
from audit.weekly import build_weekly, render_markdown, save_report
from db.database import init_db
from ingest.market import MarketData
from ingest.valuation import update_eps
from rules.config import load_rules
from settings import load_settings

TITLE = "Still Water"


def _log(text: str, data_dir, name: str) -> None:
    folder = data_dir / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    (folder / f"{name}_{stamp}.log").write_text(text + "\n", encoding="utf-8")


def cmd_daily(args) -> int:
    settings = load_settings()
    book = load_rules(settings.rules_path)
    conn = init_db(settings.db_path)
    summary = run_daily(conn, book, settings, fetch=not args.no_fetch)
    text = summary.text(book)
    print(text)
    _log(text, settings.db_path.parent, "daily")
    if not args.no_notify:
        found = summary.new_violations
        if found:
            trades = len({a.trade_id for a, _ in found})
            notify(TITLE, f"The audit found violations on {trades} fill(s), {len(found)} in total. See the Overview page.")
        elif summary.flex_failed:
            notify(TITLE, "Today's Flex statement could not be fetched; the audit used older data.")
    return 0


def cmd_weekly(args) -> int:
    settings = load_settings()
    book = load_rules(settings.rules_path)
    conn = init_db(settings.db_path)
    changes = []
    if not args.week:  # rebuilding an old report leaves valuation lines and hunches alone
        md = MarketData(conn, tz=book.calendar.tz)
        now = datetime.now(timezone.utc)
        today = now.astimezone(book.calendar.tz).date()
        changes = update_eps(conn, book, md, today)
        review_due_hunches(conn, today, close_on(md), now)
    report = build_weekly(conn, book, week_of=args.week, valuation_changes=changes)
    markdown = render_markdown(report, book)
    path = save_report(markdown, settings.db_path.parent, report.week_start)
    print(markdown)
    print(f"Saved: {path}")
    if not args.no_notify:
        notify(TITLE, f"Weekly report ready: {sum(report.violations.values())} violations, "
                      f"{len(report.earnings)} stock(s) with earnings next week.")
    return 0


def cmd_schedule(args) -> int:
    book = load_rules(load_settings().rules_path)
    if args.action == "show":
        for job in schedule.JOBS:
            print(f"── {schedule.plist_path(job)}")
            print(plistlib.dumps(schedule.build_plist(job, book.calendar.tz)).decode())
    elif args.action == "install":
        for path in schedule.install(book.calendar.tz):
            print(f"Installed: {path}")
        print("Run it once by hand to confirm it can reach the project folder: launchctl kickstart gui/$(id -u)/"
              + schedule.label("daily"))
    elif args.action == "uninstall":
        for path in schedule.uninstall():
            print(f"Removed: {path}")
    else:
        for job, state in schedule.status().items():
            print(f"{job}：{state}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m audit", description="Post-trade audit and scheduled jobs")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("daily", help="daily job")
    p.add_argument("--no-fetch", action="store_true", help="skip the Flex fetch")
    p.add_argument("--no-notify", action="store_true", help="no macOS notification")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("weekly", help="build the weekly report")
    p.add_argument("--week", type=date.fromisoformat, help="any day in the week, YYYY-MM-DD; default this week")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("schedule", help="scheduled jobs")
    p.add_argument("action", choices=["show", "install", "uninstall", "status"])
    p.set_defaults(func=cmd_schedule)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

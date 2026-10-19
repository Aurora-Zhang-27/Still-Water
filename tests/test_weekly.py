"""Weekly report: deduplicated compliance, blocked trades, returns, GTC review, reminders, next week's
earnings (sample history)."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from audit.audit import audit_new_trades
from audit.stats import check_intents
from audit.weekly import build_weekly, render_markdown
from db.database import utc_text
from rules.config import load_rules
from tests.conftest import TEST_RULES, close_on

BOOK = load_rules(TEST_RULES)
LIVE = replace(BOOK, audit=replace(BOOK.audit, start_date=date(2026, 10, 1)))
LATER = replace(BOOK, audit=replace(BOOK.audit, start_date=date(2026, 10, 19)))  # all October fills are pre-launch
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)  # Sunday evening


def et(*args):
    return datetime(*args, tzinfo=ET).astimezone(timezone.utc)


def add_check(conn, symbol, created, qty, price, verdict, override=False):
    reason = ("a reason", utc_text(created + timedelta(minutes=5)), utc_text(created + timedelta(hours=24, minutes=5))) \
        if override else (None, None, None)
    conn.execute(
        """INSERT INTO checks (created_at, symbol, side, quantity, price, verdict, results, state, rules_version,
                               override_reason, overridden_at, cooldown_until)
           VALUES (?, ?, 'BUY', ?, ?, ?, '[]', '{}', 1, ?, ?, ?)""",
        (utc_text(created), symbol, qty, price, verdict, *reason),
    )


@pytest.fixture
def report_db(sample_db):
    conn, _ = sample_db
    add_check(conn, "ZEST", et(2026, 10, 13, 10, 0), 5, 80, "block")  # twice the same day: one intent
    add_check(conn, "ZEST", et(2026, 10, 13, 10, 3), 3, 80, "block")
    add_check(conn, "ORCA", et(2026, 10, 16, 12, 0), 1, 310, "block", override=True)  # reason written, not bought
    add_check(conn, "KITE", et(2026, 10, 16, 15, 30), 3, 121, "pass")  # bought at 15:44
    conn.execute(
        """INSERT INTO orders (perm_id, account_id, symbol, side, quantity, order_type, limit_price, time_in_force,
                               status, placed_at)
           VALUES ('P1', 'U-test', 'PINE', 'BUY', 1, 'LMT', 175, 'GTC', 'Submitted', ?),
                  ('P2', 'U-test', 'MAPL', 'BUY', 1, 'LMT', 140, 'GTC', 'Filled', ?)""",
        (utc_text(NOW - timedelta(days=20)), utc_text(NOW - timedelta(days=3))),
    )
    conn.execute(
        "INSERT INTO watchlist (created_at, symbol, catalyst, invalidation, invalidation_price, invalidation_when)"
        " VALUES (?, 'ORCA', 'January earnings guidance', 'falls below 320', 320, 'below')",
        (utc_text(NOW - timedelta(days=30)),),
    )
    conn.commit()
    audit_new_trades(conn, LIVE)
    return conn


def test_checks_are_deduped_per_symbol_per_day(report_db):
    intents = check_intents(report_db, LIVE, date(2026, 10, 12), date(2026, 10, 19))
    assert [(i.symbol, len(i.check_ids)) for i in intents] == [("ZEST", 2), ("ORCA", 1), ("KITE", 1)]
    kite = intents[-1]
    assert kite.traded and kite.verdict == "pass"


def test_weekly_report(report_db):
    r = build_weekly(report_db, LIVE, NOW, week_of=date(2026, 10, 12))
    assert (r.week_start, r.week_end, r.as_of) == (date(2026, 10, 12), date(2026, 10, 16), date(2026, 10, 16))
    assert (r.buys, r.sells) == (11, 2)
    assert len(r.gtc_fills) == 2  # the DUNE fills on 10/12 and 10/13 come from orders placed on 10/08

    assert r.violations["unchecked_trade"] == 10  # the KITE buy was checked first
    assert r.violations["cash_floor"] >= 1 and r.violations["market_drop_day"] == 2
    # blocks caused by missing data (no PE lines anywhere; DUNE has no bars or earnings dates either)
    # are counted on their own, not as violations
    assert r.missing_data >= 10 and "selection_pe" not in r.violations

    c = r.compliance_week
    assert (c.blocked, c.complied, c.broke, c.overridden, c.passed) == (2, 2, 0, 1, 1)
    assert c.rate == 1

    assert r.outcomes_live["Violation"] and not r.outcomes_live["Compliant"]
    assert r.outcomes_baseline["Violation"]
    [orca] = r.blocked_outcomes  # ZEST has no bars, so it cannot be measured
    assert (orca.symbol, orca.entry, orca.latest) == ("ORCA", 310, pytest.approx(close_on("ORCA", "2026-10-16")))

    [order] = r.orders  # the filled one is left out
    assert order.symbol == "PINE" and order.stale and order.days_open == 20
    assert any("No buys over cap" in v for v in order.would_violate)

    assert any("ORCA invalidation triggered" in m.text for m in r.reminders)

    upcoming = {(e.symbol, e.earnings, e.window_start) for e in r.earnings}
    assert ("KITE", date(2026, 10, 28), date(2026, 10, 21)) in upcoming
    assert ("MAPL", date(2026, 10, 29), date(2026, 10, 22)) in upcoming
    assert not any(e.symbol == "PINE" for e in r.earnings)  # earnings 11/04, window opens 10/28

    text = render_markdown(r, LIVE)
    for heading in ("## Violations", "## Compliance", "## Compliant vs. violating", "## How blocked trades did",
                    "## GTC order review", "## Sell reminders", "## Earnings next week"):
        assert heading in text
    assert "$" not in text  # the page renders Markdown, where $ starts math


def test_week_before_start_is_marked_baseline(sample_db):
    conn, _ = sample_db
    audit_new_trades(conn, LATER)  # the test period starts 10/19: the week of 10/12 is all baseline
    r = build_weekly(conn, LATER, NOW, week_of=date(2026, 10, 12))
    assert r.period == "baseline" and not r.violations
    assert r.outcomes_baseline["Violation"] and not any(r.outcomes_live.values())
    assert "Fills before go-live record no violations" in render_markdown(r, LATER)

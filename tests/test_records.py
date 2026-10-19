"""Watchlist and hunch journal: registering, removing, effect on the rules, reviews when due."""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from audit.daily import close_on
from db.database import parse_utc_text
from db.records import accuracy, active_watch, add_hunch, add_watch, hunches, remove_watch, review_due_hunches
from ingest.market import MarketData
from rules.config import load_rules
from rules.engine import ProposedTrade, check
from tests.conftest import TEST_RULES
from tests.conftest import close_on as fixture_close

BOOK = load_rules(TEST_RULES)


# ── watchlist ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"symbol": "NV DA"}, "A ticker may only contain"),
        ({"catalyst": "  "}, "catalyst"),
        ({"invalidation": ""}, "prove you wrong"),
        ({"invalidation_price": 150.0}, "both a price and a direction"),
        ({"invalidation_price": -1.0, "invalidation_when": "below"}, "greater than 0"),
    ],
)
def test_watch_needs_catalyst_and_invalidation(sample_db, kwargs, message):
    conn, _ = sample_db
    args = {"symbol": "ZEST", "catalyst": "November earnings", "invalidation": "falls below 150"} | kwargs
    with pytest.raises(ValueError, match=message):
        add_watch(conn, args.pop("symbol"), args.pop("catalyst"), args.pop("invalidation"), **args)


def test_planned_turns_green_after_24_hours(sample_db):
    conn, _ = sample_db
    add_watch(conn, "pine", "11/04 earnings, new orders", "falls below 160", invalidation_price=160,
              invalidation_when="below")
    [item] = active_watch(conn)
    assert item.symbol == "PINE" and item.invalidation_when == "below"
    assert abs(item.created_at - datetime.now(timezone.utc)) < timedelta(minutes=1)  # the database writes the time

    trade = ProposedTrade("PINE", "BUY", 1, 180.0)
    soon = check(conn, BOOK, trade, at=item.created_at + timedelta(hours=1))
    assert soon.result("selection_catalyst").status == "pass"
    assert soon.result("selection_planned").status == "block"
    later = check(conn, BOOK, trade, at=item.created_at + timedelta(hours=25))
    assert later.result("selection_planned").status == "pass"
    # registered on (or after) the day it joined the reduce-only list: counts as a new catalyst
    assert "no catalyst registered" not in later.result("reduce_only_list").reason


def test_remove_once_with_reason(sample_db):
    conn, _ = sample_db
    watch_id = add_watch(conn, "ZEST", "earnings", "falls below 150")
    with pytest.raises(ValueError, match="reason"):
        remove_watch(conn, watch_id, " ", datetime.now(timezone.utc))
    remove_watch(conn, watch_id, "catalyst fell through", datetime.now(timezone.utc))
    assert active_watch(conn) == []
    with pytest.raises(ValueError, match="already removed"):
        remove_watch(conn, watch_id, "remove again", datetime.now(timezone.utc))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE watchlist SET created_at = '2026-01-01T00:00:00Z'")


# ── hunch journal ──────────────────────────────────────────


def test_hunch_validation(sample_db):
    conn, _ = sample_db
    with pytest.raises(ValueError, match="current price"):
        add_hunch(conn, "ORCA", "down", "take profit", None, date(2026, 10, 18), 30)
    with pytest.raises(ValueError, match="one sentence"):
        add_hunch(conn, "ORCA", "down", "", 300, date(2026, 10, 18), 30)


def test_due_hunches_are_reviewed_once(sample_db):
    conn, _ = sample_db
    md = MarketData(conn, provider=None)
    lookup = close_on(md)
    # recorded 9/01: ORCA take profit (expect a drop), KITE wait (expect a rise); due 30 days later on 10/01
    add_hunch(conn, "ORCA", "down", "ran up too fast", 320.0, date(2026, 9, 1), 30)
    add_hunch(conn, "KITE", "up", "pullback looks done", 150.0, date(2026, 9, 1), 30)
    add_hunch(conn, "MAPL", "up", "not due yet", 158.0, date(2026, 10, 1), 30)
    add_hunch(conn, "ZZZZ", "up", "no market data", 10.0, date(2026, 9, 1), 30)

    now = datetime(2026, 10, 18, 22, tzinfo=timezone.utc)
    done = {h.symbol: h for h in review_due_hunches(conn, date(2026, 10, 18), lookup, now)}
    assert set(done) == {"ORCA", "KITE"}  # MAPL is not due; ZZZZ has no price and is retried next time
    orca = done["ORCA"]
    assert orca.price_at_review == pytest.approx(lookup("ORCA", date(2026, 10, 1)))
    assert orca.was_right == (orca.price_at_review < 320)
    assert done["KITE"].was_right == (done["KITE"].price_at_review > 150)

    assert review_due_hunches(conn, date(2026, 10, 18), lookup, now) == []  # never reviewed twice
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE hunches SET was_right = 1 - was_right WHERE symbol = 'ORCA'")

    acc = accuracy(conn)
    assert (acc.reviewed, acc.pending) == (2, 2)
    assert acc.right == sum(h.was_right for h in done.values())


def test_due_today_waits_for_the_close(sample_db):
    conn, _ = sample_db
    add_hunch(conn, "ORCA", "down", "a test", 330.0, date(2026, 9, 16), 30)  # due 10/16
    lookup = close_on(MarketData(conn, provider=None))
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    assert review_due_hunches(conn, date(2026, 10, 16), lookup, now) == []  # not before that day's close
    [h] = review_due_hunches(conn, date(2026, 10, 17), lookup, now)
    assert h.price_at_review == pytest.approx(fixture_close("ORCA", "2026-10-16"))
    assert parse_utc_text(conn.execute("SELECT reviewed_at FROM hunches").fetchone()[0]) == now
    assert [x.symbol for x in hunches(conn)] == ["ORCA"]

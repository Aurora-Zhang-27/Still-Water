"""Post-trade audit: re-run the rules, match buy checks, append-only (sample history)."""

import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from audit.audit import audit_new_trades, audit_trade
from db.database import parse_utc_text, utc_text
from rules.config import load_rules
from tests.conftest import SAMPLE, TEST_RULES

BOOK = load_rules(TEST_RULES)
LATER = replace(BOOK, audit=replace(BOOK.audit, start_date=date(2026, 10, 19)))  # all October fills are pre-launch
LIVE = replace(BOOK, audit=replace(BOOK.audit, start_date=date(2026, 10, 1)))
ET = ZoneInfo("America/New_York")
FILLS = len(SAMPLE["trades"])


def trade(conn, symbol, day):
    row = conn.execute(
        "SELECT * FROM trades WHERE symbol = ? AND side = 'BUY' AND executed_at LIKE ? ORDER BY executed_at",
        (symbol, f"{day}%"),
    ).fetchone()
    decision = parse_utc_text(row["order_placed_at"] or row["executed_at"])
    return row["trade_id"], decision, row["quantity"]


def add_check(conn, symbol, created_at, quantity, verdict, override_at=None):
    override = (None, None, None)
    if override_at:
        override = ("a reason", utc_text(override_at), utc_text(override_at + timedelta(hours=24)))
    cur = conn.execute(
        """INSERT INTO checks (created_at, symbol, side, quantity, price, verdict, results, state, rules_version,
                               override_reason, overridden_at, cooldown_until)
           VALUES (?, ?, 'BUY', ?, 100, ?, '[]', '{}', 1, ?, ?, ?)""",
        (utc_text(created_at), symbol, quantity, verdict, *override),
    )
    conn.commit()
    return cur.lastrowid


def violations(conn, trade_id):
    return {r[0] for r in conn.execute("SELECT rule_id FROM violations WHERE trade_id = ?", (trade_id,))}


def test_before_start_date_only_baseline(sample_db):
    conn, _ = sample_db
    results = audit_new_trades(conn, LATER)
    assert len(results) == FILLS
    assert {r.period for r in results} == {"baseline"}
    assert conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trade_audits").fetchone()[0] == FILLS


def test_live_trade_without_a_check(sample_db):
    conn, _ = sample_db
    trade_id, _, _ = trade(conn, "MAPL", "2026-10-16")
    result = audit_trade(conn, LIVE, trade_id)
    assert (result.period, result.verdict, result.check_status) == ("live", "block", "missing")
    found = violations(conn, trade_id)
    assert {"unchecked_trade", "cash_floor", "market_drop_day", "weekly_buy_budget"} <= found
    row = conn.execute("SELECT state FROM trade_audits WHERE trade_id = ?", (trade_id,)).fetchone()
    assert '"holding_quantity": 16.0' in row["state"]  # 16 MAPL held at the time of the fill


def test_passing_check_covers_the_trade(sample_db):
    conn, _ = sample_db
    trade_id, decision, qty = trade(conn, "MAPL", "2026-10-16")
    check_id = add_check(conn, "MAPL", decision - timedelta(hours=1), qty, "pass")
    result = audit_trade(conn, LIVE, trade_id)
    assert (result.check_status, result.check_id) == ("passed", check_id)
    assert "unchecked_trade" not in violations(conn, trade_id)
    assert "cash_floor" in violations(conn, trade_id)  # rule violations are still recorded


def test_check_for_fewer_shares_does_not_count(sample_db):
    conn, _ = sample_db
    trade_id, decision, qty = trade(conn, "MAPL", "2026-10-16")
    add_check(conn, "MAPL", decision - timedelta(hours=1), qty - 1, "pass")
    result = audit_trade(conn, LIVE, trade_id)
    assert result.check_status == "missing"
    message = conn.execute(
        "SELECT message FROM violations WHERE trade_id = ? AND rule_id = 'unchecked_trade'", (trade_id,)).fetchone()[0]
    assert "covered fewer shares" in message


def test_old_check_does_not_count(sample_db):
    conn, _ = sample_db
    trade_id, decision, qty = trade(conn, "MAPL", "2026-10-16")
    add_check(conn, "MAPL", decision - timedelta(days=4), qty, "pass")
    assert audit_trade(conn, LIVE, trade_id).check_status == "missing"


def test_blocked_and_bought_anyway(sample_db):
    conn, _ = sample_db
    trade_id, decision, qty = trade(conn, "MAPL", "2026-10-16")
    check_id = add_check(conn, "MAPL", decision - timedelta(hours=1), qty, "block")
    result = audit_trade(conn, LIVE, trade_id)
    assert (result.check_status, result.check_id) == ("block_ignored", check_id)
    assert "block_ignored" in violations(conn, trade_id)


def test_ordered_before_cooldown_ended(sample_db):
    conn, _ = sample_db
    trade_id, decision, qty = trade(conn, "MAPL", "2026-10-16")
    add_check(conn, "MAPL", decision - timedelta(hours=2), qty, "block", override_at=decision - timedelta(hours=1))
    result = audit_trade(conn, LIVE, trade_id)
    assert result.check_status == "cooldown_not_elapsed"
    message = conn.execute(
        "SELECT message FROM violations WHERE trade_id = ? AND rule_id = 'cooldown_not_elapsed'", (trade_id,)
    ).fetchone()[0]
    assert "ordered 23.0 hours early" in message


def test_override_respected_cooldown(sample_db):
    conn, _ = sample_db
    trade_id, decision, qty = trade(conn, "MAPL", "2026-10-16")
    add_check(conn, "MAPL", decision - timedelta(days=2), qty, "block", override_at=decision - timedelta(days=2))
    result = audit_trade(conn, LIVE, trade_id)
    assert result.check_status == "overridden"
    assert not {"unchecked_trade", "block_ignored", "cooldown_not_elapsed"} & violations(conn, trade_id)


def test_gtc_fill_is_matched_by_when_the_order_was_placed(sample_db):
    conn, _ = sample_db
    # the ORCA fill on 10/09 comes from an order placed on 10/08: the check must come before the order
    trade_id, decision, qty = trade(conn, "ORCA", "2026-10-09")
    assert decision.astimezone(ET).date() == date(2026, 10, 8)
    add_check(conn, "ORCA", decision + timedelta(hours=2), qty, "pass")  # checked after placing: does not count
    assert audit_trade(conn, LIVE, trade_id).check_status == "missing"


def test_gtc_fill_with_check_before_order(sample_db):
    conn, _ = sample_db
    trade_id, decision, qty = trade(conn, "ORCA", "2026-10-09")
    add_check(conn, "ORCA", decision - timedelta(minutes=10), qty, "pass")
    assert audit_trade(conn, LIVE, trade_id).check_status == "passed"


def test_sells_need_no_check(sample_db):
    conn, _ = sample_db
    sell = conn.execute("SELECT trade_id FROM trades WHERE side = 'SELL' AND executed_at LIKE '2026-10-16%'").fetchone()[0]
    result = audit_trade(conn, LIVE, sell)
    assert (result.check_status, result.verdict, result.violations) == ("not_needed", "pass", [])


def test_audit_runs_once_and_cannot_be_changed(sample_db):
    conn, _ = sample_db
    first = audit_new_trades(conn, LIVE)
    assert len(first) == FILLS
    assert audit_new_trades(conn, LIVE) == []
    count = conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE trade_audits SET verdict = 'pass'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM trade_audits")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM violations")
    assert conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0] == count > 0

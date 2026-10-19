"""The logic behind the pages: check records, override reasons, cooldowns, overview data."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.logic import audited_trades, cooldowns, overview, override_check, record_check, recent_checks
from audit.audit import audit_new_trades
from rules.config import load_rules
from rules.engine import ProposedTrade, check
from tests.conftest import SAMPLE, TEST_RULES, position

BOOK = load_rules(TEST_RULES)
NOW = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)  # a Sunday


def blocked_check(conn):
    ev = check(conn, BOOK, ProposedTrade("PINE", "BUY", 1, 180.0), at=NOW)
    assert ev.verdict == "block"
    return record_check(conn, ev), ev


def test_every_check_is_recorded_with_results_and_state(sample_db):
    conn, _ = sample_db
    check_id, ev = blocked_check(conn)
    row = conn.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
    assert (row["symbol"], row["verdict"], row["rules_version"]) == ("PINE", "block", BOOK.version)
    results = {r["rule_id"]: r for r in json.loads(row["results"])}
    assert results["no_averaging_down"]["status"] == "block"
    assert json.loads(row["state"])["holding_quantity"] == 40
    assert row["override_reason"] is None  # no reason = you dropped it; the record stays anyway


def test_override_starts_24h_cooldown_once(sample_db):
    conn, _ = sample_db
    check_id, _ = blocked_check(conn)
    until = override_check(conn, BOOK, check_id, "  catalyst still intact after earnings  ", NOW)
    assert until == NOW + timedelta(hours=24)
    row = conn.execute("SELECT override_reason, cooldown_until FROM checks WHERE id = ?", (check_id,)).fetchone()
    assert row["override_reason"] == "catalyst still intact after earnings"
    with pytest.raises(ValueError, match="cannot be changed"):
        override_check(conn, BOOK, check_id, "a different reason", NOW)


def test_override_needs_a_reason(sample_db):
    conn, _ = sample_db
    check_id, _ = blocked_check(conn)
    with pytest.raises(ValueError, match="reason"):
        override_check(conn, BOOK, check_id, "   ", NOW)


def test_passing_check_has_nothing_to_override(sample_db):
    conn, _ = sample_db
    ev = check(conn, BOOK, ProposedTrade("PINE", "SELL", 1, 180.0), at=NOW)
    check_id = record_check(conn, ev)
    with pytest.raises(ValueError, match="blocked"):
        override_check(conn, BOOK, check_id, "a reason", NOW)


def test_checks_cannot_be_deleted(sample_db):
    conn, _ = sample_db
    blocked_check(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM checks")


def test_cooldown_list(sample_db):
    conn, _ = sample_db
    check_id, _ = blocked_check(conn)
    override_check(conn, BOOK, check_id, "a reason", NOW)
    [cd] = cooldowns(conn, NOW + timedelta(hours=1))
    assert cd.active(NOW + timedelta(hours=1))
    assert cd.remaining(NOW + timedelta(hours=1)) == timedelta(hours=23)
    [cd] = cooldowns(conn, NOW + timedelta(days=2))
    assert not cd.active(NOW + timedelta(days=2))
    assert cooldowns(conn, NOW + timedelta(days=10)) == []
    assert recent_checks(conn)[0]["id"] == check_id


def test_overview_matches_the_sample_account(sample_db):
    conn, _ = sample_db
    ov = overview(conn, BOOK, NOW)
    as_of, cash, nav = SAMPLE["snapshots"][-1]
    assert ov.as_of.isoformat() == as_of == "2026-10-16"
    assert ov.cash_ratio == pytest.approx(cash / nav)
    assert ov.cash_tier.level == "block"

    rows = {r.symbol: r for r in ov.holdings}
    assert rows["ORCA"].weight == pytest.approx(position("ORCA")["market_value"] / nav, abs=1e-4)
    assert {r.symbol for r in ov.holdings if r.over_cap} == {"ORCA", "PINE"}  # QQQ is under its 20% cap
    assert ov.holdings[0].symbol == "ORCA"  # sorted by weight

    texts = [a.text for a in ov.alerts if a.level == "block"]
    assert any(t.startswith(f"Cash {cash / nav:.1%}") for t in texts)
    assert any(t.startswith(f"ORCA is {rows['ORCA'].weight:.1%}, over its 16% cap") for t in texts)
    assert any("PINE is on the reduce-only list" in a.text for a in ov.alerts)

    # a new week (from 10/19): week-start cash = the 10/16 close
    assert ov.week_start.isoformat() == "2026-10-19"
    assert ov.week_budget == pytest.approx(cash / 3, abs=0.01)
    assert ov.week_used == 0


def test_audit_log_has_one_row_per_fill(sample_db):
    conn, _ = sample_db
    audit_new_trades(conn, BOOK)
    rows = audited_trades(conn)
    fills = conn.execute("SELECT COUNT(DISTINCT trade_id) FROM violations").fetchone()[0]
    assert len(rows) == fills < conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
    newest = rows[0]
    assert (newest.symbol, newest.quantity) == ("MAPL", 4)
    assert newest.rules("block")[0] == "unchecked_trade"  # check-related violations come first
    assert "selection_pe" in newest.rules(missing=True) and "selection_pe" not in newest.rules("block")

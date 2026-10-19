"""Schema, migrations, and the append-only guards."""

import sqlite3

import pytest

from db.database import connect, latest_version, list_tables, migrate, schema_version

T0 = "2026-10-18T14:00:00Z"
T1 = "2026-10-19T14:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    migrate(c)
    yield c
    c.close()


def add_trade(conn, trade_id="T1"):
    conn.execute(
        "INSERT INTO trades (trade_id, account_id, symbol, side, quantity, price, executed_at, source)"
        " VALUES (?, 'U0000000', 'KITE', 'BUY', 1, 700, ?, 'flex')",
        (trade_id, T0),
    )
    return trade_id


def add_check(conn, verdict="block"):
    cur = conn.execute(
        "INSERT INTO checks (symbol, side, quantity, price, verdict, results, state, rules_version)"
        " VALUES ('KITE', 'BUY', 1, 700, ?, '{}', '{}', 1)",
        (verdict,),
    )
    return cur.lastrowid


def add_violation(conn, trade_id):
    conn.execute(
        "INSERT INTO violations (detected_by, rule_id, level, symbol, trade_id, message, rules_version)"
        " VALUES ('audit', 'no_averaging_down', 'block', 'KITE', ?, 'added while down 13%', 1)",
        (trade_id,),
    )


def add_watch(conn):
    cur = conn.execute(
        "INSERT INTO watchlist (symbol, catalyst, invalidation) VALUES ('ZEST', 'November guidance raised', 'falls below 150')"
    )
    return cur.lastrowid


# ── schema and migrations ──────────────────────────────────


def test_creates_all_tables(conn):
    expected = {
        "positions", "trades", "orders", "watchlist", "checks",
        "violations", "hunches", "valuation", "account_snapshots",
    }
    assert expected <= set(list_tables(conn))


def test_migrate_twice_is_harmless(conn):
    assert migrate(conn) == latest_version() == schema_version(conn)


# ── violations: no updates, no deletes ─────────────────────


def test_violations_cannot_be_changed_or_deleted(conn):
    add_violation(conn, add_trade(conn))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE violations SET message = 'never mind'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM violations")
    assert conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0] == 1


def test_rerunning_audit_does_not_duplicate(conn):
    trade_id = add_trade(conn)
    add_violation(conn, trade_id)
    with pytest.raises(sqlite3.IntegrityError):
        add_violation(conn, trade_id)
    conn.execute(
        "INSERT OR IGNORE INTO violations (detected_by, rule_id, level, trade_id, message, rules_version)"
        " VALUES ('audit', 'no_averaging_down', 'block', ?, 're-run', 1)",
        (trade_id,),
    )
    assert conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0] == 1


# ── checks: an override reason can be added once ───────────


def override(conn, check_id, reason="pulled back after earnings; catalyst intact"):
    conn.execute(
        "UPDATE checks SET override_reason = ?, overridden_at = ?, cooldown_until = ? WHERE id = ?",
        (reason, T0, T1, check_id),
    )


def test_blocked_check_takes_one_override(conn):
    check_id = add_check(conn, "block")
    override(conn, check_id)
    row = conn.execute("SELECT override_reason, cooldown_until FROM checks").fetchone()
    assert row["cooldown_until"] == T1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        override(conn, check_id, "a different reason")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM checks")


def test_override_needs_a_real_reason(conn):
    check_id = add_check(conn, "block")
    with pytest.raises(sqlite3.IntegrityError):
        override(conn, check_id, "   ")


def test_only_blocked_checks_can_be_overridden(conn):
    check_id = add_check(conn, "pass")
    with pytest.raises(sqlite3.IntegrityError):
        override(conn, check_id)


def test_check_result_cannot_be_rewritten(conn):
    check_id = add_check(conn, "block")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE checks SET verdict = 'pass' WHERE id = ?", (check_id,))


# ── watchlist: timestamps fixed, removed only once ─────────


def test_watchlist_timestamp_is_fixed(conn):
    watch_id = add_watch(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE watchlist SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?", (watch_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE watchlist SET catalyst = 'changed' WHERE id = ?", (watch_id,))


def test_watchlist_remove_once_never_delete(conn):
    watch_id = add_watch(conn)
    conn.execute(
        "UPDATE watchlist SET removed_at = ?, removed_reason = 'catalyst fell through' WHERE id = ?", (T0, watch_id)
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE watchlist SET removed_reason = 'changed again' WHERE id = ?", (watch_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM watchlist")


def test_watchlist_needs_catalyst_and_invalidation(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO watchlist (symbol, catalyst, invalidation) VALUES ('ZEST', '', 'falls below 150')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO watchlist (symbol, catalyst) VALUES ('ZEST', 'earnings')")


# ── other guards ───────────────────────────────────────────


def test_trades_cannot_be_deleted(conn):
    add_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM trades")


def test_hunch_reviewed_once(conn):
    cur = conn.execute(
        "INSERT INTO hunches (symbol, expected_move, note, price_at_entry, review_due_on)"
        " VALUES ('ORCA', 'down', 'time to take profit', 300, '2026-11-17')"
    )
    hunch_id = cur.lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE hunches SET note = 'did not mean it' WHERE id = ?", (hunch_id,))
    conn.execute(
        "UPDATE hunches SET reviewed_at = ?, price_at_review = 280, was_right = 1 WHERE id = ?",
        (T1, hunch_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE hunches SET was_right = 0 WHERE id = ?", (hunch_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM hunches")

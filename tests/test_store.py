"""Writing statements to the database: importing again has no side effects."""

from dataclasses import replace
from pathlib import Path

import pytest

from db.database import init_db
from ingest.flex import parse_statements
from ingest.store import save_statement

SAMPLE = Path(__file__).parent / "fixtures" / "flex_activity_sample.xml"


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def stmt():
    return parse_statements(SAMPLE.read_bytes())[0]


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_first_import(conn, stmt):
    result = save_statement(conn, stmt)
    assert (result.positions, result.snapshots, result.trades_new, result.trades_existing) == (5, 2, 4, 0)
    row = conn.execute("SELECT * FROM trades WHERE trade_id = '9000000001'").fetchone()
    assert row["executed_at"] == "2026-10-16T14:30:12Z"
    assert row["order_placed_at"] == "2026-10-16T14:29:55Z"
    assert row["source"] == "flex"


def test_reimport_is_harmless(conn, stmt):
    save_statement(conn, stmt)
    result = save_statement(conn, stmt)
    assert (result.trades_new, result.trades_existing) == (0, 4)
    assert (count(conn, "trades"), count(conn, "positions"), count(conn, "account_snapshots")) == (4, 5, 2)


def test_position_snapshot_is_replaced_not_merged(conn, stmt):
    save_statement(conn, stmt)
    sold_pine = replace(stmt, positions=[p for p in stmt.positions if p.symbol != "PINE"])
    save_statement(conn, sold_pine)
    symbols = {r[0] for r in conn.execute("SELECT symbol FROM positions WHERE as_of = '2026-10-16'")}
    assert "PINE" not in symbols and len(symbols) == 4


def test_cash_snapshot(conn, stmt):
    save_statement(conn, stmt)
    row = conn.execute("SELECT cash, net_liquidation FROM account_snapshots WHERE as_of = '2026-10-16'").fetchone()
    assert (row["cash"], row["net_liquidation"]) == (3000.0, 62000.0)

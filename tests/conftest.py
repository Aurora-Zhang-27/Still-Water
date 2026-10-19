"""Shared test data: load the fictional account history (fixtures/sample_history.json) into SQLite."""

import json
import sqlite3
from pathlib import Path

import pytest

from db.database import init_db

FIXTURES = Path(__file__).parent / "fixtures"
TEST_RULES = FIXTURES / "rules.yaml"  # the example rules plus ORCA's cap and the reduce-only list
SEED = FIXTURES / "valuation_seed.csv"
SAMPLE = json.loads((FIXTURES / "sample_history.json").read_text(encoding="utf-8"))


def load_sample_history(c: sqlite3.Connection) -> None:
    data = SAMPLE
    c.executemany(
        """INSERT INTO trades (trade_id, account_id, symbol, side, quantity, price, proceeds, commission,
                               executed_at, order_id, order_placed_at, source, raw)
           VALUES (?, 'U-test', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'flex', ?)""",
        [(t["trade_id"], t["symbol"], t["side"], t["quantity"], t["price"], t["proceeds"], t["commission"],
          t["executed_at"], t["order_id"], t["order_placed_at"], json.dumps({"conid": t["conid"]}))
         for t in data["trades"]],
    )
    c.executemany(
        "INSERT INTO account_snapshots (as_of, account_id, cash, net_liquidation, source) VALUES (?, 'U-test', ?, ?, 'flex')",
        data["snapshots"],
    )
    c.executemany(
        """INSERT INTO positions (as_of, account_id, symbol, quantity, avg_cost, mark_price, market_value, source)
           VALUES (?, 'U-test', ?, ?, ?, ?, ?, 'flex')""",
        [(data["positions_as_of"], p["symbol"], p["quantity"], p["avg_cost"], p["mark_price"], p["market_value"])
         for p in data["positions"]],
    )
    c.executemany(
        "INSERT INTO price_history (symbol, date, open, high, low, close) VALUES (?, ?, ?, ?, ?, ?)",
        [(s, *bar) for s, bars in data["bars"].items() for bar in bars],
    )
    c.executemany(
        "INSERT INTO earnings_dates (symbol, earnings_date) VALUES (?, ?)",
        [(s, d) for s, dates in data["earnings"].items() for d in dates],
    )
    c.executemany(
        "INSERT INTO market_snapshots (symbol, kind, fetched_at, data) VALUES (?, 'fundamentals', '2026-10-18T00:00:00Z', ?)",
        [(s, json.dumps({"quote_type": q})) for s, q in data["quote_types"].items()],
    )
    c.commit()


def close_on(symbol: str, day: str) -> float:
    """The fixture's close for a ticker on a day (YYYY-MM-DD)."""
    return next(bar[4] for bar in SAMPLE["bars"][symbol] if bar[0] == day)


def position(symbol: str) -> dict:
    return next(p for p in SAMPLE["positions"] if p["symbol"] == symbol)


@pytest.fixture
def sample_db(tmp_path):
    """A database with the sample history (one per test, writable). Returns (connection, file path)."""
    path = tmp_path / "sample.db"
    c = init_db(path)
    load_sample_history(c)
    yield c, path
    c.close()

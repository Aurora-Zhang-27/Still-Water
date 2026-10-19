"""Write a parsed statement to the database.

Importing the same statement again has no side effects:
  - positions are replaced as a whole per (date, account, source); the latest import wins for that day
  - account snapshots and trades are updated by primary key
The trades table is delete-protected; this only inserts and updates, never deletes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from db.database import utc_text
from ingest.models import Statement


def archive_raw(body: bytes, query: str, data_dir: Path) -> Path:
    """Keep a copy of the raw statement in data/flex/, to re-parse later with --file."""
    folder = data_dir / "flex"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{query}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xml"
    path.write_bytes(body)
    return path


@dataclass(frozen=True)
class SaveResult:
    positions: int
    snapshots: int
    trades_new: int
    trades_existing: int


def save_statement(conn: sqlite3.Connection, stmt: Statement) -> SaveResult:
    with conn:
        position_dates = {p.as_of for p in stmt.positions}
        if "OpenPositions" in stmt.sections and stmt.to_date:
            position_dates.add(stmt.to_date.isoformat())  # also clear the old snapshot when everything was sold that day
        for as_of in position_dates:
            conn.execute(
                "DELETE FROM positions WHERE as_of = ? AND account_id = ? AND source = ?",
                (as_of, stmt.account_id, stmt.source),
            )
        conn.executemany(
            """INSERT INTO positions (as_of, account_id, symbol, asset_class, quantity, avg_cost,
                                      mark_price, market_value, unrealized_pnl, currency, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (p.as_of, p.account_id, p.symbol, p.asset_class, p.quantity, p.avg_cost,
                 p.mark_price, p.market_value, p.unrealized_pnl, p.currency, stmt.source)
                for p in stmt.positions
            ],
        )

        conn.executemany(
            """INSERT INTO account_snapshots (as_of, account_id, currency, cash, net_liquidation, source)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (as_of, account_id, source) DO UPDATE SET
                   currency = excluded.currency,
                   cash = excluded.cash,
                   net_liquidation = excluded.net_liquidation,
                   imported_at = excluded.imported_at""",
            [(s.as_of, s.account_id, s.currency, s.cash, s.net_liquidation, stmt.source)
             for s in stmt.snapshots],
        )

        existing = set()
        ids = [t.trade_id for t in stmt.trades]
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            existing |= {
                row[0] for row in conn.execute(f"SELECT trade_id FROM trades WHERE trade_id IN ({marks})", chunk)
            }
        conn.executemany(
            """INSERT INTO trades (trade_id, account_id, symbol, asset_class, side, quantity, price,
                                   proceeds, commission, currency, executed_at, order_id,
                                   order_placed_at, order_type, time_in_force, source, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (trade_id) DO UPDATE SET
                   account_id = excluded.account_id,
                   symbol = excluded.symbol,
                   asset_class = excluded.asset_class,
                   side = excluded.side,
                   quantity = excluded.quantity,
                   price = excluded.price,
                   proceeds = excluded.proceeds,
                   commission = excluded.commission,
                   currency = excluded.currency,
                   executed_at = excluded.executed_at,
                   order_id = excluded.order_id,
                   order_placed_at = excluded.order_placed_at,
                   order_type = excluded.order_type,
                   time_in_force = COALESCE(excluded.time_in_force, trades.time_in_force),
                   raw = excluded.raw""",
            [
                (t.trade_id, t.account_id, t.symbol, t.asset_class, t.side, t.quantity, t.price,
                 t.proceeds, t.commission, t.currency, utc_text(t.executed_at), t.order_id,
                 utc_text(t.order_placed_at) if t.order_placed_at else None, t.order_type,
                 t.time_in_force, stmt.source, json.dumps(t.raw, ensure_ascii=False, sort_keys=True))
                for t in stmt.trades
            ],
        )

    return SaveResult(
        positions=len(stmt.positions),
        snapshots=len(stmt.snapshots),
        trades_new=len(set(ids) - existing),
        trades_existing=len(set(ids) & existing),
    )

"""IBKR watchlists → the candidate pool on the Watchlist page.

Flex statements do not include watchlists, so they are imported from a JSON file (data/ibkr_watchlists.json
by default), written by hand or by any tool that can read your IBKR watchlists:

    {"synced_at": "2026-10-18T22:00:00Z",
     "lists": {"Chips": [{"symbol": "NVDA", "conid": "4815747"}, ...], "Favorites": [...]}}

Every import replaces the ibkr_watchlist table. FX pairs (EUR.USD) are skipped; tickers with a space such as
BRK B become BRK-B. Importing never writes the watchlist itself: for "Has catalyst" and "Planned" you still
register a catalyst and an invalidation yourself.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from db.database import utc_text
from db.records import SYMBOL_PATTERN

FX_PAIR = re.compile(r"^[A-Z]{3}\.[A-Z]{3}$")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    ibkr_symbol: str
    lists: tuple[str, ...]  # the IBKR lists it appears in, in list order
    supported: bool  # whether the buy check can handle it (non-US tickers such as 000660 cannot)


def normalize(ibkr_symbol: str) -> str | None:
    """IBKR's ticker → the program's ticker; None for FX pairs (skipped)."""
    raw = (ibkr_symbol or "").strip().upper()
    if not raw or FX_PAIR.match(raw):
        return None
    return re.sub(r"\s+", "-", raw)


def save_watchlists(conn: sqlite3.Connection, lists: dict[str, list[dict]], synced_at: datetime) -> dict[str, int]:
    """Replace the IBKR watchlists whole. Returns list name → stocks imported (skipped FX not counted)."""
    rows, counts = [], {}
    for name, items in lists.items():
        seen = set()
        for n, item in enumerate(items):
            symbol = normalize(item.get("symbol", ""))
            if symbol is None or symbol in seen or "@" in str(item.get("conid") or ""):
                continue
            seen.add(symbol)
            rows.append((name, symbol, item["symbol"].strip(), item.get("conid"), n, utc_text(synced_at)))
        counts[name] = len(seen)
    with conn:
        conn.execute("DELETE FROM ibkr_watchlist")
        conn.executemany(
            "INSERT INTO ibkr_watchlist (list_name, symbol, ibkr_symbol, conid, position, synced_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    return counts


def import_file(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    synced = data.get("synced_at")
    when = datetime.fromisoformat(synced.replace("Z", "+00:00")) if synced else datetime.now(timezone.utc)
    return save_watchlists(conn, data["lists"], when)


def list_names(conn: sqlite3.Connection) -> list[str]:
    """IBKR list names, in import order."""
    return [r[0] for r in conn.execute("SELECT list_name FROM ibkr_watchlist GROUP BY list_name ORDER BY MIN(rowid)")]


def candidates(conn: sqlite3.Connection) -> list[Candidate]:
    """The candidate pool: one item per stock, ordered by the first list it appears in and its place there."""
    order = {name: n for n, name in enumerate(list_names(conn))}
    found: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM ibkr_watchlist"):
        item = found.setdefault(r["symbol"], {"ibkr": r["ibkr_symbol"], "lists": [], "key": (99, 0)})
        item["lists"].append(r["list_name"])
        item["key"] = min(item["key"], (order[r["list_name"]], r["position"]))
    return [
        Candidate(symbol, v["ibkr"], tuple(sorted(v["lists"], key=order.get)), bool(SYMBOL_PATTERN.match(symbol)))
        for symbol, v in sorted(found.items(), key=lambda kv: kv[1]["key"])
    ]


def synced_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(synced_at) FROM ibkr_watchlist").fetchone()
    return row[0] if row else None

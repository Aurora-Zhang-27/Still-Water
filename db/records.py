"""The two kinds of records you write by hand: the watchlist and the hunch journal. Independent of the
pages, so tests and scheduled jobs can reuse them.

Both tables are append-only (enforced by triggers):
  - watchlist: the database writes the registration time, which cannot change; an entry can be
    removed once, with a reason
  - hunch journal: the price is recorded automatically; a scheduled job adds the review once it is due
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from db.database import parse_utc_text, utc_text

SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def clean_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip().upper()
    if not SYMBOL_PATTERN.match(symbol):
        raise ValueError("A ticker may only contain letters, digits, dots or dashes")
    return symbol


# ── watchlist ──────────────────────────────────────────────


@dataclass(frozen=True)
class WatchItem:
    id: int
    symbol: str
    created_at: datetime
    catalyst: str
    catalyst_due_on: date | None
    invalidation: str
    invalidation_price: float | None
    invalidation_when: str | None  # below / above

    def hours_on_list(self, now: datetime) -> float:
        return (now - self.created_at).total_seconds() / 3600


def add_watch(
    conn: sqlite3.Connection,
    symbol: str,
    catalyst: str,
    invalidation: str,
    *,
    catalyst_due_on: date | None = None,
    invalidation_price: float | None = None,
    invalidation_when: str | None = None,
) -> int:
    """Register an entry. The database writes the time (no time can be passed in)."""
    symbol = clean_symbol(symbol)
    catalyst, invalidation = (catalyst or "").strip(), (invalidation or "").strip()
    if not catalyst:
        raise ValueError("Write a catalyst (event + time window)")
    if not invalidation:
        raise ValueError("Write what would prove you wrong (a price or an event)")
    if (invalidation_price is None) != (invalidation_when is None):
        raise ValueError("A price-based invalidation needs both a price and a direction")
    if invalidation_price is not None and invalidation_price <= 0:
        raise ValueError("The invalidation price must be greater than 0")
    with conn:
        cur = conn.execute(
            """INSERT INTO watchlist (symbol, catalyst, catalyst_due_on, invalidation, invalidation_price,
                                      invalidation_when) VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, catalyst, catalyst_due_on.isoformat() if catalyst_due_on else None, invalidation,
             invalidation_price, invalidation_when),
        )
    return cur.lastrowid


def remove_watch(conn: sqlite3.Connection, watch_id: int, reason: str, now: datetime) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Removing an entry needs a reason")
    row = conn.execute("SELECT removed_at FROM watchlist WHERE id = ?", (watch_id,)).fetchone()
    if row is None:
        raise ValueError(f"No such entry #{watch_id}")
    if row["removed_at"]:
        raise ValueError("This entry was already removed")
    with conn:
        conn.execute("UPDATE watchlist SET removed_at = ?, removed_reason = ? WHERE id = ?",
                     (utc_text(now), reason, watch_id))


def active_watch(conn: sqlite3.Connection) -> list[WatchItem]:
    rows = conn.execute("SELECT * FROM watchlist WHERE removed_at IS NULL ORDER BY symbol, created_at")
    return [
        WatchItem(r["id"], r["symbol"], parse_utc_text(r["created_at"]), r["catalyst"],
                  date.fromisoformat(r["catalyst_due_on"]) if r["catalyst_due_on"] else None,
                  r["invalidation"], r["invalidation_price"], r["invalidation_when"])
        for r in rows
    ]


def removed_watch(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM watchlist WHERE removed_at IS NOT NULL ORDER BY removed_at DESC LIMIT ?", (limit,)
    ).fetchall()


# ── hunch journal ──────────────────────────────────────────

# The three calls on the page → expected direction. "Take profit" means you expect a drop; "wait" has to say
# whether you are waiting for it to rise or to fall
CALLS = {"Take profit (expect a drop)": "down", "Wait, expect more upside": "up", "Wait, expect more downside": "down"}


@dataclass(frozen=True)
class Hunch:
    id: int
    symbol: str
    created_at: datetime
    expected_move: str  # up / down
    note: str
    price_at_entry: float
    review_due_on: date
    reviewed_at: datetime | None
    price_at_review: float | None
    was_right: bool | None

    @property
    def reviewed(self) -> bool:
        return self.reviewed_at is not None


def _hunch(r: sqlite3.Row) -> Hunch:
    return Hunch(
        r["id"], r["symbol"], parse_utc_text(r["created_at"]), r["expected_move"], r["note"], r["price_at_entry"],
        date.fromisoformat(r["review_due_on"]), parse_utc_text(r["reviewed_at"]) if r["reviewed_at"] else None,
        r["price_at_review"], bool(r["was_right"]) if r["was_right"] is not None else None,
    )


def add_hunch(
    conn: sqlite3.Connection, symbol: str, expected_move: str, note: str, price: float, today: date, review_after_days: int
) -> int:
    """Record a hunch. The caller takes the price from market data (never typed by hand);
    review_after_days comes from rules.yaml."""
    symbol = clean_symbol(symbol)
    note = (note or "").strip()
    if expected_move not in ("up", "down"):
        raise ValueError("Direction must be up or down")
    if not note:
        raise ValueError("Write one sentence")
    if not price or price <= 0:
        raise ValueError(f"No current price for {symbol}; update market data first")
    with conn:
        cur = conn.execute(
            """INSERT INTO hunches (symbol, expected_move, note, price_at_entry, review_due_on)
               VALUES (?, ?, ?, ?, ?)""",
            (symbol, expected_move, note, price, (today + timedelta(days=review_after_days)).isoformat()),
        )
    return cur.lastrowid


def hunches(conn: sqlite3.Connection) -> list[Hunch]:
    return [_hunch(r) for r in conn.execute("SELECT * FROM hunches ORDER BY created_at DESC, id DESC")]


def review_due_hunches(
    conn: sqlite3.Connection, today: date, close_on: Callable[[str, date], float | None], now: datetime
) -> list[Hunch]:
    """Review hunches that are due: compare the close on the due day (or the last trading day before it)
    with the price when the hunch was recorded.

    The due day must be before today, so that day's close is final (the morning catch-up job will not
    pick up the previous day's price too early).

    close_on(ticker, day) returns the close on or before that day; without data for that day the hunch is
    skipped and reviewed next time.
    """
    done = []
    due = conn.execute(
        "SELECT * FROM hunches WHERE reviewed_at IS NULL AND review_due_on < ? ORDER BY review_due_on",
        (today.isoformat(),),
    ).fetchall()
    for r in due:
        h = _hunch(r)
        price = close_on(h.symbol, h.review_due_on)
        if price is None:
            continue
        right = price < h.price_at_entry if h.expected_move == "down" else price > h.price_at_entry
        with conn:
            conn.execute(
                "UPDATE hunches SET reviewed_at = ?, price_at_review = ?, was_right = ? WHERE id = ?",
                (utc_text(now), price, int(right), h.id),
            )
        done.append(_hunch(conn.execute("SELECT * FROM hunches WHERE id = ?", (h.id,)).fetchone()))
    return done


@dataclass(frozen=True)
class Accuracy:
    reviewed: int
    right: int
    pending: int
    by_move: dict[str, tuple[int, int]]  # up / down → (reviewed, right)

    @property
    def rate(self) -> float | None:
        return self.right / self.reviewed if self.reviewed else None


def accuracy(conn: sqlite3.Connection) -> Accuracy:
    items = hunches(conn)
    reviewed = [h for h in items if h.reviewed]
    by_move = {m: (sum(h.expected_move == m for h in reviewed), sum(h.expected_move == m and h.was_right
                                                                      for h in reviewed)) for m in ("up", "down")}
    return Accuracy(len(reviewed), sum(bool(h.was_right) for h in reviewed), len(items) - len(reviewed), by_move)

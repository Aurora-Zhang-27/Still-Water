"""Sell-side reminders (never blocking): staged take-profits, above the rich line, invalidation triggered.
Shared by the weekly report and the Sell reminders page."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from ingest.market import MarketData
from ingest.valuation import latest_rows, zone
from rules.config import RuleBook
from rules.context import account_state


@dataclass(frozen=True)
class Reminder:
    level: str  # remind / warn
    text: str


@dataclass(frozen=True)
class HoldingStatus:
    symbol: str
    quantity: float
    avg_cost: float | None
    price: float
    gain: float | None  # unrealized P&L vs. cost
    reached: float | None  # highest take-profit level reached, e.g. 0.5
    next_level: float | None  # next take-profit level
    zone: str | None  # valuation band; None without valuation lines
    rich: float | None  # rich line

    @property
    def above_rich(self) -> bool:
        return self.rich is not None and self.price > self.rich

    @property
    def next_level_price(self) -> float | None:
        return self.avg_cost * (1 + self.next_level) if self.avg_cost and self.next_level is not None else None


@dataclass(frozen=True)
class InvalidationStatus:
    watch_id: int
    symbol: str
    invalidation: str
    target: float | None  # price-based invalidation; None for event-based ones
    when: str | None  # below / above
    price: float | None
    held: bool

    @property
    def triggered(self) -> bool:
        if self.target is None or self.price is None:
            return False
        return self.price <= self.target if self.when == "below" else self.price >= self.target

    @property
    def distance(self) -> float | None:
        """How far from triggering (positive = not reached yet)."""
        if self.target is None or self.price is None:
            return None
        return self.price / self.target - 1 if self.when == "below" else self.target / self.price - 1


def _close(md: MarketData, symbol: str, as_of: date) -> float | None:
    bars = md.bars(symbol, end=as_of)
    return bars[-1].close if bars else None


def holding_status(
    conn: sqlite3.Connection, book: RuleBook, md: MarketData, now: datetime, as_of: date
) -> list[HoldingStatus]:
    levels = book.take_profit_reminder.gain_levels
    valued = {r["symbol"]: r for r in latest_rows(conn, as_of)}
    out = []
    for symbol, h in sorted(account_state(conn, book, now).holdings.items()):
        price = _close(md, symbol, as_of)
        if price is None:
            continue
        gain = price / h.avg_cost - 1 if h.avg_cost else None
        reached = max((lvl for lvl in levels if gain is not None and gain >= lvl), default=None)
        upcoming = next((lvl for lvl in levels if gain is None or gain < lvl), None)
        row = valued.get(symbol)
        lines = {k: row[f"{k}_line"] for k in ("pressure", "cheap", "normal", "rich")} if row else None
        out.append(HoldingStatus(
            symbol, h.quantity, h.avg_cost, price, gain, reached, upcoming,
            zone(price, lines) if lines else None,
            lines[book.above_rich_line.line] if lines else None,
        ))
    return out


def invalidation_status(
    conn: sqlite3.Connection, md: MarketData, as_of: date, held: set[str]
) -> list[InvalidationStatus]:
    rows = conn.execute("SELECT * FROM watchlist WHERE removed_at IS NULL ORDER BY symbol, created_at")
    return [
        InvalidationStatus(r["id"], r["symbol"], r["invalidation"], r["invalidation_price"], r["invalidation_when"],
                           _close(md, r["symbol"], as_of), r["symbol"] in held)
        for r in rows
    ]


def reminders(
    book: RuleBook, holdings: list[HoldingStatus], invalidations: list[InvalidationStatus]
) -> list[Reminder]:
    out = []
    for h in holdings:
        if h.reached is not None:
            out.append(Reminder(book.take_profit_reminder.level,
                                f"{h.symbol} is up {h.gain:+.0%}, past the +{h.reached:.0%} take-profit level"))
        if h.above_rich:
            out.append(Reminder(book.above_rich_line.level, f"{h.symbol} at {h.price:,.2f} is above the rich line {h.rich:,.2f}"))
    review = "; needs a review" if book.invalidation_triggered.require_review else ""
    for w in invalidations:
        if w.triggered:
            out.append(Reminder(book.invalidation_triggered.level,
                                f"{w.symbol} invalidation triggered: {w.invalidation} (price {w.price:,.2f}){review}"))
    return out

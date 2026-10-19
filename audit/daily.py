"""Daily job (after the close): fetch Flex → refresh market data → audit new fills → write a log, and
notify when there are new violations.

Every step can run again: statements update by primary key, audited fills are never audited twice.
If the Flex fetch fails, the audit still runs on the data already in the database and the
notification says so.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from audit.audit import AuditResult, audit_new_trades
from db.records import Hunch, review_due_hunches
from ingest.flex import FlexClient, FlexError, parse_statements
from ingest.market import MarketData, tracked_symbols
from ingest.store import archive_raw, save_statement
from rules.config import RuleBook
from rules.engine import LABELS
from settings import Settings

# tickers needing data daily: positions and watchlist, plus recently traded or checked ones (for the audit and review)
TRADED_LOOKBACK_DAYS = 400
CHECKED_LOOKBACK_DAYS = 120


@dataclass
class DailySummary:
    started_at: datetime
    flex: list[str] = field(default_factory=list)  # fetch results, or the error when it failed
    flex_failed: bool = False
    market_errors: dict[str, str] = field(default_factory=dict)
    audits: list[AuditResult] = field(default_factory=list)
    hunches: list[Hunch] = field(default_factory=list)  # hunches reviewed this run

    @property
    def new_violations(self):
        return [(a, v) for a in self.audits for v in a.violations]

    def text(self, book: RuleBook) -> str:
        tz = book.calendar.tz
        lines = [f"Daily job {self.started_at.astimezone(tz):%Y-%m-%d %H:%M} ET"]
        lines += [f"Flex: {line}" for line in self.flex] or ["Flex: skipped"]
        if self.market_errors:
            lines.append("Market data failed: " + "; ".join(f"{s} {e}" for s, e in self.market_errors.items()))
        live = [a for a in self.audits if a.period == "live"]
        lines.append(f"Audit: {len(self.audits)} new fills ({len(live)} in the test period, "
                     f"{len(self.audits) - len(live)} pre-launch baseline)")
        for a in live:
            mark = "🔴" if any(v.level == "block" for v in a.violations) else ("🟡" if a.violations else "🟢")
            lines.append(f"  {mark} {a.executed_at.astimezone(tz):%m-%d %H:%M} {a.side} {a.symbol}")
            for v in a.violations:
                lines.append(f"      {LABELS.get(v.rule_id, v.rule_id)}: {v.message}"
                             + (" (missing data)" if v.missing_data else ""))
        if self.hunches:
            right = sum(bool(h.was_right) for h in self.hunches)
            lines.append(f"Hunch reviews: {len(self.hunches)} due, {right} right")
            for h in self.hunches:
                lines.append(f"  {'✓' if h.was_right else '✗'} {h.symbol} expected {h.expected_move}: "
                             f"{h.price_at_entry:,.2f} → {h.price_at_review:,.2f} ({h.note})")
        return "\n".join(lines)


def audit_symbols(conn: sqlite3.Connection, book: RuleBook, now: datetime) -> list[str]:
    # one ticker per contract, from its latest fill (after a ticker change the old one no longer exists on yfinance)
    traded = conn.execute(
        """SELECT symbol FROM (
               SELECT symbol, ROW_NUMBER() OVER (
                   PARTITION BY COALESCE(json_extract(raw, '$.conid'), symbol) ORDER BY executed_at DESC) AS n
               FROM trades WHERE asset_class = 'STK' AND executed_at >= ?)
           WHERE n = 1""",
        ((now - timedelta(days=TRADED_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    checked = conn.execute(
        "SELECT DISTINCT symbol FROM checks WHERE created_at >= ?",
        ((now - timedelta(days=CHECKED_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    valued = conn.execute("SELECT DISTINCT symbol FROM valuation")  # the valuation page needs prices
    hunched = conn.execute("SELECT DISTINCT symbol FROM hunches WHERE reviewed_at IS NULL")  # due hunches need a review
    extra = [book.market_drop_day.index_symbol] + [r[0] for r in traded] + [r[0] for r in checked] + \
        [r[0] for r in valued] + [r[0] for r in hunched]
    return tracked_symbols(conn, extra=extra)


def run_daily(
    conn: sqlite3.Connection,
    book: RuleBook,
    settings: Settings,
    *,
    fetch: bool = True,
    client: FlexClient | None = None,
    md: MarketData | None = None,
    now: datetime | None = None,
) -> DailySummary:
    now = now or datetime.now(timezone.utc)
    summary = DailySummary(started_at=now)

    if fetch:
        queries = [("activity", settings.flex_query_id_activity), ("trade_confirm", settings.flex_query_id_trade_confirm)]
        try:
            if not settings.flex_query_id_activity:
                raise FlexError("IBKR_FLEX_QUERY_ID_ACTIVITY is not set in .env")
            client = client or FlexClient(settings.flex_token or "")
            for name, query_id in queries:
                if not query_id:
                    continue
                body = client.fetch(query_id)
                archive_raw(body, name, settings.db_path.parent)
                for stmt in parse_statements(body, expected_account=settings.account_id):
                    r = save_statement(conn, stmt)
                    summary.flex.append(f"{name} {stmt.from_date} → {stmt.to_date}: {r.trades_new} new fills, "
                                        f"{r.positions} position rows, {r.snapshots} days of snapshots")
        except FlexError as exc:
            summary.flex_failed = True
            summary.flex.append(f"Fetch failed: {exc} (auditing with the data already stored)")

    md = md or MarketData(conn, tz=book.calendar.tz)
    for symbol in audit_symbols(conn, book, now):
        errors = {k: v for k, v in md.refresh(symbol).items() if v.startswith("error")}
        if errors:
            summary.market_errors[symbol] = "；".join(f"{k} {v[7:]}" for k, v in errors.items())

    summary.audits = audit_new_trades(conn, book, md)
    summary.hunches = review_due_hunches(conn, now.astimezone(book.calendar.tz).date(), close_on(md), now)
    return summary


def close_on(md: MarketData):
    """(ticker, day) → close on that day or the last trading day before it."""
    def lookup(symbol: str, day) -> float | None:
        bars = md.bars(symbol, day - timedelta(days=10), day)
        return bars[-1].close if bars else None
    return lookup

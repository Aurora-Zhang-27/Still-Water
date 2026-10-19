"""Account and market state at a given moment: the rule engine's input.

Proposed and past trades are rebuilt with the same logic; only the "moment" differs:
  - proposed trade: now. The index move prefers the latest quote
  - past trade: the fill time, using only fills before that moment and bars before that trading day

How the state is rebuilt:
  - positions and cost: FIFO replay from the first fill up to this moment (rules.portfolio)
  - cash and NAV: the latest end-of-day snapshot before the trading day, plus fills after the
    snapshot and before this moment
  - 52-week low, recent high: daily bars before the trading day (not including it)
  - valuation lines: the latest week on or before the trading day
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from db.database import parse_utc_text, utc_text
from ingest.market import MarketData
from rules.calendar import trade_date_for, trading_days_between, week_start
from rules.config import RuleBook
from rules.portfolio import Fill, Holding, replay

FUND_QUOTE_TYPES = ("ETF", "MUTUALFUND")


@dataclass(frozen=True)
class WatchEntry:
    id: int
    created_at: datetime
    catalyst: str
    catalyst_due_on: date | None
    invalidation: str


@dataclass(frozen=True)
class ValuationLines:
    as_of: date
    metric: str  # pe / pb
    pressure: float
    cheap: float
    normal: float
    rich: float

    def line(self, name: str) -> float:
        return getattr(self, name)

    def as_lines(self) -> dict[str, float]:
        return {"pressure": self.pressure, "cheap": self.cheap, "normal": self.normal, "rich": self.rich}


@dataclass(frozen=True)
class TradeContext:
    symbol: str
    moment: datetime  # moment being evaluated (UTC)
    trade_date: date  # trading day this moment belongs to (US Eastern)
    live: bool

    # account: before this trade
    snapshot_as_of: date | None
    nav: float | None
    cash: float | None
    holding_quantity: float
    holding_avg_cost: float | None

    # pace
    week_start: date
    week_start_cash: float | None
    week_buys: float  # amount already bought earlier this week
    day_buy_orders: frozenset[str]  # buy orders earlier the same day
    index_symbol: str
    index_change: float | None  # index move
    index_change_date: date | None  # trading day the move above belongs to

    # market data for this stock
    fifty_two_week_low: float | None  # lowest price in the 52 weeks before the trading day
    recent_high: float | None  # highest price in the N trading days before the trading day
    recent_high_days: int  # number of trading days actually used
    valuation: ValuationLines | None
    next_earnings: date | None
    trading_days_to_earnings: int | None
    is_fund: bool

    watch_entries: tuple[WatchEntry, ...]  # watchlist entries active at this moment
    notes: tuple[str, ...]  # notes about the data

    def to_dict(self) -> dict:
        def clean(value):
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, frozenset):
                return sorted(value)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            return value

        return clean(asdict(self))


def load_fills(conn: sqlite3.Connection) -> list[Fill]:
    rows = conn.execute(
        """SELECT trade_id, symbol, side, quantity, price, commission, proceeds, executed_at, order_id,
                  json_extract(raw, '$.conid') AS conid
           FROM trades WHERE asset_class = 'STK'"""
    )
    return [
        Fill(r["trade_id"], r["symbol"], r["side"], r["quantity"], r["price"], r["commission"], r["proceeds"],
             parse_utc_text(r["executed_at"]), r["order_id"], r["conid"] or None)
        for r in rows
    ]


@dataclass(frozen=True)
class AccountState:
    """Account-level state (not tied to any stock). Shared by the overview page and the rule engine."""

    moment: datetime
    trade_date: date
    snapshot_as_of: date | None
    nav: float | None
    cash: float | None
    holdings: dict[str, Holding]
    week_start: date
    week_start_cash: float | None
    week_buys: float
    day_buy_orders: frozenset[str]
    notes: tuple[str, ...]

    @property
    def cash_ratio(self) -> float | None:
        return self.cash / self.nav if self.cash is not None and self.nav else None


def account_state(
    conn: sqlite3.Connection, book: RuleBook, moment: datetime, *, trade_id: str | None = None
) -> AccountState:
    tz = book.calendar.tz
    trade_date = trade_date_for(moment, tz)
    notes: list[str] = []

    def fill_date(f: Fill) -> date:
        return trade_date_for(f.executed_at, tz)

    cutoff = (moment, trade_id or "")
    before = [f for f in load_fills(conn) if (f.sort_key < cutoff if trade_id else f.executed_at < moment)]
    holdings, anomalies = replay(before)
    notes += anomalies

    snap = conn.execute(
        """SELECT as_of, cash, net_liquidation FROM account_snapshots
           WHERE as_of < ? ORDER BY as_of DESC LIMIT 1""",
        (trade_date.isoformat(),),
    ).fetchone()
    snapshot_as_of = nav = cash = None
    if snap:
        snapshot_as_of = date.fromisoformat(snap["as_of"])
        nav = snap["net_liquidation"]
        cash = snap["cash"] + sum(f.cash_effect for f in before if fill_date(f) > snapshot_as_of)
    else:
        notes.append(f"No account snapshot before {trade_date}")

    ws = week_start(trade_date, book.calendar.week_starts_on)
    week_snap = conn.execute(
        "SELECT cash FROM account_snapshots WHERE as_of < ? ORDER BY as_of DESC LIMIT 1", (ws.isoformat(),)
    ).fetchone()
    buys = [f for f in before if f.side == "BUY"]

    return AccountState(
        moment=moment,
        trade_date=trade_date,
        snapshot_as_of=snapshot_as_of,
        nav=nav,
        cash=cash,
        holdings=holdings,
        week_start=ws,
        week_start_cash=week_snap["cash"] if week_snap else None,
        week_buys=sum(f.quantity * f.price for f in buys if fill_date(f) >= ws),
        day_buy_orders=frozenset(f.order_id or f.trade_id for f in buys if fill_date(f) == trade_date),
        notes=tuple(notes),
    )


def build_context(
    conn: sqlite3.Connection,
    book: RuleBook,
    symbol: str,
    moment: datetime,
    *,
    live: bool,
    trade_id: str | None = None,
    md: MarketData | None = None,
) -> TradeContext:
    """trade_id: pass it when re-running a past trade; that fill and fills sorted after it at the same
    moment do not count as "before"."""
    symbol = symbol.upper()
    tz = book.calendar.tz
    md = md or MarketData(conn, tz=tz)
    account = account_state(conn, book, moment, trade_id=trade_id)
    trade_date = account.trade_date
    notes = list(account.notes)
    holding = account.holdings.get(symbol)

    index_symbol = book.market_drop_day.index_symbol
    index_change = index_change_date = None
    if live:
        quote = md.latest(index_symbol, "quote") or {}
        if quote.get("last_price") and quote.get("previous_close") and quote.get("session_date"):
            index_change = quote["last_price"] / quote["previous_close"] - 1
            index_change_date = date.fromisoformat(quote["session_date"])
    if index_change is None:
        bars = md.bars(index_symbol, trade_date - timedelta(days=10), trade_date)
        if len(bars) >= 2 and (bars[-1].date == trade_date or live):
            index_change = bars[-1].close / bars[-2].close - 1
            index_change_date = bars[-1].date

    # market data for this stock (before the trading day)
    prior = md.bars(symbol, trade_date - timedelta(weeks=52), trade_date - timedelta(days=1))
    low52 = min((b.low if b.low is not None else b.close) for b in prior) if prior else None
    lookback = book.selection_near_low.recent_high_lookback_days
    recent = prior[-lookback:]
    recent_high = max((b.high if b.high is not None else b.close) for b in recent) if recent else None
    if prior and len(recent) < lookback:
        notes.append(f"{symbol} has only {len(recent)} trading days of bars; the recent high uses those")

    val = conn.execute(
        "SELECT * FROM valuation WHERE symbol = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (symbol, trade_date.isoformat()),
    ).fetchone()
    valuation = ValuationLines(
        date.fromisoformat(val["as_of"]), val["metric"], val["pressure_line"], val["cheap_line"],
        val["normal_line"], val["rich_line"],
    ) if val else None

    next_earnings = md.next_earnings(symbol, trade_date)
    fundamentals = md.latest(symbol, "fundamentals") or {}
    is_fund = symbol == index_symbol or fundamentals.get("quote_type") in FUND_QUOTE_TYPES

    watch_rows = conn.execute(
        """SELECT id, created_at, catalyst, catalyst_due_on, invalidation FROM watchlist
           WHERE symbol = ? AND created_at <= ? AND (removed_at IS NULL OR removed_at > ?)
           ORDER BY created_at""",
        (symbol, utc_text(moment), utc_text(moment)),
    )
    watch_entries = tuple(
        WatchEntry(r["id"], parse_utc_text(r["created_at"]), r["catalyst"],
                   date.fromisoformat(r["catalyst_due_on"]) if r["catalyst_due_on"] else None, r["invalidation"])
        for r in watch_rows
    )

    return TradeContext(
        symbol=symbol,
        moment=moment,
        trade_date=trade_date,
        live=live,
        snapshot_as_of=account.snapshot_as_of,
        nav=account.nav,
        cash=account.cash,
        holding_quantity=holding.quantity if holding else 0.0,
        holding_avg_cost=holding.avg_cost if holding else None,
        week_start=account.week_start,
        week_start_cash=account.week_start_cash,
        week_buys=account.week_buys,
        day_buy_orders=account.day_buy_orders,
        index_symbol=index_symbol,
        index_change=index_change,
        index_change_date=index_change_date,
        fifty_two_week_low=low52,
        recent_high=recent_high,
        recent_high_days=len(recent),
        valuation=valuation,
        next_earnings=next_earnings,
        trading_days_to_earnings=trading_days_between(trade_date, next_earnings) if next_earnings else None,
        is_fund=is_fund,
        watch_entries=watch_entries,
        notes=tuple(notes),
    )

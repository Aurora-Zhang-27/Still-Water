"""yfinance prices, fundamentals and earnings dates, with a SQLite cache.

Each kind of data expires separately (CACHE_TTL). Until then yfinance is not asked again, which saves
time and avoids Yahoo rate limits. All data keeps its history so the audit can recompute "at fill time".
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

from db.database import parse_utc_text, utc_text
from ingest.models import Bar

# cache lifetimes. These are data-freshness settings, not trading rules
CACHE_TTL = {
    "history": timedelta(hours=6),
    "quote": timedelta(minutes=15),
    "fundamentals": timedelta(days=1),
    "earnings": timedelta(days=1),
}
KINDS = tuple(CACHE_TTL)
HISTORY_BOOTSTRAP_DAYS = 400  # calendar days fetched the first time: 52 weeks plus a margin
HISTORY_OVERLAP_DAYS = 7  # incremental fetches redo the last few days, covering an unfinished intraday bar
MARKET_TIMEZONE = ZoneInfo("America/New_York")

FUNDAMENTAL_FIELDS = {
    "forwardEps": "forward_eps",
    "trailingEps": "trailing_eps",
    "bookValue": "book_value_per_share",
    "forwardPE": "forward_pe",
    "trailingPE": "trailing_pe",
    "priceToBook": "price_to_book",
    "fiftyTwoWeekLow": "fifty_two_week_low",
    "fiftyTwoWeekHigh": "fifty_two_week_high",
    "currency": "currency",
    "quoteType": "quote_type",
    "shortName": "name",
}


class MarketDataError(RuntimeError):
    pass


class MarketProvider(Protocol):
    def history(self, symbol: str, start: date, end: date) -> list[Bar]: ...
    def quote(self, symbol: str) -> dict: ...
    def fundamentals(self, symbol: str) -> dict: ...
    def earnings_dates(self, symbol: str) -> list[date]: ...


def _clean(value):
    """NaN / numpy values → plain Python values, for JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


class YFinanceProvider:
    def __init__(self):
        # ETFs have no EPS or earnings dates and yfinance prints 404s to the terminal; refresh() reports failures
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    def _ticker(self, symbol: str):
        import yfinance as yf

        return yf.Ticker(symbol)

    def history(self, symbol: str, start: date, end: date) -> list[Bar]:
        frame = self._ticker(symbol).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance's end is exclusive
            interval="1d",
            auto_adjust=False,  # actual traded prices (split-adjusted only), comparable with fills and cost
            actions=False,
        )
        bars = []
        for stamp, row in frame.iterrows():
            close = _clean(row.get("Close"))
            if close is None:
                continue
            volume = _clean(row.get("Volume"))
            bars.append(
                Bar(symbol, stamp.date(), _clean(row.get("Open")), _clean(row.get("High")),
                    _clean(row.get("Low")), close, int(volume) if volume is not None else None)
            )
        return bars

    def quote(self, symbol: str) -> dict:
        # not fast_info: its previous_close does not match the actual previous close.
        # use the last few daily bars: intraday, the last bar is today and its close is the current price
        frame = self._ticker(symbol).history(period="5d", interval="1d", auto_adjust=False, actions=False)
        closes = [(stamp.date(), _clean(row.get("Close"))) for stamp, row in frame.iterrows()]
        closes = [(d, c) for d, c in closes if c is not None]
        if not closes:
            return {"last_price": None}
        session, last = closes[-1]
        return {
            "last_price": last,
            "previous_close": closes[-2][1] if len(closes) > 1 else None,
            "session_date": session.isoformat(),  # trading day the price belongs to
        }

    def fundamentals(self, symbol: str) -> dict:
        info = self._ticker(symbol).get_info() or {}
        return {ours: _clean(info.get(theirs)) for theirs, ours in FUNDAMENTAL_FIELDS.items()}

    def earnings_dates(self, symbol: str) -> list[date]:
        ticker = self._ticker(symbol)
        found: set[date] = set()
        errors = []
        try:
            for value in (ticker.get_calendar() or {}).get("Earnings Date", []) or []:
                found.add(value.date() if isinstance(value, datetime) else value)
        except Exception as exc:  # yfinance's exception types are not stable
            errors.append(exc)
        try:
            frame = ticker.get_earnings_dates(limit=12)
            if frame is not None:
                found |= {stamp.date() for stamp in frame.index}
        except Exception as exc:
            errors.append(exc)
        if len(errors) == 2:
            raise MarketDataError(f"Both earnings-date sources failed: {errors[0]}")
        return sorted(found)


class MarketData:
    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: MarketProvider | None = None,
        now: Callable[[], datetime] | None = None,
        tz: ZoneInfo = MARKET_TIMEZONE,
    ):
        self.conn = conn
        self.provider = provider or YFinanceProvider()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.tz = tz

    # ── refresh ────────────────────────────────────────────

    def refresh(self, symbol: str, kinds: Iterable[str] = KINDS, force: bool = False) -> dict[str, str]:
        """Return the status of each kind of data: cached / fetched / error: .... One failure does not
        affect the other kinds."""
        symbol = symbol.upper()
        status = {}
        for kind in kinds:
            if not force and self.is_fresh(symbol, kind):
                status[kind] = "cached"
                continue
            try:
                getattr(self, f"_fetch_{kind}")(symbol)
            except Exception as exc:  # yfinance's exception types are not stable; record and carry on
                status[kind] = f"error: {exc}"
                continue
            with self.conn:
                self.conn.execute(
                    """INSERT INTO market_fetch_log (symbol, kind, fetched_at) VALUES (?, ?, ?)
                       ON CONFLICT (symbol, kind) DO UPDATE SET fetched_at = excluded.fetched_at""",
                    (symbol, kind, utc_text(self._now())),
                )
            status[kind] = "fetched"
        return status

    def is_fresh(self, symbol: str, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT fetched_at FROM market_fetch_log WHERE symbol = ? AND kind = ?", (symbol.upper(), kind)
        ).fetchone()
        return row is not None and self._now() - parse_utc_text(row[0]) < CACHE_TTL[kind]

    def _today(self) -> date:
        return self._now().astimezone(self.tz).date()

    def _fetch_history(self, symbol: str) -> None:
        last = self.conn.execute("SELECT MAX(date) FROM price_history WHERE symbol = ?", (symbol,)).fetchone()[0]
        today = self._today()
        if last:
            start = date.fromisoformat(last) - timedelta(days=HISTORY_OVERLAP_DAYS)
        else:
            start = today - timedelta(days=HISTORY_BOOTSTRAP_DAYS)
        bars = self.provider.history(symbol, start, today)
        if not bars:
            raise MarketDataError(f"No daily bars for {symbol} (wrong ticker?)")
        fetched_at = utc_text(self._now())
        with self.conn:
            self.conn.executemany(
                """INSERT INTO price_history (symbol, date, open, high, low, close, volume, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (symbol, date) DO UPDATE SET
                       open = excluded.open, high = excluded.high, low = excluded.low,
                       close = excluded.close, volume = excluded.volume, fetched_at = excluded.fetched_at""",
                [(symbol, b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume, fetched_at) for b in bars],
            )

    def _snapshot(self, symbol: str, kind: str, data: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO market_snapshots (symbol, kind, fetched_at, data) VALUES (?, ?, ?, ?)",
                (symbol, kind, utc_text(self._now()), json.dumps(data, ensure_ascii=False, sort_keys=True)),
            )

    def _fetch_quote(self, symbol: str) -> None:
        data = self.provider.quote(symbol)
        if data.get("last_price") is None:
            raise MarketDataError(f"No current price for {symbol}")
        self._snapshot(symbol, "quote", data)

    def _fetch_fundamentals(self, symbol: str) -> None:
        self._snapshot(symbol, "fundamentals", self.provider.fundamentals(symbol))

    def _fetch_earnings(self, symbol: str) -> None:
        dates = {d.isoformat() for d in self.provider.earnings_dates(symbol)}
        today = self._today().isoformat()
        seen_at = utc_text(self._now())
        with self.conn:
            # the latest estimate wins for future dates; past earnings dates are kept
            stale = [
                row[0]
                for row in self.conn.execute(
                    "SELECT earnings_date FROM earnings_dates WHERE symbol = ? AND earnings_date >= ?",
                    (symbol, today),
                )
                if row[0] not in dates
            ]
            self.conn.executemany(
                "DELETE FROM earnings_dates WHERE symbol = ? AND earnings_date = ?", [(symbol, d) for d in stale]
            )
            self.conn.executemany(
                """INSERT INTO earnings_dates (symbol, earnings_date, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (symbol, earnings_date) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                [(symbol, d, seen_at, seen_at) for d in sorted(dates)],
            )

    # ── reading (database only, no network) ─────────────────

    def bars(self, symbol: str, start: date | None = None, end: date | None = None) -> list[Bar]:
        rows = self.conn.execute(
            """SELECT * FROM price_history WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date""",
            (symbol.upper(), (start or date.min).isoformat(), (end or date.max).isoformat()),
        )
        return [
            Bar(r["symbol"], date.fromisoformat(r["date"]), r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows
        ]

    def latest(self, symbol: str, kind: str, as_of: datetime | None = None) -> dict | None:
        """The latest snapshot; with as_of, the last one before that moment (for the audit)."""
        row = self.conn.execute(
            """SELECT data FROM market_snapshots WHERE symbol = ? AND kind = ? AND fetched_at <= ?
               ORDER BY fetched_at DESC LIMIT 1""",
            (symbol.upper(), kind, utc_text(as_of) if as_of else "9999"),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def earnings_dates(self, symbol: str) -> list[date]:
        rows = self.conn.execute(
            "SELECT earnings_date FROM earnings_dates WHERE symbol = ? ORDER BY earnings_date", (symbol.upper(),)
        )
        return [date.fromisoformat(r[0]) for r in rows]

    def next_earnings(self, symbol: str, on_or_after: date) -> date | None:
        return next((d for d in self.earnings_dates(symbol) if d >= on_or_after), None)

    def fifty_two_week_range(self, symbol: str, as_of: date) -> tuple[float, float] | None:
        """52-week low and high up to and including as_of, from intraday highs and lows."""
        bars = self.bars(symbol, as_of - timedelta(weeks=52) + timedelta(days=1), as_of)
        if not bars:
            return None
        low = min(b.low if b.low is not None else b.close for b in bars)
        high = max(b.high if b.high is not None else b.close for b in bars)
        return low, high


def tracked_symbols(conn: sqlite3.Connection, extra: Iterable[str] = ()) -> list[str]:
    """Tickers to track: stocks in the latest positions + the watchlist + any extras (e.g. QQQ)."""
    symbols = {s.upper() for s in extra}
    latest = conn.execute("SELECT MAX(as_of) FROM positions").fetchone()[0]
    if latest:
        symbols |= {
            r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM positions WHERE as_of = ? AND asset_class = 'STK'", (latest,)
            )
        }
    symbols |= {r[0].upper() for r in conn.execute("SELECT DISTINCT symbol FROM watchlist WHERE removed_at IS NULL")}
    return sorted(symbols)

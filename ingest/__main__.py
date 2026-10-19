"""Command line:

    python -m ingest flex                       fetch the Activity Flex Query and write it to the database
    python -m ingest flex --query trade_confirm fetch today's trade confirmations
    python -m ingest flex --file statement.xml  parse a local file (no network)
    python -m ingest market                     refresh the market cache for positions + watchlist + index
    python -m ingest market AAPL NVDA --force   given tickers, ignoring the cache
    python -m ingest valuation import           import valuation_seed.csv from the project root
    python -m ingest valuation update           weekly EPS update (the weekly job runs it)
    python -m ingest valuation show             list the latest valuation lines and where prices sit
    python -m ingest watchlists                 import IBKR watchlists (data/ibkr_watchlists.json) as the candidate pool
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from db.database import init_db
from ingest.flex import FLEX_TIMEZONE, FlexClient, FlexError, parse_statements
from ingest.market import MarketData, tracked_symbols
from ingest.store import archive_raw, save_statement
from ingest.valuation import SeedError, import_seed, latest_rows, update_eps, zone
from ingest.watchlists import import_file
from rules.config import load_rules
from settings import PROJECT_ROOT, load_settings
from textfmt import pad

RECENT_TRADES = 15  # the CLI only lists the most recent fills

EXPECTED_SECTIONS = {
    "OpenPositions": "Open Positions",
    "Trades": "Trades",
}


def _pct(value: float | None) -> str:
    return f"{value:6.1%}" if value is not None else "     -"


def _header(*columns: tuple[str, int]) -> str:
    """Header row: first column left-aligned, the rest right-aligned."""
    return "".join(pad(title, size, right=i > 0) for i, (title, size) in enumerate(columns))


def cmd_flex(args) -> int:
    settings = load_settings()
    if args.file:
        body = Path(args.file).read_bytes()
        print(f"Parsing local file: {args.file}")
    else:
        query_id = {
            "activity": settings.flex_query_id_activity,
            "trade_confirm": settings.flex_query_id_trade_confirm,
        }[args.query]
        if not query_id:
            env_key = "IBKR_FLEX_QUERY_ID_" + ("ACTIVITY" if args.query == "activity" else "TRADE_CONFIRM")
            print(f"{env_key} is not set in .env")
            return 1
        print(f"Fetching the {args.query} statement from IBKR…")
        body = FlexClient(settings.flex_token or "").fetch(query_id)
        path = archive_raw(body, args.query, settings.db_path.parent)
        print(f"Raw statement saved: {path.relative_to(settings.db_path.parent.parent)}")

    statements = parse_statements(body, expected_account=settings.account_id)
    conn = init_db(settings.db_path)
    for stmt in statements:
        result = save_statement(conn, stmt)
        print()
        print(f"Statement period: {stmt.from_date} → {stmt.to_date}")
        print(
            f"Saved: {result.positions} position rows, {result.snapshots} days of account snapshots, "
            f"{result.trades_new} new fills ({result.trades_existing} already stored)"
        )
        if args.query == "activity":
            missing = [label for tag, label in EXPECTED_SECTIONS.items() if tag not in stmt.sections]
            if not stmt.snapshots:
                missing.append("Cash Report")
            if missing:
                print("⚠ The statement is missing these sections; enable them in the Flex Query: " + ", ".join(missing))
            if "EquitySummaryInBase" not in stmt.sections:
                print("⚠ Net Asset Value (NAV) in Base is missing: only the last day's cash and NAV are available. "
                      "The audit needs daily NAV to recompute the cash ratio on each fill day and week-start cash.")
        for note in stmt.skipped:
            print(f"  Skipped: {note}")
        _print_statement(stmt)
    conn.close()
    return 0


def _print_statement(stmt) -> None:
    snapshot = max(stmt.snapshots, key=lambda s: s.as_of, default=None)
    nav = snapshot.net_liquidation if snapshot else None

    if snapshot:
        print()
        print(f"{snapshot.as_of}  NAV {snapshot.net_liquidation:,.0f}  cash {snapshot.cash:,.0f}"
              f" ({snapshot.cash / snapshot.net_liquidation:.1%})")

    latest = max((p.as_of for p in stmt.positions), default=None)
    rows = sorted(
        (p for p in stmt.positions if p.as_of == latest),
        key=lambda p: -(p.market_value or 0),
    )
    if rows:
        print()
        print(_header(("Ticker", 10), ("Qty", 8), ("Cost", 12), ("Price", 12), ("Value", 14), ("Weight", 9), ("P&L", 9)))
        for p in rows:
            weight = p.market_value / nav if nav and p.market_value is not None else None
            pnl = (p.mark_price / p.avg_cost - 1) if p.avg_cost and p.mark_price else None
            print(
                f"{p.symbol:<10}{p.quantity:>8g}{p.avg_cost or 0:>12.2f}{p.mark_price or 0:>12.2f}"
                f"{p.market_value or 0:>14,.0f}{_pct(weight):>9}{_pct(pnl):>9}"
            )

    stocks = sorted((t for t in stmt.trades if t.asset_class == "STK"), key=lambda t: t.executed_at)
    if stmt.trades:
        print()
        others = len(stmt.trades) - len(stocks)
        print(f"Fills: {len(stocks)} stock" + (f", {others} other (FX etc., saved but not listed)" if others else ""))
        if len(stocks) > RECENT_TRADES:
            print(f"Latest {RECENT_TRADES} (US Eastern time):")
        for t in stocks[-RECENT_TRADES:]:
            local = t.executed_at.astimezone(FLEX_TIMEZONE)
            note = ""
            if t.order_placed_at and t.order_placed_at.astimezone(FLEX_TIMEZONE).date() < local.date():
                placed = t.order_placed_at.astimezone(FLEX_TIMEZONE)
                note = f"  ← order placed {placed:%m-%d}"
            print(f"  {local:%Y-%m-%d %H:%M}  {t.side:<4} {t.symbol:<8}{t.quantity:>8g} @ {t.price:,.2f}{note}")


def cmd_market(args) -> int:
    settings = load_settings()
    book = load_rules(settings.rules_path)
    conn = init_db(settings.db_path)
    md = MarketData(conn, tz=book.calendar.tz)
    symbols = [s.upper() for s in args.symbols] or tracked_symbols(conn, extra=[book.market_drop_day.index_symbol])
    if not symbols:
        print("No tickers to refresh: run python -m ingest flex to import positions, or list tickers after the command")
        return 1

    today = datetime.now(timezone.utc).astimezone(book.calendar.tz).date()
    print(_header(("Ticker", 8), ("Price", 10), ("52w low", 11), ("vs low", 9), ("52w high", 11),
                  ("fwd EPS", 9), ("Earnings", 14)) + "  Cache")
    failed = 0
    for symbol in symbols:
        status = md.refresh(symbol, force=args.force)
        quote = md.latest(symbol, "quote") or {}
        fundamentals = md.latest(symbol, "fundamentals") or {}
        price = quote.get("last_price")
        rng = md.fifty_two_week_range(symbol, today)
        above_low = (price / rng[0] - 1) if price and rng else None
        eps = fundamentals.get("forward_eps")
        nxt = md.next_earnings(symbol, today)
        errors = {k: v for k, v in status.items() if v.startswith("error")}
        failed += bool(errors)
        cache = "failed: " + "; ".join(f"{k} {v[7:]}" for k, v in errors.items()) if errors else \
            ("cached" if set(status.values()) == {"cached"} else "updated")
        print(
            f"{symbol:<8}{price or 0:>10.2f}{rng[0] if rng else 0:>11.2f}{_pct(above_low):>9}"
            f"{rng[1] if rng else 0:>11.2f}{f'{eps:.2f}' if eps is not None else '-':>9}"
            f"{nxt.isoformat() if nxt else '-':>14}  {cache}"
        )
    conn.close()
    return 1 if failed else 0


def cmd_valuation(args) -> int:
    settings = load_settings()
    book = load_rules(settings.rules_path)
    conn = init_db(settings.db_path)
    md = MarketData(conn, tz=book.calendar.tz)
    today = datetime.now(timezone.utc).astimezone(book.calendar.tz).date()

    if args.action == "import":
        path = Path(args.file) if args.file else PROJECT_ROOT / "valuation_seed.csv"
        try:
            imported, notes = import_seed(conn, book, path, md, today)
        except SeedError as exc:
            print(f"Import failed: {exc}")
            return 1
        print(f"Imported {len(imported)} stocks ({path.name})")
        print(_header(("Ticker", 8), ("Your EPS", 10), ("yfinance", 10), ("Gap", 9), ("Lines as of", 14)))
        for item in imported:
            ref = item.reference
            diff = f"{ref / item.row.base_value - 1:+.0%}" if ref else "-"
            print(f"{item.row.symbol:<8}{item.row.base_value:>10.2f}{(f'{ref:.2f}' if ref else '-'):>10}"
                  f"{diff:>9}{item.as_of.isoformat():>14}")
        for note in notes:
            print(f"  ⓘ {note}")
        return 0

    if args.action == "update":
        changes = update_eps(conn, book, md, today, force=args.force)
        if not changes:
            print("Already updated this week (less than a week since the last update); add --force to update anyway")
        for c in changes:
            moved = f"{c.old:.2f} → {c.new:.2f} ({c.change:+.1%})" if c.new is not None else f"{c.old:.2f} unchanged"
            print(f"{c.symbol:<8}{moved}  {c.note}")
        return 0

    rows = latest_rows(conn)
    if not rows:
        print("No valuation lines yet: run python -m ingest valuation import first")
        return 1
    print(_header(("Ticker", 8), ("Price", 10), ("Pressure", 9), ("Cheap", 9), ("Normal", 9), ("Rich", 9),
                  ("  Band", 20), ("As of", 12)))
    for r in rows:
        bars = md.bars(r["symbol"], end=today)
        price = bars[-1].close if bars else None
        lines = {k: r[f"{k}_line"] for k in ("pressure", "cheap", "normal", "rich")}
        where = zone(price, lines) if price else "no price"
        if price and price < lines["pressure"]:
            where += " · below pressure"
        print(f"{r['symbol']:<8}{price or 0:>10.2f}{lines['pressure']:>9.1f}{lines['cheap']:>9.1f}"
              f"{lines['normal']:>9.1f}{lines['rich']:>9.1f}  {pad(where, 18)}{r['as_of']:>12}")
    return 0


def cmd_watchlists(args) -> int:
    settings = load_settings()
    path = Path(args.file) if args.file else settings.db_path.parent / "ibkr_watchlists.json"
    if not path.exists():
        print(f"Not found: {path}")
        return 1
    conn = init_db(settings.db_path)
    counts = import_file(conn, path)
    total = conn.execute("SELECT COUNT(DISTINCT symbol) FROM ibkr_watchlist").fetchone()[0]
    print(f"Imported {len(counts)} lists, {total} stocks after removing duplicates: "
          + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print("This is only a candidate pool, not registration: write a catalyst and an invalidation on the Watchlist page.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest", description="Data ingestion (read-only)")
    sub = parser.add_subparsers(dest="command", required=True)

    flex = sub.add_parser("flex", help="fetch an IBKR Flex statement")
    flex.add_argument("--query", choices=["activity", "trade_confirm"], default="activity")
    flex.add_argument("--file", help="parse a local XML file, no network")
    flex.set_defaults(func=cmd_flex)

    market = sub.add_parser("market", help="refresh the yfinance market cache")
    market.add_argument("symbols", nargs="*")
    market.add_argument("--force", action="store_true", help="ignore the cache")
    market.set_defaults(func=cmd_market)

    valuation = sub.add_parser("valuation", help="valuation lines")
    valuation.add_argument("action", choices=["import", "update", "show"])
    valuation.add_argument("--file", help="seed file; default valuation_seed.csv in the project root")
    valuation.add_argument("--force", action="store_true", help="for update: update no matter how recent the last one was")
    valuation.set_defaults(func=cmd_valuation)

    watchlists = sub.add_parser("watchlists", help="import IBKR watchlists as the candidate pool")
    watchlists.add_argument("--file", help="JSON file; default data/ibkr_watchlists.json")
    watchlists.set_defaults(func=cmd_watchlists)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FlexError as exc:
        print(f"Flex error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Command line (prints results only; does not write the checks / violations tables — the buy check
page and the audit do that):

    python -m rules check NVDA 1 180.50          check a proposed buy on the current state
    python -m rules recheck --since 2026-10-01   re-run past buys on the state at fill time
    python -m rules recheck --symbol NVDA
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date

from db.database import init_db
from ingest.market import MarketData
from rules.config import load_rules
from rules.engine import LABELS, Evaluation, ProposedTrade, check, recheck_trade
from settings import load_settings
from textfmt import pad

ICON = {"pass": "🟢", "warn": "🟡", "block": "🔴"}


def _print(ev: Evaluation, only_problems: bool = False) -> None:
    for r in ev.results:
        if only_problems and r.status == "pass":
            continue
        print(f"  {ICON[r.status]} {pad(LABELS[r.rule_id], 20)}{r.reason}")
    for note in ev.context.notes:
        print(f"  ⓘ {note}")


def cmd_check(args) -> int:
    settings = load_settings()
    book = load_rules(settings.rules_path)
    conn = init_db(settings.db_path)
    md = MarketData(conn, tz=book.calendar.tz)
    for symbol in {args.symbol.upper(), book.market_drop_day.index_symbol}:
        failed = {k: v for k, v in md.refresh(symbol).items() if v.startswith("error")}
        if failed:
            print(f"⚠ {symbol} market data failed to update: {failed}")
    trade = ProposedTrade(args.symbol, "SELL" if args.sell else "BUY", args.quantity, args.price)
    ev = check(conn, book, trade, md=md)
    c = ev.context
    print(f"{trade.side} {trade.symbol} {trade.quantity:g} sh @ {trade.price:,.2f}"
          f" ({trade.amount:,.2f}), trading day {c.trade_date}, end-of-day state {c.snapshot_as_of}")
    if trade.side == "SELL":
        print("  Sells are not limited by the buy rules")
        return 0
    print(f"Verdict: {ICON[ev.verdict]} {ev.verdict}")
    _print(ev)
    if ev.verdict == "block":
        print(f"\nBlocked: to override, write a one-line reason and wait {book.override.cooldown_hours:g} hours "
              f"(on the Buy check page)")
    return 0


def cmd_recheck(args) -> int:
    settings = load_settings()
    book = load_rules(settings.rules_path)
    conn = init_db(settings.db_path)
    sql = "SELECT trade_id FROM trades WHERE asset_class = 'STK' AND side = 'BUY' AND executed_at >= ?"
    params: list = [args.since or "0000"]
    if args.symbol:
        sql += " AND symbol = ?"
        params.append(args.symbol.upper())
    trade_ids = [r[0] for r in conn.execute(sql + " ORDER BY executed_at, trade_id", params)]
    if not trade_ids:
        print("No matching buy fills")
        return 0

    verdicts: Counter = Counter()
    hits: Counter = Counter()
    for trade_id in trade_ids:
        ev = recheck_trade(conn, book, trade_id)
        t, c = ev.trade, ev.context
        local = c.moment.astimezone(book.calendar.tz)
        verdicts[ev.verdict] += 1
        hits.update(r.rule_id for r in ev.results if r.status == "block" and not r.missing_data)
        print(f"\n{local:%Y-%m-%d %H:%M}  BUY {t.symbol} {t.quantity:g} @ {t.price:,.2f}  → {ICON[ev.verdict]}")
        _print(ev, only_problems=True)

    print(f"\n{len(trade_ids)} buys: " + ", ".join(f"{ICON[s]} {verdicts[s]}" for s in ("block", "warn", "pass")))
    if hits:
        print("Blocking rules (excluding missing data):")
        for rule_id, n in hits.most_common():
            print(f"  {pad(LABELS[rule_id], 20)}{n:>3}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m rules", description="Rule engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="check a proposed trade")
    p.add_argument("symbol")
    p.add_argument("quantity", type=float)
    p.add_argument("price", type=float)
    p.add_argument("--sell", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("recheck", help="re-run past buys on the state at fill time")
    p.add_argument("--since", type=lambda s: date.fromisoformat(s).isoformat(), help="start date YYYY-MM-DD")
    p.add_argument("--symbol")
    p.set_defaults(func=cmd_recheck)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

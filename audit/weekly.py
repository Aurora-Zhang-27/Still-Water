"""Weekly report (every Sunday).

Contents: this week's trades and violations, compliance rate (deduplicated checks), returns of compliant
vs. violating trades, how blocked trades did afterwards, GTC order review, sell reminders (take profit,
invalidation, above the rich line), and next week's earnings.
Saved as Markdown in data/reports/ and shown on the Reports page.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from audit.stats import (
    GROUPS,
    Compliance,
    Intent,
    Outcome,
    average,
    blocked_not_bought,
    buy_outcomes,
    check_intents,
    compliance,
)
from db.database import parse_utc_text
from db.records import Accuracy, Hunch, accuracy, hunches
from ingest.market import MarketData
from audit.reminders import Reminder, holding_status, invalidation_status, reminders
from ingest.valuation import BIG_CHANGE, EpsChange, latest_rows
from rules.calendar import previous_trading_day, trade_date_for, week_start
from rules.config import RuleBook
from rules.context import FUND_QUOTE_TYPES, account_state
from rules.engine import LABELS, ProposedTrade, check

CLOSED_ORDER_STATUSES = {"FILLED", "CANCELLED", "APICANCELLED", "INACTIVE"}


@dataclass(frozen=True)
class OrderReview:
    symbol: str
    side: str
    quantity: float
    limit_price: float | None
    days_open: int | None
    stale: bool
    would_violate: list[str]  # blocks a fill now would trigger


@dataclass(frozen=True)
class EarningsItem:
    symbol: str
    earnings: date
    window_start: date
    held: bool


@dataclass
class WeeklyReport:
    week_start: date
    week_end: date
    generated_at: datetime
    as_of: date | None  # latest day of market data
    period: str  # whether the week is in the test period or before go-live
    buys: int = 0
    sells: int = 0
    gtc_fills: list[str] = field(default_factory=list)
    violations: Counter = field(default_factory=Counter)
    missing_data: int = 0
    violation_lines: list[str] = field(default_factory=list)
    compliance_week: Compliance | None = None
    compliance_live: Compliance | None = None
    outcomes_live: dict[str, list[Outcome]] = field(default_factory=dict)
    outcomes_baseline: dict[str, list[Outcome]] = field(default_factory=dict)
    blocked_outcomes: list[Outcome] = field(default_factory=list)
    orders: list[OrderReview] | None = None  # None: no order data
    reminders: list[Reminder] = field(default_factory=list)
    earnings: list[EarningsItem] = field(default_factory=list)
    valuation_changes: list[EpsChange] = field(default_factory=list)
    hunch_accuracy: Accuracy | None = None
    hunches_reviewed: list[Hunch] = field(default_factory=list)  # hunches reviewed this week
    holding_zones: list[tuple[str, str, float, float]] = field(default_factory=list)  # ticker, band, price, normal line
    missing_valuation: list[str] = field(default_factory=list)  # holdings without valuation lines


def _window_start(earnings: date, days_before: int) -> date:
    day = earnings
    for _ in range(days_before):
        day = previous_trading_day(day)
    return day


def build_weekly(
    conn: sqlite3.Connection,
    book: RuleBook,
    now: datetime | None = None,
    *,
    week_of: date | None = None,
    md: MarketData | None = None,
    valuation_changes: list[EpsChange] | None = None,
) -> WeeklyReport:
    now = now or datetime.now(timezone.utc)
    tz = book.calendar.tz
    md = md or MarketData(conn, tz=tz)
    ws = week_start(week_of or now.astimezone(tz).date(), book.calendar.week_starts_on)
    we = ws + timedelta(days=7)
    index_bars = md.bars(book.market_drop_day.index_symbol, end=now.astimezone(tz).date())
    as_of = index_bars[-1].date if index_bars else None
    report = WeeklyReport(ws, we - timedelta(days=3), now, as_of,
                          "live" if ws >= book.audit.start_date else "baseline",
                          valuation_changes=valuation_changes or [])

    # this week's trades
    for t in conn.execute("SELECT * FROM trades WHERE asset_class = 'STK' ORDER BY executed_at"):
        executed = parse_utc_text(t["executed_at"])
        day = trade_date_for(executed, tz)
        if not ws <= day < we:
            continue
        if t["side"] == "BUY":
            report.buys += 1
        else:
            report.sells += 1
        if t["order_placed_at"]:
            placed = trade_date_for(parse_utc_text(t["order_placed_at"]), tz)
            if placed < day:
                report.gtc_fills.append(f"{day:%m-%d} {t['side']} {t['symbol']} {t['quantity']:g} sh @ "
                                        f"{t['price']:,.2f} (order placed {placed:%m-%d})")

    # violations (test-period fills only): counted by rule, one detail line per fill
    rows = conn.execute(
        """SELECT v.rule_id, v.level, v.details, t.trade_id, t.symbol, t.executed_at, t.side, t.quantity, t.price
           FROM violations v JOIN trades t ON t.trade_id = v.trade_id ORDER BY t.executed_at, v.id"""
    )
    per_trade: dict[str, list] = {}
    for v in rows:
        day = trade_date_for(parse_utc_text(v["executed_at"]), tz)
        if not ws <= day < we:
            continue
        if json.loads(v["details"] or "{}").get("missing_data"):
            report.missing_data += 1
            continue
        report.violations[v["rule_id"]] += 1
        per_trade.setdefault(v["trade_id"], []).append(v)
    for items in per_trade.values():
        t = items[0]
        day = trade_date_for(parse_utc_text(t["executed_at"]), tz)
        icon = "🔴" if any(i["level"] == "block" for i in items) else "🟡"
        names = ", ".join(LABELS.get(i["rule_id"], i["rule_id"]) for i in items)
        report.violation_lines.append(
            f"{icon} {day:%m-%d} {t['side']} {t['symbol']} {t['quantity']:g} sh @ {t['price']:,.2f}: {names}")

    # compliance rate (deduplicated checks)
    intents_week = check_intents(conn, book, ws, we)
    intents_live = check_intents(conn, book, book.audit.start_date, we)
    report.compliance_week = compliance(intents_week)
    report.compliance_live = compliance(intents_live)

    # returns
    if as_of:
        report.outcomes_live = buy_outcomes(conn, book, md, "live", as_of)
        report.outcomes_baseline = buy_outcomes(conn, book, md, "baseline", as_of)
        report.blocked_outcomes = blocked_not_bought(md, book, intents_live, as_of)

    # GTC review (the orders table is only filled by a read-only IB Gateway, not connected yet; review GTC orders in IBKR yourself)
    orders = conn.execute("SELECT * FROM orders WHERE UPPER(COALESCE(time_in_force, '')) = 'GTC'").fetchall()
    if orders or conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
        report.orders = []
        for o in orders:
            if (o["status"] or "").upper() in CLOSED_ORDER_STATUSES:
                continue
            placed = parse_utc_text(o["placed_at"]) if o["placed_at"] else None
            days = (now - placed).days if placed else None
            blocked: list[str] = []
            if o["side"] == "BUY" and o["limit_price"] and book.gtc_review.flag_if_fill_would_violate:
                ev = check(conn, book, ProposedTrade(o["symbol"], "BUY", o["quantity"], o["limit_price"]), md=md)
                blocked = [f"{LABELS[r.rule_id]}: {r.reason}" for r in ev.by_status("block") if not r.missing_data]
            report.orders.append(OrderReview(
                o["symbol"], o["side"], o["quantity"], o["limit_price"], days,
                days is not None and days > book.gtc_review.stale_after_days, blocked))

    # sell reminders, where holdings sit on their valuation lines
    state = account_state(conn, book, now)
    statuses = holding_status(conn, book, md, now, as_of) if as_of else []
    invalidations = invalidation_status(conn, md, as_of, set(state.holdings)) if as_of else []
    report.reminders = reminders(book, statuses, invalidations)
    valued = {r["symbol"]: r for r in latest_rows(conn, as_of)} if as_of else {}
    for s in statuses:
        row = valued.get(s.symbol)
        if row is not None:
            report.holding_zones.append((s.symbol, s.zone, s.price, row["normal_line"]))
    for symbol in sorted(state.holdings):
        fundamentals = md.latest(symbol, "fundamentals") or {}
        is_fund = symbol == book.market_drop_day.index_symbol or fundamentals.get("quote_type") in FUND_QUOTE_TYPES
        if symbol not in valued and not (is_fund and book.selection_pe.funds_exempt):
            report.missing_valuation.append(symbol)

    # hunch journal
    report.hunch_accuracy = accuracy(conn)
    report.hunches_reviewed = [h for h in hunches(conn) if h.reviewed and ws <= h.review_due_on < we]

    # next week's earnings
    next_monday = we
    next_friday = next_monday + timedelta(days=4)
    watched = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM watchlist WHERE removed_at IS NULL")}
    for symbol in sorted(set(state.holdings) | watched):
        fundamentals = md.latest(symbol, "fundamentals") or {}
        if symbol == book.market_drop_day.index_symbol or fundamentals.get("quote_type") in FUND_QUOTE_TYPES:
            continue
        e = md.next_earnings(symbol, next_monday)
        if e is None:
            continue
        start = _window_start(e, book.earnings_blackout.trading_days_before)
        if e <= next_friday or start <= next_friday:
            report.earnings.append(EarningsItem(symbol, e, start, symbol in state.holdings))
    report.earnings.sort(key=lambda item: item.earnings)
    return report


# ── output ─────────────────────────────────────────────────


def _pct(x: float | None) -> str:
    return f"{x:+.1%}" if x is not None else "—"


def _compliance_line(c: Compliance) -> str:
    if not c.blocked and not c.passed:
        return "No buy checks"
    rate = f", compliance {c.rate:.0%}" if c.rate is not None else ""
    return (f"blocked {c.blocked}: complied {c.complied}, broke {c.broke} (with a reason {c.overridden}){rate}; "
            f"passed {c.passed}")


def _outcome_table(groups: dict[str, list[Outcome]]) -> list[str]:
    lines = ["| Group | Buys | Avg return | QQQ same period | Excess |", "| --- | ---: | ---: | ---: | ---: |"]
    for g in GROUPS:
        items = groups.get(g, [])
        lines.append(f"| {g} | {len(items)} | {_pct(average([o.ret for o in items]))} | "
                     f"{_pct(average([o.index_ret for o in items]))} | {_pct(average([o.excess for o in items]))} |")
    return lines


def render_markdown(r: WeeklyReport, book: RuleBook) -> str:
    tz = book.calendar.tz
    out = [
        f"# Weekly report {r.week_start} → {r.week_end}",
        "",
        f"Generated {r.generated_at.astimezone(tz):%Y-%m-%d %H:%M} ET · market data to {r.as_of or '—'} · "
        f"test period from {book.audit.start_date}" + ("" if r.period == "live" else " (this week is before go-live; no violations recorded)"),
        "",
        "## This week's trades",
        "",
        f"- {r.buys} buys, {r.sells} sells",
    ]
    out += [f"- Resting order filled: {line}" for line in r.gtc_fills] or ["- No resting orders filled"]

    out += ["", "## Violations", ""]
    if r.violations:
        out += ["| Rule | Count |", "| --- | ---: |"]
        out += [f"| {LABELS.get(k, k)} | {n} |" for k, n in r.violations.most_common()]
        out += [""] + [f"- {line}" for line in r.violation_lines]
    else:
        out.append("No violations" if r.period == "live" else "Fills before go-live record no violations")
    if r.missing_data:
        out += ["", f"Plus {r.missing_data} block(s) caused by missing data (gone once valuation lines are imported), not counted above"]

    out += ["", "## Compliance", "", "Several checks of one stock on one trading day count once.", ""]
    out.append(f"- This week: {_compliance_line(r.compliance_week)}")
    out.append(f"- Test period so far: {_compliance_line(r.compliance_live)}")

    out += ["", f"## Compliant vs. violating: after the buy (buy price → {r.as_of} close)", "", "**Test period**", ""]
    out += _outcome_table(r.outcomes_live)
    out += ["", "**Pre-launch baseline** (audited with the current rules, for comparison)", ""]
    out += _outcome_table(r.outcomes_baseline)

    out += ["", "## How blocked trades did afterwards", ""]
    if r.blocked_outcomes:
        out += ["| Intent | Check price | Price now | Change | QQQ same period |", "| --- | ---: | ---: | ---: | ---: |"]
        out += [f"| {o.label} | {o.entry:,.2f} | {o.latest:,.2f} | {_pct(o.ret)} | {_pct(o.index_ret)} |"
                for o in r.blocked_outcomes]
    else:
        out.append("No blocked checks you skipped in the test period yet")

    out += ["", "## GTC order review", ""]
    if r.orders is None:
        out.append("No order data (Flex has no open orders and IB Gateway is not connected): review your GTC orders in "
                   f"IBKR. Consider cancelling ones open longer than {book.gtc_review.stale_after_days} days; to see "
                   "whether a buy order would break a rule when it fills, run a buy check at its limit price.")
    elif not r.orders:
        out.append("No open GTC orders")
    else:
        for o in r.orders:
            icon = "🔴" if o.would_violate else ("⏰" if o.stale else "•")
            age = f"open {o.days_open} days" if o.days_open is not None else "order age unknown"
            stale = f" (over {book.gtc_review.stale_after_days} days)" if o.stale else ""
            price = f" @ {o.limit_price:,.2f}" if o.limit_price else ""
            out.append(f"- {icon} {o.side} {o.symbol} {o.quantity:g} sh{price}, {age}{stale}")
            out += [f"    - a fill now would trigger {v}" for v in o.would_violate]

    out += ["", "## Valuation lines", ""]
    if r.valuation_changes:
        updated = [ch for ch in r.valuation_changes if ch.new is not None]
        out.append(f"- EPS updated for {len(updated)} stocks this week (by yfinance's revisions)")
        big = [ch for ch in updated if ch.change is not None and abs(ch.change) >= BIG_CHANGE]
        out += [f"- ⚠ {ch.symbol} EPS {ch.old:,.2f} → {ch.new:,.2f} ({ch.change:+.0%}), a big move; check for a fiscal-year rollover"
                for ch in big]
        out += [f"- {ch.symbol}: {ch.note}" for ch in r.valuation_changes if ch.note]
    if r.holding_zones:
        out += ["", "| Holding | Band | Price | vs. normal line |", "| --- | --- | ---: | ---: |"]
        out += [f"| {s} | {z} | {p:,.2f} | {p / n - 1:+.0%} |"
                for s, z, p, n in sorted(r.holding_zones, key=lambda x: x[2] / x[3])]
    if r.missing_valuation:
        out.append(f"\nHoldings without valuation lines: {', '.join(r.missing_valuation)} (a buy would treat "
                   f"\"PE not high\" as missing data)")
    if not (r.valuation_changes or r.holding_zones or r.missing_valuation):
        out.append("No valuation lines yet")

    out += ["", "## Hunch accuracy", ""]
    acc = r.hunch_accuracy
    if acc is None or (acc.reviewed == 0 and acc.pending == 0):
        out.append("No hunches recorded yet")
    else:
        rate = f", accuracy {acc.rate:.0%}" if acc.rate is not None else ""
        up, down = acc.by_move["up"], acc.by_move["down"]
        out.append(f"- {acc.reviewed} reviewed, {acc.right} right{rate}; {acc.pending} still waiting")
        if acc.reviewed:
            out.append(f"- Expected up: {up[1]}/{up[0]} right; expected down (incl. take profit): {down[1]}/{down[0]} right")
        out += [f"- {'✓' if h.was_right else '✗'} due this week: {h.symbol} expected {h.expected_move}, "
                f"{h.price_at_entry:,.2f} → {h.price_at_review:,.2f} ({h.note})" for h in r.hunches_reviewed]

    out += ["", "## Sell reminders", ""]
    out += [f"- {'🟡' if m.level == 'warn' else '🔔'} {m.text}" for m in r.reminders] or ["None"]

    nm = r.week_start + timedelta(days=7)
    out += ["", f"## Earnings next week ({nm:%m-%d} → {nm + timedelta(days=4):%m-%d})", ""]
    if r.earnings:
        out += ["| Ticker | Earnings | No-buy window | |", "| --- | --- | --- | --- |"]
        out += [f"| {e.symbol} | {e.earnings:%m-%d} | from {e.window_start:%m-%d} | {'holding' if e.held else 'watching'} |"
                for e in r.earnings]
    else:
        out.append("No earnings or earnings windows next week for holdings or the watchlist")
    return "\n".join(out) + "\n"


def save_report(markdown: str, data_dir: Path, week: date) -> Path:
    folder = data_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"weekly_{week.isoformat()}.md"
    path.write_text(markdown, encoding="utf-8")
    return path

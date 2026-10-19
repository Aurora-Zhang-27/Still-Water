"""Rule engine: one trade + the state at that moment → pass / warn / block for every rule, with a reason.

evaluate() is a pure function: no database, no network. check() rebuilds the state and calls evaluate():
  - check(conn, book, trade)                            proposed trade, judged on the current state
  - check(conn, book, trade, at=fill_time, trade_id=…)  a past trade, re-run on the state at fill time
  - recheck_trade(conn, book, trade_id)                 same, reading the trade from the trades table

Two conventions:
  - rules come from the current rules.yaml; data comes from the moment of the trade
  - missing data is treated at the rule's own level (a block rule blocks) and the reason says what is
    missing: if the system cannot prove the trade complies, it does not let it through

Only buys are checked. Sells have no hard blocks; take-profit, invalidation and above-rich-line
reminders run in the scheduled jobs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction

from db.database import parse_utc_text
from ingest.market import MarketData
from rules.config import RuleBook
from rules.context import TradeContext, build_context

STATUSES = ("pass", "warn", "block")
SELECTION_RULES = ("selection_near_low", "selection_pe", "selection_catalyst", "selection_planned")

# Short names for the pages and the CLI (thresholds and descriptions live in rules.yaml)
LABELS = {
    "selection_near_low": "Near a low",
    "selection_pe": "PE not high",
    "selection_catalyst": "Has catalyst",
    "selection_planned": "Planned",
    "position_cap": "Position cap",
    "no_buy_over_cap": "No buys over cap",
    "cash_floor": "Cash floor",
    "no_averaging_down": "No averaging down",
    "earnings_blackout": "Earnings blackout",
    "reduce_only_list": "Reduce-only",
    "market_drop_day": "Drop-day limit",
    "weekly_buy_budget": "Weekly budget",
    "gtc_review": "GTC review",
    "take_profit_reminder": "Take profit",
    "invalidation_triggered": "Invalidation",
    "above_rich_line": "Above rich line",
    # three extra violations recorded by the post-trade audit
    "unchecked_trade": "Unchecked",
    "block_ignored": "Bought after block",
    "cooldown_not_elapsed": "Cooldown not over",
}


@dataclass(frozen=True)
class ProposedTrade:
    symbol: str
    side: str
    quantity: float
    price: float
    order_id: str | None = None  # only for past trades; several fills of one order count as one "buy"

    def __post_init__(self):
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "side", self.side.strip().upper())
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {self.side!r}")
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("quantity and price must both be greater than 0")

    @property
    def amount(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    status: str  # pass / warn / block
    reason: str
    details: dict = field(default_factory=dict)
    missing_data: bool = False

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "status": self.status, "reason": self.reason,
                "details": self.details, "missing_data": self.missing_data}


@dataclass(frozen=True)
class Evaluation:
    trade: ProposedTrade
    context: TradeContext
    results: tuple[RuleResult, ...]
    rules_version: int

    @property
    def verdict(self) -> str:
        return max((r.status for r in self.results), key=STATUSES.index, default="pass")

    def result(self, rule_id: str) -> RuleResult | None:
        return next((r for r in self.results if r.rule_id == rule_id), None)

    def by_status(self, status: str) -> list[RuleResult]:
        return [r for r in self.results if r.status == status]


# ── helpers ────────────────────────────────────────────────


def _missing(rule_id: str, level: str, what: str) -> RuleResult:
    return RuleResult(rule_id, level, f"Missing data: {what}; cannot confirm", missing_data=True)


def _ok(rule_id: str, reason: str, **details) -> RuleResult:
    return RuleResult(rule_id, "pass", reason, details)


def _fail(rule_id: str, level: str, reason: str, **details) -> RuleResult:
    return RuleResult(rule_id, level, reason, details)


def _money(x: float) -> str:
    return f"{x:,.2f}"


# ── the three stock-selection rules ────────────────────────


def _near_low(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.selection_near_low, "selection_near_low"
    checks: list[tuple[bool | None, str]] = []

    if c.fifty_two_week_low:
        low = min(c.fifty_two_week_low, t.price)
        above = t.price / low - 1
        checks.append((above <= rule.max_above_52w_low,
                       f"{above:.1%} above the 52-week low {low:.2f} (needs ≤ {rule.max_above_52w_low:.0%})"))
    else:
        checks.append((None, "no 52-week daily bars"))

    if c.recent_high:
        drop = 1 - t.price / c.recent_high
        checks.append((drop >= rule.min_below_recent_high,
                       f"{drop:.1%} below the {c.recent_high_days}-day high {c.recent_high:.2f}"
                       f" (needs ≥ {rule.min_below_recent_high:.0%})"))
    else:
        checks.append((None, "no recent daily bars"))

    line_name = rule.valuation_max_line
    if c.valuation:
        line = c.valuation.line(line_name)
        checks.append((t.price <= line,
                       f"price {'at or below' if t.price <= line else 'above'} the normal valuation line {line:.2f}"))
    else:
        checks.append((None, "no valuation lines"))

    passed = [text for ok, text in checks if ok]
    if passed:
        return _ok(rid, "; ".join(passed))
    if all(ok is None for ok, _ in checks):
        return _missing(rid, rule.level, "52-week bars, recent bars and valuation lines")
    return _fail(rid, rule.level, "None of the three conditions holds: " + "; ".join(text for _, text in checks))


def _pe(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.selection_pe, "selection_pe"
    block_level = rule.tiers[0].level
    cyclical = b.valuation.is_cyclical(t.symbol)
    if c.is_fund and rule.funds_exempt:
        return _ok(rid, "Funds / ETFs skip the PE check")
    if c.valuation is None:
        kind = "price-to-book" if cyclical else "PE"
        return _missing(rid, block_level, f"{t.symbol} has no {kind} valuation lines on this day "
                                          f"(not in valuation_seed.csv, or the lines are dated later)")
    if cyclical and c.valuation.metric != b.valuation.cyclical_metric:
        return _missing(rid, block_level, f"{t.symbol} is cyclical, PE is misleading; needs price-to-book lines")
    lines = c.valuation.as_lines()
    tier = rule.tier_for(t.price, lines)
    details = {k: round(v, 2) for k, v in lines.items()} | {"valuation_as_of": c.valuation.as_of.isoformat()}
    if tier is None:
        return _ok(rid, f"Price {t.price:.2f} is at or below the normal line {lines['normal']:.2f}", **details)
    names = {"rich": "rich line", "normal": "normal line", "cheap": "cheap line", "pressure": "pressure line"}
    return _fail(rid, tier.level, f"Price {t.price:.2f} is above the {names[tier.above_line]} "
                                  f"{lines[tier.above_line]:.2f}", **details)


def _catalyst(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.selection_catalyst, "selection_catalyst"
    if not c.watch_entries:
        return _fail(rid, rule.level, f"{t.symbol} is not on the watchlist: no catalyst or invalidation registered")
    valid = [
        e for e in c.watch_entries
        if (not rule.require_invalidation or e.invalidation.strip())
        and (e.catalyst_due_on is None or e.catalyst_due_on >= c.trade_date)
    ]
    if not valid:
        dues = [e.catalyst_due_on for e in c.watch_entries if e.catalyst_due_on]
        why = f"The catalyst window has passed (due {max(dues)})" if dues else "The entry has no invalidation"
        return _fail(rid, rule.level, f"{why}; register it again")
    e = valid[-1]
    return _ok(rid, f"Catalyst: {e.catalyst}; invalidation: {e.invalidation}", watch_id=e.id)


def _planned(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.selection_planned, "selection_planned"
    if not c.watch_entries:
        return _fail(rid, rule.level, f"{t.symbol} is not on the watchlist")
    first = min(e.created_at for e in c.watch_entries)
    hours = (c.moment - first).total_seconds() / 3600
    if hours < rule.min_hours_on_watchlist:
        return _fail(rid, rule.level,
                     f"On the watchlist for only {hours:.1f} hours (needs {rule.min_hours_on_watchlist:g})",
                     hours=round(hours, 1))
    return _ok(rid, f"On the watchlist for {hours / 24:.1f} days", hours=round(hours, 1))


# ── position and cash ──────────────────────────────────────


def _position_cap(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.position_cap, "position_cap"
    if not c.nav:
        return _missing(rid, rule.level, "account net liquidation value")
    cap = rule.max_weight(t.symbol)
    after = (c.holding_quantity + t.quantity) * t.price / c.nav
    details = {"weight_after": round(after, 4), "cap": cap}
    if after > cap:
        return _fail(rid, rule.level, f"After buying, {t.symbol} would be {after:.1%}, over the {cap:.0%} cap",
                     **details)
    return _ok(rid, f"{after:.1%} after buying (cap {cap:.0%})", **details)


def _no_buy_over_cap(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rid = "no_buy_over_cap"
    level = b.no_buy_over_cap.level
    if c.holding_quantity <= 0:
        return _ok(rid, "No current position")
    if not c.nav:
        return _missing(rid, level, "account net liquidation value")
    cap = b.position_cap.max_weight(t.symbol)
    before = c.holding_quantity * t.price / c.nav
    details = {"weight_before": round(before, 4), "cap": cap}
    if before > cap:
        return _fail(rid, level, f"{t.symbol} is already {before:.1%}, over the {cap:.0%} cap; no more buying",
                     **details)
    return _ok(rid, f"Currently {before:.1%} (cap {cap:.0%})", **details)


def _cash_floor(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.cash_floor, "cash_floor"
    if c.cash is None or not c.nav:
        return _missing(rid, rule.tiers[0].level, "cash or net liquidation value")
    after = (c.cash - t.amount) / c.nav
    details = {"cash_before": round(c.cash, 2), "cash_after_ratio": round(after, 4), "nav": round(c.nav, 2)}
    tier = rule.tier_for(after)
    if tier:
        return _fail(rid, tier.level, f"Cash after buying {after:.1%}, below {tier.below:.0%}", **details)
    return _ok(rid, f"Cash after buying {after:.1%}", **details)


# ── buying ─────────────────────────────────────────────────


def _no_averaging_down(t: ProposedTrade, c: TradeContext, b: RuleBook, selection: list[RuleResult]) -> RuleResult:
    rule, rid = b.no_averaging_down, "no_averaging_down"
    if c.holding_quantity <= 0 or not c.holding_avg_cost:
        return _ok(rid, "Not adding to a position")
    change = t.price / c.holding_avg_cost - 1
    details = {"avg_cost": round(c.holding_avg_cost, 4), "unrealized": round(change, 4)}
    if change >= 0:
        return _ok(rid, f"Position up {change:.1%}", **details)
    if -change > rule.max_unrealized_loss:
        return _fail(rid, rule.level,
                     f"Down {change:.1%} (cost {c.holding_avg_cost:.2f}), more than {rule.max_unrealized_loss:.0%}; "
                     f"no adding", **details)
    if rule.losing_requires_selection_pass:
        failing = [LABELS[r.rule_id] for r in selection if r.status != "pass"]
        if failing:
            return _fail(rid, rule.level,
                         f"Down {change:.1%}; adding needs all three selection rules to pass, "
                         f"failed: {', '.join(failing)}", **details)
    return _ok(rid, f"Down {change:.1%}, within {rule.max_unrealized_loss:.0%} and all selection rules pass",
               **details)


def _earnings(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.earnings_blackout, "earnings_blackout"
    if c.is_fund:
        return _ok(rid, "Funds / ETFs have no earnings")
    if c.next_earnings is None:
        return _missing(rid, rule.level, f"the next earnings date for {t.symbol}")
    n = c.trading_days_to_earnings
    details = {"next_earnings": c.next_earnings.isoformat(), "trading_days": n}
    if n == 0 and rule.include_earnings_day:
        return _fail(rid, rule.level, f"Today ({c.next_earnings}) is the earnings day", **details)
    if 1 <= n <= rule.trading_days_before:
        return _fail(rid, rule.level,
                     f"Earnings on {c.next_earnings}, only {n} trading days away "
                     f"(no buying in the {rule.trading_days_before} trading days before earnings)", **details)
    return _ok(rid, f"Next earnings {c.next_earnings}, {n} trading days away", **details)


def _reduce_only(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.reduce_only_list, "reduce_only_list"
    if not rule.contains(t.symbol):
        return _ok(rid, "Not on the reduce-only list")
    added = rule.added_on[t.symbol]
    unmet = []
    weight = c.holding_quantity * t.price / c.nav if c.nav else None
    if weight is None or weight > rule.exit_max_weight:
        unmet.append(f"weight {weight:.1%} > {rule.exit_max_weight:.0%}" if weight is not None
                     else "no position data")
    days = (c.trade_date - added).days
    if days < rule.exit_min_days_on_list:
        unmet.append(f"on the list {max(days, 0)} days < {rule.exit_min_days_on_list} days")
    # dates are only accurate to the day: an entry registered on the day it was added counts as "after"
    if rule.exit_new_catalyst_after_added and not any(
        e.created_at.astimezone(b.calendar.tz).date() >= added for e in c.watch_entries
    ):
        unmet.append(f"no catalyst registered since {added}")
    if unmet:
        return _fail(rid, rule.level, f"{t.symbol} is on the reduce-only list; exit conditions not met: "
                                      f"{'; '.join(unmet)}")
    return _fail(rid, rule.level, f"{t.symbol} is on the reduce-only list; all exit conditions are met, "
                                  f"you can remove it in rules.yaml")


# ── pace ───────────────────────────────────────────────────


def _market_drop(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.market_drop_day, "market_drop_day"
    if c.index_change is None or (c.index_change_date != c.trade_date and not c.live):
        return _missing(rid, rule.level, f"{c.index_symbol}'s change on {c.trade_date}")
    details = {"index_change": round(c.index_change, 4), "index_change_date": c.index_change_date.isoformat()}
    if c.index_change_date != c.trade_date:
        # a proposed trade on a trading day that has not opened yet: the day's move is unknown, so let it
        # through for now; the post-trade audit recomputes it with that day's close
        return _ok(rid, f"{c.trade_date} has not opened yet (previous day {c.index_symbol} {c.index_change:+.1%}); "
                        f"the day's own move applies once it opens", **details)
    if c.index_change >= -rule.min_daily_drop:
        return _ok(rid, f"{c.index_symbol} {c.index_change:+.1%} on the day", **details)
    orders = set(c.day_buy_orders) | {t.order_id or "this"}
    count = len(orders)
    details["buy_orders_today"] = count
    if count > rule.max_buys_per_day:
        return _fail(rid, rule.level,
                     f"{c.index_symbol} {c.index_change:+.1%} on the day; at most {rule.max_buys_per_day} buy(s) "
                     f"on a drop day, this is buy #{count}", **details)
    return _ok(rid, f"{c.index_symbol} {c.index_change:+.1%} on the day; buy #{count} today "
               f"(limit {rule.max_buys_per_day})", **details)


def _weekly_budget(t: ProposedTrade, c: TradeContext, b: RuleBook) -> RuleResult:
    rule, rid = b.weekly_buy_budget, "weekly_buy_budget"
    if c.week_start_cash is None:
        return _missing(rid, rule.level, f"cash at the start of the week of {c.week_start}")
    frac = rule.max_fraction_of_week_start_cash
    frac_text = str(Fraction(frac).limit_denominator(12))
    budget = frac * c.week_start_cash
    used = c.week_buys + t.amount
    details = {"week_start": c.week_start.isoformat(), "week_start_cash": round(c.week_start_cash, 2),
               "budget": round(budget, 2), "used_after": round(used, 2)}
    if used > budget:
        return _fail(rid, rule.level,
                     f"Buys this week would reach {_money(used)}, over {frac_text} of week-start cash "
                     f"{_money(c.week_start_cash)} ({_money(budget)})", **details)
    return _ok(rid, f"This week {_money(used)} / budget {_money(budget)}", **details)


# ── entry points ───────────────────────────────────────────


def evaluate(trade: ProposedTrade, ctx: TradeContext, book: RuleBook) -> Evaluation:
    if trade.symbol != ctx.symbol:
        raise ValueError(f"The state is for {ctx.symbol} but the trade is for {trade.symbol}")
    if trade.side == "SELL":
        return Evaluation(trade, ctx, (), book.version)

    selection = [
        _near_low(trade, ctx, book),
        _pe(trade, ctx, book),
        _catalyst(trade, ctx, book),
        _planned(trade, ctx, book),
    ]
    results = selection + [
        _position_cap(trade, ctx, book),
        _no_buy_over_cap(trade, ctx, book),
        _cash_floor(trade, ctx, book),
        _no_averaging_down(trade, ctx, book, selection),
        _earnings(trade, ctx, book),
        _reduce_only(trade, ctx, book),
        _market_drop(trade, ctx, book),
        _weekly_budget(trade, ctx, book),
    ]
    return Evaluation(trade, ctx, tuple(results), book.version)


def check(
    conn: sqlite3.Connection,
    book: RuleBook,
    trade: ProposedTrade,
    at: datetime | None = None,
    *,
    trade_id: str | None = None,
    md: MarketData | None = None,
) -> Evaluation:
    """at is None: a proposed trade, on the current state. at given: the state at that moment
    (for a past trade, also pass trade_id)."""
    live = at is None
    moment = at or datetime.now(timezone.utc)
    ctx = build_context(conn, book, trade.symbol, moment, live=live, trade_id=trade_id, md=md)
    return evaluate(trade, ctx, book)


def recheck_trade(conn: sqlite3.Connection, book: RuleBook, trade_id: str, md: MarketData | None = None) -> Evaluation:
    row = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
    if row is None:
        raise KeyError(f"No trade {trade_id}")
    trade = ProposedTrade(row["symbol"], row["side"], row["quantity"], row["price"], order_id=row["order_id"])
    return check(conn, book, trade, at=parse_utc_text(row["executed_at"]), trade_id=trade_id, md=md)

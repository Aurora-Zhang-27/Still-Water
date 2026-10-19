"""The logic behind the pages, independent of Streamlit so it can be tested.

The checks table is append-only: every check is a row (including blocked ones you gave up on);
a blocked check can get an override reason once, which starts the cooldown.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from db.database import parse_utc_text, utc_text
from rules.config import CashTier, RuleBook
from rules.context import account_state
from rules.engine import LABELS, Evaluation

# remind when data is older than this many days (not a trading rule)
STALE_AFTER_DAYS = 4


# ── check records ──────────────────────────────────────────


def record_check(conn: sqlite3.Connection, ev: Evaluation) -> int:
    t = ev.trade
    with conn:
        cur = conn.execute(
            """INSERT INTO checks (created_at, symbol, side, quantity, price, verdict, results, state, rules_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                utc_text(ev.context.moment), t.symbol, t.side, t.quantity, t.price, ev.verdict,
                json.dumps([r.to_dict() for r in ev.results], ensure_ascii=False),
                json.dumps(ev.context.to_dict(), ensure_ascii=False),
                ev.rules_version,
            ),
        )
    return cur.lastrowid


def override_check(conn: sqlite3.Connection, book: RuleBook, check_id: int, reason: str, now: datetime) -> datetime:
    """Write the override reason for a blocked check; returns when the cooldown ends. The reason can
    never be changed."""
    reason = reason.strip()
    if book.override.reason_required and not reason:
        raise ValueError("The reason cannot be empty")
    row = conn.execute("SELECT verdict, override_reason FROM checks WHERE id = ?", (check_id,)).fetchone()
    if row is None:
        raise ValueError(f"No check record {check_id}")
    if row["verdict"] != "block":
        raise ValueError("Only blocked checks need an override reason")
    if row["override_reason"]:
        raise ValueError("This check already has a reason; it cannot be changed")
    until = now + timedelta(hours=book.override.cooldown_hours)
    with conn:
        conn.execute(
            "UPDATE checks SET override_reason = ?, overridden_at = ?, cooldown_until = ? WHERE id = ?",
            (reason, utc_text(now), utc_text(until), check_id),
        )
    return until


@dataclass(frozen=True)
class Cooldown:
    check_id: int
    symbol: str
    quantity: float
    price: float
    reason: str
    overridden_at: datetime
    until: datetime

    def remaining(self, now: datetime) -> timedelta:
        return max(self.until - now, timedelta(0))

    def active(self, now: datetime) -> bool:
        return self.until > now


def cooldowns(conn: sqlite3.Connection, now: datetime, ended_within_days: int = 7) -> list[Cooldown]:
    """Overrides still cooling down, plus ones that ended in the last few days."""
    rows = conn.execute(
        """SELECT id, symbol, quantity, price, override_reason, overridden_at, cooldown_until FROM checks
           WHERE override_reason IS NOT NULL AND cooldown_until >= ? ORDER BY cooldown_until""",
        (utc_text(now - timedelta(days=ended_within_days)),),
    )
    return [
        Cooldown(r["id"], r["symbol"], r["quantity"], r["price"], r["override_reason"],
                 parse_utc_text(r["overridden_at"]), parse_utc_text(r["cooldown_until"]))
        for r in rows
    ]


def recent_checks(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, created_at, symbol, side, quantity, price, verdict, override_reason, cooldown_until
           FROM checks ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


# ── overview ───────────────────────────────────────────────


@dataclass(frozen=True)
class HoldingRow:
    symbol: str
    quantity: float
    avg_cost: float | None
    price: float
    value: float
    weight: float
    cap: float
    unrealized: float | None
    reduce_only: bool

    @property
    def over_cap(self) -> bool:
        return self.weight > self.cap


@dataclass(frozen=True)
class Alert:
    level: str  # block / warn / info
    text: str


# extra violations the audit records (about buy checks) go first for each fill
AUDIT_RULES = ("unchecked_trade", "block_ignored", "cooldown_not_elapsed")
_RULE_ORDER = {rid: n for n, rid in enumerate(dict.fromkeys([*AUDIT_RULES, *LABELS]))}


@dataclass(frozen=True)
class AuditedTrade:
    """A fill with violations. The violations table has one row per rule; the page merges them into one
    row per fill so a single buy does not look like several."""
    trade_id: str | None
    symbol: str
    executed_at: datetime | None
    side: str | None
    quantity: float | None
    price: float | None
    items: list[sqlite3.Row]  # every rule this fill broke

    @staticmethod
    def is_missing(item: sqlite3.Row) -> bool:
        return bool(json.loads(item["details"] or "{}").get("missing_data"))

    def rules(self, level: str | None = None, missing: bool = False) -> list[str]:
        """Ids of broken rules. missing=True: only those caused by missing data; otherwise excluding them,
        optionally filtered by level."""
        return [i["rule_id"] for i in self.items
                if self.is_missing(i) == missing and (level is None or i["level"] == level)]


def audited_trades(conn: sqlite3.Connection, limit: int = 50) -> list[AuditedTrade]:
    """The latest limit fills with violations, newest first. Violations recorded by buy checks (no fill)
    count one each."""
    rows = conn.execute(
        """SELECT v.*, t.executed_at, t.side, t.quantity, t.price FROM violations v
           LEFT JOIN trades t ON t.trade_id = v.trade_id
           ORDER BY COALESCE(t.executed_at, v.created_at) DESC, v.id"""
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        grouped.setdefault(r["trade_id"] or f"violation-{r['id']}", []).append(r)
    trades = []
    for items in list(grouped.values())[:limit]:
        first = items[0]
        items.sort(key=lambda i: _RULE_ORDER.get(i["rule_id"], len(_RULE_ORDER)))
        trades.append(AuditedTrade(
            trade_id=first["trade_id"], symbol=first["symbol"],
            executed_at=parse_utc_text(first["executed_at"]) if first["executed_at"] else None,
            side=first["side"], quantity=first["quantity"], price=first["price"], items=items,
        ))
    return trades


@dataclass(frozen=True)
class Overview:
    as_of: date | None
    trade_date: date
    nav: float | None
    cash: float | None
    cash_ratio: float | None
    cash_tier: CashTier | None
    holdings: list[HoldingRow]
    week_start: date
    week_start_cash: float | None
    week_budget: float | None
    week_used: float
    alerts: list[Alert]
    audited: list[AuditedTrade]  # recent fills with violations, one item each
    violation_count: int  # total rows in violations (one per fill per rule)
    cooldowns: list[Cooldown]


def _price(conn: sqlite3.Connection, symbol: str, as_of: date | None, fallback: float | None) -> float:
    """Price used to value a holding: the statement's mark price → that day's close → cost.
    Same moment as the NAV snapshot."""
    if as_of:
        row = conn.execute(
            "SELECT mark_price FROM positions WHERE as_of = ? AND symbol = ? AND mark_price IS NOT NULL",
            (as_of.isoformat(), symbol),
        ).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT close FROM price_history WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (symbol, as_of.isoformat()),
        ).fetchone()
        if row:
            return row[0]
    return fallback or 0.0


def overview(conn: sqlite3.Connection, book: RuleBook, now: datetime) -> Overview:
    state = account_state(conn, book, now)
    tz = book.calendar.tz
    alerts: list[Alert] = []

    holdings = []
    for symbol, h in sorted(state.holdings.items()):
        price = _price(conn, symbol, state.snapshot_as_of, h.avg_cost)
        value = h.quantity * price
        holdings.append(HoldingRow(
            symbol=symbol,
            quantity=h.quantity,
            avg_cost=h.avg_cost,
            price=price,
            value=value,
            weight=value / state.nav if state.nav else 0.0,
            cap=book.position_cap.max_weight(symbol),
            unrealized=price / h.avg_cost - 1 if h.avg_cost else None,
            reduce_only=book.reduce_only_list.contains(symbol),
        ))
    holdings.sort(key=lambda r: -r.weight)

    tier = book.cash_floor.tier_for(state.cash_ratio) if state.cash_ratio is not None else None
    if tier:
        alerts.append(Alert(tier.level, f"Cash {state.cash_ratio:.1%}, below {tier.below:.0%}"))
    for row in holdings:
        if row.over_cap:
            alerts.append(Alert("block", f"{row.symbol} is {row.weight:.1%}, over its {row.cap:.0%} cap; no more buying"))
    loss_limit = book.no_averaging_down.max_unrealized_loss
    for row in holdings:
        if row.unrealized is not None and row.unrealized < -loss_limit:
            alerts.append(Alert("info", f"{row.symbol} is down {row.unrealized:.1%}, more than {loss_limit:.0%}; no adding"))
    ro = book.reduce_only_list
    for row in holdings:
        if row.reduce_only:
            days = (state.trade_date - ro.added_on[row.symbol]).days
            alerts.append(Alert(
                "info",
                f"{row.symbol} is on the reduce-only list: weight {row.weight:.1%} (exit needs ≤ "
                f"{ro.exit_max_weight:.0%}), on the list {max(days, 0)} days (needs {ro.exit_min_days_on_list})",
            ))
    if state.snapshot_as_of is None:
        alerts.append(Alert("warn", "No account data yet: run python -m ingest flex"))
    elif (now.astimezone(tz).date() - state.snapshot_as_of).days > STALE_AFTER_DAYS:
        alerts.append(Alert("warn", f"Account data stops at {state.snapshot_as_of}; run python -m ingest flex"))
    for note in state.notes:
        alerts.append(Alert("info", note))

    frac = book.weekly_buy_budget.max_fraction_of_week_start_cash
    return Overview(
        as_of=state.snapshot_as_of,
        trade_date=state.trade_date,
        nav=state.nav,
        cash=state.cash,
        cash_ratio=state.cash_ratio,
        cash_tier=tier,
        holdings=holdings,
        week_start=state.week_start,
        week_start_cash=state.week_start_cash,
        week_budget=frac * state.week_start_cash if state.week_start_cash is not None else None,
        week_used=state.week_buys,
        alerts=alerts,
        audited=audited_trades(conn),
        violation_count=conn.execute("SELECT count(*) FROM violations").fetchone()[0],
        cooldowns=cooldowns(conn, now),
    )

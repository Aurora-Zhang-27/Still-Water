"""Statistics for the weekly report and the review: deduplicated checks, compliance rate, returns of
compliant vs. violating trades, how blocked trades did afterwards, monthly reports."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from audit.audit import CHECK_VIOLATIONS
from db.database import parse_utc_text, utc_text
from ingest.market import MarketData
from rules.calendar import trade_date_for
from rules.config import RuleBook

GROUPS = ("Compliant", "Violation", "Missing data only")


# ── deduplicating checks ───────────────────────────────────


@dataclass(frozen=True)
class Intent:
    """One buy intent: several checks of the same stock on the same trading day count once
    (trying different quantities counts once)."""

    symbol: str
    trade_date: date
    check_ids: tuple[int, ...]
    check_id: int  # the check representing the group: one with a reason first, else the last one
    verdict: str
    price: float
    quantity: float
    created_at: datetime
    overridden: bool
    traded: bool  # a fill matched one of the group's checks


def check_intents(conn: sqlite3.Connection, book: RuleBook, start: date, end: date) -> list[Intent]:
    """Buy intents in [start, end) (US Eastern trading days)."""
    tz = book.calendar.tz
    lo = datetime.combine(start, time(), tzinfo=tz)
    hi = datetime.combine(end, time(), tzinfo=tz)
    rows = conn.execute(
        """SELECT id, created_at, symbol, quantity, price, verdict, override_reason FROM checks
           WHERE side = 'BUY' AND created_at >= ? AND created_at < ? ORDER BY created_at, id""",
        (utc_text(lo), utc_text(hi)),
    ).fetchall()
    linked = {r[0] for r in conn.execute("SELECT check_id FROM trade_audits WHERE check_id IS NOT NULL")}

    groups: dict[tuple[str, date], list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((r["symbol"], trade_date_for(parse_utc_text(r["created_at"]), tz)), []).append(r)

    intents = []
    for (symbol, day), members in groups.items():
        rep = next((m for m in reversed(members) if m["override_reason"]), members[-1])
        ids = tuple(m["id"] for m in members)
        intents.append(Intent(
            symbol=symbol, trade_date=day, check_ids=ids, check_id=rep["id"], verdict=rep["verdict"],
            price=rep["price"], quantity=rep["quantity"], created_at=parse_utc_text(rep["created_at"]),
            overridden=bool(rep["override_reason"]), traded=any(i in linked for i in ids),
        ))
    return sorted(intents, key=lambda i: i.created_at)


@dataclass(frozen=True)
class Compliance:
    blocked: int  # intents that were blocked
    complied: int  # complied: did not buy
    broke: int  # broke the block: bought (with or without a reason)
    overridden: int  # of those, with a reason
    passed: int  # intents that passed or only warned

    @property
    def rate(self) -> float | None:
        return self.complied / self.blocked if self.blocked else None


def compliance(intents: list[Intent]) -> Compliance:
    blocked = [i for i in intents if i.verdict == "block"]
    return Compliance(
        blocked=len(blocked),
        complied=sum(not i.traded for i in blocked),
        broke=sum(i.traded for i in blocked),
        overridden=sum(i.overridden for i in blocked),
        passed=len(intents) - len(blocked),
    )


# ── returns ────────────────────────────────────────────────


def _close_on_or_before(md: MarketData, symbol: str, day: date) -> float | None:
    bars = md.bars(symbol, day - timedelta(days=10), day)
    return bars[-1].close if bars else None


def _latest_close(md: MarketData, symbol: str, as_of: date) -> tuple[date, float] | None:
    bars = md.bars(symbol, as_of - timedelta(days=14), as_of)
    return (bars[-1].date, bars[-1].close) if bars else None


@dataclass(frozen=True)
class Outcome:
    label: str  # ticker + date
    symbol: str
    day: date
    entry: float
    latest: float
    ret: float
    index_ret: float | None

    @property
    def excess(self) -> float | None:
        return self.ret - self.index_ret if self.index_ret is not None else None


def _outcome(md: MarketData, book: RuleBook, symbol: str, day: date, entry: float, as_of: date) -> Outcome | None:
    last = _latest_close(md, symbol, as_of)
    if not last or entry <= 0:
        return None
    index = book.market_drop_day.index_symbol
    idx_then, idx_now = _close_on_or_before(md, index, day), _latest_close(md, index, as_of)
    index_ret = idx_now[1] / idx_then - 1 if idx_then and idx_now else None
    return Outcome(f"{symbol} {day:%m-%d}", symbol, day, entry, last[1], last[1] / entry - 1, index_ret)


def audit_group(results_json: str, check_status: str) -> str:
    """Violation: a block not caused by missing data, or a check-related violation.
    Missing data only: every block was caused by missing data."""
    results = json.loads(results_json)
    if check_status in CHECK_VIOLATIONS or any(r["status"] == "block" and not r["missing_data"] for r in results):
        return "Violation"
    if any(r["status"] == "block" for r in results):
        return "Missing data only"
    return "Compliant"


def buy_outcomes(
    conn: sqlite3.Connection, book: RuleBook, md: MarketData, period: str, as_of: date
) -> dict[str, list[Outcome]]:
    """Returns of audited buys (buy price → latest close before as_of), grouped as compliant /
    violation / missing data only, with the index over the same period."""
    tz = book.calendar.tz
    rows = conn.execute(
        """SELECT t.symbol, t.price, t.executed_at, a.results, a.check_status FROM trade_audits a
           JOIN trades t ON t.trade_id = a.trade_id WHERE t.side = 'BUY' AND a.period = ?
           ORDER BY t.executed_at""",
        (period,),
    )
    groups: dict[str, list[Outcome]] = {g: [] for g in GROUPS}
    for r in rows:
        day = trade_date_for(parse_utc_text(r["executed_at"]), tz)
        outcome = _outcome(md, book, r["symbol"], day, r["price"], as_of)
        if outcome:
            groups[audit_group(r["results"], r["check_status"])].append(outcome)
    return groups


def blocked_not_bought(md: MarketData, book: RuleBook, intents: list[Intent], as_of: date) -> list[Outcome]:
    """Blocked intents you did not buy: did they rise or fall afterwards (from the price entered in the check)."""
    out = []
    for i in intents:
        if i.verdict == "block" and not i.traded:
            outcome = _outcome(md, book, i.symbol, i.trade_date, i.price, as_of)
            if outcome:
                out.append(outcome)
    return out


def average(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


# ── monthly report ─────────────────────────────────────────


@dataclass(frozen=True)
class MonthRow:
    month: str  # 2026-10
    buys: int
    groups: dict[str, int]  # compliant / violation / missing data only → count
    top_rules: list[tuple[str, int]]  # most-broken rules (excluding missing data)
    avg_ret: float | None  # average return from the buy to as_of
    avg_excess: float | None  # vs. QQQ over the same period
    compliance: Compliance


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def violations_by_rule(conn: sqlite3.Connection, since: date | None = None, book: RuleBook | None = None) -> Counter:
    """Test-period violations counted by rule (excluding missing data); with since, only fills from that day on."""
    counts: Counter = Counter()
    rows = conn.execute(
        """SELECT v.rule_id, v.details, t.executed_at FROM violations v JOIN trades t ON t.trade_id = v.trade_id"""
    )
    for r in rows:
        if json.loads(r["details"] or "{}").get("missing_data"):
            continue
        if since and book and trade_date_for(parse_utc_text(r["executed_at"]), book.calendar.tz) < since:
            continue
        counts[r["rule_id"]] += 1
    return counts


def monthly(conn: sqlite3.Connection, book: RuleBook, md: MarketData, as_of: date) -> list[MonthRow]:
    """One row per month of the test period (months by US Eastern trading day)."""
    tz = book.calendar.tz
    rows = conn.execute(
        """SELECT t.trade_id, t.symbol, t.price, t.executed_at, a.results, a.check_status FROM trade_audits a
           JOIN trades t ON t.trade_id = a.trade_id WHERE t.side = 'BUY' AND a.period = 'live'
           ORDER BY t.executed_at"""
    ).fetchall()
    rule_rows = conn.execute(
        """SELECT v.rule_id, v.details, t.executed_at FROM violations v JOIN trades t ON t.trade_id = v.trade_id"""
    ).fetchall()

    buckets: dict[str, dict] = {}
    for r in rows:
        day = trade_date_for(parse_utc_text(r["executed_at"]), tz)
        b = buckets.setdefault(f"{day:%Y-%m}", {"start": day.replace(day=1), "groups": Counter(), "outcomes": []})
        b["groups"][audit_group(r["results"], r["check_status"])] += 1
        outcome = _outcome(md, book, r["symbol"], day, r["price"], as_of)
        if outcome:
            b["outcomes"].append(outcome)
    rules: dict[str, Counter] = {}
    for v in rule_rows:
        if json.loads(v["details"] or "{}").get("missing_data"):
            continue
        day = trade_date_for(parse_utc_text(v["executed_at"]), tz)
        rules.setdefault(f"{day:%Y-%m}", Counter())[v["rule_id"]] += 1

    out = []
    for month, b in sorted(buckets.items()):
        outcomes = b["outcomes"]
        out.append(MonthRow(
            month=month,
            buys=sum(b["groups"].values()),
            groups={g: b["groups"][g] for g in GROUPS},
            top_rules=rules.get(month, Counter()).most_common(3),
            avg_ret=average([o.ret for o in outcomes]),
            avg_excess=average([o.excess for o in outcomes]),
            compliance=compliance(check_intents(conn, book, b["start"], _next_month(b["start"]))),
        ))
    return out

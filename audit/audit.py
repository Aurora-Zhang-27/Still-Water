"""Post-trade audit: re-run the rules for every fill on the data at fill time, and match it with buy checks.

Results go to trade_audits (append-only). For fills in the test period (from audit.start_date),
violations are also written to violations; fills before go-live are audited only as a baseline.

Matching a buy fill with a check (by "decision time": the order time, else the fill time):
  - the check must cover at least the filled quantity (no checking 1 share and buying 100)
  - a passed / warned check: within check_valid_days before the order
  - a blocked check with a reason: ordered within check_valid_days after the cooldown ends;
    ordered during the cooldown → cooldown not over
  - a blocked check without a reason: ordered within check_valid_days → bought after block
  - none of these → unchecked
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from db.database import parse_utc_text, utc_text
from ingest.market import MarketData
from rules.calendar import trade_date_for
from rules.config import RuleBook
from rules.engine import recheck_trade

CHECK_VIOLATIONS = {"missing": "unchecked_trade", "block_ignored": "block_ignored",
                    "cooldown_not_elapsed": "cooldown_not_elapsed"}
# when several checks match, take the one most favourable to you
_PREFERENCE = ("passed", "overridden", "cooldown_not_elapsed", "block_ignored")


@dataclass(frozen=True)
class CheckMatch:
    status: str
    check_id: int | None
    message: str | None = None  # explanation when there is a violation


@dataclass(frozen=True)
class Violation:
    rule_id: str
    level: str
    message: str
    missing_data: bool = False


@dataclass(frozen=True)
class AuditResult:
    trade_id: str
    symbol: str
    side: str
    executed_at: datetime
    period: str
    verdict: str
    check_status: str
    check_id: int | None
    violations: list[Violation] = field(default_factory=list)


def match_check(
    conn: sqlite3.Connection, book: RuleBook, symbol: str, quantity: float, decision_at: datetime
) -> CheckMatch:
    tz = book.calendar.tz
    window = timedelta(days=book.audit.check_valid_days)
    earliest = decision_at - window - timedelta(hours=book.override.cooldown_hours)
    rows = conn.execute(
        """SELECT id, created_at, quantity, verdict, override_reason, cooldown_until FROM checks
           WHERE symbol = ? AND side = 'BUY' AND created_at <= ? AND created_at >= ?
           ORDER BY created_at DESC, id DESC""",
        (symbol, utc_text(decision_at), utc_text(earliest)),
    ).fetchall()

    found: dict[str, CheckMatch] = {}
    uncovered = 0
    for r in rows:
        created = parse_utc_text(r["created_at"])
        if r["quantity"] < quantity:
            uncovered += 1
            continue
        if r["verdict"] in ("pass", "warn"):
            if decision_at - created <= window:
                found.setdefault("passed", CheckMatch("passed", r["id"]))
        elif r["override_reason"]:
            until = parse_utc_text(r["cooldown_until"])
            if decision_at < until:
                early = (until - decision_at).total_seconds() / 3600
                found.setdefault("cooldown_not_elapsed", CheckMatch(
                    "cooldown_not_elapsed", r["id"],
                    f"Check #{r['id']} cooled down until {until.astimezone(tz):%m-%d %H:%M} ET; "
                    f"ordered {early:.1f} hours early"))
            elif decision_at - until <= window:
                found.setdefault("overridden", CheckMatch("overridden", r["id"]))
        elif decision_at - created <= window:
            found.setdefault("block_ignored", CheckMatch(
                "block_ignored", r["id"], f"Check #{r['id']} was blocked; ordered without writing a reason"))

    for status in _PREFERENCE:
        if status in found:
            return found[status]
    note = f" ({uncovered} check(s) covered fewer shares than this fill)" if uncovered else ""
    return CheckMatch("missing", None,
                      f"No buy check covering this fill in the {book.audit.check_valid_days} days before the "
                      f"order{note}; ordered {decision_at.astimezone(tz):%m-%d %H:%M} ET")


def audit_trade(
    conn: sqlite3.Connection, book: RuleBook, trade_id: str, md: MarketData | None = None
) -> AuditResult:
    row = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
    executed_at = parse_utc_text(row["executed_at"])
    decision_at = parse_utc_text(row["order_placed_at"]) if row["order_placed_at"] else executed_at
    period = "live" if trade_date_for(executed_at, book.calendar.tz) >= book.audit.start_date else "baseline"
    ev = recheck_trade(conn, book, trade_id, md=md)

    if row["side"] == "BUY":
        match = match_check(conn, book, row["symbol"], row["quantity"], decision_at)
    else:
        match = CheckMatch("not_needed", None)

    violations = [
        Violation(r.rule_id, r.status, r.reason, r.missing_data) for r in ev.results if r.status in ("block", "warn")
    ]
    if match.status in CHECK_VIOLATIONS:
        violations.append(Violation(CHECK_VIOLATIONS[match.status], "block", match.message))

    with conn:
        conn.execute(
            """INSERT INTO trade_audits (trade_id, period, verdict, results, state, decision_at, check_id,
                                         check_status, rules_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, period, ev.verdict, json.dumps([r.to_dict() for r in ev.results], ensure_ascii=False),
             json.dumps(ev.context.to_dict(), ensure_ascii=False), utc_text(decision_at), match.check_id,
             match.status, book.version),
        )
        if period == "live":
            conn.executemany(
                """INSERT OR IGNORE INTO violations (detected_by, rule_id, level, symbol, trade_id, check_id,
                                                     message, details, rules_version)
                   VALUES ('audit', ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (v.rule_id, v.level, row["symbol"], trade_id, match.check_id, v.message,
                     json.dumps({"missing_data": v.missing_data}, ensure_ascii=False), book.version)
                    for v in violations
                ],
            )

    return AuditResult(trade_id, row["symbol"], row["side"], executed_at, period, ev.verdict,
                       match.status, match.check_id, violations if period == "live" else [])


def unaudited_trade_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT t.trade_id FROM trades t LEFT JOIN trade_audits a ON a.trade_id = t.trade_id
           WHERE t.asset_class = 'STK' AND a.trade_id IS NULL ORDER BY t.executed_at, t.trade_id"""
    )
    return [r[0] for r in rows]


def audit_new_trades(conn: sqlite3.Connection, book: RuleBook, md: MarketData | None = None) -> list[AuditResult]:
    """Audit every stock fill not audited yet (FX and other fills are skipped). Safe to run again."""
    md = md or MarketData(conn, tz=book.calendar.tz)
    return [audit_trade(conn, book, trade_id, md=md) for trade_id in unaudited_trade_ids(conn)]

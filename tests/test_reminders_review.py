"""Sell reminders and review statistics (sample history)."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from audit.audit import audit_new_trades
from audit.reminders import holding_status, invalidation_status, reminders
from audit.stats import monthly, violations_by_rule
from db.records import add_watch
from ingest.market import MarketData
from ingest.valuation import import_seed
from rules.config import load_rules
from tests.conftest import SEED, TEST_RULES, close_on, position

BOOK = load_rules(TEST_RULES)  # the test period starts 2026-10-01
NOW = datetime(2026, 10, 18, 16, tzinfo=timezone.utc)
AS_OF = date(2026, 10, 16)


class NoNetwork:
    def fundamentals(self, symbol):
        return {}


def test_holding_status(sample_db):
    conn, _ = sample_db
    md = MarketData(conn, NoNetwork())
    rows = {s.symbol: s for s in holding_status(conn, BOOK, md, NOW, AS_OF)}
    assert set(rows) == {"ORCA", "QQQ", "PINE", "KITE", "MAPL"}  # the fixture only has bars for these

    orca, cost = rows["ORCA"], position("ORCA")["avg_cost"]
    assert orca.gain == pytest.approx(close_on("ORCA", "2026-10-16") / cost - 1, abs=1e-4)
    assert (orca.reached, orca.next_level) == (None, 0.5)
    assert orca.next_level_price == pytest.approx(cost * 1.5, abs=0.01)
    assert orca.zone is None  # no valuation lines imported yet

    import_seed(conn, BOOK, SEED, md, date(2026, 10, 18))
    rows = {s.symbol: s for s in holding_status(conn, BOOK, md, NOW, AS_OF)}
    assert rows["ORCA"].zone == "Below cheap" and not rows["ORCA"].above_rich


def test_take_profit_reminder_when_gain_crosses_a_level(sample_db):
    conn, _ = sample_db
    md = MarketData(conn, NoNetwork())
    # QQQ is up about 15%: lower the first level to +10% to see the reminder
    lower = replace(BOOK, take_profit_reminder=replace(BOOK.take_profit_reminder, gain_levels=(0.10, 0.50)))
    statuses = holding_status(conn, lower, md, NOW, AS_OF)
    gain = close_on("QQQ", "2026-10-16") / position("QQQ")["avg_cost"] - 1
    texts = [r.text for r in reminders(lower, statuses, [])]
    assert f"QQQ is up {gain:+.0%}, past the +10% take-profit level" in texts
    assert reminders(BOOK, holding_status(conn, BOOK, md, NOW, AS_OF), []) == []


def test_invalidation_status(sample_db):
    conn, _ = sample_db
    md = MarketData(conn, NoNetwork())
    add_watch(conn, "ORCA", "January earnings", "falls below 320", invalidation_price=320, invalidation_when="below")
    add_watch(conn, "KITE", "10/28 earnings", "falls below 100", invalidation_price=100, invalidation_when="below")
    add_watch(conn, "MAPL", "new product cycle", "guidance cut")
    items = {w.symbol: w for w in invalidation_status(conn, md, AS_OF, {"ORCA", "KITE", "MAPL"})}
    assert items["ORCA"].triggered and items["ORCA"].distance < 0
    kite = close_on("KITE", "2026-10-16")
    assert not items["KITE"].triggered and items["KITE"].distance == pytest.approx(kite / 100 - 1, abs=1e-3)
    assert items["MAPL"].target is None and not items["MAPL"].triggered  # event-based: the program does not judge
    texts = [r.text for r in reminders(BOOK, [], list(items.values()))]
    orca = close_on("ORCA", "2026-10-16")
    assert texts == [f"ORCA invalidation triggered: falls below 320 (price {orca:,.2f}); needs a review"]


def test_monthly_report_and_rule_counts(sample_db):
    conn, _ = sample_db
    audit_new_trades(conn, BOOK)
    md = MarketData(conn, NoNetwork())
    [october] = monthly(conn, BOOK, md, AS_OF)
    assert october.month == "2026-10"
    assert october.buys == 14 and october.groups["Violation"] == 14
    assert october.top_rules[0][0] in {"unchecked_trade", "selection_catalyst", "selection_planned"}
    assert october.avg_excess is not None
    assert october.compliance.blocked == 0  # no buy checks in October yet

    counts = violations_by_rule(conn)
    assert counts["unchecked_trade"] == 14
    assert "selection_pe" not in counts  # blocks caused by missing valuation lines do not count
    assert violations_by_rule(conn, since=date(2026, 10, 16), book=BOOK)["unchecked_trade"] == 3

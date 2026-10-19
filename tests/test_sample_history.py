"""The rule engine on the sample history (fixtures/sample_history.json, see make_sample_history.py).

Same path as a real run: data in SQLite → state rebuilt at the fill time → every rule judged.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from db.database import init_db
from rules.config import load_rules
from rules.context import load_fills
from rules.engine import ProposedTrade, check, recheck_trade
from rules.portfolio import replay
from tests.conftest import SAMPLE as DATA
from tests.conftest import TEST_RULES, close_on, load_sample_history

BOOK = load_rules(TEST_RULES)
ET = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    c = init_db(tmp_path_factory.mktemp("sample") / "sample.db")
    load_sample_history(c)
    yield c
    c.close()


def buys(conn, symbol, start, end):
    """Buy fill ids for a ticker between start and end (US Eastern dates, inclusive)."""
    rows = conn.execute(
        "SELECT trade_id, executed_at FROM trades WHERE symbol = ? AND side = 'BUY' ORDER BY executed_at", (symbol,)
    )
    out = []
    for r in rows:
        day = datetime.strptime(r["executed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ET).date()
        if start <= day.isoformat() <= end:
            out.append(r["trade_id"])
    return out


def test_fifo_replay_matches_the_statement_positions(conn):
    holdings, anomalies = replay(load_fills(conn))
    assert anomalies == []  # OLDC → NEWC is merged by contract id, so the NEWC sells are not oversold
    assert sorted(holdings) == sorted(p["symbol"] for p in DATA["positions"])
    for p in DATA["positions"]:
        h = holdings[p["symbol"]]
        assert h.quantity == pytest.approx(p["quantity"])
        assert h.avg_cost == pytest.approx(p["avg_cost"], abs=1e-3), p["symbol"]


def test_pine_averaging_down_in_mid_october(conn):
    ids = buys(conn, "PINE", "2026-10-12", "2026-10-15")
    assert len(ids) == 3
    for trade_id in ids:
        ev = recheck_trade(conn, BOOK, trade_id)
        down = ev.result("no_averaging_down")
        assert down.status == "block", down.reason
        assert down.details["unrealized"] < -0.10
        assert ev.result("reduce_only_list").status == "block"


def test_kite_averaging_down_on_oct_16(conn):
    [trade_id] = buys(conn, "KITE", "2026-10-16", "2026-10-16")
    ev = recheck_trade(conn, BOOK, trade_id)
    down = ev.result("no_averaging_down")
    assert down.status == "block"
    before = [t for t in DATA["trades"] if t["symbol"] == "KITE" and t["trade_id"] < trade_id]
    cost = sum(t["quantity"] * t["price"] - t["commission"] for t in before) / sum(t["quantity"] for t in before)
    assert down.details["unrealized"] == pytest.approx(121 / cost - 1, abs=1e-3)
    assert ev.result("reduce_only_list").status == "block"


def test_mapl_buy_on_oct_16_breaks_cash_floor(conn):
    [trade_id] = buys(conn, "MAPL", "2026-10-16", "2026-10-16")
    ev = recheck_trade(conn, BOOK, trade_id)
    floor = ev.result("cash_floor")
    assert floor.status == "block", floor.reason
    assert floor.details["cash_after_ratio"] < 0.05
    as_of, cash, nav = DATA["snapshots"][-1]
    assert as_of == "2026-10-16" and cash / nav < 0.05


def test_orca_over_its_cap_cannot_be_added(conn):
    # after the 10/16 after-hours session (an order now lands on 10/19), try to buy 1 more ORCA
    after_close = datetime(2026, 10, 16, 20, 30, tzinfo=ET).astimezone(timezone.utc)
    ev = check(conn, BOOK, ProposedTrade("ORCA", "BUY", 1, 300.0), at=after_close)
    assert ev.context.holding_quantity == 30
    over = ev.result("no_buy_over_cap")
    assert over.status == "block"
    assert over.details["weight_before"] > 0.16
    assert ev.result("position_cap").status == "block"


def test_buying_into_the_oct_16_drop(conn):
    # QQQ falls 2.5% that day: the first buy (KITE) is allowed, the QQQ and MAPL buys after it are blocked
    order = []
    for symbol in ("KITE", "QQQ", "MAPL"):
        [trade_id] = buys(conn, symbol, "2026-10-16", "2026-10-16")
        ev = recheck_trade(conn, BOOK, trade_id)
        order.append((ev.context.moment, symbol, ev.result("market_drop_day").status))
    order.sort()
    assert [s for _, _, s in order] == ["pass", "block", "block"]
    assert ev.context.index_change == pytest.approx(close_on("QQQ", "2026-10-16") / close_on("QQQ", "2026-10-15") - 1)
    assert ev.context.index_change == pytest.approx(-0.025, abs=0.001)


def test_weekly_budget_blown_in_week_of_oct_12(conn):
    [trade_id] = buys(conn, "MAPL", "2026-10-16", "2026-10-16")
    weekly = recheck_trade(conn, BOOK, trade_id).result("weekly_buy_budget")
    assert weekly.status == "warn"
    assert weekly.details["used_after"] > 2 * weekly.details["budget"]

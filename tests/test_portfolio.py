from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from rules.portfolio import Fill, replay

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def fill(n, side, qty, price, commission=-1.0, symbol="PINE"):
    return Fill(f"T{n:03d}", symbol, side, qty, price, commission, None, T0 + timedelta(hours=n), None)


def test_fifo_sell_removes_oldest_lot_first():
    holdings, anomalies = replay([
        fill(1, "BUY", 5, 210),
        fill(2, "BUY", 3, 198),
        fill(3, "SELL", 2, 205),
    ])
    pine = holdings["PINE"]
    assert pine.quantity == 6
    # 3 shares at 210 + 3 at 198 remain, with commissions spread into the cost
    assert pine.avg_cost == pytest.approx((3 * (210 + 1 / 5) + 3 * (198 + 1 / 3)) / 6)
    assert anomalies == []


def test_order_of_input_does_not_matter():
    fills = [fill(1, "BUY", 5, 100), fill(2, "SELL", 5, 110), fill(3, "BUY", 2, 90)]
    holdings, _ = replay(reversed(fills))
    assert holdings["PINE"].quantity == 2
    assert holdings["PINE"].avg_cost == pytest.approx(90.5)


def test_closed_positions_disappear():
    holdings, _ = replay([fill(1, "BUY", 5, 100), fill(2, "SELL", 5, 110)])
    assert "PINE" not in holdings


def test_oversell_is_reported():
    holdings, anomalies = replay([fill(1, "BUY", 30, 40, symbol="NEWC"), fill(2, "SELL", 52, 44, symbol="NEWC")])
    assert "NEWC" not in holdings
    assert len(anomalies) == 1 and "22" in anomalies[0]


def test_ticker_change_is_merged_by_contract_id():
    # buy 30 OLDC → the ticker becomes NEWC → buy 22 more → sell 26 twice
    fills = [
        replace(fill(1, "BUY", 30, 40.0, symbol="OLDC"), contract_id="900010"),
        replace(fill(2, "BUY", 22, 47.5, symbol="NEWC"), contract_id="900010"),
        replace(fill(3, "SELL", 26, 44.0, symbol="NEWC"), contract_id="900010"),
        replace(fill(4, "SELL", 26, 44.0, symbol="NEWC"), contract_id="900010"),
    ]
    holdings, anomalies = replay(fills[:2])
    assert set(holdings) == {"NEWC"} and holdings["NEWC"].quantity == 52
    holdings, anomalies = replay(fills)
    assert holdings == {} and anomalies == []


def test_cash_effect():
    buy = Fill("T1", "MAPL", "BUY", 3, 120.50, -1.0, -361.50, T0, None)
    sell = Fill("T2", "SAGE", "SELL", 4, 88.25, -1.0, None, T0, None)
    assert buy.cash_effect == pytest.approx(-362.50)
    assert sell.cash_effect == pytest.approx(4 * 88.25 - 1)

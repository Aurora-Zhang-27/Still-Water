"""Flex parsing, tested on a made-up example XML."""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ingest.flex import FlexError, parse_flex_datetime, parse_statements

SAMPLE = Path(__file__).parent / "fixtures" / "flex_activity_sample.xml"


@pytest.fixture(scope="module")
def stmt():
    [statement] = parse_statements(SAMPLE.read_bytes())
    return statement


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_statement_header(stmt):
    assert stmt.account_id == "U0000000"
    assert (stmt.from_date, stmt.to_date) == (date(2026, 10, 15), date(2026, 10, 16))
    assert {"OpenPositions", "Trades", "CashReport", "EquitySummaryInBase"} <= stmt.sections


def test_positions_use_summary_rows_only(stmt):
    by_symbol = {p.symbol: p for p in stmt.positions}
    assert sorted(by_symbol) == ["KITE", "MAPL", "ORCA", "PINE", "QQQ"]  # ORCA's LOT row is not counted twice
    kite = by_symbol["KITE"]
    assert kite.as_of == "2026-10-16"
    assert (kite.quantity, kite.avg_cost, kite.mark_price) == (12, 805.0, 700.0)
    assert (kite.market_value, kite.unrealized_pnl) == (8400.0, -1260.0)


def test_trades_skip_order_rows_and_cancellations(stmt):
    ids = sorted(t.trade_id for t in stmt.trades)
    assert ids == ["9000000001", "9000000002", "9000000003", "9000000004"]
    assert any("Cancelled" in note and "9000000005" in note for note in stmt.skipped)


def test_trade_fields(stmt):
    trades = {t.trade_id: t for t in stmt.trades}
    mapl = trades["9000000001"]
    assert (mapl.symbol, mapl.side, mapl.quantity, mapl.price) == ("MAPL", "BUY", 40, 225.5)
    assert mapl.commission == -1.0
    # 10:30:12 ET (daylight time, UTC-4) → 14:30:12 UTC
    assert mapl.executed_at == utc(2026, 10, 16, 14, 30, 12)

    orca = trades["9000000004"]
    assert (orca.side, orca.quantity) == ("SELL", 5)  # -5 in Flex

    kite = trades["9000000002"]
    assert kite.order_placed_at == utc(2026, 10, 9, 13, 15, 0)  # an order placed in advance
    assert kite.order_placed_at.date() < kite.executed_at.date()


def test_snapshots_from_daily_equity_summary(stmt):
    snaps = {s.as_of: s for s in stmt.snapshots}
    assert sorted(snaps) == ["2026-10-15", "2026-10-16"]
    assert (snaps["2026-10-16"].cash, snaps["2026-10-16"].net_liquidation) == (3000.0, 62000.0)
    assert (snaps["2026-10-15"].cash, snaps["2026-10-15"].net_liquidation) == (12000.0, 61500.0)


def test_snapshot_falls_back_to_cash_report():
    root = ET.fromstring(SAMPLE.read_bytes())
    statement_el = root.find(".//FlexStatement")
    statement_el.remove(statement_el.find("EquitySummaryInBase"))
    [stmt] = parse_statements(ET.tostring(root))
    [snap] = stmt.snapshots
    assert snap.as_of == "2026-10-16"
    assert snap.cash == 3000.0
    assert snap.net_liquidation == 3000.0 + 59000.0  # cash + position market value


def test_account_must_match_env():
    with pytest.raises(FlexError, match="does not match"):
        parse_statements(SAMPLE.read_bytes(), expected_account="U9999999")
    parse_statements(SAMPLE.read_bytes(), expected_account="U0000000")


def test_error_response():
    body = (
        "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
        "<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
    )
    with pytest.raises(FlexError) as exc:
        parse_statements(body)
    assert exc.value.code == "1012"


def test_csv_format_gets_a_hint():
    with pytest.raises(FlexError, match="XML"):
        parse_statements('"ClientAccountID","Symbol"\n"U0000000","ORCA"\n')


@pytest.mark.parametrize(
    "value",
    ["20261016;103012", "20261016 103012", "2026-10-16;10:30:12", "2026-10-16, 10:30:12", "10/16/2026 10:30:12"],
)
def test_flex_datetime_formats(value):
    assert parse_flex_datetime(value) == utc(2026, 10, 16, 14, 30, 12)


def test_winter_time_offset():
    # after daylight time ends in early November it is UTC-5
    assert parse_flex_datetime("20261110;100000") == utc(2026, 11, 10, 15, 0, 0)

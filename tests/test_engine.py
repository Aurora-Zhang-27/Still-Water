"""Unit tests per rule: hand-built states, testing evaluate() only (no database).

In the base state a ZEST buy passes everything; each test changes one or two fields.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from rules.config import load_rules
from rules.context import TradeContext, ValuationLines, WatchEntry
from rules.engine import ProposedTrade, evaluate
from tests.conftest import TEST_RULES

BOOK = load_rules(TEST_RULES)
MOMENT = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
LINES = ValuationLines(date(2026, 10, 19), "pe", pressure=75, cheap=90, normal=110, rich=130)
WATCH = WatchEntry(1, MOMENT - timedelta(days=10), "November earnings guidance raised", None, "falls below 85")


def ctx(**changes) -> TradeContext:
    base = TradeContext(
        symbol="ZEST", moment=MOMENT, trade_date=date(2026, 10, 20), live=False,
        snapshot_as_of=date(2026, 10, 19), nav=100_000, cash=30_000,
        holding_quantity=0, holding_avg_cost=None,
        week_start=date(2026, 10, 19), week_start_cash=30_000, week_buys=0, day_buy_orders=frozenset(),
        index_symbol="QQQ", index_change=0.005, index_change_date=date(2026, 10, 20),
        fifty_two_week_low=90, recent_high=130, recent_high_days=60, valuation=LINES,
        next_earnings=date(2026, 11, 20), trading_days_to_earnings=22, is_fund=False,
        watch_entries=(WATCH,), notes=(),
    )
    return replace(base, **changes)


def buy(qty=10, price=100.0, symbol="ZEST", order_id=None):
    return ProposedTrade(symbol, "BUY", qty, price, order_id=order_id)


def run(trade=None, **changes):
    trade = trade or buy()
    return evaluate(trade, ctx(symbol=trade.symbol, **changes), BOOK)


def status(ev, rule_id):
    return ev.result(rule_id).status


def test_baseline_passes_everything():
    ev = run()
    assert ev.verdict == "pass"
    assert len(ev.results) == 12
    assert {r.status for r in ev.results} == {"pass"}


def test_sell_is_not_checked():
    ev = evaluate(ProposedTrade("ZEST", "SELL", 10, 100), ctx(), BOOK)
    assert ev.results == () and ev.verdict == "pass"


def test_state_must_match_symbol():
    with pytest.raises(ValueError):
        evaluate(buy(symbol="AMD"), ctx(), BOOK)


# ── near a low ─────────────────────────────────────────────


def test_near_low_any_one_condition_is_enough():
    # far from the 52-week low, not far enough below the high, but under the normal line
    ev = run(fifty_two_week_low=50, recent_high=110)
    assert status(ev, "selection_near_low") == "pass"
    assert "normal valuation line" in ev.result("selection_near_low").reason


def test_near_low_fails_when_none_hold():
    ev = run(buy(price=115), fifty_two_week_low=50, recent_high=120)
    r = ev.result("selection_near_low")
    assert r.status == "block" and "None of the three conditions" in r.reason and not r.missing_data


def test_near_low_counts_todays_price_as_a_new_low():
    assert status(run(buy(price=80), fifty_two_week_low=90, recent_high=90, valuation=None), "selection_near_low") == "pass"


def test_near_low_missing_all_data():
    r = run(fifty_two_week_low=None, recent_high=None, valuation=None).result("selection_near_low")
    assert r.status == "block" and r.missing_data


# ── PE ──────────────────────────────────────────────────────


@pytest.mark.parametrize("price, expected", [(110, "pass"), (120, "warn"), (130, "warn"), (131, "block")])
def test_pe_tiers(price, expected):
    assert status(run(buy(price=price)), "selection_pe") == expected


def test_pe_missing_valuation_blocks():
    r = run(valuation=None).result("selection_pe")
    assert r.status == "block" and r.missing_data


def test_funds_do_not_need_valuation_lines():
    r = run(buy(symbol="QQQ"), valuation=None, is_fund=True).result("selection_pe")
    assert r.status == "pass" and not r.missing_data
    strict = replace(BOOK, selection_pe=replace(BOOK.selection_pe, funds_exempt=False))
    trade = buy(symbol="QQQ")
    r = evaluate(trade, ctx(symbol="QQQ", valuation=None, is_fund=True), strict).result("selection_pe")
    assert r.status == "block" and r.missing_data


def test_cyclical_needs_price_to_book():
    trade = buy(symbol="MU")
    assert run(trade).result("selection_pe").missing_data
    pb_lines = replace(LINES, metric="pb")
    assert status(run(trade, valuation=pb_lines), "selection_pe") == "pass"


# ── catalyst, planned ──────────────────────────────────────


def test_not_on_watchlist_fails_both():
    ev = run(watch_entries=())
    assert status(ev, "selection_catalyst") == "block"
    assert status(ev, "selection_planned") == "block"


def test_expired_catalyst():
    expired = replace(WATCH, catalyst_due_on=date(2026, 10, 15))
    r = run(watch_entries=(expired,)).result("selection_catalyst")
    assert r.status == "block" and "2026-10-15" in r.reason


@pytest.mark.parametrize("hours, expected", [(23.5, "block"), (24, "pass"), (25, "pass")])
def test_planned_needs_24_hours(hours, expected):
    entry = replace(WATCH, created_at=MOMENT - timedelta(hours=hours))
    assert status(run(watch_entries=(entry,)), "selection_planned") == expected


# ── position ───────────────────────────────────────────────


@pytest.mark.parametrize("held, expected", [(120, "pass"), (121, "block")])  # 13.0% / 13.1% after the buy
def test_position_cap_after_buy(held, expected):
    assert status(run(holding_quantity=held, holding_avg_cost=90), "position_cap") == expected


def test_symbol_specific_cap():
    trade = buy(symbol="ORCA")
    assert status(run(trade, holding_quantity=150, holding_avg_cost=90), "position_cap") == "pass"  # 16%
    assert status(run(trade, holding_quantity=151, holding_avg_cost=90), "position_cap") == "block"


def test_already_over_cap_cannot_buy_more():
    ev = run(buy(qty=1), holding_quantity=140, holding_avg_cost=90)
    assert status(ev, "no_buy_over_cap") == "block"
    assert "14.0%" in ev.result("no_buy_over_cap").reason


# ── cash ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cash, expected",
    [(5_500, "block"), (6_000, "warn"), (10_500, "warn"), (11_000, "pass")],  # 4.5% / 5.0% / 9.5% / 10.0% after the buy
)
def test_cash_floor_tiers(cash, expected):
    assert status(run(cash=cash), "cash_floor") == expected


def test_cash_floor_missing_snapshot():
    r = run(cash=None, nav=None).result("cash_floor")
    assert r.status == "block" and r.missing_data


# ── averaging down ─────────────────────────────────────────


def test_averaging_down_over_10_percent_blocks():
    r = run(holding_quantity=10, holding_avg_cost=112).result("no_averaging_down")  # down 10.7%
    assert r.status == "block" and "-10.7%" in r.reason


def test_small_loss_ok_when_selection_passes():
    assert status(run(holding_quantity=10, holding_avg_cost=105), "no_averaging_down") == "pass"


def test_small_loss_needs_every_selection_rule_to_pass():
    # down 4%, but the price is between the normal and rich lines (a warning is not a pass)
    r = run(buy(price=120), holding_quantity=10, holding_avg_cost=125).result("no_averaging_down")
    assert r.status == "block" and "PE not high" in r.reason


def test_adding_to_a_winner_is_fine():
    assert status(run(holding_quantity=10, holding_avg_cost=80), "no_averaging_down") == "pass"


# ── earnings blackout ──────────────────────────────────────


@pytest.mark.parametrize("days, expected", [(0, "block"), (1, "block"), (5, "block"), (6, "pass")])
def test_earnings_window(days, expected):
    assert status(run(trading_days_to_earnings=days), "earnings_blackout") == expected


def test_funds_have_no_earnings():
    assert status(run(next_earnings=None, trading_days_to_earnings=None, is_fund=True), "earnings_blackout") == "pass"


def test_unknown_earnings_date_blocks():
    r = run(next_earnings=None, trading_days_to_earnings=None).result("earnings_blackout")
    assert r.status == "block" and r.missing_data


# ── reduce-only ────────────────────────────────────────────


def test_reduce_only_lists_unmet_exit_conditions():
    r = run(buy(symbol="KITE"), holding_quantity=90, holding_avg_cost=100).result("reduce_only_list")
    assert r.status == "block"
    assert "9.0% > 8%" in r.reason and "< 30 days" in r.reason and "no catalyst registered" in r.reason


def test_reduce_only_still_blocks_when_exit_conditions_met():
    later = date(2026, 11, 25)
    fresh = replace(WATCH, created_at=datetime(2026, 11, 1, 15, tzinfo=timezone.utc))
    r = run(buy(symbol="KITE"), trade_date=later, holding_quantity=70, holding_avg_cost=100,
            watch_entries=(fresh,)).result("reduce_only_list")
    assert r.status == "block" and "all exit conditions are met" in r.reason


# ── pace ───────────────────────────────────────────────────


def test_first_buy_on_a_drop_day_is_allowed():
    assert status(run(index_change=-0.025), "market_drop_day") == "pass"


def test_second_buy_on_a_drop_day_blocks():
    r = run(index_change=-0.025, day_buy_orders=frozenset({"O1"})).result("market_drop_day")
    assert r.status == "block" and "buy #2" in r.reason


def test_second_fill_of_same_order_counts_once():
    ev = run(buy(order_id="O1"), index_change=-0.025, day_buy_orders=frozenset({"O1"}))
    assert status(ev, "market_drop_day") == "pass"


def test_live_check_before_the_open_passes_with_a_note():
    # a weekend check for a Monday buy: Monday's move is unknown, Friday's drop does not count
    r = run(live=True, index_change=-0.025, index_change_date=date(2026, 10, 16),
            day_buy_orders=frozenset({"O1"})).result("market_drop_day")
    assert r.status == "pass" and "has not opened yet" in r.reason


def test_audit_needs_that_days_index_move():
    r = run(index_change=-0.025, index_change_date=date(2026, 10, 16)).result("market_drop_day")
    assert r.status == "block" and r.missing_data


def test_exactly_2_percent_is_not_a_drop_day():
    assert status(run(index_change=-0.02, day_buy_orders=frozenset({"O1"})), "market_drop_day") == "pass"


@pytest.mark.parametrize("price, expected", [(100, "pass"), (100.1, "warn")])
def test_weekly_budget(price, expected):
    # 1/3 of week-start cash 3,000 = 1,000
    assert status(run(buy(price=price), week_start_cash=3_000), "weekly_buy_budget") == expected


def test_weekly_budget_counts_earlier_buys():
    r = run(week_start_cash=3_000, week_buys=500).result("weekly_buy_budget")
    assert r.status == "warn" and "1,500.00" in r.reason


# ── overall verdict ────────────────────────────────────────


def test_verdict_is_worst_result():
    assert run(cash=10_500).verdict == "warn"
    assert run(cash=5_500).verdict == "block"
    assert {r.rule_id for r in run(cash=5_500).by_status("block")} == {"cash_floor"}

"""rules.yaml loads with the intended values, and a mistake raises an error instead of silently
switching a rule off."""

from datetime import date

import pytest
import yaml

from rules.config import RULE_IDS, RulesConfigError, load_rules, parse_rules
from settings import PROJECT_ROOT
from tests.conftest import TEST_RULES

RULES_FILE = TEST_RULES


@pytest.fixture(scope="module")
def book():
    return load_rules(RULES_FILE)


@pytest.fixture
def raw():
    return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))


# ── the intended values ────────────────────────────────────


def test_position_caps(book):
    cap = book.position_cap
    assert cap.level == "block"
    assert cap.max_weight("ZEST") == 0.13
    assert cap.max_weight("orca") == 0.16
    assert cap.max_weight("QQQ") == 0.20
    assert book.no_buy_over_cap.level == "block"


def test_cash_floor(book):
    floor = book.cash_floor
    assert floor.level_for(0.049) == "block"
    assert floor.level_for(0.05) == "warn"
    assert floor.level_for(0.099) == "warn"
    assert floor.level_for(0.10) is None


def test_buy_rules(book):
    assert book.no_averaging_down.level == "block"
    assert book.no_averaging_down.max_unrealized_loss == 0.10
    assert book.no_averaging_down.losing_requires_selection_pass is True

    assert book.earnings_blackout.level == "block"
    assert book.earnings_blackout.trading_days_before == 5

    ro = book.reduce_only_list
    assert ro.level == "block"
    assert set(ro.added_on) == {"PINE", "KITE"}
    assert ro.contains("pine") and not ro.contains("ZEST")
    assert all(isinstance(d, date) for d in ro.added_on.values())
    assert ro.exit_max_weight == 0.08
    assert ro.exit_min_days_on_list == 30
    assert ro.exit_new_catalyst_after_added is True


def test_selection_rules(book):
    low = book.selection_near_low
    assert low.level == "block"
    assert low.max_above_52w_low == 0.15
    assert low.recent_high_lookback_days == 60
    assert low.min_below_recent_high == 0.20
    assert low.valuation_max_line == "normal"

    lines = {"pressure": 75, "cheap": 80, "normal": 100, "rich": 120}
    pe = book.selection_pe
    assert pe.funds_exempt is True
    assert pe.level_for(121, lines) == "block"
    assert pe.level_for(110, lines) == "warn"
    assert pe.level_for(100, lines) is None

    assert book.selection_catalyst.level == "block"
    assert book.selection_catalyst.require_invalidation is True
    assert book.selection_planned.level == "block"
    assert book.selection_planned.min_hours_on_watchlist == 24


def test_pace_rules(book):
    drop = book.market_drop_day
    assert (drop.level, drop.index_symbol, drop.min_daily_drop, drop.max_buys_per_day) == (
        "block", "QQQ", 0.02, 1,
    )
    budget = book.weekly_buy_budget
    assert budget.level == "warn"
    assert budget.max_fraction_of_week_start_cash == pytest.approx(1 / 3)

    gtc = book.gtc_review
    assert (gtc.level, gtc.review_weekday, gtc.stale_after_days) == ("remind", "sunday", 14)
    assert gtc.flag_if_fill_would_violate is True


def test_sell_rules_never_block(book):
    assert book.take_profit_reminder.level == "remind"
    assert book.take_profit_reminder.gain_levels == (0.50, 1.00)
    assert book.invalidation_triggered.level == "remind"
    assert book.invalidation_triggered.require_review is True
    assert book.above_rich_line.level == "warn"
    assert book.above_rich_line.line == "rich"
    sell_rules = [rid for rid, m in book.meta.items() if m.category == "Selling"]
    assert sell_rules and all(getattr(book, rid).level != "block" for rid in sell_rules)


def test_override_valuation_hunches(book):
    assert book.override.reason_required is True
    assert book.override.cooldown_hours == 24
    assert book.calendar.timezone == "America/New_York"
    assert book.valuation.pressure_multiple_of_normal == 0.75
    assert book.valuation.is_cyclical("mu")
    assert not book.valuation.is_cyclical("ZEST")
    assert book.valuation.cyclical_metric == "pb"
    assert book.hunches.review_after_days == 30
    assert book.audit.start_date == date(2026, 10, 1)
    assert book.audit.check_valid_days == 3


def test_example_rules_in_the_project_root_load():
    example = load_rules(PROJECT_ROOT / "rules.yaml")
    assert set(example.meta) == set(RULE_IDS)
    assert example.reduce_only_list.added_on == {}


def test_every_rule_has_category_and_description(book):
    assert set(book.meta) == set(RULE_IDS)
    for meta in book.meta.values():
        assert meta.category and meta.description


# ── mistakes raise errors ──────────────────────────────────


def test_missing_threshold(raw):
    del raw["rules"]["cash_floor"]["tiers"]
    with pytest.raises(RulesConfigError, match="cash_floor.*tiers"):
        parse_rules(raw)


def test_missing_rule(raw):
    del raw["rules"]["market_drop_day"]
    with pytest.raises(RulesConfigError, match="market_drop_day"):
        parse_rules(raw)


def test_unknown_rule(raw):
    raw["rules"]["new_idea"] = {"category": "x", "description": "x", "level": "block"}
    with pytest.raises(RulesConfigError, match="new_idea"):
        parse_rules(raw)


def test_misspelled_key(raw):
    raw["rules"]["position_cap"]["defualt_max_weight"] = 0.10
    with pytest.raises(RulesConfigError, match="defualt_max_weight"):
        parse_rules(raw)


def test_invalid_level(raw):
    raw["rules"]["position_cap"]["level"] = "hard"
    with pytest.raises(RulesConfigError, match="level"):
        parse_rules(raw)


def test_percent_written_as_whole_number(raw):
    raw["rules"]["position_cap"]["default_max_weight"] = 13
    with pytest.raises(RulesConfigError, match="default_max_weight"):
        parse_rules(raw)


def test_tiers_must_go_strict_to_loose(raw):
    raw["rules"]["cash_floor"]["tiers"].reverse()
    with pytest.raises(RulesConfigError, match="strictest first"):
        parse_rules(raw)


def test_unquoted_ticker_read_as_boolean():
    # YAML 1.1 reads the ticker ON as True: it must fail and ask for quotes
    text = RULES_FILE.read_text(encoding="utf-8").replace("ORCA: 0.16", "ON: 0.16")
    with pytest.raises(RulesConfigError, match="quotes"):
        parse_rules(yaml.safe_load(text))


def test_bad_timezone(raw):
    raw["calendar"]["timezone"] = "Mars/Olympus"
    with pytest.raises(RulesConfigError, match="time zone"):
        parse_rules(raw)

"""Load and validate rules.yaml.

The rule engine and the pages get every threshold from here; no numbers are hard-coded.
Validation is strict: a missing key, an extra key, an unknown rule name or an invalid level
is an error, so a typo can never silently switch a rule off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

LEVELS = ("block", "warn", "remind")
# valuation lines, low to high
VALUATION_LINES = ("pressure", "cheap", "normal", "rich")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
VALUATION_METRICS = ("pe", "pb")

# description fields every rule carries, shown on the pages
_META_KEYS = ("category", "description")


class RulesConfigError(ValueError):
    """rules.yaml is invalid."""


# ── reading helpers ────────────────────────────────────────


def _ratio(value: Any, where: str, max_value: float | None = 1.0) -> float:
    """A ratio: a decimal or a fraction string ("1/3"), must be > 0."""
    if isinstance(value, bool):
        raise RulesConfigError(f"{where}: expected a ratio, got {value!r}")
    if isinstance(value, str):
        try:
            number = float(Fraction(value.strip()))
        except (ValueError, ZeroDivisionError):
            raise RulesConfigError(f"{where}: cannot parse ratio {value!r}") from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise RulesConfigError(f"{where}: expected a ratio, got {value!r}")
    if number <= 0 or (max_value is not None and number > max_value):
        bound = f"(0, {max_value}]" if max_value is not None else "> 0"
        raise RulesConfigError(f"{where}: ratio {number} should be in {bound}")
    return number


def _symbol(value: Any, where: str) -> str:
    # YAML 1.1 reads tickers like ON, NO or Y as booleans, so they must be quoted
    if not isinstance(value, str) or not value.strip():
        raise RulesConfigError(f"{where}: ticker {value!r} is not a string; put it in quotes")
    return value.strip().upper()


class _Section:
    """Wraps a dict: remembers which keys were read, and done() rejects any keys left over."""

    def __init__(self, data: Any, where: str):
        if not isinstance(data, dict):
            raise RulesConfigError(f"{where}: expected a key: value mapping")
        self.data = data
        self.where = where
        self._used: set[str] = set()

    def raw(self, key: str) -> Any:
        if key not in self.data:
            raise RulesConfigError(f"{self.where}: missing {key}")
        self._used.add(key)
        return self.data[key]

    def section(self, key: str) -> _Section:
        return _Section(self.raw(key), f"{self.where}.{key}")

    def items(self, key: str) -> list[_Section]:
        value = self.raw(key)
        if not isinstance(value, list) or not value:
            raise RulesConfigError(f"{self.where}.{key}: expected a non-empty list")
        return [_Section(item, f"{self.where}.{key}[{i}]") for i, item in enumerate(value)]

    def ratio(self, key: str, max_value: float | None = 1.0) -> float:
        return _ratio(self.raw(key), f"{self.where}.{key}", max_value)

    def positive_int(self, key: str) -> int:
        value = self.raw(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RulesConfigError(f"{self.where}.{key}: expected a positive integer, got {value!r}")
        return value

    def positive_number(self, key: str) -> float:
        value = self.raw(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise RulesConfigError(f"{self.where}.{key}: expected a positive number, got {value!r}")
        return float(value)

    def boolean(self, key: str) -> bool:
        value = self.raw(key)
        if not isinstance(value, bool):
            raise RulesConfigError(f"{self.where}.{key}: expected true / false, got {value!r}")
        return value

    def choice(self, key: str, options: tuple[str, ...]) -> str:
        value = self.raw(key)
        if value not in options:
            raise RulesConfigError(f"{self.where}.{key}: {value!r} is not one of {list(options)}")
        return value

    def text(self, key: str) -> str:
        value = self.raw(key)
        if not isinstance(value, str) or not value.strip():
            raise RulesConfigError(f"{self.where}.{key}: expected non-empty text")
        return value.strip()

    def symbol(self, key: str) -> str:
        return _symbol(self.raw(key), f"{self.where}.{key}")

    def level(self) -> str:
        return self.choice("level", LEVELS)

    def done(self) -> None:
        extra = set(self.data) - self._used
        if extra:
            raise RulesConfigError(f"{self.where}: unknown keys {sorted(map(str, extra))}")


# ── config objects ─────────────────────────────────────────


@dataclass(frozen=True)
class Override:
    reason_required: bool
    cooldown_hours: float


@dataclass(frozen=True)
class Calendar:
    timezone: str
    week_starts_on: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class RuleMeta:
    rule_id: str
    category: str
    description: str


@dataclass(frozen=True)
class LevelOnly:
    level: str


@dataclass(frozen=True)
class PositionCap:
    level: str
    default_max_weight: float
    symbol_max_weight: Mapping[str, float]

    def max_weight(self, symbol: str) -> float:
        return self.symbol_max_weight.get(symbol.upper(), self.default_max_weight)


@dataclass(frozen=True)
class CashTier:
    below: float
    level: str


@dataclass(frozen=True)
class CashFloor:
    tiers: tuple[CashTier, ...]  # strictest first

    def tier_for(self, cash_ratio: float) -> CashTier | None:
        return next((t for t in self.tiers if cash_ratio < t.below), None)

    def level_for(self, cash_ratio: float) -> str | None:
        tier = self.tier_for(cash_ratio)
        return tier.level if tier else None


@dataclass(frozen=True)
class NoAveragingDown:
    level: str
    max_unrealized_loss: float
    losing_requires_selection_pass: bool


@dataclass(frozen=True)
class EarningsBlackout:
    level: str
    trading_days_before: int
    include_earnings_day: bool


@dataclass(frozen=True)
class ReduceOnlyList:
    level: str
    added_on: Mapping[str, date]  # ticker → date it was added to the list
    exit_max_weight: float
    exit_min_days_on_list: int
    exit_new_catalyst_after_added: bool

    def contains(self, symbol: str) -> bool:
        return symbol.upper() in self.added_on


@dataclass(frozen=True)
class SelectionNearLow:
    level: str
    max_above_52w_low: float
    recent_high_lookback_days: int
    min_below_recent_high: float
    valuation_max_line: str


@dataclass(frozen=True)
class LineTier:
    above_line: str
    level: str


@dataclass(frozen=True)
class SelectionPE:
    tiers: tuple[LineTier, ...]  # strictest first: the higher the line, the stricter
    funds_exempt: bool  # funds / ETFs skip the PE check

    def tier_for(self, price: float, lines: Mapping[str, float]) -> LineTier | None:
        return next((t for t in self.tiers if price > lines[t.above_line]), None)

    def level_for(self, price: float, lines: Mapping[str, float]) -> str | None:
        tier = self.tier_for(price, lines)
        return tier.level if tier else None


@dataclass(frozen=True)
class SelectionCatalyst:
    level: str
    require_invalidation: bool


@dataclass(frozen=True)
class SelectionPlanned:
    level: str
    min_hours_on_watchlist: float


@dataclass(frozen=True)
class MarketDropDay:
    level: str
    index_symbol: str
    min_daily_drop: float
    max_buys_per_day: int


@dataclass(frozen=True)
class WeeklyBuyBudget:
    level: str
    max_fraction_of_week_start_cash: float


@dataclass(frozen=True)
class GtcReview:
    level: str
    review_weekday: str
    stale_after_days: int
    flag_if_fill_would_violate: bool


@dataclass(frozen=True)
class TakeProfitReminder:
    level: str
    gain_levels: tuple[float, ...]


@dataclass(frozen=True)
class InvalidationTriggered:
    level: str
    require_review: bool


@dataclass(frozen=True)
class AboveRichLine:
    level: str
    line: str


@dataclass(frozen=True)
class Valuation:
    update_frequency: str
    pressure_multiple_of_normal: float
    cyclical_symbols: frozenset[str]
    cyclical_metric: str

    def is_cyclical(self, symbol: str) -> bool:
        return symbol.upper() in self.cyclical_symbols


@dataclass(frozen=True)
class Hunches:
    review_after_days: int


@dataclass(frozen=True)
class Audit:
    start_date: date  # test period start: fills on or after this day get violation records
    check_valid_days: int  # a buy check counts if done within this many days before the order


@dataclass(frozen=True)
class RuleBook:
    version: int
    override: Override
    calendar: Calendar
    meta: Mapping[str, RuleMeta]

    position_cap: PositionCap
    no_buy_over_cap: LevelOnly
    cash_floor: CashFloor
    no_averaging_down: NoAveragingDown
    earnings_blackout: EarningsBlackout
    reduce_only_list: ReduceOnlyList
    selection_near_low: SelectionNearLow
    selection_pe: SelectionPE
    selection_catalyst: SelectionCatalyst
    selection_planned: SelectionPlanned
    market_drop_day: MarketDropDay
    weekly_buy_budget: WeeklyBuyBudget
    gtc_review: GtcReview
    take_profit_reminder: TakeProfitReminder
    invalidation_triggered: InvalidationTriggered
    above_rich_line: AboveRichLine

    valuation: Valuation
    hunches: Hunches
    audit: Audit


# ── per-rule parsing ───────────────────────────────────────


def _position_cap(s: _Section) -> PositionCap:
    overrides = s.section("symbol_max_weight")
    caps = {}
    for key in list(overrides.data):
        caps[_symbol(key, overrides.where)] = overrides.ratio(key)
    overrides.done()
    return PositionCap(s.level(), s.ratio("default_max_weight"), caps)


def _cash_floor(s: _Section) -> CashFloor:
    tiers = []
    for item in s.items("tiers"):
        tiers.append(CashTier(item.ratio("below"), item.level()))
        item.done()
    if any(a.below >= b.below for a, b in zip(tiers, tiers[1:])):
        raise RulesConfigError(f"{s.where}.tiers: list the strictest first (below increasing)")
    return CashFloor(tuple(tiers))


def _no_averaging_down(s: _Section) -> NoAveragingDown:
    return NoAveragingDown(
        s.level(), s.ratio("max_unrealized_loss"), s.boolean("losing_requires_selection_pass")
    )


def _earnings_blackout(s: _Section) -> EarningsBlackout:
    return EarningsBlackout(
        s.level(), s.positive_int("trading_days_before"), s.boolean("include_earnings_day")
    )


def _reduce_only_list(s: _Section) -> ReduceOnlyList:
    symbols = s.section("symbols")
    added_on = {}
    for key in list(symbols.data):
        value = symbols.raw(key)
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                pass
        if not isinstance(value, date):
            raise RulesConfigError(f"{symbols.where}.{key}: expected the date added, YYYY-MM-DD")
        added_on[_symbol(key, symbols.where)] = value
    symbols.done()
    exit_ = s.section("exit_requires")
    rule = ReduceOnlyList(
        s.level(),
        added_on,
        exit_.ratio("max_weight"),
        exit_.positive_int("min_days_on_list"),
        exit_.boolean("new_catalyst_after_added"),
    )
    exit_.done()
    return rule


def _selection_near_low(s: _Section) -> SelectionNearLow:
    return SelectionNearLow(
        s.level(),
        s.ratio("max_above_52w_low"),
        s.positive_int("recent_high_lookback_days"),
        s.ratio("min_below_recent_high"),
        s.choice("valuation_max_line", VALUATION_LINES),
    )


def _selection_pe(s: _Section) -> SelectionPE:
    tiers = []
    for item in s.items("tiers"):
        tiers.append(LineTier(item.choice("above_line", VALUATION_LINES), item.level()))
        item.done()
    rank = [VALUATION_LINES.index(t.above_line) for t in tiers]
    if any(a <= b for a, b in zip(rank, rank[1:])):
        raise RulesConfigError(f"{s.where}.tiers: list the strictest first (lines high to low)")
    return SelectionPE(tuple(tiers), s.boolean("funds_exempt"))


def _selection_catalyst(s: _Section) -> SelectionCatalyst:
    return SelectionCatalyst(s.level(), s.boolean("require_invalidation"))


def _selection_planned(s: _Section) -> SelectionPlanned:
    return SelectionPlanned(s.level(), s.positive_number("min_hours_on_watchlist"))


def _market_drop_day(s: _Section) -> MarketDropDay:
    return MarketDropDay(
        s.level(),
        s.symbol("index_symbol"),
        s.ratio("min_daily_drop"),
        s.positive_int("max_buys_per_day"),
    )


def _weekly_buy_budget(s: _Section) -> WeeklyBuyBudget:
    return WeeklyBuyBudget(s.level(), s.ratio("max_fraction_of_week_start_cash"))


def _gtc_review(s: _Section) -> GtcReview:
    return GtcReview(
        s.level(),
        s.choice("review_weekday", WEEKDAYS),
        s.positive_int("stale_after_days"),
        s.boolean("flag_if_fill_would_violate"),
    )


def _take_profit_reminder(s: _Section) -> TakeProfitReminder:
    raw = s.raw("gain_levels")
    if not isinstance(raw, list) or not raw:
        raise RulesConfigError(f"{s.where}.gain_levels: expected a non-empty list")
    levels = tuple(_ratio(v, f"{s.where}.gain_levels", max_value=None) for v in raw)
    if any(a >= b for a, b in zip(levels, levels[1:])):
        raise RulesConfigError(f"{s.where}.gain_levels: list them from small to large")
    return TakeProfitReminder(s.level(), levels)


def _invalidation_triggered(s: _Section) -> InvalidationTriggered:
    return InvalidationTriggered(s.level(), s.boolean("require_review"))


def _above_rich_line(s: _Section) -> AboveRichLine:
    return AboveRichLine(s.level(), s.choice("line", VALUATION_LINES))


_RULE_PARSERS: dict[str, Callable[[_Section], Any]] = {
    "position_cap": _position_cap,
    "no_buy_over_cap": lambda s: LevelOnly(s.level()),
    "cash_floor": _cash_floor,
    "no_averaging_down": _no_averaging_down,
    "earnings_blackout": _earnings_blackout,
    "reduce_only_list": _reduce_only_list,
    "selection_near_low": _selection_near_low,
    "selection_pe": _selection_pe,
    "selection_catalyst": _selection_catalyst,
    "selection_planned": _selection_planned,
    "market_drop_day": _market_drop_day,
    "weekly_buy_budget": _weekly_buy_budget,
    "gtc_review": _gtc_review,
    "take_profit_reminder": _take_profit_reminder,
    "invalidation_triggered": _invalidation_triggered,
    "above_rich_line": _above_rich_line,
}

RULE_IDS = tuple(_RULE_PARSERS)


# ── entry points ───────────────────────────────────────────


def parse_rules(data: Any) -> RuleBook:
    root = _Section(data, "rules.yaml")

    version = root.positive_int("version")

    o = root.section("override")
    override = Override(o.boolean("reason_required"), o.positive_number("cooldown_hours"))
    o.done()

    c = root.section("calendar")
    calendar = Calendar(c.text("timezone"), c.choice("week_starts_on", WEEKDAYS))
    c.done()
    try:
        calendar.tz
    except (ZoneInfoNotFoundError, ValueError):
        raise RulesConfigError(f"rules.yaml.calendar.timezone: unknown time zone {calendar.timezone!r}") from None

    rules = root.section("rules")
    unknown = set(rules.data) - set(RULE_IDS)
    if unknown:
        raise RulesConfigError(f"rules.yaml.rules: unknown rules {sorted(map(str, unknown))}")
    parsed: dict[str, Any] = {}
    meta: dict[str, RuleMeta] = {}
    for rule_id, parser in _RULE_PARSERS.items():
        s = rules.section(rule_id)
        meta[rule_id] = RuleMeta(rule_id, s.text("category"), s.text("description"))
        parsed[rule_id] = parser(s)
        s.done()
    rules.done()

    v = root.section("valuation")
    cyc = v.section("cyclical")
    raw_symbols = cyc.raw("symbols")
    if not isinstance(raw_symbols, list):
        raise RulesConfigError(f"{cyc.where}.symbols: expected a list")
    valuation = Valuation(
        v.choice("update_frequency", ("weekly",)),
        v.ratio("pressure_multiple_of_normal"),
        frozenset(_symbol(x, f"{cyc.where}.symbols") for x in raw_symbols),
        cyc.choice("metric", VALUATION_METRICS),
    )
    cyc.done()
    v.done()

    h = root.section("hunches")
    hunches = Hunches(h.positive_int("review_after_days"))
    h.done()

    a = root.section("audit")
    start = a.raw("start_date")
    if isinstance(start, str):
        try:
            start = date.fromisoformat(start)
        except ValueError:
            pass
    if not isinstance(start, date):
        raise RulesConfigError("rules.yaml.audit.start_date: expected a date, YYYY-MM-DD")
    audit = Audit(start, a.positive_int("check_valid_days"))
    a.done()

    root.done()
    return RuleBook(
        version=version,
        override=override,
        calendar=calendar,
        meta=meta,
        valuation=valuation,
        hunches=hunches,
        audit=audit,
        **parsed,
    )


def load_rules(path: Path | str) -> RuleBook:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RulesConfigError(f"{path.name}: invalid YAML\n{exc}") from None
    return parse_rules(data)

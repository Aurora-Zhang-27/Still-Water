"""Valuation lines: parsing the seed file, the four lines, used by the buy check after import, weekly
revision updates, the weekly report."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from audit.weekly import build_weekly, render_markdown
from ingest.market import MarketData
from ingest.valuation import SeedError, compute_lines, import_seed, latest_rows, parse_seed, update_eps, zone
from rules.config import load_rules
from rules.engine import ProposedTrade, check
from tests.conftest import SEED, TEST_RULES

BOOK = load_rules(TEST_RULES)
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)


class FakeFundamentals:
    """A fake data source that only provides fundamentals."""

    def __init__(self, values):
        self.values = values  # ticker → forward EPS (None = not available)

    def fundamentals(self, symbol):
        return {"forward_eps": self.values.get(symbol), "book_value_per_share": self.values.get(symbol)}


def market(conn, values, now=NOW):
    clock = {"now": now}
    md = MarketData(conn, FakeFundamentals(values), now=lambda: clock["now"], tz=ET)
    return md, clock


def write_csv(tmp_path, text):
    path = tmp_path / "seed.csv"
    path.write_text(text, encoding="utf-8")
    return path


# ── parsing ────────────────────────────────────────────────


def test_parses_the_seed_file():
    rows, notes = parse_seed(SEED, BOOK)
    assert len(rows) == 6 and notes == []  # every stress multiple equals normal × 0.75
    pine = next(r for r in rows if r.symbol == "PINE")
    assert (pine.base_value, pine.cheap, pine.normal, pine.rich) == (9.60, 16.0, 20.0, 25.0)
    assert (pine.metric, pine.as_of) == ("pe", date(2026, 10, 15))
    assert next(r for r in rows if r.symbol == "MU").metric == "pb"  # cyclical: price-to-book by default


def test_header_aliases_and_cyclical_default(tmp_path):
    path = write_csv(tmp_path, "Symbol,Cheap (x),Normal (x),Rich (x),Forward EPS\nzest,21,26,33,9.26\nMU,1.5,2.0,3.0,40\n")
    rows, _ = parse_seed(path, BOOK)
    assert {(r.symbol, r.metric) for r in rows} == {("ZEST", "pe"), ("MU", "pb")}
    assert all(r.as_of is None for r in rows)


@pytest.mark.parametrize(
    "text, message",
    [
        ("ticker,cheap_mult,normal_mult\nORCA,21,24.5\n", "missing columns: rich, forward_eps"),
        ("ticker,forward_eps,cheap_mult,normal_mult,expensive_mult\nORCA,abc,21,24.5,29\n", "is not a number"),
        ("ticker,forward_eps,cheap_mult,normal_mult,expensive_mult\nORCA,22,25,24.5,29\n", "0 < cheap < normal < rich"),
        ("ticker,forward_eps,cheap_mult,normal_mult,expensive_mult\nORCA,-1,21,24.5,29\n", "EPS must be greater than 0"),
        ("ticker,forward_eps,cheap_mult,normal_mult,expensive_mult\nORCA,22,21,24.5,29\norca,22,21,24.5,29\n",
         "duplicate"),
    ],
)
def test_seed_errors(tmp_path, text, message):
    with pytest.raises(SeedError, match=message):
        parse_seed(write_csv(tmp_path, text), BOOK)


def test_stress_multiple_that_disagrees_with_rules(tmp_path):
    path = write_csv(tmp_path, "ticker,forward_eps,cheap_mult,normal_mult,expensive_mult,stress_mult\n"
                               "ORCA,22,21,24.5,29,15\n")
    _, notes = parse_seed(path, BOOK)
    assert "using the rule" in notes[0]


def test_lines():
    assert compute_lines(BOOK, 10, 20, 25, 30) == {"pressure": 187.5, "cheap": 200, "normal": 250, "rich": 300}


@pytest.mark.parametrize("price, expected",
                         [(301, "Above rich"), (300, "Normal–rich"), (250, "Cheap–normal"), (200, "Below cheap")])
def test_zone(price, expected):
    assert zone(price, compute_lines(BOOK, 10, 20, 25, 30)) == expected


# ── import and weekly updates ──────────────────────────────


def test_import_makes_pe_rule_real(sample_db):
    conn, _ = sample_db
    md, _ = market(conn, {"PINE": 9.80})
    before = check(conn, BOOK, ProposedTrade("PINE", "BUY", 1, 180.0), at=NOW)
    assert before.result("selection_pe").missing_data

    imported, _ = import_seed(conn, BOOK, SEED, md, NOW.date())
    pine = next(i for i in imported if i.row.symbol == "PINE")
    assert (pine.as_of, pine.reference) == (date(2026, 10, 15), 9.80)

    after = check(conn, BOOK, ProposedTrade("PINE", "BUY", 1, 180.0), at=NOW)
    pe = after.result("selection_pe")
    assert pe.status == "pass" and not pe.missing_data
    assert pe.details["normal"] == pytest.approx(20 * 9.60, abs=0.01)
    assert "normal valuation line" in after.result("selection_near_low").reason


def test_weekly_update_follows_yfinance_revision(sample_db):
    conn, _ = sample_db
    md, clock = market(conn, {"PINE": 9.80})
    import_seed(conn, BOOK, SEED, md, NOW.date())

    assert update_eps(conn, BOOK, md, date(2026, 10, 19)) == []  # less than a week since 10/15

    md.provider.values["PINE"] = 10.29  # yfinance revises up 5%
    clock["now"] = NOW + timedelta(days=7)
    changes = {c.symbol: c for c in update_eps(conn, BOOK, md, date(2026, 10, 25))}
    pine = changes["PINE"]
    assert pine.new == pytest.approx(9.60 * 10.29 / 9.80) and pine.change == pytest.approx(0.05, abs=1e-3)

    latest = {r["symbol"]: r for r in latest_rows(conn)}["PINE"]
    assert (latest["as_of"], latest["source"], latest["reference_value"]) == ("2026-10-25", "yfinance", 10.29)
    assert latest["normal_line"] == pytest.approx(20 * pine.new)
    assert conn.execute("SELECT COUNT(*) FROM valuation WHERE symbol = 'PINE'").fetchone()[0] == 2  # history kept


def test_update_without_reference_starts_tracking(sample_db):
    conn, _ = sample_db
    md, clock = market(conn, {})  # yfinance has nothing at import time
    imported, notes = import_seed(conn, BOOK, SEED, md, NOW.date())
    assert all(i.reference is None for i in imported) and notes

    md.provider.values["ORCA"] = 15.75
    clock["now"] = NOW + timedelta(days=7)
    change = {c.symbol: c for c in update_eps(conn, BOOK, md, date(2026, 10, 25))}["ORCA"]
    assert change.new == 15.0 and "Started tracking" in change.note
    assert change.symbol == "ORCA"


def test_missing_yfinance_keeps_last_lines(sample_db):
    conn, _ = sample_db
    md, clock = market(conn, {"KITE": 6.40})
    import_seed(conn, BOOK, SEED, md, NOW.date())
    md.provider.values["KITE"] = None
    clock["now"] = NOW + timedelta(days=7)
    change = {c.symbol: c for c in update_eps(conn, BOOK, md, date(2026, 10, 25))}["KITE"]
    assert change.new is None and "keeping the lines from 2026-10-15" in change.note
    assert {r["symbol"]: r for r in latest_rows(conn)}["KITE"]["as_of"] == "2026-10-15"


def test_weekly_report_valuation_section(sample_db):
    conn, _ = sample_db
    md, _ = market(conn, {})
    report = build_weekly(conn, BOOK, NOW, week_of=date(2026, 10, 12))
    assert "QQQ" not in report.missing_valuation and "ORCA" in report.missing_valuation  # ETFs need no lines

    import_seed(conn, BOOK, SEED, md, NOW.date())
    report = build_weekly(conn, BOOK, NOW, week_of=date(2026, 10, 12))
    assert set(report.missing_valuation) == {"NEWC", "NOVA", "RIVR", "SAGE"}  # not in the seed file
    zones = {s: z for s, z, _, _ in report.holding_zones}
    assert zones["ORCA"] == "Below cheap" and zones["KITE"] == "Cheap–normal"
    assert "## Valuation lines" in render_markdown(report, BOOK)

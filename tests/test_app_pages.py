"""Run the pages headless with Streamlit's AppTest (sample history, no network)."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from streamlit.testing.v1 import AppTest

from audit.audit import audit_new_trades
from db.database import utc_text
from ingest.market import KINDS, MarketData
from ingest.valuation import import_seed
from rules.config import load_rules
from settings import PROJECT_ROOT
from tests.conftest import SAMPLE, SEED, TEST_RULES, close_on

VIEWS = PROJECT_ROOT / "app" / "views"
BOOK = load_rules(TEST_RULES)


@pytest.fixture
def app_db(sample_db, monkeypatch):
    conn, path = sample_db
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("RULES_PATH", str(TEST_RULES))
    # mark the market cache as just refreshed, so the pages never call yfinance
    fresh = utc_text(datetime.now(timezone.utc))
    conn.executemany(
        "INSERT INTO market_fetch_log (symbol, kind, fetched_at) VALUES (?, ?, ?)",
        [(s, k, fresh) for s in ("ORCA", "PINE", "KITE", "MAPL", "QQQ") for k in KINDS],
    )
    conn.commit()
    return conn


def run(page: str) -> AppTest:
    at = AppTest.from_file(str(VIEWS / page), default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_overview(app_db):
    at = run("overview.py")
    html = [h.proto.body for h in at.get("html")]
    assert any("bg_overview.jpg" in h and "plate-title" in h for h in html)  # Water Lilies behind a glass title
    cards = next(h for h in html if "kpi-grid" in h)
    _, cash, nav = SAMPLE["snapshots"][-1]
    assert f'<div class="klabel">Cash ratio</div><div class="kvalue">{cash / nav:.1%}</div>' in cards
    # cash floor, ORCA over its cap, PINE over its cap
    assert '<div class="klabel">Current violations</div><div class="kvalue">3</div>' in cards
    assert 'class="spark"' in cards  # trend lines for NAV and the cash ratio
    text = "\n".join(m.value for m in at.markdown)
    assert "ORCA is 20.0%, over its 16% cap" in text
    assert "PINE is on the reduce-only list" in text
    assert len(at.get("vega_lite_chart")) == 3  # cash, this week's buying, positions


def test_buy_check_block_then_override(app_db):
    at = run("buy_check.py")
    at.text_input(key="symbol").input("pine").run()
    at.number_input(key="price_PINE").set_value(180.0).run()
    at.button[0].click().run()
    assert not at.exception, at.exception

    assert at.error[0].value.startswith("Blocked")  # Streamlit turns the leading 🔴 into an icon
    rows = app_db.execute("SELECT symbol, verdict, override_reason FROM checks").fetchall()
    assert [tuple(r) for r in rows] == [("PINE", "block", None)]

    # typing in a form does not rerun (same as the browser); submit directly
    at.text_area[0].input("Catalyst intact after earnings; adding 1 share only")
    submit = next(b for b in at.button if "cooldown" in b.label)
    submit.click().run()
    assert not at.exception, at.exception

    row = app_db.execute("SELECT override_reason, overridden_at, cooldown_until FROM checks").fetchone()
    assert row["override_reason"] == "Catalyst intact after earnings; adding 1 share only"
    hours = (datetime.fromisoformat(row["cooldown_until"].replace("Z", "+00:00"))
             - datetime.fromisoformat(row["overridden_at"].replace("Z", "+00:00"))).total_seconds() / 3600
    assert hours == 24
    assert any("Cooldown until" in i.value for i in at.info)
    assert not any("cooldown" in b.label for b in at.button)  # the reason can only be written once


def test_empty_reason_is_rejected(app_db):
    at = run("buy_check.py")
    at.text_input(key="symbol").input("PINE").run()
    at.number_input(key="price_PINE").set_value(180.0).run()
    at.button[0].click().run()
    next(b for b in at.button if "cooldown" in b.label).click().run()
    assert any("cannot be empty" in e.value for e in at.error)
    assert app_db.execute("SELECT override_reason FROM checks").fetchone()[0] is None


def test_overview_lists_audit_records(app_db):
    live = replace(BOOK, audit=replace(BOOK.audit, start_date=date(2026, 10, 1)))
    audit_new_trades(app_db, live)
    at = run("overview.py")
    assert not at.exception, at.exception
    table = at.dataframe[-1].value
    assert {"Filled (ET)", "Ticker", "Buy", "Verdict", "Broke", "Warnings", "Missing data"} <= set(table.columns)
    # one row per fill, not one per rule
    trades = app_db.execute("SELECT count(DISTINCT trade_id) FROM violations").fetchone()[0]
    rows = app_db.execute("SELECT count(*) FROM violations").fetchone()[0]
    assert len(table) == trades < rows
    assert all(v.startswith("Unchecked") for v in table["Broke"])  # buys without a check come first
    text = "\n".join(m.value for m in at.markdown)
    assert "**Unchecked**" in text  # rule by rule


def test_reports_page_generates_and_shows(app_db):
    at = run("reports.py")
    assert any("No weekly reports yet" in i.value for i in at.info)
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert any(m.value.startswith("### Weekly report") for m in at.markdown)


def test_valuation_page(app_db):
    at = run("valuation.py")
    assert any("No valuation lines yet" in i.value for i in at.info)

    class NoNetwork:
        def fundamentals(self, symbol):
            return {}

    import_seed(app_db, BOOK, SEED, MarketData(app_db, NoNetwork()), date(2026, 10, 18))
    at = run("valuation.py")
    titles = [h.value for h in at.subheader]
    assert "Holdings (4)" in titles  # the fixture has bars for ORCA, PINE, KITE and MAPL only
    # QQQ has no lines either, but it is an ETF and skips the PE check
    [warning] = at.warning
    assert "NEWC, NOVA, RIVR, SAGE" in warning.value and "QQQ" not in warning.value
    assert len(at.get("vega_lite_chart")) >= 2  # band chart + one stock's price


def test_watchlist_page_register_and_remove(app_db):
    at = run("watchlist.py")
    assert any("nothing registered yet" in h.proto.body for h in at.get("html"))

    # typing in a form does not rerun; fill it in and submit
    at.text_input[0].input("pine")
    at.text_input[1].input("11/04 earnings, new orders")
    at.text_input[2].input("falls below 160")
    next(b for b in at.button if b.label == "Register").click().run()
    assert not at.exception, at.exception
    assert any("Registered PINE" in s.value for s in at.success)
    assert any("reduce-only list" in i.value for i in at.info)
    table = at.dataframe[0].value
    assert list(table["Ticker"]) == ["PINE"] and table["Planned"][0].startswith("needs")

    at.text_input[-1].input("removing as a test")
    next(b for b in at.button if b.label == "Remove").click().run()
    assert not at.exception, at.exception
    assert app_db.execute("SELECT removed_reason FROM watchlist").fetchone()[0] == "removing as a test"


def test_watchlist_page_rejects_missing_catalyst(app_db):
    at = run("watchlist.py")
    at.text_input[0].input("ZEST")
    at.text_input[2].input("falls below 150")
    next(b for b in at.button if b.label == "Register").click().run()
    assert any("catalyst" in e.value for e in at.error)
    assert app_db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


def test_hunches_page_records_price_automatically(app_db):
    at = run("hunches.py")
    at.text_input[0].input("orca")
    at.text_input[1].input("Ran up too fast; taking some off")
    next(b for b in at.button if b.label == "Record").click().run()
    assert not at.exception, at.exception
    last = close_on("ORCA", "2026-10-16")
    assert any(f"ORCA at {last:,.2f}" in s.value for s in at.success)  # no quote: the latest close is used
    row = app_db.execute("SELECT expected_move, price_at_entry, review_due_on FROM hunches").fetchone()
    assert row["expected_move"] == "down" and row["price_at_entry"] == pytest.approx(last)
    cards = next(h.proto.body for h in at.get("html") if "kpi-grid" in h.proto.body)
    assert '<div class="klabel">Awaiting review</div><div class="kvalue">1</div>' in cards


def test_reminders_page(app_db):
    from db.records import add_watch

    add_watch(app_db, "ORCA", "January earnings", "falls below 320", invalidation_price=320, invalidation_when="below")
    at = run("reminders.py")
    text = "\n".join(m.value for m in at.markdown)
    assert "ORCA invalidation triggered" in text
    holdings = at.dataframe[0].value
    assert "ORCA" in set(holdings["Ticker"]) and "Next level price" in holdings.columns
    assert any("IBKR" in i.value for i in at.info)


def test_review_page(app_db):
    at = run("review.py")
    assert any("No audit records yet" in i.value for i in at.info)

    audit_new_trades(app_db, BOOK)
    at = run("review.py")
    cards = next(h.proto.body for h in at.get("html") if "kpi-grid" in h.proto.body)
    assert '<div class="klabel">Compliance</div><div class="kvalue">-</div>' in cards  # no buy checks yet
    assert '<div class="klabel">Violating buys, excess</div><div class="kvalue">-</div>' not in cards
    assert len(at.get("vega_lite_chart")) == 2  # return dots + violations by rule
    monthly_table = at.dataframe[-1].value
    assert list(monthly_table["Month"]) == ["2026-10"]


def test_bad_symbol(app_db):
    at = run("buy_check.py")
    at.text_input(key="symbol").input("NV DA").run()
    assert any("A ticker may only contain" in e.value for e in at.error)
    assert at.button[0].disabled

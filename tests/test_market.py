"""The yfinance cache layer (fake data source, no network)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from db.database import init_db
from ingest.market import CACHE_TTL, HISTORY_BOOTSTRAP_DAYS, HISTORY_OVERLAP_DAYS, MarketData, tracked_symbols
from ingest.models import Bar

# after the close on 2026-10-16 ET
NOW = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.bars = {}
        self.earnings = [date(2026, 7, 30), date(2026, 10, 29)]
        self.fail = set()

    def _call(self, kind, *args):
        self.calls.append((kind, *args))
        if kind in self.fail:
            raise RuntimeError("Too Many Requests")

    def history(self, symbol, start, end):
        self._call("history", symbol, start, end)
        return [b for b in self.bars.get(symbol, []) if start <= b.date <= end]

    def quote(self, symbol):
        self._call("quote", symbol)
        return {"last_price": 700.0, "previous_close": 710.0, "currency": "USD"}

    def fundamentals(self, symbol):
        self._call("fundamentals", symbol)
        return {"forward_eps": 35.0, "book_value_per_share": 30.0}

    def earnings_dates(self, symbol):
        self._call("earnings", symbol)
        return list(self.earnings)


def bar(day, close, low=None, high=None):
    return Bar("KITE", day, close, high or close, low or close, close, 1000)


@pytest.fixture
def setup(tmp_path):
    conn = init_db(tmp_path / "test.db")
    provider = FakeProvider()
    provider.bars["KITE"] = [bar(date(2026, 10, 16) - timedelta(days=i), 700 + i) for i in range(380)]
    clock = {"now": NOW}
    md = MarketData(conn, provider, now=lambda: clock["now"])
    yield md, provider, clock
    conn.close()


def kinds_called(provider):
    return [c[0] for c in provider.calls]


def test_second_refresh_uses_cache(setup):
    md, provider, _ = setup
    assert set(md.refresh("KITE").values()) == {"fetched"}
    assert set(md.refresh("kite").values()) == {"cached"}
    assert len(provider.calls) == 4


def test_each_kind_expires_on_its_own(setup):
    md, provider, clock = setup
    md.refresh("KITE")
    clock["now"] = NOW + CACHE_TTL["quote"] + timedelta(seconds=1)
    status = md.refresh("KITE")
    assert status["quote"] == "fetched"
    assert status["fundamentals"] == "cached" and status["earnings"] == "cached"


def test_force_refetches(setup):
    md, provider, _ = setup
    md.refresh("KITE")
    md.refresh("KITE", kinds=["fundamentals"], force=True)
    assert kinds_called(provider).count("fundamentals") == 2


def test_history_bootstrap_then_incremental(setup):
    md, provider, clock = setup
    md.refresh("KITE", kinds=["history"])
    _, _, start, end = provider.calls[0]
    assert end == date(2026, 10, 16)
    assert start == end - timedelta(days=HISTORY_BOOTSTRAP_DAYS)

    clock["now"] = NOW + timedelta(days=1)
    md.refresh("KITE", kinds=["history"])
    _, _, start, _ = provider.calls[1]
    assert start == date(2026, 10, 16) - timedelta(days=HISTORY_OVERLAP_DAYS)


def test_partial_bar_is_overwritten(setup):
    md, provider, clock = setup
    md.refresh("KITE", kinds=["history"])
    provider.bars["KITE"][0] = bar(date(2026, 10, 16), 690)  # the close is corrected
    clock["now"] = NOW + CACHE_TTL["history"] + timedelta(seconds=1)
    md.refresh("KITE", kinds=["history"])
    assert md.bars("KITE", date(2026, 10, 16), date(2026, 10, 16))[0].close == 690


def test_failure_is_reported_and_retried(setup):
    md, provider, _ = setup
    provider.fail.add("fundamentals")
    status = md.refresh("KITE")
    assert status["fundamentals"].startswith("error") and status["quote"] == "fetched"
    provider.fail.clear()
    assert md.refresh("KITE")["fundamentals"] == "fetched"  # a failure does not record a fetch time, so it is retried next time


def test_estimated_earnings_date_moves(setup):
    md, provider, clock = setup
    md.refresh("KITE", kinds=["earnings"])
    assert md.next_earnings("KITE", date(2026, 10, 16)) == date(2026, 10, 29)

    provider.earnings = [date(2026, 7, 30), date(2026, 10, 27)]  # the estimated date moved
    clock["now"] = NOW + CACHE_TTL["earnings"] + timedelta(seconds=1)
    md.refresh("KITE", kinds=["earnings"])
    assert md.earnings_dates("KITE") == [date(2026, 7, 30), date(2026, 10, 27)]


def test_past_earnings_are_kept(setup):
    md, provider, clock = setup
    md.refresh("KITE", kinds=["earnings"])
    provider.earnings = [date(2026, 10, 29)]  # the source no longer returns past dates
    clock["now"] = NOW + CACHE_TTL["earnings"] + timedelta(seconds=1)
    md.refresh("KITE", kinds=["earnings"])
    assert date(2026, 7, 30) in md.earnings_dates("KITE")


def test_52_week_range_is_point_in_time(setup):
    md, provider, _ = setup
    provider.bars["KITE"] = [
        bar(date(2025, 9, 20), 500, low=480),  # older than 52 weeks (before 2025-10-02): ignored
        bar(date(2026, 3, 2), 600, low=590, high=610),
        bar(date(2026, 8, 3), 900, low=880, high=950),
        bar(date(2026, 10, 16), 700, low=650),  # after as_of: ignored
    ]
    md.refresh("KITE", kinds=["history"])
    assert md.fifty_two_week_range("KITE", date(2026, 10, 1)) == (590, 950)


def test_latest_snapshot_as_of(setup):
    md, _, clock = setup
    md.refresh("KITE", kinds=["quote"])
    assert md.latest("KITE", "quote")["last_price"] == 700.0
    assert md.latest("KITE", "quote", as_of=NOW - timedelta(minutes=1)) is None


def test_tracked_symbols(setup):
    md, _, _ = setup
    conn = md.conn
    conn.execute(
        "INSERT INTO positions (as_of, account_id, symbol, asset_class, quantity, source)"
        " VALUES ('2026-10-16', 'U0', 'KITE', 'STK', 12, 'flex'), ('2026-10-16', 'U0', 'USD.CAD', 'CASH', 1, 'flex'),"
        " ('2026-10-15', 'U0', 'OLD', 'STK', 1, 'flex')"
    )
    conn.execute("INSERT INTO watchlist (symbol, catalyst, invalidation) VALUES ('zest', 'earnings', 'falls below 150')")
    assert tracked_symbols(conn, extra=["QQQ"]) == ["KITE", "QQQ", "ZEST"]

"""IBKR watchlists → the candidate pool: ticker clean-up, FX skipped, duplicates merged, whole replacement,
and filling in the ticker on the Watchlist page."""

import json
from datetime import datetime, timezone

import pytest

from db.records import add_watch
from ingest.watchlists import candidates, import_file, list_names, normalize, save_watchlists, synced_at
from tests.test_app_pages import app_db, run  # noqa: F401  (the page tests' database and runner)

T0 = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)
LISTS = {
    "Chips": [{"symbol": "ZEST", "conid": "900005"}, {"symbol": "KITE"}, {"symbol": "000660"}],
    "Favorites": [{"symbol": "ORCA"}, {"symbol": "ZEST"}, {"symbol": "EUR.USD", "conid": "12087792@IDEALPRO"},
                  {"symbol": "BRK B"}],
    "Mining": [{"symbol": "LODE"}],
}


@pytest.mark.parametrize("raw, expected", [("zest", "ZEST"), ("BRK B", "BRK-B"), (" MOG  A ", "MOG-A"),
                                           ("EUR.USD", None), ("", None)])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_save_and_list_candidates(sample_db):
    conn, _ = sample_db
    counts = save_watchlists(conn, LISTS, T0)
    assert counts == {"Chips": 3, "Favorites": 3, "Mining": 1}  # the FX pair is skipped
    assert list_names(conn) == ["Chips", "Favorites", "Mining"]
    pool = {c.symbol: c for c in candidates(conn)}
    assert [c.symbol for c in candidates(conn)] == ["ZEST", "KITE", "000660", "ORCA", "BRK-B", "LODE"]
    assert pool["ZEST"].lists == ("Chips", "Favorites")  # one entry, both lists
    assert pool["BRK-B"].ibkr_symbol == "BRK B"
    assert not pool["000660"].supported and pool["KITE"].supported
    assert synced_at(conn) == "2026-10-18T22:00:00Z"


def test_sync_replaces_everything(sample_db):
    conn, _ = sample_db
    save_watchlists(conn, LISTS, T0)
    save_watchlists(conn, {"Chips": [{"symbol": "AMD"}]}, T0)
    assert [(c.symbol, c.lists) for c in candidates(conn)] == [("AMD", ("Chips",))]


def test_import_file_does_not_register_anything(sample_db, tmp_path):
    conn, _ = sample_db
    path = tmp_path / "ibkr_watchlists.json"
    path.write_text(json.dumps({"synced_at": "2026-10-18T22:00:00Z", "lists": LISTS}), encoding="utf-8")
    import_file(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM ibkr_watchlist").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0  # the pool is not registration


def test_watchlist_page_prefills_from_the_pool(app_db):
    save_watchlists(app_db, LISTS, T0)
    add_watch(app_db, "KITE", "10/28 earnings", "falls below 100")
    at = run("watchlist.py")
    assert "IBKR watchlists (candidate pool, 6)" in [h.value for h in at.subheader]
    picker = at.selectbox[0]
    assert "KITE" not in " ".join(picker.options)  # registered ones drop out
    assert all("000660" not in o for o in picker.options)  # so do tickers the buy check cannot handle
    picker.set_value("ZEST  ·  Chips, Favorites").run()
    assert at.text_input[0].value == "ZEST"

    at.text_input[1].input("November earnings, new product line")
    at.text_input[2].input("falls below 70")
    next(b for b in at.button if b.label == "Register").click().run()
    assert not at.exception, at.exception
    assert any("Registered ZEST" in s.value for s in at.success)
    assert "ZEST" not in " ".join(at.selectbox[0].options)
    table = at.dataframe[-1].value
    assert "ZEST" not in set(table["Ticker"]) and "000660" in set(table["Ticker"])  # unregistered only by default

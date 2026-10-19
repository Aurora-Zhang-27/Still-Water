"""Valuation: where each price sits between the pressure / cheap / normal / rich lines; holdings and the
watchlist shown separately."""

from datetime import timedelta

import pandas as pd
import streamlit as st

from app import charts, ui
from app.common import colors, connect, md, rulebook, show_table, theme_mode, utc_now
from ingest.market import MarketData
from ingest.valuation import latest_rows, zone
from rules.context import FUND_QUOTE_TYPES, account_state

book = rulebook()
conn = connect()
c = colors()
now = utc_now()
tz = book.calendar.tz
today = now.astimezone(tz).date()
market = MarketData(conn, tz=tz)

factor = book.valuation.pressure_multiple_of_normal
cyclical = ", ".join(sorted(book.valuation.cyclical_symbols))
ui.title("valuation", "Valuation lines",
         f"Pressure line = normal multiple × {factor:g} (a rate-hike scenario). EPS is updated every Sunday by "
         f"yfinance's revisions, keeping your basis." + (f" Cyclicals ({cyclical}) use price-to-book." if cyclical else ""))

rows = latest_rows(conn)
if not rows:
    with ui.panel("none"):
        st.info("No valuation lines yet: put valuation_seed.csv in the project root and run "
                "python -m ingest valuation import")
    st.stop()

held = set(account_state(conn, book, now).holdings)
items = []
for r in rows:
    bars = market.bars(r["symbol"], today - timedelta(days=14), today)
    if not bars:
        continue
    price = bars[-1].close
    lines = {k: r[f"{k}_line"] for k in ("pressure", "cheap", "normal", "rich")}
    items.append({
        "symbol": r["symbol"], "price": price, **lines, "zone": zone(price, lines),
        "below_pressure": price < lines["pressure"], "metric": r["metric"].upper(), "base": r["base_value"],
        "multiple": price / r["base_value"], "as_of": r["as_of"], "source": r["source"], "held": r["symbol"] in held,
        "price_date": bars[-1].date,
    })
no_price = sorted({r["symbol"] for r in rows} - {i["symbol"] for i in items})


def is_fund(symbol: str) -> bool:
    quote_type = (market.latest(symbol, "fundamentals") or {}).get("quote_type")
    return symbol == book.market_drop_day.index_symbol or quote_type in FUND_QUOTE_TYPES


# funds / ETFs skip the PE check (selection_pe.funds_exempt in rules.yaml), so no reminder for missing lines
no_lines = sorted(s for s in held - {r["symbol"] for r in rows}
                  if not (book.selection_pe.funds_exempt and is_fund(s)))

latest_as_of = max(r["as_of"] for r in rows)


def table(group: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([
        {"Ticker": i["symbol"], "Price": i["price"],
         "Band": i["zone"] + (" · below pressure" if i["below_pressure"] else ""),
         "vs. normal": (i["price"] / i["normal"] - 1) * 100, "Pressure": i["pressure"], "Cheap": i["cheap"],
         "Normal": i["normal"], "Rich": i["rich"], "Metric": i["metric"], "Base": i["base"], "Multiple": i["multiple"],
         "Lines as of": i["as_of"]}
        for i in group
    ])


groups = (("Holdings", [i for i in items if i["held"]]), ("Watchlist", [i for i in items if not i["held"]]))
for n, (name, group) in enumerate(groups):
    group.sort(key=lambda i: i["price"] / i["normal"])
    with ui.panel(f"group-{n}"):
        st.subheader(f"{name} ({len(group)})")
        if n == 0:
            st.caption(f"Lines as of {latest_as_of} · prices at the "
                       f"{max(i['price_date'] for i in items) if items else '—'} close · sorted by distance from the "
                       f"normal line. The dot is the price; the vertical rule is the pressure line ({factor:.0%} of "
                       f"normal, which can sit above the cheap line).")
            if no_lines:
                st.warning(f"These holdings have no valuation lines: {', '.join(no_lines)}. A buy will treat "
                           f"\"PE not high\" as missing data (blocked).")
            if no_price:
                st.caption(f"No market data, not drawn: {', '.join(no_price)}")
        if not group:
            ui.empty("None")
            continue
        st.altair_chart(charts.valuation_zones(group, c, theme_mode()), width="stretch", theme=None)
        with st.expander("Table"):
            show_table(table(group), formats={
                **{col: "{:.2f}" for col in ("Price", "Pressure", "Cheap", "Normal", "Rich", "Base")},
                "vs. normal": "{:+.1f}%", "Multiple": "{:.1f}",
            })

with ui.panel("single"):
    st.subheader("One stock")
    pick = st.selectbox("Pick", [i["symbol"] for i in sorted(items, key=lambda i: (not i["held"], i["symbol"]))])
    item = next(i for i in items if i["symbol"] == pick)
    bars = market.bars(pick, today - timedelta(days=183), today)
    st.altair_chart(
        charts.price_with_lines(pd.DataFrame([{"date": b.date, "close": b.close} for b in bars]),
                                {k: item[k] for k in ("pressure", "cheap", "normal", "rich")}, c),
        width="stretch", theme=None,
    )
    history = conn.execute("SELECT * FROM valuation WHERE symbol = ? ORDER BY as_of DESC", (pick,)).fetchall()
    st.markdown(md(f"**{pick}** at {item['price']:,.2f}, {item['zone']}, forward {item['metric']} "
                   f"{item['multiple']:.1f}× now (normal {history[0]['normal_multiple']:g}×)"))
    rows_hist = []
    for h in history:
        on = market.bars(pick, end=pd.Timestamp(h["as_of"]).date())
        price_then = on[-1].close if on else None
        rows_hist.append({
            "Date": h["as_of"], "Source": {"seed": "your seed", "yfinance": "weekly update", "manual": "manual"}[h["source"]],
            "Base": h["base_value"], "Multiple then": price_then / h["base_value"] if price_then else None,
            "Cheap": h["cheap_multiple"], "Normal": h["normal_multiple"], "Rich": h["rich_multiple"],
            "yfinance ref.": h["reference_value"],
        })
    st.caption(f"Forward {item['metric']} history (one row per week; multiple then = that day's close ÷ that week's base)")
    show_table(rows_hist, formats={"Base": "{:.2f}", "yfinance ref.": "{:.2f}", "Multiple then": "{:.1f}",
                                   "Cheap": "{:g}", "Normal": "{:g}", "Rich": "{:g}"})

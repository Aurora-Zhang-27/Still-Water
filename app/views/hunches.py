"""Hunch journal: when you feel like taking profit or waiting, write one sentence; the price is recorded
automatically and compared when it is due, to measure how good your instincts are."""

from datetime import timedelta

import streamlit as st

from app import ui
from app.common import connect, et, md, pct, rulebook, show_table, utc_now
from db.records import CALLS, accuracy, add_hunch, clean_symbol, hunches
from ingest.market import MarketData

book = rulebook()
conn = connect()
now = utc_now()
tz = book.calendar.tz
today = now.astimezone(tz).date()
days = book.hunches.review_after_days
market = MarketData(conn, tz=tz)

ui.title("hunches", "Hunch journal", f"Write down the call you are making; the price is recorded for you and "
         f"compared with the close {days} days later. This tests your instincts — it is not for placing orders.")


def latest_price(symbol: str) -> float | None:
    market.refresh(symbol, kinds=["quote"])  # cached; no new request within 15 minutes
    quote = market.latest(symbol, "quote") or {}
    if quote.get("last_price"):
        return quote["last_price"]
    bars = market.bars(symbol, end=today)
    return bars[-1].close if bars else None


with ui.panel("add"):
    with st.form("add_hunch", clear_on_submit=True):
        c1, c2 = st.columns([1, 2])
        symbol = c1.text_input("Ticker", placeholder="AAPL")
        call = c2.radio("Call", list(CALLS), horizontal=True)
        note = st.text_input("One sentence", placeholder="Ran up too fast; take some off before earnings")
        submitted = st.form_submit_button("Record", type="primary")
    if submitted:
        try:
            sym = clean_symbol(symbol)
            price = latest_price(sym)
            add_hunch(conn, sym, CALLS[call], note, price, today, days)
            st.success(md(f"Recorded: {sym} at {price:,.2f}; reviewed in {days} days "
                          f"({today + timedelta(days=days):%Y-%m-%d})."))
        except ValueError as exc:
            st.error(str(exc))

acc = accuracy(conn)
up, down = acc.by_move["up"], acc.by_move["down"]
ui.kpi_row([
    ui.Kpi("Accuracy", pct(acc.rate, 0), "reviewed hunches only"),
    ui.Kpi("Reviewed", str(acc.reviewed),
           f"up {up[1]}/{up[0]} right; down {down[1]}/{down[0]} right" if acc.reviewed else "none due yet"),
    ui.Kpi("Awaiting review", str(acc.pending), f"reviewed automatically after {days} days"),
])

items = hunches(conn)
pending = [h for h in items if not h.reviewed]
done = [h for h in items if h.reviewed]

with ui.panel("pending"):
    st.subheader("Awaiting review")
    if not pending:
        ui.empty("No hunches recorded yet. Next time you feel like taking profit or waiting, write it above.")
    else:
        rows = []
        for h in pending:
            bars = market.bars(h.symbol, end=today)
            last = bars[-1].close if bars else None
            rows.append({
                "Ticker": h.symbol, "Call": h.expected_move, "Note": h.note,
                "Recorded (ET)": et(h.created_at, book), "Price then": h.price_at_entry,
                "Since": (last / h.price_at_entry - 1) * 100 if last else None,
                "Review on": h.review_due_on.isoformat(), "Days left": (h.review_due_on - today).days,
            })
        show_table(rows, formats={"Price then": "{:.2f}"}, changes=("Since",))

with ui.panel("done"):
    st.subheader("Reviewed")
    if not done:
        ui.empty(f"Nothing due yet. The first hunch is reviewed by the daily job {days} days after you record it.")
    else:
        show_table([
            {"Result": "✓ right" if h.was_right else "✗ wrong", "Ticker": h.symbol,
             "Call": h.expected_move, "Note": h.note,
             "Recorded": h.created_at.astimezone(tz).date().isoformat(), "Price then": h.price_at_entry,
             "Review on": h.review_due_on.isoformat(), "Price at review": h.price_at_review,
             "Change": (h.price_at_review / h.price_at_entry - 1) * 100}
            for h in done
        ], formats={"Price then": "{:.2f}", "Price at review": "{:.2f}"}, changes=("Change",))

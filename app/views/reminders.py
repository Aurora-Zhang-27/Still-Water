"""Sell reminders: staged take-profits, above the rich line, invalidations. Selling has no hard blocks;
this page only reminds."""

import streamlit as st

from app import ui
from app.common import connect, md, rulebook, show_table, utc_now
from audit.reminders import holding_status, invalidation_status, reminders
from ingest.market import MarketData
from rules.context import account_state

book = rulebook()
conn = connect()
now = utc_now()
tz = book.calendar.tz
today = now.astimezone(tz).date()
market = MarketData(conn, tz=tz)

levels = ", ".join(f"+{lvl:.0%}" for lvl in book.take_profit_reminder.gain_levels)
ui.title("reminders", "Sell reminders", f"Selling has no hard blocks, only reminders: take profits in stages at "
         f"{levels}; price above the rich line; a watchlist invalidation triggered. Prices are the latest close.")

held = set(account_state(conn, book, now).holdings)
statuses = holding_status(conn, book, market, now, today)
invalidations = invalidation_status(conn, market, today, held)
alerts = reminders(book, statuses, invalidations)

with ui.panel("now"):
    st.subheader("Needs a look now")
    if not alerts:
        st.success("🟢 Nothing to act on")
    for a in alerts:
        st.markdown(md(f"{'🟡' if a.level == 'warn' else '🔔'} {a.text}"))

with ui.panel("holdings"):
    st.subheader("Holdings")
    if not statuses:
        ui.empty("No position data")
    else:
        show_table(
            [
                {
                    "Ticker": s.symbol, "Shares": s.quantity, "Cost": s.avg_cost, "Price": s.price,
                    "P&L": s.gain * 100 if s.gain is not None else None,
                    "Level reached": f"+{s.reached:.0%}" if s.reached is not None else "",
                    "Next level": f"+{s.next_level:.0%}" if s.next_level is not None else "all reached",
                    "Next level price": s.next_level_price,
                    "To next level": (s.next_level_price / s.price - 1) * 100 if s.next_level_price else None,
                    "Valuation band": s.zone or "—", "Rich line": s.rich,
                }
                for s in sorted(statuses, key=lambda s: -(s.gain or -9))
            ],
            formats={"Shares": "{:g}", "Cost": "{:.2f}", "Price": "{:.2f}", "Next level price": "{:.2f}",
                     "To next level": "{:+.1f}%", "Rich line": "{:.2f}"},
            changes=("P&L",),
        )
        st.caption("Sorted by unrealized gain. Take-profit levels are measured from cost; valuation bands come from "
                   "the Valuation page.")

with ui.panel("invalidation"):
    st.subheader("Invalidations")
    if not invalidations:
        ui.empty("Nothing registered on the watchlist yet. Write catalysts and invalidations on the Watchlist page "
                 "and they are watched here.")
    else:
        priced = [w for w in invalidations if w.target is not None]
        events = [w for w in invalidations if w.target is None]
        if priced:
            show_table(
                [
                    {
                        "Status": "🔔 triggered" if w.triggered else "not triggered", "Ticker": w.symbol,
                        "Invalidation": w.invalidation,
                        "Price trigger": f"{'below' if w.when == 'below' else 'above'} {w.target:,.2f}",
                        "Price": w.price,
                        "To trigger": w.distance * 100 if w.distance is not None else None,
                        "": "holding" if w.held else "watching",
                    }
                    for w in sorted(priced, key=lambda w: (not w.triggered, w.distance if w.distance is not None else 9))
                ],
                formats={"Price": "{:.2f}", "To trigger": "{:.1f}%"},
            )
            st.caption("Price triggers are watched automatically; a triggered one needs a review.")
        if events:
            st.markdown("##### Event-based (your judgement)")
            for w in events:
                st.markdown(md(f"• **{w.symbol}** ({'holding' if w.held else 'watching'}): {w.invalidation}"))

with ui.panel("orders"):
    st.subheader("Open orders")
    st.info(md(f"IB Gateway is not connected, so check open orders in IBKR yourself. Consider cancelling ones open "
               f"longer than {book.gtc_review.stale_after_days} days; to see whether a buy order would break a rule "
               "when it fills, run a check at its limit price on the Buy check page."))

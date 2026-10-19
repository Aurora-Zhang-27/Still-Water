"""Buy check: red / yellow / green lights; when blocked, write a one-line reason and start a 24-hour
cooldown. Every check is written to the checks table."""

import re
from datetime import timedelta

import pandas as pd
import streamlit as st

from app import charts, ui
from app.common import ICON, colors, connect, duration, et, md, pct, rulebook, utc_now
from app.logic import cooldowns, override_check, record_check, recent_checks
from db.database import parse_utc_text
from ingest.market import MarketData
from ingest.valuation import zone
from rules.engine import LABELS, SELECTION_RULES, ProposedTrade, check

SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

book = rulebook()
conn = connect()
c = colors()
now = utc_now()
market = MarketData(conn, tz=book.calendar.tz)

ui.title("buy_check", "Buy check", "The program only checks; it never places orders for you — place them yourself "
                                    "in IBKR. Every check is recorded permanently, including blocked ones you drop.")

with ui.panel("input"):
    col_symbol, col_qty, col_price = st.columns(3)
    symbol = col_symbol.text_input("Ticker", key="symbol", placeholder="e.g. NVDA").strip().upper()
    valid_symbol = bool(SYMBOL_PATTERN.match(symbol))
    if symbol and not valid_symbol:
        col_symbol.error("A ticker may only contain letters, digits, dots or dashes")

    quote = None
    if valid_symbol:
        market.refresh(symbol, kinds=["quote"])  # cached; no new request within 15 minutes
        quote = market.latest(symbol, "quote")
    quantity = col_qty.number_input("Shares", min_value=1.0, value=1.0, step=1.0, format="%g", key="quantity")
    default_price = quote.get("last_price") if quote else None
    price = col_price.number_input("Price", min_value=0.0, value=default_price, step=0.01, format="%.2f",
                                   key=f"price_{symbol}")
    if quote and quote.get("last_price"):
        col_price.caption(f"Last {quote['last_price']:.2f} ({quote.get('session_date', '')})")

    if st.button("Check", type="primary", disabled=not (valid_symbol and price)):
        with st.spinner("Refreshing market data and checking every rule…"):
            for s in {symbol, book.market_drop_day.index_symbol}:
                market.refresh(s)
            ev = check(conn, book, ProposedTrade(symbol, "BUY", quantity, price), md=market)
            st.session_state["last_check"] = {"id": record_check(conn, ev), "ev": ev}

last = st.session_state.get("last_check")
if last and last["ev"].trade.symbol == symbol:  # a different ticker hides the previous result
    ev, check_id = last["ev"], last["id"]
    ctx, trade = ev.context, ev.trade
    blocks, warns = ev.by_status("block"), ev.by_status("warn")
    with ui.panel("result"):
        st.subheader(md(f"Buy {trade.quantity:g} {trade.symbol} @ {trade.price:,.2f} (${trade.amount:,.2f})"))
        st.caption(f"Checked for trading day {ctx.trade_date}: positions at the {ctx.snapshot_as_of} close plus later "
                   f"fills · check #{check_id}")

        if ev.verdict == "block":
            st.error(f"🔴 Blocked: {len(blocks)} hard rule(s) failed. To override, you must write a one-line reason "
                     f"and wait {book.override.cooldown_hours:g} hours.")
        elif ev.verdict == "warn":
            st.warning(f"🟡 You may buy, with {len(warns)} warning(s)")
        else:
            st.success("🟢 Everything passes")

        weight = ev.result("position_cap").details.get("weight_after")
        cash_after = ev.result("cash_floor").details.get("cash_after_ratio")
        m1, m2, m3 = st.columns(3)
        m1.metric("Weight after", pct(weight), help=f"cap {book.position_cap.max_weight(trade.symbol):.0%}")
        m2.metric("Cash after", pct(cash_after))
        if ctx.valuation:
            lines = ctx.valuation.as_lines()
            m3.metric("Valuation band", zone(trade.price, lines))
            if trade.price < lines["pressure"]:
                m3.caption("Still below the pressure line")
            st.altair_chart(charts.valuation_strip(lines, trade.price, c), width="stretch", theme=None)
        else:
            m3.metric("Valuation band", "—")
            m3.caption("This stock has no valuation lines yet")

        def show(results):
            for r in results:
                st.markdown(f"{ICON[r.status]} **{LABELS[r.rule_id]}** — {r.reason}")

        left, right = st.columns(2)
        with left:
            st.markdown("##### The three selection rules")
            show([r for r in ev.results if r.rule_id in SELECTION_RULES])
        with right:
            st.markdown("##### Position, cash, pace")
            show([r for r in ev.results if r.rule_id not in SELECTION_RULES])
        for note in ctx.notes:
            st.caption(f"ⓘ {note}")

    if ev.verdict == "block":
        row = conn.execute("SELECT override_reason, cooldown_until FROM checks WHERE id = ?", (check_id,)).fetchone()
        with ui.panel("override"):
            if row["override_reason"]:
                until = parse_utc_text(row["cooldown_until"])
                st.info(f"⏳ Reason written: {row['override_reason']}\n\nCooldown until {et(until, book)} ET, "
                        f"{duration(max(until - now, timedelta(0)))} left. Order only after it ends.")
            else:
                with st.form(f"override_{check_id}"):
                    st.markdown("##### Override?")
                    reason = st.text_area("Reason (one sentence; cannot be changed and is kept forever)",
                                          placeholder="Why is this one worth an exception?")
                    submitted = st.form_submit_button(
                        f"Write the reason and start the {book.override.cooldown_hours:g}-hour cooldown")
                if submitted:
                    try:
                        override_check(conn, book, check_id, reason, utc_now())
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
                st.caption("No reason means you drop this buy; the check is recorded either way.")

with ui.panel("cooldowns"):
    st.subheader("Cooldowns")
    items = cooldowns(conn, now)
    if not items:
        ui.empty("No cooldowns running. When you override a block with a reason, the countdown shows here.")
    for cd in items:
        status = f"{duration(cd.remaining(now))} left" if cd.active(now) else "ended, you may order"
        st.markdown(f"{'⏳' if cd.active(now) else '✅'} **{cd.symbol}** {cd.quantity:g} sh @ {cd.price:,.2f} · "
                    f"{status} (until {et(cd.until, book)} ET)  \nReason: {cd.reason}")

    with st.expander("Recent checks"):
        rows = recent_checks(conn)
        if not rows:
            st.caption("No checks yet")
        else:
            st.dataframe(
                pd.DataFrame([
                    {"#": r["id"], "Time (ET)": et(parse_utc_text(r["created_at"]), book), "Ticker": r["symbol"],
                     "Shares": r["quantity"], "Price": r["price"], "Verdict": f"{ICON[r['verdict']]} {r['verdict']}",
                     "Override reason": r["override_reason"] or ""}
                    for r in rows
                ]),
                hide_index=True, width="stretch",
            )

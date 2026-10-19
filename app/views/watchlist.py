"""Watchlist: register catalysts and invalidations while calm. The time is recorded automatically, and an
entry counts as "planned" only after 24 hours."""

from datetime import timedelta

import pandas as pd
import streamlit as st

from app import ui
from app.common import connect, duration, et, md, rulebook, utc_now
from db.records import active_watch, add_watch, clean_symbol, remove_watch, removed_watch
from db.database import parse_utc_text
from ingest.watchlists import candidates, list_names, synced_at
from rules.context import account_state

book = rulebook()
conn = connect()
now = utc_now()
need_hours = book.selection_planned.min_hours_on_watchlist

ui.title("watchlist", "Watchlist", f"Register while you are calm, not when you are about to order. The program records "
         f"the time and it cannot be changed; an entry counts as \"planned\" after {need_hours:g} hours. For a stock "
         f"not registered here, the buy check blocks on both \"Has catalyst\" and \"Planned\".")

items = active_watch(conn)
registered = {i.symbol for i in items}
pool = candidates(conn)

with ui.panel("add"):
    picked = ""
    open_pool = [c for c in pool if c.supported and c.symbol not in registered]
    if open_pool:
        # no key: when the options change (say, right after registering one) it is a new widget and resets
        labels = {"—": ""} | {f"{c.symbol}  ·  {', '.join(c.lists)}": c.symbol for c in open_pool}
        picked = labels[st.selectbox(f"Pick one from your IBKR watchlists ({len(open_pool)} not registered yet)",
                                     list(labels),
                                     help="This only fills in the ticker; you still write the catalyst and the "
                                          "invalidation yourself — that step is the point")]
    with st.form("add_watch", clear_on_submit=True):
        st.markdown("##### Register a stock")
        c1, c2 = st.columns([1, 3])
        symbol = c1.text_input("Ticker", value=picked, placeholder="NVDA")
        catalyst = c2.text_input("Catalyst (event + time window)",
                                 placeholder="November earnings: guidance raised, data-center revenue beats")
        c3, c4 = st.columns([1, 3])
        due = c3.date_input("Window ends (optional)", value=None,
                            help="If it has not played out by this day, the buy check asks you to register again")
        invalidation = c4.text_input("What would prove you wrong (a price or an event)",
                                     placeholder="Falls below 150, or guidance is cut")
        c5, c6, _ = st.columns([1.4, 1, 1.6])
        price = c5.number_input("Invalidation price (optional)", min_value=0.0, value=None, step=1.0, format="%.2f",
                                help="With a price, the program watches it weekly and reminds you to review")
        when = c6.selectbox("Direction", ["falls below", "rises above"])
        submitted = st.form_submit_button("Register", type="primary")
    if submitted:
        try:
            sym = clean_symbol(symbol)
            add_watch(conn, sym, catalyst, invalidation, catalyst_due_on=due,
                      invalidation_price=price or None,
                      invalidation_when=({"falls below": "below", "rises above": "above"}[when] if price else None))
            ready = et(utc_now() + timedelta(hours=need_hours), book)
            flash = [("success", md(f"Registered {sym}. It counts as planned after {ready} ET."))]
            if book.reduce_only_list.contains(sym):
                flash.append(("info", f"{sym} is on the reduce-only list: this entry counts as \"a catalyst "
                                      f"registered again\", one of the three conditions for leaving the list."))
            # rerun so the pool and the list drop the stock straight away; the messages show after the rerun
            st.session_state["watch_flash"] = flash
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    for kind, text in st.session_state.pop("watch_flash", []):
        getattr(st, kind)(text)

with ui.panel("list"):
    valued = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM valuation")}
    st.subheader(f"Watching ({len(items)})")
    if not items:
        ui.empty("The pond is still: nothing registered yet. While you are calm, write down what you are watching.")
    else:
        st.dataframe(
            pd.DataFrame([
                {
                    "#": i.id, "Ticker": i.symbol, "Catalyst": i.catalyst,
                    "Window ends": i.catalyst_due_on.isoformat() if i.catalyst_due_on else "",
                    "Invalidation": i.invalidation,
                    "Price trigger": (f"{'below' if i.invalidation_when == 'below' else 'above'} "
                                      f"{i.invalidation_price:,.2f}" if i.invalidation_price else ""),
                    "Registered (ET)": et(i.created_at, book),
                    "Planned": "✓" if i.hours_on_list(now) >= need_hours
                    else f"needs {duration(timedelta(hours=need_hours - i.hours_on_list(now)))}",
                    "Valuation lines": "✓" if i.symbol in valued else "missing",
                }
                for i in items
            ]),
            hide_index=True, width="stretch",
        )
        expired = [i for i in items if i.catalyst_due_on and i.catalyst_due_on < now.astimezone(book.calendar.tz).date()]
        if expired:
            st.warning("Catalyst window passed, register again: "
                       + ", ".join(f"{i.symbol} ({i.catalyst_due_on})" for i in expired))

        with st.form("remove_watch"):
            st.markdown("##### Remove")
            labels = {f"#{i.id} {i.symbol}: {i.catalyst[:30]}": i.id for i in items}
            pick = st.selectbox("Which entry", list(labels))
            reason = st.text_input("Reason (cannot be changed)",
                                   placeholder="Catalyst fell through / bought it and registered again")
            if st.form_submit_button("Remove"):
                try:
                    remove_watch(conn, labels[pick], reason, utc_now())
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with st.expander("Removed entries"):
        rows = removed_watch(conn)
        if not rows:
            ui.empty("Nothing removed yet")
        else:
            st.dataframe(pd.DataFrame([
                {"Ticker": r["symbol"], "Catalyst": r["catalyst"],
                 "Registered (ET)": et(parse_utc_text(r["created_at"]), book),
                 "Removed (ET)": et(parse_utc_text(r["removed_at"]), book), "Reason": r["removed_reason"]}
                for r in rows
            ]), hide_index=True, width="stretch")

with ui.panel("ibkr"):
    st.subheader(f"IBKR watchlists (candidate pool, {len(pool)})")
    if not pool:
        ui.empty("No IBKR watchlists imported yet. Run python -m ingest watchlists (reads data/ibkr_watchlists.json).")
    else:
        held = set(account_state(conn, book, now).holdings)
        valued = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM valuation")}
        st.caption(md(
            f"Synced {et(parse_utc_text(synced_at(conn)), book)} ET. Registered {sum(c.symbol in registered for c in pool)}, "
            f"held {sum(c.symbol in held for c in pool)}, with valuation lines {sum(c.symbol in valued for c in pool)}. "
            "The pool does not count as registration: the buy check's \"Has catalyst\" and \"Planned\" only read the "
            "entries above."))
        f1, f2 = st.columns([2, 1])
        names = list_names(conn)
        group = f1.selectbox("IBKR list", ["All"] + names)
        only_open = f2.toggle("Not registered only", value=True)
        shown = [c for c in pool if (group == "All" or group in c.lists) and not (only_open and c.symbol in registered)]
        if not shown:
            ui.empty("Everything in this list is registered")
        else:
            st.dataframe(pd.DataFrame([
                {"Ticker": c.symbol, "IBKR lists": ", ".join(c.lists),
                 "Registered": "✓" if c.symbol in registered else "", "Held": "✓" if c.symbol in held else "",
                 "Valuation lines": "✓" if c.symbol in valued else "missing",
                 "Note": "" if c.supported else "non-US ticker; the buy check does not support it"}
                for c in shown
            ]), hide_index=True, width="stretch")

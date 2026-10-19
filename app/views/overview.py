"""Overview: cash, this week's buying budget, positions and caps, current violations and reminders."""

from fractions import Fraction

import streamlit as st

from app import charts, ui
from app.common import (
    ICON, LEVEL_COLOR, colors, connect, duration, et, md, money, pct, rulebook, show_table, utc_now,
)
from app.logic import overview
from rules.engine import LABELS

book = rulebook()
conn = connect()
c = colors()
now = utc_now()
ov = overview(conn, book, now)

ui.title("overview", "Overview",
         f"Account data as of {ov.as_of or '—'} close · next trading day {ov.trade_date} · rules v{book.version}")

blocks = [a for a in ov.alerts if a.level == "block"]
warns = [a for a in ov.alerts if a.level == "warn"]
cash_level = ov.cash_tier.level if ov.cash_tier else "pass"

# NAV and cash ratio over the last 30 trading days, drawn as small trend lines in the cards
history = conn.execute(
    "SELECT net_liquidation, cash FROM account_snapshots ORDER BY as_of DESC LIMIT 30").fetchall()[::-1]
frac = Fraction(book.weekly_buy_budget.max_fraction_of_week_start_cash).limit_denominator(12)
week_fraction = ov.week_used / ov.week_budget if ov.week_budget else 0.0
ui.kpi_row([
    ui.Kpi("Net liquidation", money(ov.nav), f"last {len(history)} trading days",
           spark=[r["net_liquidation"] for r in history]),
    ui.Kpi("Cash ratio", pct(ov.cash_ratio),
           f"below {ov.cash_tier.below:.0%}" if ov.cash_tier else "above the warning line", tone=cash_level,
           spark=[r["cash"] / r["net_liquidation"] for r in history]),
    ui.Kpi("Bought this week", money(ov.week_used),
           f"budget {money(ov.week_budget)} ({frac} of week-start cash)" if ov.week_budget is not None
           else "no week-start cash",
           tone="warn" if week_fraction > 1 else "pass", meter=(week_fraction, [1.0])),
    ui.Kpi("Current violations", str(len(blocks)),
           "; ".join(a.text.split(",")[0] for a in blocks[:2]) or "none", tone="block" if blocks else "pass",
           hint=f"plus {len(warns)} warning(s)" if warns else ""),
])

left, right = st.columns(2)
with left, ui.panel("cash"):
    st.subheader("Cash")
    if ov.cash_ratio is None:
        st.info("No account data yet")
    else:
        tiers = book.cash_floor.tiers
        markers = [(t.below, f"{t.below:.0%} {'block' if t.level == 'block' else 'warning'}") for t in tiers]
        top = max(max(t.below for t in tiers) * 2.5, ov.cash_ratio * 1.2)
        st.altair_chart(
            charts.meter(ov.cash_ratio, markers, fill=LEVEL_COLOR[cash_level], c=c, domain_max=top,
                         value_text=f"{ov.cash_ratio:.1%} ({money(ov.cash)})"),
            width="stretch", theme=None,
        )
        st.caption(md(f"{ICON[cash_level]} Cash {money(ov.cash)}, {pct(ov.cash_ratio)} of NAV. A buy that leaves "
                      f"less than {tiers[0].below:.0%} is blocked; less than {tiers[-1].below:.0%} warns."))

with right, ui.panel("week"):
    st.subheader("Buying this week")
    if ov.week_budget is None:
        st.info("No week-start cash, so no budget can be computed")
    else:
        over = ov.week_used > ov.week_budget
        top = max(ov.week_budget * 1.5, ov.week_used * 1.15, 1)
        st.altair_chart(
            charts.meter(ov.week_used, [(ov.week_budget, f"budget {money(ov.week_budget)}")],
                         fill=LEVEL_COLOR["warn" if over else "pass"], c=c, domain_max=top,
                         value_text=money(ov.week_used)),
            width="stretch", theme=None,
        )
        left_amount = ov.week_budget - ov.week_used
        st.caption(md(f"{ICON['warn' if over else 'pass']} Used {money(ov.week_used)}, "
                      + (f"{money(-left_amount)} over" if over else f"{money(left_amount)} left")
                      + f". The week starts {ov.week_start}; week-start cash {money(ov.week_start_cash)}."))

with ui.panel("positions"):
    st.subheader("Positions and caps")
    if not ov.holdings:
        st.info("No position data yet")
    else:
        st.altair_chart(charts.positions(ov.holdings, c), width="stretch", theme=None)
        special = ", ".join(f"{s} {w:.0%}" for s, w in book.position_cap.symbol_max_weight.items())
        st.caption(f"The red tick is the cap: {book.position_cap.default_max_weight:.0%} by default"
                   + (f", {special}" if special else "") + ". The part over a cap is red, and a stock over its cap "
                   f"cannot be bought. Valued at the {ov.as_of} close.")
        with st.expander("Table"):
            show_table(
                [{"Ticker": r.symbol, "Shares": r.quantity, "Cost": r.avg_cost, "Price": r.price, "Value": r.value,
                  "Weight": r.weight * 100, "Cap": r.cap * 100,
                  "P&L": r.unrealized * 100 if r.unrealized is not None else None,
                  "Status": "⚠ over cap" if r.over_cap else ("reduce-only" if r.reduce_only else "")}
                 for r in ov.holdings],
                formats={"Shares": "{:g}", "Cost": "{:.2f}", "Price": "{:.2f}", "Value": "${:,.0f}",
                         "Weight": "{:.1f}%", "Cap": "{:.0f}%"},
                changes=("P&L",),
            )

left, right = st.columns([3, 2])
with left, ui.panel("alerts"):
    st.subheader("Violations and reminders")
    order = {"block": 0, "warn": 1, "info": 2}
    if not ov.alerts:
        st.success("🟢 No violations")
    for alert in sorted(ov.alerts, key=lambda a: order[a.level]):
        st.markdown(f"{ICON[alert.level]} {alert.text}")
with right, ui.panel("cooldowns"):
    st.subheader("Overrides cooling down")
    if not ov.cooldowns:
        ui.empty("No cooldowns running. When you override a block with a reason, the countdown shows here.")
    for cd in ov.cooldowns:
        if cd.active(now):
            st.markdown(f"⏳ **{cd.symbol}** {cd.quantity:g} sh @ {cd.price:,.2f} · {duration(cd.remaining(now))} left"
                        f" (until {et(cd.until, book)} ET)  \nReason: {cd.reason}")
        else:
            st.markdown(f"✅ **{cd.symbol}** {cd.quantity:g} sh @ {cd.price:,.2f} · cooldown ended {et(cd.until, book)}"
                        f"  \nReason: {cd.reason}")

with ui.panel("audit"):
    st.subheader("Audit log")
    if not ov.audited:
        st.caption(f"No violations in the test period (from {book.audit.start_date}). New fills are audited after "
                   "every close; violations are recorded here and can never be deleted.")
    else:
        def names(rule_ids):
            return ", ".join(LABELS.get(r, r) for r in rule_ids)

        def trade_text(a):
            if a.quantity is None:
                return "buy check"
            return f"{'' if a.side == 'BUY' else 'sell '}{a.quantity:g} sh @ {a.price:,.2f}"

        def verdict(a):
            if a.rules("block"):
                return f"{ICON['block']} blocked"
            return f"{ICON['warn']} warning" if a.rules("warn") else "missing data"

        st.caption(f"One row per buy: {len(ov.audited)} with violations, {ov.violation_count} violation records in "
                   "total (one per rule a buy broke). Rules are re-run on the data at fill time; blocks caused by "
                   "missing data are listed separately and left out of the review statistics.")
        show_table([
            {"Filled (ET)": et(a.executed_at, book) if a.executed_at else "", "Ticker": a.symbol,
             "Buy": trade_text(a), "Verdict": verdict(a), "Broke": names(a.rules("block")),
             "Warnings": names(a.rules("warn")), "Missing data": names(a.rules(missing=True))}
            for a in ov.audited
        ])
        with st.expander("Rule by rule"):
            labels = {f"{et(a.executed_at, book) if a.executed_at else 'check'} · {a.symbol} · {trade_text(a)}": a
                      for a in ov.audited}
            picked = labels[st.selectbox("Which buy", list(labels), key="audit_pick")]
            for v in picked.items:
                icon = ICON.get(v["level"], ICON["info"])
                tag = " (missing data)" if picked.is_missing(v) else ""
                st.markdown(md(f"{icon} **{LABELS.get(v['rule_id'], v['rule_id'])}**{tag} — {v['message']}"))

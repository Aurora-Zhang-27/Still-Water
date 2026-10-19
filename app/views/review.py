"""Review: the test period's three measures + violation counts + hunch accuracy + monthly reports.

The measures from the spec: compliance rate, returns of compliant vs. violating trades, and how blocked
trades did afterwards. Win rate is deliberately left out.
"""

from datetime import timedelta

import streamlit as st

from app import charts
from app import ui
from app.common import colors, connect, pct, rulebook, show_table, utc_now
from audit.stats import (
    GROUPS,
    average,
    blocked_not_bought,
    buy_outcomes,
    check_intents,
    compliance,
    monthly,
    violations_by_rule,
)
from db.records import accuracy
from ingest.market import MarketData
from rules.engine import LABELS

book = rulebook()
conn = connect()
c = colors()
now = utc_now()
tz = book.calendar.tz
today = now.astimezone(tz).date()
start = book.audit.start_date
market = MarketData(conn, tz=tz)

index_bars = market.bars(book.market_drop_day.index_symbol, end=today)
as_of = index_bars[-1].date if index_bars else today

ui.title("review", "Review", f"The test period starts {start}; returns run to the {as_of} close and are compared "
         "with QQQ over the same period. With a small sample, ignore win rate and look at three things: did you "
         "comply when blocked, do disciplined trades do better, and how did the ones you skipped do afterwards.")

audited = conn.execute("SELECT COUNT(*) FROM trade_audits").fetchone()[0]
if not audited:
    with ui.panel("none"):
        st.info("No audit records yet: they appear after the daily job runs (python -m audit daily)")
    st.stop()

intents = check_intents(conn, book, start, today + timedelta(days=1))
comp = compliance(intents)
live = buy_outcomes(conn, book, market, "live", as_of)
baseline = buy_outcomes(conn, book, market, "baseline", as_of)
hunch = accuracy(conn)
COMPLIANT, VIOLATION, MISSING_ONLY = GROUPS

ui.kpi_row([
    ui.Kpi("Compliance", pct(comp.rate, 0),
           f"blocked {comp.blocked}: complied {comp.complied}, broke {comp.broke}" if comp.blocked
           else "no blocked checks yet",
           hint="Share of blocked buy intents you did not buy (several checks of one stock on one day count once)"),
    ui.Kpi("Compliant buys, excess", pct(average([o.excess for o in live[COMPLIANT]])), f"{len(live[COMPLIANT])} buys",
           tone="pass" if live[COMPLIANT] else None, hint="Average excess return vs. QQQ over the same period"),
    ui.Kpi("Violating buys, excess", pct(average([o.excess for o in live[VIOLATION]])), f"{len(live[VIOLATION])} buys",
           tone="block" if live[VIOLATION] else None, hint="Average excess return vs. QQQ over the same period"),
    ui.Kpi("Hunch accuracy", pct(hunch.rate, 0), f"{hunch.reviewed} reviewed, {hunch.pending} waiting"),
])


def avg_pct(values):
    value = average(values)
    return value * 100 if value is not None else None  # leave it empty rather than show 0%


with ui.panel("returns"):
    st.subheader("Compliant vs. violating: after the buy")
    show_baseline = st.toggle("Add the pre-launch baseline", value=False,
                              help=f"Fills before {start}, audited with the current rules for comparison only; "
                                   "no violations recorded")
    groups = {f"Test · {g}": live[g] for g in GROUPS if live[g]}
    if show_baseline:
        groups |= {f"Baseline · {g}": baseline[g] for g in GROUPS if baseline[g]}
    if any(groups.values()):
        st.altair_chart(charts.excess_strip(groups, c), width="stretch", theme=None)
        st.caption("Each dot is a buy; right of 0 beat QQQ over the same period. \"Missing data only\" means it was "
                   "blocked only because data such as valuation lines was missing.")
    table = []
    for name, data in (("Test period", live), ("Pre-launch baseline", baseline)):
        for g in GROUPS:
            items = data[g]
            table.append({"Period": name, "Group": g, "Buys": len(items),
                          "Avg return": avg_pct([o.ret for o in items]),
                          "QQQ same period": avg_pct([o.index_ret for o in items]),
                          "Excess": avg_pct([o.excess for o in items])})
    show_table(table, changes=("Avg return", "QQQ same period", "Excess"))

with ui.panel("rules"):
    st.subheader("Violations by rule")
    counts = violations_by_rule(conn)
    if not counts:
        ui.empty("No violations in the test period yet")
    else:
        st.altair_chart(charts.rule_bars([(LABELS.get(k, k), n) for k, n in counts.most_common()], c),
                        width="stretch", theme=None)
        st.caption("Test period to date, excluding blocks caused by missing data.")

with ui.panel("blocked"):
    st.subheader("How blocked trades did afterwards")
    blocked = blocked_not_bought(market, book, intents, as_of)
    if not blocked:
        ui.empty("No blocked checks you skipped yet. Once you use the buy check, this shows how the ones you did "
                 "not buy did afterwards.")
    else:
        show_table([
            {"Intent": o.label, "Check price": o.entry, "Price now": o.latest, "Change": o.ret * 100,
             "QQQ same period": o.index_ret * 100 if o.index_ret is not None else None}
            for o in blocked
        ], formats={"Check price": "{:.2f}", "Price now": "{:.2f}"}, changes=("Change", "QQQ same period"))
        st.caption(f"Of the ones you skipped, {sum(o.ret < 0 for o in blocked)} fell afterwards and "
                   f"{sum(o.ret > 0 for o in blocked)} rose.")

with ui.panel("monthly"):
    st.subheader("Monthly report")
    rows = monthly(conn, book, market, as_of)
    if not rows:
        ui.empty("No buys in the test period yet")
    else:
        show_table([
            {"Month": m.month, "Buys": m.buys, "Compliant": m.groups[COMPLIANT], "Violation": m.groups[VIOLATION],
             "Missing data only": m.groups[MISSING_ONLY],
             "Top violations": ", ".join(f"{LABELS.get(k, k)} {n}" for k, n in m.top_rules),
             "Avg return": m.avg_ret * 100 if m.avg_ret is not None else None,
             "Excess": m.avg_excess * 100 if m.avg_excess is not None else None,
             "Compliance": f"{m.compliance.rate:.0%}" if m.compliance.rate is not None else "—"}
            for m in rows
        ], changes=("Avg return", "Excess"))
        st.caption("Months follow US Eastern trading days; returns run to the latest close. Don't change rules during "
                   "the test period: note the ones that get in the way and adjust them together at the end.")

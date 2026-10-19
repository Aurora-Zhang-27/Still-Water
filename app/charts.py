"""Altair charts.

Following data-viz conventions: thin bars (≤ 24px), 4px rounded data ends, hairline grids, text in ink
rather than the data colour, a tooltip on every chart, and a table view next to it on the page.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

from app.common import STATUS
from app.logic import HoldingRow

FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


def _style(chart: alt.TopLevelMixin, c: dict[str, str]) -> alt.TopLevelMixin:
    if chart.padding is alt.Undefined:
        # cards have no horizontal padding (see app/style.css); the padding lives in the chart so it never overflows
        chart = chart.properties(padding={"left": 16, "right": 22, "top": 6, "bottom": 4})
    return (
        # fit the width only: height is the plot area, axes and padding come on top and are never squeezed
        chart.properties(background="transparent", autosize=alt.AutoSizeParams(type="fit-x", contains="padding"))
        .configure_view(stroke=None)
        .configure_axis(
            gridColor=c["grid"], domainColor=c["baseline"], tickColor=c["baseline"],
            labelColor=c["muted"], titleColor=c["secondary"], labelFont=FONT, titleFont=FONT, labelFontSize=12,
        )
        .configure_text(font=FONT)
    )


# vertical pixel positions for one-row charts (meters, valuation strip): label on top, bar in the middle,
# ticks across the bar. Streamlit pins chart height near `height`, so everything is drawn inside the plot, no axes
_STRIP_HEIGHT = 56
_LABEL_Y, _BAR_TOP, _BAR_BOTTOM, _TICK_TOP, _TICK_BOTTOM = 10, 30, 44, 24, 50


def meter(
    value: float,
    markers: list[tuple[float, str]],
    *,
    fill: str,
    c: dict[str, str],
    domain_max: float,
    value_text: str,
) -> alt.TopLevelMixin:
    """One value against thresholds: grey track + status-coloured fill + labelled threshold ticks (no axis needed)."""
    x = alt.X("start:Q", scale=alt.Scale(domain=[0, domain_max], nice=False), axis=None)
    bar_y = {"x2": "end:Q", "y": alt.value(_BAR_TOP), "y2": alt.value(_BAR_BOTTOM)}
    track = alt.Chart(pd.DataFrame([{"start": 0, "end": domain_max}])).mark_bar(
        cornerRadius=4, color=c["track"]).encode(x=x, **bar_y)
    bar = alt.Chart(pd.DataFrame([{"start": 0, "end": min(value, domain_max), "text": value_text}])).mark_bar(
        cornerRadiusEnd=4, color=fill
    ).encode(x="start:Q", tooltip=[alt.Tooltip("text:N", title="Now")], **bar_y)
    marks = pd.DataFrame([{"start": v, "label": label} for v, label in markers])
    ticks = alt.Chart(marks).mark_rule(strokeWidth=2, color=c["ink"]).encode(
        x="start:Q", y=alt.value(_TICK_TOP), y2=alt.value(_TICK_BOTTOM), tooltip=[alt.Tooltip("label:N", title="Threshold")])
    labels = alt.Chart(marks).mark_text(fontSize=11, color=c["secondary"]).encode(
        x="start:Q", y=alt.value(_LABEL_Y), text="label:N")
    return _style(alt.layer(track, bar, ticks, labels).properties(height=_STRIP_HEIGHT), c)


def positions(rows: list[HoldingRow], c: dict[str, str]) -> alt.TopLevelMixin:
    """Position weights (bars) + each stock's cap tick; the part over the cap is red."""
    df = pd.DataFrame([
        {
            "symbol": r.symbol,
            "weight": r.weight,
            "cap": r.cap,
            "within": min(r.weight, r.cap),
            "tip": max(r.weight, r.cap),
            "over": r.over_cap,
            "label": f"{r.weight:.1%}" + ("  ⚠ over cap" if r.over_cap else ""),
            "value": r.value,
            "pnl": r.unrealized,
            "qty": r.quantity,
            "cost": r.avg_cost,
        }
        for r in rows
    ])
    order = list(df["symbol"])
    domain_max = float(max(df["weight"].max(), df["cap"].max())) * 1.25
    y = alt.Y("symbol:N", sort=order, title=None,
              axis=alt.Axis(labelColor=c["ink"], labelFontSize=13, ticks=False, domain=False, labelPadding=8))
    x = alt.X("within:Q", scale=alt.Scale(domain=[0, domain_max], nice=False),
              axis=alt.Axis(format="%", title=None, tickCount=6))
    tooltip = [
        alt.Tooltip("symbol:N", title="Ticker"),
        alt.Tooltip("weight:Q", title="Weight", format=".1%"),
        alt.Tooltip("cap:Q", title="Cap", format=".0%"),
        alt.Tooltip("value:Q", title="Value", format="$,.0f"),
        alt.Tooltip("pnl:Q", title="P&L", format="+.1%"),
        alt.Tooltip("qty:Q", title="Shares"),
        alt.Tooltip("cost:Q", title="Cost", format=".2f"),
    ]
    base = alt.Chart(df)
    under = base.transform_filter("!datum.over").mark_bar(size=18, cornerRadiusEnd=4, color=c["series"]).encode(
        x=x, y=y, tooltip=tooltip)
    within = base.transform_filter("datum.over").mark_bar(size=18, color=c["series"]).encode(
        x=x, y=y, tooltip=tooltip)
    excess = base.transform_filter("datum.over").mark_bar(size=18, cornerRadiusEnd=4, color=STATUS["critical"]).encode(
        x="cap:Q", x2="weight:Q", y=y, tooltip=tooltip)
    cap_line = base.mark_tick(thickness=2, size=30, color=STATUS["critical"]).encode(x="cap:Q", y=y, tooltip=tooltip)
    labels = base.mark_text(align="left", dx=8, fontSize=12, color=c["ink"]).encode(x="tip:Q", y=y, text="label:N")
    chart = alt.layer(under, within, excess, cap_line, labels).properties(height=34 * len(df))
    return _style(chart, c)


# valuation bands: a blue ↔ red diverging scale with the normal line as the midpoint; darker further away
ZONE_COLORS = {
    # the cheap side is the lily pond's mist blue, the rich side dawn pink to dusk rose
    "light": {"Below cheap": "#6A8AB2", "Cheap–normal": "#A4BCD8", "Normal–rich": "#E3B0AB", "Above rich": "#B07571"},
    "dark": {"Below cheap": "#8FA9C4", "Cheap–normal": "#4E5F78", "Normal–rich": "#7A5560", "Above rich": "#C99A9C"},
}


def valuation_zones(rows: list[dict], c: dict[str, str], mode: str) -> alt.TopLevelMixin:
    """One row per stock: the four bands drawn "relative to the normal line", a dot for the price and a
    vertical rule for the pressure line.

    Each item in rows: symbol, price, pressure, cheap, normal, rich, zone (absolute prices).
    """
    colors = ZONE_COLORS[mode]
    r = [{**row, **{k: row[k] / row["normal"] for k in ("price", "pressure", "cheap", "rich")}} for row in rows]
    lo = min(min(x["cheap"], x["pressure"], x["price"]) for x in r) - 0.08
    hi = max(max(x["rich"], x["price"]) for x in r) + 0.10
    bands, marks = [], []
    for x, raw in zip(r, rows):
        edges = [(lo, x["cheap"], "Below cheap"), (x["cheap"], 1.0, "Cheap–normal"), (1.0, x["rich"], "Normal–rich"),
                 (x["rich"], hi, "Above rich")]
        bands += [{"symbol": x["symbol"], "start": a, "end": b, "band": name} for a, b, name in edges]
        marks.append({
            "symbol": x["symbol"], "price_r": x["price"], "pressure_r": x["pressure"], "zone": raw["zone"],
            "price": raw["price"], "pressure": raw["pressure"], "cheap": raw["cheap"], "normal": raw["normal"],
            "rich": raw["rich"], "gap": raw["price"] / raw["normal"] - 1,
        })
    order = [x["symbol"] for x in rows]
    y = alt.Y("symbol:N", sort=order, title=None,
              axis=alt.Axis(labelColor=c["ink"], labelFontSize=13, ticks=False, domain=False, labelPadding=8))
    x = alt.X("start:Q", scale=alt.Scale(domain=[lo, hi], nice=False),
              axis=alt.Axis(format="%", title="Relative to the normal line (100% = normal line)", tickCount=6))
    tooltip = [
        alt.Tooltip("symbol:N", title="Ticker"), alt.Tooltip("zone:N", title="Band"),
        alt.Tooltip("price:Q", title="Price", format=",.2f"), alt.Tooltip("gap:Q", title="vs. normal", format="+.1%"),
        alt.Tooltip("pressure:Q", title="Pressure", format=",.2f"), alt.Tooltip("cheap:Q", title="Cheap", format=",.2f"),
        alt.Tooltip("normal:Q", title="Normal", format=",.2f"), alt.Tooltip("rich:Q", title="Rich", format=",.2f"),
    ]
    band = alt.Chart(pd.DataFrame(bands)).mark_bar(size=20).encode(
        x=x, x2="end:Q", y=y,
        color=alt.Color("band:N", scale=alt.Scale(domain=list(colors), range=list(colors.values())),
                        legend=alt.Legend(orient="top", title=None, labelColor=c["secondary"], symbolType="square")),
    )
    m = alt.Chart(pd.DataFrame(marks))
    # pressure line = normal line × a fixed ratio, so relative to the normal line it sits at the same spot
    # for every stock: draw one rule across all rows
    level = r[0]["pressure"]
    pressure_df = pd.DataFrame([{"start": level, "label": f"Pressure {level:.0%}"}])
    pressure = alt.Chart(pressure_df).mark_rule(strokeWidth=2, color=c["ink"], opacity=0.6).encode(
        x="start:Q", tooltip=[alt.Tooltip("label:N", title="")])
    pressure_label = alt.Chart(pressure_df).mark_text(
        align="left", dx=4, dy=-6, fontSize=11, color=c["secondary"]).encode(
        x="start:Q", y=alt.value(0), text="label:N")
    # a 2px surface seam between bands: the light blue and light pink around the normal line are separated
    # by the seam, not by colour alone
    seams = alt.Chart(pd.DataFrame([{"symbol": x["symbol"], "start": v} for x in r
                                    for v in (x["cheap"], 1.0, x["rich"])])).mark_tick(
        thickness=2, size=20, color=c["surface"], opacity=1).encode(x="start:Q", y=y)
    price = m.mark_point(filled=True, size=110, color=c["ink"], stroke=c["surface"], strokeWidth=2, opacity=1).encode(
        x="price_r:Q", y=y, tooltip=tooltip)
    chart = alt.layer(band, seams, pressure, pressure_label, price).properties(height=30 * len(rows))
    return _style(chart, c)


def price_with_lines(bars: pd.DataFrame, lines: dict[str, float], c: dict[str, str]) -> alt.TopLevelMixin:
    """Closes over the last half year + the four current valuation lines (labelled on the right)."""
    names = {"pressure": "Pressure", "cheap": "Cheap", "normal": "Normal", "rich": "Rich"}
    lo = min(bars["close"].min(), min(lines.values())) * 0.95
    hi = max(bars["close"].max(), max(lines.values())) * 1.05
    y = alt.Y("close:Q", scale=alt.Scale(domain=[lo, hi], nice=False), title=None, axis=alt.Axis(format="$,.0f"))
    line = alt.Chart(bars).mark_line(strokeWidth=2, color=c["series"]).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%m-%d")), y=y,
        tooltip=[alt.Tooltip("date:T", title="Date", format="%Y-%m-%d"), alt.Tooltip("close:Q", title="Close", format=",.2f")])
    last = bars["date"].max()
    df = pd.DataFrame([{"close": v, "date": last, "label": f"{names[k]} {v:,.0f}"} for k, v in lines.items()])
    rules = alt.Chart(df).mark_rule(color=c["muted"], strokeWidth=1).encode(y=y)  # shares the price line's y axis, which does not start at 0
    labels = alt.Chart(df).mark_text(align="left", dx=6, fontSize=11, color=c["secondary"]).encode(
        x="date:T", y=y, text="label:N")  # labelled outside the right end, clear of the price line
    chart = alt.layer(rules, line, labels).properties(height=260, padding={"right": 84, "left": 16, "top": 8})
    return _style(chart, c)


def excess_strip(groups: dict[str, list], c: dict[str, str]) -> alt.TopLevelMixin:
    """One dot per buy: x is the excess return vs. QQQ since the buy, y is the group.
    groups: group → list of Outcome."""
    rows = [
        {"group": g, "label": o.label, "excess": o.excess, "ret": o.ret, "index_ret": o.index_ret}
        for g, items in groups.items() for o in items if o.excess is not None
    ]
    order = [g for g in groups if groups[g]]
    df = pd.DataFrame(rows)
    y = alt.Y("group:N", sort=order, title=None,
              axis=alt.Axis(labelColor=c["ink"], labelFontSize=13, ticks=False, domain=False, labelPadding=8))
    dots = alt.Chart(df).mark_point(filled=True, size=90, opacity=0.85, stroke=c["surface"], strokeWidth=2).encode(
        # beat the index pine, lagged brick; position already shows the sign, colour only helps
        color=alt.condition("datum.excess >= 0", alt.value(STATUS["good"]), alt.value(STATUS["critical"])),
        x=alt.X("excess:Q", title="Excess return vs. QQQ since the buy", axis=alt.Axis(format="+%", tickCount=6)),
        y=y,
        yOffset=alt.YOffset("jitter:Q", scale=alt.Scale(domain=[-1, 1])),
        tooltip=[alt.Tooltip("label:N", title="Buy"), alt.Tooltip("excess:Q", title="Excess", format="+.1%"),
                 alt.Tooltip("ret:Q", title="Return", format="+.1%"),
                 alt.Tooltip("index_ret:Q", title="QQQ same period", format="+.1%")],
    ).transform_calculate(jitter="sqrt(-2*log(random()))*cos(2*PI*random()) * 0.35")
    zero = alt.Chart(pd.DataFrame([{"x": 0}])).mark_rule(color=c["baseline"], strokeWidth=1).encode(x="x:Q")
    return _style(alt.layer(zero, dots).properties(height=70 * max(len(order), 1)), c)


def rule_bars(counts: list[tuple[str, int]], c: dict[str, str]) -> alt.TopLevelMixin:
    """Violations by rule: bars with the count at the end. counts is sorted, items are (rule name, count)."""
    df = pd.DataFrame([{"rule": name, "count": n} for name, n in counts])
    y = alt.Y("rule:N", sort=[name for name, _ in counts], title=None,
              axis=alt.Axis(labelColor=c["ink"], labelFontSize=13, ticks=False, domain=False, labelPadding=8))
    x = alt.X("count:Q", title=None, axis=alt.Axis(tickMinStep=1, format="d"))
    bars = alt.Chart(df).mark_bar(size=18, cornerRadiusEnd=4, color=c["series"]).encode(
        x=x, y=y, tooltip=[alt.Tooltip("rule:N", title="Rule"), alt.Tooltip("count:Q", title="Count")])
    labels = alt.Chart(df).mark_text(align="left", dx=6, fontSize=12, color=c["ink"]).encode(
        x="count:Q", y=y, text="count:Q")
    return _style(alt.layer(bars, labels).properties(height=30 * len(df)), c)


def valuation_strip(lines: dict[str, float], price: float, c: dict[str, str]) -> alt.TopLevelMixin:
    """Where the price sits on the four valuation lines."""
    names = {"pressure": "Pressure", "cheap": "Cheap", "normal": "Normal", "rich": "Rich"}
    df = pd.DataFrame([{"x": v, "label": f"{names[k]} {v:.0f}"} for k, v in lines.items()])
    lo, hi = min(min(lines.values()), price), max(max(lines.values()), price)
    pad = (hi - lo) * 0.12 or hi * 0.1
    x = alt.X("x:Q", scale=alt.Scale(domain=[lo - pad, hi + pad], nice=False), axis=None)
    baseline = alt.Chart(pd.DataFrame([{"x": lo - pad, "x2": hi + pad}])).mark_rule(color=c["baseline"]).encode(
        x=x, x2="x2:Q", y=alt.value((_BAR_TOP + _BAR_BOTTOM) / 2))
    ticks = alt.Chart(df).mark_rule(strokeWidth=2, color=c["muted"]).encode(
        x="x:Q", y=alt.value(_TICK_TOP), y2=alt.value(_TICK_BOTTOM), tooltip=[alt.Tooltip("label:N", title="Valuation line")])
    labels = alt.Chart(df).mark_text(fontSize=11, color=c["secondary"]).encode(
        x="x:Q", y=alt.value(_LABEL_Y), text="label:N")
    dot = alt.Chart(pd.DataFrame([{"x": price, "label": f"Price {price:.2f}"}])).mark_point(
        filled=True, size=140, color=c["series"], stroke=c["surface"], strokeWidth=2, opacity=1
    ).encode(x="x:Q", y=alt.value((_BAR_TOP + _BAR_BOTTOM) / 2), tooltip=[alt.Tooltip("label:N", title="")])
    return _style(alt.layer(baseline, ticks, labels, dot).properties(height=_STRIP_HEIGHT), c)

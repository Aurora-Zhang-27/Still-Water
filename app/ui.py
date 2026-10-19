"""Page components for Monet + liquid glass. Styles live in app/style.css.

Each page has one Monet painting filling the whole page as its background; every section is liquid glass
floating on it. The paintings are public-domain works from the Art Institute of Chicago's open-access
collection, prepared and stored in app/static/.
Components only display: any text passed in is escaped, and callers compute the numbers.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from html import escape

import streamlit as st

STATIC = "/app/static"
MUSEUM = "Art Institute of Chicago"


@dataclass(frozen=True)
class Art:
    title: str
    year: str
    focus: str = "center"  # how the painting is positioned when it covers the page


ART = {
    "overview": Art("Water Lilies", "1906", "center 60%"),
    "buy_check": Art("Water Lily Pond", "1900", "center 35%"),
    "valuation": Art("Stacks of Wheat (End of Summer)", "1890–91", "center 55%"),
    "watchlist": Art("Branch of the Seine near Giverny (Mist)", "1897"),
    "hunches": Art("Poppy Field (Giverny)", "1890–91", "center 60%"),
    "reminders": Art("Stacks of Wheat (Sunset, Snow Effect)", "1890–91"),
    "review": Art("Houses of Parliament, London", "1900–01", "center 40%"),
    "reports": Art("Waterloo Bridge, Sunlight Effect", "1903"),
}

# the paint wash in a metric card's corner: colours from the paintings, by status
TONE = {
    "pass": ("#9FC1C8", "#336B78"),  # pond teal
    "warn": ("#EBD7A6", "#A27D33"),  # hay gold
    "block": ("#E8B9AE", "#A05A3B"),  # sunset brick
    None: ("#C8BDE0", "#5B4E8C"),  # lavender
}
DOT = {"pass": "#336B78", "warn": "#A27D33", "block": "#A05A3B"}


def title(page: str, text: str, subtitle: str = "") -> None:
    """This page's painting fills the background; the title sits on a pane of liquid glass with the
    painting's credit underneath."""
    art = ART[page]
    sub = f'<p class="plate-sub">{escape(subtitle)}</p>' if subtitle else ""
    st.html(f"""
<style>
  [data-testid="stApp"]::before {{
    background-image: url('{STATIC}/bg_{page}.jpg');
    background-position: {art.focus};
  }}
</style>
<section class="plate glass">
  <h1 class="plate-title">{escape(text)}</h1>{sub}
  <div class="plate-credit">Background: Claude Monet, <i>{escape(art.title)}</i>, {art.year} · {MUSEUM}</div>
</section>""")


def panel(key: str):
    """A pane of liquid glass: everything inside `with ui.panel("name"):` sits on the same pane."""
    return st.container(border=True, key=f"panel-{key}")


def sidebar_brand() -> None:
    st.html("""
<div class="brand">
  <div class="brand-name">Still Water</div>
  <div class="brand-motto">See clearly, then act</div>
</div>""")


def _sparkline(values: list[float], color: str) -> str:
    """A thin trend line. st.html strips inline SVG, so it is encoded as a background image (via style)."""
    if len(values) < 2:
        return ""
    w, h, pad = 240, 34, 3
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pts = [(pad + i * (w - 2 * pad) / (len(values) - 1), h - pad - (v - lo) / span * (h - 2 * pad))
           for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
           f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linecap="round" '
           f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>')
    data = base64.b64encode(svg.encode()).decode()
    return f"<div class=\"spark\" style=\"background-image:url('data:image/svg+xml;base64,{data}')\"></div>"


def _meter(fraction: float, marks: list[float], color: str) -> str:
    """A thin meter: fraction and marks are positions between 0 and 1."""
    fill = max(0.0, min(fraction, 1.0)) * 100
    ticks = "".join(f'<i style="left:{max(0, min(m, 1)) * 100:.1f}%"></i>' for m in marks)
    return f'<div class="kmeter"><b style="width:{fill:.1f}%;background:{color}"></b>{ticks}</div>'


@dataclass
class Kpi:
    label: str
    value: str
    note: str = ""
    tone: str | None = None  # pass / warn / block: sets the corner wash and the dot
    spark: list[float] | None = None  # recent trend
    meter: tuple[float, list[float]] | None = None  # (position, tick positions), all between 0 and 1
    hint: str = ""  # hover text


def kpi_row(items: list[Kpi]) -> None:
    """A row of liquid-glass metric cards."""
    cards = []
    for k in items:
        wash, ink = TONE[k.tone]
        dot = f'<span class="kdot" style="background:{DOT[k.tone]}"></span>' if k.tone in DOT else ""
        extra = _sparkline(k.spark, ink) if k.spark else (_meter(*k.meter, ink) if k.meter else "")
        note = f'<div class="knote">{dot}{escape(k.note)}</div>' if k.note else ""
        cards.append(
            f'<div class="kpi glass" title="{escape(k.hint)}"><span class="kwash" style="background:{wash}"></span>'
            f'<div class="klabel">{escape(k.label)}</div><div class="kvalue">{escape(k.value)}</div>'
            f'{extra}{note}</div>'
        )
    st.html(f'<div class="kpi-grid">{"".join(cards)}</div>')


def empty(text: str) -> None:
    """One sentence for when there is no data (inside a panel)."""
    st.html(f'<div class="empty">{escape(text)}</div>')

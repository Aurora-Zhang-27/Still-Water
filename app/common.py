"""Shared by the pages: database connection, colours, formatting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from db.database import init_db
from rules.config import RuleBook, load_rules
from settings import load_settings

ICON = {"pass": "🟢", "warn": "🟡", "block": "🔴", "info": "ⓘ"}
VERDICT_TEXT = {"pass": "Pass", "warn": "Warning", "block": "Blocked"}

# Colours: Monet impressionism (see .streamlit/config.toml, app/style.css). Mist blue draws ordinary data;
# status colours only mean good / bad. No pure black: text is dusk purple. "dark" is a fallback for Streamlit's dark theme
PALETTE = {
    "light": {"series": "#56759A", "track": "#E6DFEA", "ink": "#2E2A3A", "secondary": "#5B5468",
              "muted": "#8A8496", "grid": "#E8E1EC", "baseline": "#CFC6D6", "surface": "#FBF8F4"},
    "dark": {"series": "#8FA9C4", "track": "#3A3546", "ink": "#F1ECF4", "secondary": "#CFC6D6",
             "muted": "#9C95A8", "grid": "#3A3546", "baseline": "#4A4458", "surface": "#2E2A3A"},
}
STATUS = {"good": "#336B78", "warning": "#A27D33", "critical": "#A05A3B"}  # pine, hay gold, brick (checked for contrast and colour-blind separation)
LEVEL_COLOR = {"pass": STATUS["good"], "warn": STATUS["warning"], "block": STATUS["critical"]}
UP, DOWN = STATUS["good"], STATUS["critical"]  # price changes: low-saturation pine / brick


CHANGE = "{:+.1f}%"  # change columns arrive as percentages: 12.3 → "+12.3%"
MISSING = "—"


def _missing(value) -> bool:
    return value is None or value != value  # None / NaN


def _tint(value) -> str:
    if _missing(value):
        return ""
    return f"color: {UP}" if value > 0 else f"color: {DOWN}" if value < 0 else ""


def show_table(rows, formats: dict[str, str] | None = None, changes: tuple[str, ...] = ()) -> None:
    """One way to show tables. formats gives each numeric column's format (e.g. "{:.2f}"); changes are
    change columns, written as "+12.3%" and the only ones coloured (up pine, down brick); other numbers
    stay dark. Cells that cannot be computed show "—".

    Streamlit always shows empty cells as None and ignores pandas' na_rep, so numeric columns with gaps
    become right-aligned text; columns without gaps stay numeric and still sort by value.
    """
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    formats = {c: f for c, f in ({c: CHANGE for c in changes} | (formats or {})).items() if c in df.columns}
    shown, as_numbers, config = df.copy(), {}, {}
    for col, fmt in formats.items():
        if df[col].isna().any():
            shown[col] = [MISSING if _missing(v) else fmt.format(v) for v in df[col]]
            config[col] = st.column_config.TextColumn(alignment="right")
        else:
            as_numbers[col] = fmt
    styler = shown.style.format(as_numbers).apply(
        lambda col: [_tint(v) for v in df[col.name]], subset=[c for c in changes if c in df.columns])
    st.dataframe(styler, hide_index=True, width="stretch", column_config=config)


def theme_mode() -> str:
    # the Monet theme only has a light "morning" version (.streamlit/config.toml fixes base = light);
    # it ignores the system dark mode, or charts would draw light text on light cards
    return "light"


def colors() -> dict[str, str]:
    return PALETTE[theme_mode()]


def connect():
    return init_db(load_settings().db_path)


def rulebook() -> RuleBook:
    return load_rules(load_settings().rules_path)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pct(x: float | None, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if x is not None else "-"


def money(x: float | None) -> str:
    return f"${x:,.0f}" if x is not None else "-"


def md(text: str) -> str:
    """For markdown / captions: Streamlit reads text between two $ as math, so escape them."""
    return text.replace("$", r"\$")


def et(moment: datetime, book: RuleBook) -> str:
    return moment.astimezone(book.calendar.tz).strftime("%m-%d %H:%M")


def duration(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"

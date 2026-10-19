"""Style layer: injects app/style.css. Call apply() once in the entry point and every page gets it.

Each page's background painting is set by title() in app/ui.py; style is only atmosphere, and numbers
and text always stay dark and readable.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

CSS_FILE = Path(__file__).with_name("style.css")


def css() -> str:
    return CSS_FILE.read_text(encoding="utf-8")


def apply() -> None:
    st.html(f"<style>{css()}</style>")

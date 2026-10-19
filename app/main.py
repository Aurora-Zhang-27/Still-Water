"""Streamlit entry point:

    .venv/bin/streamlit run app/main.py
"""

import sys
from pathlib import Path

# put the project root on the import path. No reliance on an editable install: Python 3.13 skips
# .pth files that macOS marks as hidden
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from app import style, ui  # noqa: E402
from app.common import connect, rulebook  # noqa: E402

st.set_page_config(page_title="Still Water", page_icon="🪷", layout="wide")
style.apply()

page = st.navigation([
    st.Page("views/overview.py", title="Overview", icon="📊", default=True),
    st.Page("views/buy_check.py", title="Buy check", icon="🚦"),
    st.Page("views/valuation.py", title="Valuation", icon="📏"),
    st.Page("views/watchlist.py", title="Watchlist", icon="👀"),
    st.Page("views/hunches.py", title="Hunches", icon="💭"),
    st.Page("views/reminders.py", title="Sell reminders", icon="🔔"),
    st.Page("views/review.py", title="Review", icon="📈"),
    st.Page("views/reports.py", title="Weekly reports", icon="🗒️"),
])

with st.sidebar:
    ui.sidebar_brand()
    conn = connect()
    snap = conn.execute("SELECT MAX(as_of) FROM account_snapshots").fetchone()[0]
    quote = conn.execute("SELECT MAX(fetched_at) FROM market_fetch_log").fetchone()[0]
    st.caption(f"Account data: {snap or 'none yet'} end of day (Flex)")
    st.caption(f"Market data updated: {quote.replace('T', ' ').replace('Z', ' UTC') if quote else 'never'}")
    st.caption(f"Rules v{rulebook().version} · read-only, never places orders")
    conn.close()

page.run()

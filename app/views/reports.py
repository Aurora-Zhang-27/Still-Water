"""Weekly reports: Markdown generated automatically every Sunday; this week's can also be built here."""

import streamlit as st

from app import ui
from app.common import connect, md, rulebook, utc_now
from audit.weekly import build_weekly, render_markdown, save_report
from settings import load_settings

book = rulebook()
folder = load_settings().db_path.parent / "reports"

ui.title("reports", "Weekly reports", "Built automatically every Sunday (scheduled job) and saved in data/reports/.")

with ui.panel("controls"):
    if st.button("Build this week's report"):
        with st.spinner("Crunching…"):
            conn = connect()
            report = build_weekly(conn, book, utc_now())
            save_report(render_markdown(report, book), folder.parent, report.week_start)
            st.session_state["report_pick"] = f"weekly_{report.week_start.isoformat()}.md"

    files = sorted(folder.glob("weekly_*.md"), reverse=True) if folder.exists() else []
    if not files:
        st.info("No weekly reports yet")
    else:
        names = [f.name for f in files]
        pick = st.selectbox("Week", names, key="report_pick",
                            format_func=lambda n: "Week of " + n.removeprefix("weekly_").removesuffix(".md"))

if files:
    text = (folder / pick).read_text(encoding="utf-8")
    # the page already has a title: show the report's headings two levels lower
    text = "\n".join("##" + line if line.startswith("#") else line for line in text.splitlines())
    with ui.panel("report"):
        st.markdown(md(text))

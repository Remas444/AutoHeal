"""AutoHeal incident operations console.  Run:  streamlit run app.py"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from autoheal_ui import session, theme

ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="AutoHeal", page_icon=":material/monitor_heart:", layout="wide")
st.logo(str(ROOT / "assets" / "logo.svg"), size="large")
theme.inject_css()

page = st.navigation([
    st.Page("app_pages/operations.py", title="Operations", icon=":material/emergency:", default=True),
    st.Page("app_pages/insights.py", title="System Insights", icon=":material/monitoring:"),
    st.Page("app_pages/safety_audit.py", title="Safety & Audit", icon=":material/verified_user:"),
])

# The controls for these two live in Safety & Audit > Advanced settings; they apply on every page.
session.console(risk=st.session_state.get("risk", "medium"), dry_run=st.session_state.get("dry_run", False))
page.run()

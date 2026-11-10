import streamlit as st

from dashboard.database import initialize_database
from dashboard.ai_settings import render_sidebar_settings
from dashboard.navigation import TAB_LABELS, TAB_STATE_KEY, apply_pending_navigation
from dashboard.pages import home, portfolio, quant_lab, research


st.set_page_config(
    page_title="Investment Command Center",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

initialize_database()

# Callback requests are consumed before st.tabs owns the selected-tab key.
apply_pending_navigation(st.session_state)
if TAB_STATE_KEY not in st.session_state:
    st.session_state[TAB_STATE_KEY] = TAB_LABELS[0]

# Market Home has its own DeepSeek research controls in the sidebar. Keep the
# shared provider settings available on the other tabs without duplicating them.
if st.session_state.get(TAB_STATE_KEY) != TAB_LABELS[0]:
    AI_SETTINGS = render_sidebar_settings()
else:
    AI_SETTINGS = None

market_tab, portfolio_tab, research_tab, quant_tab = st.tabs(
    TAB_LABELS,
    key=TAB_STATE_KEY,
    on_change="rerun",
)

if market_tab.open:
    with market_tab:
        home.render()

if portfolio_tab.open:
    with portfolio_tab:
        portfolio.render()

if research_tab.open:
    with research_tab:
        research.render()

if quant_tab.open:
    with quant_tab:
        quant_lab.render()

import streamlit as st

from dashboard.database import initialize_database
from dashboard.pages import home, market_risk, portfolio, research, watchlist


st.set_page_config(
    page_title="Investment Dashboard",
    page_icon="📈",
    layout="wide",
)

initialize_database()

st.sidebar.title("Investment Dashboard")
page_name = st.sidebar.radio(
    "Navigate",
    ["Home", "Portfolio", "Market Risk", "Watchlist", "Research"],
)
st.sidebar.caption("Local-first • SQLite storage")

pages = {
    "Home": home,
    "Portfolio": portfolio,
    "Market Risk": market_risk,
    "Watchlist": watchlist,
    "Research": research,
}
pages[page_name].render()

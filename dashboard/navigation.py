"""Shared labels and state helper for the four top-level Streamlit tabs."""

TAB_LABELS = (
    "Market Home",
    "Portfolio",
    "Research & Watchlist",
    "Quant Lab",
)
TAB_STATE_KEY = "investment_command_center_tab"
RESEARCH_TAB = "Research & Watchlist"
WORKSPACE_STATE_KEY = "research_workspace_symbol"
PENDING_NAVIGATION_KEY = "pending_dashboard_navigation"


def activate_research_workspace(session_state, symbol: str) -> None:
    """Queue navigation from a callback without mutating the tabs widget key.

    Streamlit runs callbacks before the next script pass. The app consumes this
    request before constructing the tabs widget, where its selected value can
    safely be initialized.
    """
    normalized_symbol = symbol.strip().upper()
    session_state[WORKSPACE_STATE_KEY] = normalized_symbol
    session_state[PENDING_NAVIGATION_KEY] = {"tab": RESEARCH_TAB}


def apply_pending_navigation(session_state) -> None:
    """Apply callback navigation before the keyed tabs widget is instantiated."""
    request = session_state.pop(PENDING_NAVIGATION_KEY, None)
    if request and request.get("tab") in TAB_LABELS:
        session_state[TAB_STATE_KEY] = request["tab"]

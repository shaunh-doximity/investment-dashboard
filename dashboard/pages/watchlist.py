import streamlit as st

from dashboard.data_access import add_watchlist_item, delete_watchlist_item, list_watchlist


def render() -> None:
    st.title("Watchlist")
    st.caption("A local list only. No prices or company data are fetched.")
    with st.form("watchlist_form", clear_on_submit=True):
        symbol = st.text_input("Ticker symbol")
        company = st.text_input("Company name (optional)")
        submitted = st.form_submit_button("Add to watchlist")
        if submitted:
            if not symbol.strip():
                st.error("Enter a ticker symbol.")
            else:
                add_watchlist_item(symbol, company)
                st.success(f"Saved {symbol.strip().upper()} locally.")
                st.rerun()

    items = list_watchlist()
    if items:
        st.dataframe(items, use_container_width=True, hide_index=True)
        symbol_to_remove = st.selectbox("Remove symbol", [item["symbol"] for item in items])
        if st.button("Remove from watchlist"):
            delete_watchlist_item(symbol_to_remove)
            st.rerun()
    else:
        st.write("Your watchlist is empty.")

import streamlit as st

from dashboard.data_access import add_risk_rule, delete_risk_rule, list_risk_rules


def render(show_title: bool = True) -> None:
    if show_title:
        st.header("Market Risk")
    else:
        st.subheader("Market-risk review reminders")
    st.info(
        "No market indicators are connected. Add thresholds as reminders for your own review; "
        "this page does not calculate signals from current market data."
    )
    with st.form("risk_rule_form", clear_on_submit=True):
        st.subheader("Save a review rule")
        name = st.text_input("Rule name", placeholder="Example: Portfolio drawdown review")
        description = st.text_input("What should prompt a review?")
        threshold = st.number_input("Threshold value", value=0.0, step=1.0)
        submitted = st.form_submit_button("Save rule")
        if submitted:
            if not name.strip():
                st.error("Give the rule a name.")
            else:
                add_risk_rule(name, description, threshold)
                st.success("Rule saved. It is a reminder only and will not trigger trades.")
                st.rerun()

    rules = list_risk_rules()
    st.subheader("Saved rules")
    if not rules:
        st.write("No rules saved yet.")
    for rule in rules:
        st.write(f"**{rule['name']}** · threshold {rule['threshold']:g}")
        if rule["description"]:
            st.caption(rule["description"])
        if st.button("Delete rule", key=f"delete_rule_{rule['id']}"):
            delete_risk_rule(rule["id"])
            st.rerun()
    st.caption("Rules may later create alerts and proposed actions. Automated brokerage orders are out of scope.")

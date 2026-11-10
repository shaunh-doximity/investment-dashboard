import streamlit as st


def render() -> None:
    st.header("Quant Lab")
    st.caption("Quantitative research workspace · no models are connected or running.")

    columns = st.columns(2)
    placeholders = (
        "Correction probability",
        "Market-regime classification",
        "Model evaluation",
        "Historical prediction tracking",
    )
    for column, title in zip(columns * 2, placeholders):
        column.markdown(f"**{title}**")
        column.caption("Not available yet · no prediction or evaluation is fabricated.")

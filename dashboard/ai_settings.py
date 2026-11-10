"""Compact global Streamlit controls for the selected AI provider."""

import os

import streamlit as st

from dashboard.ai_providers import (
    DEFAULT_PROVIDER, MODEL_CHOICES, default_model, load_ai_environment, normalize_provider,
)


def _provider_changed() -> None:
    st.session_state["ai_model"] = default_model(st.session_state["ai_provider"])


def render_sidebar_settings() -> dict:
    """Render non-networking settings and return the selected generation options."""
    load_ai_environment()
    if "ai_provider" not in st.session_state:
        try:
            st.session_state["ai_provider"] = normalize_provider(os.environ.get("AI_PROVIDER", DEFAULT_PROVIDER))
        except ValueError:
            st.session_state["ai_provider"] = DEFAULT_PROVIDER
    if "ai_model" not in st.session_state:
        st.session_state["ai_model"] = default_model(st.session_state["ai_provider"])
    elif (st.session_state["ai_provider"] == "deepseek"
          and st.session_state["ai_model"] not in MODEL_CHOICES["deepseek"]):
        # Clear a stale/unsupported model value before its widget is created.
        st.session_state["ai_model"] = default_model(st.session_state["ai_provider"])

    with st.sidebar.expander("AI settings", expanded=False):
        provider = st.selectbox(
            "Provider", ("deepseek", "openai"), key="ai_provider",
            format_func=lambda value: "DeepSeek" if value == "deepseek" else "OpenAI",
            on_change=_provider_changed,
        )
        model_options = list(MODEL_CHOICES[provider])
        current_model = st.session_state.get("ai_model")
        if provider == "openai" and current_model and current_model not in model_options:
            model_options.append(current_model)
        if current_model not in model_options:
            current_model = default_model(provider)
            st.session_state["ai_model"] = current_model
        model = st.selectbox("Model", model_options, index=model_options.index(current_model), key="ai_model")
        max_tokens = st.number_input(
            "Maximum output tokens", min_value=64, max_value=8192, step=256,
            value=int(st.session_state.get("ai_max_output_tokens", 1400)), key="ai_max_output_tokens",
        )
        include_portfolio = st.checkbox(
            "Include portfolio context", value=bool(st.session_state.get("ai_include_portfolio", False)),
            key="ai_include_portfolio",
            help="When checked, an explicit market assessment can include held tickers, horizon/status, valid RRR, and aggregate cached beta/volatility with coverage. Research chat can include its active strategy. No shares, cash balance, position values, or cost basis are sent.",
        )
        st.caption("AI requests run only after you press a Generate or Send button. No automatic provider fallback.")
    return {"provider": provider, "model": model, "max_output_tokens": int(max_tokens),
            "include_portfolio_context": include_portfolio}

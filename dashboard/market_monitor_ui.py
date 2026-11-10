"""Phase-one Market Regime & Capital Rotation UI using local cached data only."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import hashlib
import json
import logging
import re
import uuid
from zoneinfo import ZoneInfo
import plotly.graph_objects as go
import streamlit as st

from dashboard.cross_market import (
    classify_breadth_condition, correlation_by_spy_200d_regime, daily_returns_by_session,
    lead_lag_correlations, macro_changes_by_session, normalized_performance,
    observation_freshness, pair_observations, relative_ratio_return,
    lead_lag_sample_diagnostics, rolling_correlations, sector_participation_metrics, session_calendar,
)
from dashboard.market_history import (
    BROAD_MARKET_SYMBOLS, CREDIT_PROXY_SYMBOLS, MACRO_PROXY_SYMBOLS,
    MARKET_ETF_SYMBOLS, SECTOR_SYMBOLS,
)
from dashboard.market_historical_context import (
    completed_month_relative_history, completed_month_seasonality, summarize_vix_stress_history,
)
from dashboard.fred_macro import SERIES, refresh_fred_macro, diagnose_fred_connection
from dashboard.fred_volatility import load_fred_vix, refresh_fred_vix
from dashboard.cboe_volatility import load_cboe_volatility
from dashboard.data_access import list_volatility_observations
from dashboard.theme import PLOTLY_MUTED_COLOR, apply_plotly_theme
from dashboard.market_explorer import (
    BROAD_EQUITY_INDEX_SYMBOLS, BROAD_EQUITY_WINDOWS, COMPARISON_UNIVERSE, COMPARISON_WINDOWS, DEFAULT_BASKETS,
    basket_performance, broad_index_chart_data, broad_index_comparison_library, compare_etfs,
)
from dashboard.data_access import (
    list_market_explorer_favorites, save_market_explorer_favorite,
    remove_market_explorer_favorite, save_app_preference, get_app_preference,
    list_daily_price_coverage, list_market_events, list_market_ai_request_audit,
    save_market_regime_transition_record,
)
from dashboard.deepseek_market_research import (
    MARKET_ANALYST_PROMPT_VERSION, TOPICS as DEEPSEEK_TOPICS, estimate_market_research_cost, market_research_mode_settings,
    market_research_retry_settings, market_analysis_model, public_market_snapshot, run_market_research,
)
from dashboard.data_access import (
    clear_market_section_chat, deepseek_market_research_daily_spend,
    clear_market_section_chat_groups, get_app_preference, get_deepseek_market_research_by_key,
    latest_market_section_chat_context, list_deepseek_market_research_reports, list_market_section_chat,
    list_market_section_chat_groups,
    save_app_preference, save_market_section_chat_message,
)
from dashboard.market_chat import (
    PREMARKET_OUTPUT_TOKENS, answer_market_section_question, build_market_chat_context, parse_premarket_what_if,
    deepseek_chat_configured, estimate_market_chat_cost, earlier_market_chat_groups,
)
from dashboard.market_scenarios import (
    extract_market_symbols, list_market_research_notes, list_market_scenarios,
    list_market_scenario_revisions, list_market_user_observations, save_market_user_observation,
    save_market_research_note, validate_user_observation,
)
from dashboard.vix_risk import (build_vix_risk_monitor, latest_completed_weekday,
                                 vix_candlestick_data, vix_selected_window_expansion)
from dashboard.volatility_cycle import absolute_vix_regime, analyze_volatility_cycle
from dashboard.vix_statistics import build_vix_descriptive_signals, summarize_vix_statistical_signals
from dashboard.treasury_analytics import WINDOWS as TREASURY_WINDOWS, treasury_event_study, treasury_window_summary
from dashboard.treasury_yields import treasury_freshness
from dashboard.cboe_volatility import volatility_summary
from dashboard.market_regime import build_regime_features, historical_analog_distribution
from dashboard.market_snapshot import (
    build_market_snapshot, compare_market_snapshots, parse_market_analysis_json,
    provider_accent, saved_report_is_stale, select_saved_market_report,
)
from dashboard.market_visuals import sector_heatmap_grid, sector_return_rows, sector_rotation_points
from dashboard.risk_appetite import calculate_risk_appetite
from dashboard.regime_transition import (TRANSITION_METHODOLOGY_VERSION,
    build_transition_signal_strip, calculate_regime_transition)
from dashboard.regime_evidence import (METHODOLOGY_VERSION as REGIME_EVIDENCE_VERSION,
    GROUP_LABELS as REGIME_EVIDENCE_GROUP_LABELS, REGIMES as EVIDENCE_REGIMES,
    FUTURE_PREDICTION_IMPORT_SCHEMA, build_regime_evidence, compare_scenario_support,
    TOOLTIPS as REGIME_EVIDENCE_TOOLTIPS)
from dashboard.recent_catalysts import load_recent_catalysts


RATE_LABELS = {
    "DGS2": "2Y Treasury yield",
    "DGS10": "10Y Treasury yield",
    "DFF": "Effective Fed funds rate",
    "BAMLH0A0HYM2": "High-yield OAS",
    "BAMLC0A0CM": "Investment-grade OAS",
    "10Y-2Y": "10Y–2Y yield curve",
    "VIX": "VIX change",
}
logger = logging.getLogger(__name__)
ETF_CANDIDATES = tuple(dict.fromkeys((*BROAD_MARKET_SYMBOLS, *SECTOR_SYMBOLS.keys(), *CREDIT_PROXY_SYMBOLS)))
DEFAULT_EXPLORER_SYMBOLS = ("SPY", "RSP", "QQQ", "IWM", "SMH", "HYG", "LQD", "IEF")
VIX_SIGNAL_LAYOUT = {
    "column_ratio": (1.0, 1.25),
    "responsive_stack": True,
    "state_heading": "Current state & volatility signals",
    "chart_heading": "Recent VIX OHLC candlestick chart",
    "primary_headline": "VIX close",
    "signal_cards": ("Absolute regime", "Daily VIX change", "5-session range expansion",
                     "5-session net close change", "Peak surge unwound", "1-year historical percentile"),
}
CROSS_ASSET_RELATIONSHIP_VIEWS = (
    "ETF returns vs macro changes", "Rolling correlations", "Scatter relationship", "Exploratory lead / lag",
)
MARKET_SECTION_NAV = (
    ("Upcoming Market Catalysts", "upcoming-market-catalysts", "catalysts"),
    ("Market Regime Summary", "market-regime-summary", "regime"),
    ("Sector Rotation & Breadth", "sector-rotation-breadth", "sector_rotation"),
    ("Relative Strength & Defensive Rotation", "relative-strength-defensive-rotation", "relative_strength"),
    ("Rates & Macro", "rates-macro", "rates_macro"),
    ("Volatility Risk Monitor", "volatility-risk-monitor", "vix"),
    ("AI Market Intelligence Desk", "ai-market-briefing", "briefing"),
)
INTEGRATED_OUTLOOK_LAYOUT = {"scenario_columns": 2, "signal_columns": 2,
    "responsive_stack": True, "principal_headline_wrap": "anywhere",
    "fixed_headline_truncation": False, "signal_card_min_height_px": 178,
    "scenario_card_min_height_px": 190, "max_monitor_items": 3}
TURNING_POINT_SIGNAL_LABELS = {
    "volatility_stabilization": "VIX Stress",
    "technology_mag7_recovery": "Technology / Mag 7 Leadership",
    "broad_market_participation": "Broad Equity Participation",
    "defensive_leadership": "Defensive Leadership",
    "credit_confirmation": "Credit Confirmation",
}
REGIME_OUTLOOK_COLORS = {
    "regime": {"Risk-on": "#35b779", "Risk-on Expansion": "#35b779",
               "Risk-off": "#e05a67", "Rotation / Mixed": "#e3ad4f",
               "Risk-off Contraction": "#e05a67",
               "Insufficient Evidence": "#9aa4b2"},
    "turning_point": {"constructive": "#35b779", "caution": "#e3ad4f",
                       "deteriorating": "#e05a67", "unavailable": "#9aa4b2"},
}
PUBLIC_BENCHMARK_EARNINGS = frozenset({"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"})


def turning_point_signal_color(signal_id: str, state: str) -> str:
    """Map the subject-specific signal state to a consistent semantic tone."""
    if state in {"Insufficient data", "Mixed / insufficient data"}:
        tone = "unavailable"
    elif state in {"Stable / mixed", "Mixed / neutral", "Recovery unconfirmed", "Dominant and strengthening", "Dominant but fading", "Leadership shifting to cyclicals"}:
        tone = "caution"
    elif state in {"Pressure rising", "Underperformance deepening", "Narrowing", "Credit deteriorating"}:
        tone = "deteriorating"
    elif state in {"Pressure easing", "Relative recovery strengthening", "Broadening", "Credit appetite improving"}:
        tone = "constructive"
    else:
        tone = "unavailable"
    return REGIME_OUTLOOK_COLORS["turning_point"][tone]


def scenario_status_label(assessment: str) -> str:
    return {"supported": "Supported now",
            "plausible but unconfirmed": "Plausible but unconfirmed"}.get(assessment, "Insufficient evidence")


def scenario_status_color(assessment: str, scenario_id: str = "") -> str:
    if assessment == "plausible but unconfirmed":
        return "#e3ad4f"
    if assessment != "supported":
        return "#9aa4b2"
    if scenario_id == "broader_risk_on_participation":
        return "#35b779"
    if scenario_id == "stress_spreading_across_sectors":
        return "#e05a67"
    return "#e3ad4f"


def scenario_contradictions(scenario_id: str, signals: dict, risk_appetite: dict | None = None) -> list[str]:
    """Return directly observed Turning-Point signals that oppose a scenario."""
    signals = signals or {}
    rules = {
        "broader_risk_on_participation": {
            "volatility_stabilization": {"Pressure rising"},
            "defensive_leadership": {"Dominant and strengthening"},
            "technology_mag7_recovery": {"Underperformance deepening"},
            "broad_market_participation": {"Narrowing"},
        },
        "leadership_handoff_without_broad_improvement": {
            "broad_market_participation": {"Broadening"},
            "technology_mag7_recovery": {"Underperformance deepening"},
        },
        "stress_spreading_across_sectors": {
            "broad_market_participation": {"Broadening"},
            "volatility_stabilization": {"Pressure easing"},
            "defensive_leadership": {"Leadership shifting to cyclicals"},
        },
        "current_leadership_continues": {
            "technology_mag7_recovery": {"Underperformance deepening", "Mixed / insufficient data"},
            "broad_market_participation": {"Narrowing", "Stable / mixed"},
            "defensive_leadership": {"Dominant but fading", "Mixed / insufficient data"},
        },
    }
    conflicts = []
    for signal_id, opposing_states in rules.get(scenario_id, {}).items():
        item = signals.get(signal_id) or {}
        if item.get("state") in opposing_states:
            label = item.get("label") or TURNING_POINT_SIGNAL_LABELS.get(signal_id, signal_id)
            conflicts.append(f"{label}: {item.get('headline') or item.get('state')}")
    credit = (((risk_appetite or {}).get("components") or {}).get("credit_risk_appetite") or {})
    credit_direction = credit.get("direction")
    if scenario_id == "broader_risk_on_participation" and credit_direction == -1:
        conflicts.append("Credit ETF appetite: weakening")
    elif scenario_id == "stress_spreading_across_sectors" and credit_direction == 1:
        conflicts.append("Credit ETF appetite: improving")
    return conflicts[:2]


def render_market_sidebar_navigation() -> None:
    """Stable in-page anchors; links do not mutate widget-owned state."""
    with st.sidebar.expander("Market sections", expanded=True):
        for label, anchor, _section in MARKET_SECTION_NAV:
            st.markdown(f"[{label}](#{anchor})")


def _section_anchor(anchor: str) -> None:
    st.markdown(f'<span id="{anchor}"></span>', unsafe_allow_html=True)


def _paired_market_chart_layout(yaxis_title: str) -> dict:
    """Shared plotting geometry keeps the two side-by-side panels aligned."""
    return {"height": 345, "hovermode": "x unified", "yaxis_title": yaxis_title,
            "margin": {"l": 40, "r": 16, "t": 34, "b": 42},
            "legend": {"orientation": "h", "y": 1.08},
            "xaxis": {"showgrid": False},
            "yaxis": {"gridcolor": "rgba(150,160,175,.12)"}}


def _series_observations(macro, vix_history):
    output = {key: rows for key, rows in macro.items()}
    left = {row["observation_date"]: float(row["value"]) for row in macro.get("DGS2", [])}
    right = {row["observation_date"]: float(row["value"]) for row in macro.get("DGS10", [])}
    output["10Y-2Y"] = [{"observation_date": day, "value": right[day] - left[day],
                         "units": "percent", "provider": "derived_from_fred_initial_release"}
                        for day in sorted(left.keys() & right.keys())]
    output["VIX"] = [{"observation_date": row["observation_date"], "value": float(row["close"]),
                       "units": "index_points", "provider": row.get("provider", "FRED VIXCLS")}
                      for row in vix_history]
    return output


def _changes_by_session(observations, sessions, series_id):
    units = "points" if series_id == "VIX" else "percent"
    return {day: item["change"] for day, item in macro_changes_by_session(
        observations.get(series_id, []), sessions, units=units).items()}


def _macro_metric(observations, series_id, *, lookback=5):
    rows = observations.get(series_id, [])
    if not rows:
        return None, None, None, {"status": "unavailable", "age_days": None}
    latest = rows[-1]
    change_bps = None
    if len(rows) > lookback:
        change_bps = (float(latest["value"]) - float(rows[-lookback - 1]["value"])) * 100
    return float(latest["value"]), latest["observation_date"], change_bps, observation_freshness(latest["observation_date"])


def _ratio_return(histories, numerator, denominator, window):
    result = relative_ratio_return(histories, numerator, denominator, window)
    return result["return"] if result["status"] == "available" else None


def _status_for_series(series_id, rows, health):
    diagnostic = (health or {}).get("diagnostics", {}).get(series_id, {})
    error = (health or {}).get("errors", {}).get(series_id)
    if diagnostic.get("status") in {"authentication_error", "rate_limit", "timeout", "network_dns_proxy_failure", "invalid_series", "valid_response_no_observations"}:
        return diagnostic["status"]
    if error:
        return diagnostic.get("status", "refresh_failed")
    if not rows:
        return "unavailable"
    return observation_freshness(rows[-1]["observation_date"])["status"]


def _render_fred_status(macro, health, diagnostic):
    unavailable = []
    failed = []
    stale = []
    for series_id in SERIES:
        rows = macro.get(series_id, [])
        status = _status_for_series(series_id, rows, health)
        if not rows:
            unavailable.append(series_id)
        if (health or {}).get("errors", {}).get(series_id):
            failed.append(series_id)
        if status == "stale":
            stale.append(series_id)
    if unavailable:
        title = f"FRED: {len(unavailable)} series unavailable — View details"
    elif failed:
        title = f"FRED: refresh failed for {len(failed)} series; cached values retained — View details"
    elif stale:
        title = f"FRED: {len(stale)} cached series stale — View details"
    else:
        title = "FRED: cached series available — View details"
    with st.expander(title, expanded=False):
        st.markdown("**Data Health**")
        st.caption("Optional FRED refresh and connection test are available in Data Sources & Diagnostics at the top of Market.")
        rows = []
        errors = (health or {}).get("errors", {})
        for series_id, metadata in SERIES.items():
            observations = macro.get(series_id, [])
            latest = observations[-1] if observations else {}
            status = _status_for_series(series_id, observations, health)
            error = errors.get(series_id) or (errors.get("Setup") if status == "missing_credentials" else "")
            if error and observations:
                freshness = observation_freshness(observations[-1]["observation_date"])["status"]
                status = f"refresh failed · cached {freshness}"
            rows.append({"Series": series_id, "Last observation": latest.get("observation_date", "—"),
                         "Last refresh": latest.get("retrieved_at", "—"), "Status": status.replace("_", " ").title(),
                         "Error category": ((health or {}).get("diagnostics", {}).get(series_id, {}).get("error_category") or ("cached value stale" if status == "stale" else "—")),
                         "Source": metadata["source"]})
        st.dataframe(rows, hide_index=True, width="stretch")
        for series_id, message in errors.items():
            st.caption(f"{series_id}: {message}")
        if diagnostic:
            st.caption(f"Connection check · {diagnostic.get('series_id', 'DGS10')}: {diagnostic.get('status', 'unknown')} · {diagnostic.get('message', '')} · credential source: {diagnostic.get('credential_source', 'not available')}")
        st.caption("Python uses the environment first, then this project's .env, then Streamlit secrets. The API key and request URL are never shown. Refreshes are manual.")


def _render_relative_strength_explorer(histories, *, compact=False):
    st.markdown("##### Custom Relative Strength Explorer" if compact else "#### 3 · Custom Relative Strength Explorer")
    favorites = list_market_explorer_favorites()
    preset_labels = [f"{a}/{b}" for a, b in (("XLY", "XLP"), ("XLY", "SPY"), ("RSP", "SPY"), ("SMH", "SPY"), ("SMH", "QQQ"))]
    favorite_labels_for_preset = [f"Saved · {item['first']}/{item['second']} · {item['window']}D" for item in favorites]
    c1, c2 = st.columns([1.5, 1])
    selected_preset = c1.selectbox("Comparison preset", ["Custom", *preset_labels, *favorite_labels_for_preset], key="rs_pair_preset")
    default_window = 20
    if selected_preset.startswith("Saved · "):
        chosen_favorite = favorites[favorite_labels_for_preset.index(selected_preset)]
        default_window = chosen_favorite["window"]
    window = c2.selectbox("Window", COMPARISON_WINDOWS, index=COMPARISON_WINDOWS.index(default_window), format_func=lambda n: f"{n}D", key="rs_pair_window")
    pair_choices = list(COMPARISON_UNIVERSE)
    default_first = "SMH" if selected_preset == "Custom" else selected_preset.split("/")[0]
    default_second = "SPY" if selected_preset == "Custom" else selected_preset.split("/")[1]
    favorite = None
    if selected_preset.startswith("Saved · "):
        favorite = favorites[favorite_labels_for_preset.index(selected_preset)]
        default_first, default_second = favorite["first"], favorite["second"]
    with st.expander("Advanced · symbols, display and favorites", expanded=False):
        default_mode = favorite.get("mode", "excess") if favorite else "excess"
        mode_labels = ("Cumulative excess return", "Normalized relative-price ratio", "Two-line normalized performance")
        mode_options = {"excess": mode_labels[0], "relative_ratio": mode_labels[1], "two_line": mode_labels[2]}
        a, b, c = st.columns(3)
        first = a.selectbox("First ETF", pair_choices, index=pair_choices.index(default_first), key="rs_custom_first")
        second = b.selectbox("Second ETF", pair_choices, index=pair_choices.index(default_second), key="rs_custom_second")
        selected_mode = c.selectbox("Display", mode_labels, index=mode_labels.index(mode_options.get(default_mode, mode_labels[0])), key="rs_custom_mode")
        if st.button("Save comparison favorite", key="rs_save_favorite"):
            try:
                save_market_explorer_favorite(first, second, window, {mode_labels[0]:"excess", mode_labels[1]:"relative_ratio", mode_labels[2]:"two_line"}[selected_mode])
                st.success("Comparison saved locally.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))
        if favorites:
            favorite_labels = [f"{i+1}. {f['first']}/{f['second']} · {f['window']}D · {f['mode']}" for i, f in enumerate(favorites)]
            selected = st.selectbox("Saved comparison", favorite_labels, key="rs_favorite_select")
            if st.button("Remove favorite", key="rs_remove_favorite"):
                remove_market_explorer_favorite(favorite_labels.index(selected))
                st.rerun()
    if selected_preset in preset_labels:
        first, second = selected_preset.split("/")
        selected_mode = "Cumulative excess return"
    elif favorite:
        selected_mode = {"excess":"Cumulative excess return", "relative_ratio":"Normalized relative-price ratio", "two_line":"Two-line normalized performance"}.get(favorite.get("mode"), "Cumulative excess return")
    else:
        first = st.session_state.get("rs_custom_first", default_first)
        second = st.session_state.get("rs_custom_second", default_second)
        selected_mode = st.session_state.get("rs_custom_mode", "Cumulative excess return")
    mode_key = {"Cumulative excess return":"excess", "Normalized relative-price ratio":"relative_ratio", "Two-line normalized performance":"two_line"}[selected_mode]
    try:
        result = compare_etfs(histories, first, second, window, mode=mode_key)
    except ValueError as error:
        result = {"status": "invalid", "message": str(error)}
    if result["status"] == "available":
        figure = go.Figure()
        dates = result["dates"]
        if mode_key == "excess":
            figure.add_trace(go.Scatter(x=dates, y=result["cumulative_excess_pp"], mode="lines", name=f"{first} minus {second}", line={"width": 2.7, "color": "#8ab4f8"}, hovertemplate="%{x}<br>Excess return: %{y:+.2f} pp<extra></extra>"))
            figure.add_hline(y=0, line_color="rgba(180,190,205,.4)", line_dash="dot")
            ytitle = "Cumulative excess return (percentage points)"
        elif mode_key == "relative_ratio":
            figure.add_trace(go.Scatter(x=dates, y=result["relative_ratio_index"], mode="lines", name=f"{first}/{second}", line={"width": 2.7, "color": "#57c4a0"}, hovertemplate="%{x}<br>Relative ratio index: %{y:.2f}<extra></extra>"))
            figure.add_hline(y=100, line_color="rgba(180,190,205,.4)", line_dash="dot")
            ytitle = "Relative-price ratio index (start = 100)"
        else:
            for symbol, values, color in ((first, result["normalized_first"], "#8ab4f8"), (second, result["normalized_second"], "#e5b45c")):
                figure.add_trace(go.Scatter(x=dates, y=values, mode="lines", name=symbol, line={"width": 2.4, "color": color}, hovertemplate="%{x}<br>%{fullData.name}: %{y:.2f}<extra></extra>"))
            figure.add_hline(y=100, line_color="rgba(180,190,205,.4)", line_dash="dot")
            ytitle = "Normalized adjusted close (start = 100)"
        figure.update_layout(**(_paired_market_chart_layout(ytitle) if compact else {**_paired_market_chart_layout(ytitle), "height":400}))
        apply_plotly_theme(figure)
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
        st.caption(f"Tiingo adjusted closes · {window} sessions · through {result['as_of']} · excess {result['excess_pp']:+.2f} pp.")
    elif result["status"] == "insufficient_history":
        st.info(f"{first}/{second} needs {result['required_observations']} common adjusted closes for {window}D; {result['available_observations']} are cached. No dates were filled.")
    else:
        st.info(result.get("message", "Select two different ETFs."))


def _render_configurable_baskets(histories, periods):
    stored = get_app_preference("market_rotation_baskets_v1", DEFAULT_BASKETS)
    with st.expander("Advanced · configure cyclical and defensive baskets"):
        cyclical = st.multiselect("Cyclical components", list(SECTOR_SYMBOLS), default=list(stored.get("cyclical", DEFAULT_BASKETS["cyclical"])), key="basket_cyclical")
        defensive = st.multiselect("Defensive components", list(SECTOR_SYMBOLS), default=list(stored.get("defensive", DEFAULT_BASKETS["defensive"])), key="basket_defensive")
        st.caption("Set relative component weights; each basket is normalized to 100% for calculation.")
        cyc_weights, def_weights = {}, {}
        for label, symbols, source, target, prefix in (
            ("Cyclical weights", cyclical, stored.get("cyclical", {}), cyc_weights, "cyc"),
            ("Defensive weights", defensive, stored.get("defensive", {}), def_weights, "def"),
        ):
            if symbols:
                st.caption(label)
                for start in range(0, len(symbols), 4):
                    group = symbols[start:start + 4]
                    cols = st.columns(len(group))
                    for col, symbol in zip(cols, group):
                        target[symbol] = col.number_input(symbol, min_value=0.01, value=float(source.get(symbol, 1)), step=0.25, key=f"basket_weight_{prefix}_{symbol}")
        if st.button("Save basket configuration", key="basket_save"):
            if not cyclical or not defensive:
                st.error("Choose at least one ETF in each basket.")
            else:
                save_app_preference("market_rotation_baskets_v1", {"cyclical": cyc_weights, "defensive": def_weights})
                st.rerun()
        if st.button("Reset baskets to defaults", key="basket_reset"):
            save_app_preference("market_rotation_baskets_v1", DEFAULT_BASKETS)
            st.rerun()
    try:
        return basket_performance(histories, stored, periods)
    except (ValueError, TypeError):
        return {"status": "invalid"}


def _save_market_spending_defaults() -> None:
    values = {
        "per_request_usd": float(st.session_state.get("market_ai_request_cap", 0.5)),
        "daily_usd": float(st.session_state.get("market_ai_daily_cap", 2.0)),
    }
    save_app_preference("market_ai_spending_limits_v1", values)


def _save_market_ai_model() -> None:
    model = st.session_state.get("market_ai_model", "deepseek-flash")
    if model in {"deepseek-flash", "deepseek-v4-pro"}:
        save_app_preference("market_ai_model_v1", model)


def _save_market_analysis_depth() -> None:
    depth = st.session_state.get("deepseek_market_mode", "Deep Research")
    if depth in {"Fast Briefing", "Deep Research"}:
        save_app_preference("market_ai_analysis_depth_v1", depth)


def _daily_market_refresh_gaps(refresh_result: dict) -> list[str]:
    """Return dated Market-source gaps; stock symbols are outside this refresh universe."""
    gaps = []
    historical = refresh_result.get("historical") or {}
    diagnostics = historical.get("diagnostics", [])
    for row in diagnostics:
        symbol = row.get("symbol")
        state = row.get("final_freshness_state")
        if symbol in MARKET_ETF_SYMBOLS and state not in {"Updated", "Already current"}:
            gaps.append(f"ETF {symbol}: {state or 'not refreshed'}")
    for source in ("historical", "vix", "treasury", "calendar"):
        errors = (refresh_result.get(source) or {}).get("errors") or {}
        for name, detail in errors.items():
            if source == "historical" and name not in MARKET_ETF_SYMBOLS and name != "stage":
                continue
            label = f"{source.upper()} {name}" if name != "stage" else source.upper()
            gaps.append(f"{label}: {str(detail)[:100]}")
    # Keep the warning compact and stable if a provider reports several row errors.
    return list(dict.fromkeys(gaps))[:8]


def _run_status_label(result: dict) -> str:
    if result.get("cached"):
        return "Cached result reused"
    status = result.get("research_status")
    return {
        "complete": "Completed",
        "complete_cost_warning": "Completed · usage exceeded estimate",
        "spending_cap": "Blocked by budget",
        "incomplete": "Incomplete model output",
        "empty_synthesis": "Response validation failure · no visible answer",
        "invalid_analysis_contract": "Response validation failure",
        "synthesis_error": "Network/API failure",
        "web_search_not_verified": "Web search not verified",
        "web_search_sources_missing": "Web sources unavailable",
    }.get(status, str(status or "Failed").replace("_", " ").title())


def _render_gpt_research(comparison=None, regime_snapshot=None, unavailable=(), calendar_rows=(), snapshot=None, saved_report=None):
    """One explicit full-snapshot analysis action; settings are local/persisted."""
    if snapshot is None:
        local_snapshot = dict(regime_snapshot or {})
        local_snapshot.update({"selected_comparison": comparison, "missing_data": list(unavailable)})
        snapshot = public_market_snapshot(comparison=comparison, vix=local_snapshot.get("vix"),
            sector_metrics=local_snapshot.get("sector_metrics"), treasury=local_snapshot.get("treasury"),
            missing=unavailable, timeframe=local_snapshot.get("timeframe"), upcoming_events=calendar_rows)

    preferences = get_app_preference("market_ai_spending_limits_v1", {}) or {}
    request_default = float(preferences.get("per_request_usd", 0.5))
    daily_default = float(preferences.get("daily_usd", 2.0))
    saved_model = get_app_preference("market_ai_model_v1", "deepseek-flash")
    if "market_ai_model" not in st.session_state:
        st.session_state["market_ai_model"] = saved_model if saved_model in {"deepseek-flash", "deepseek-v4-pro"} else "deepseek-flash"
    saved_depth = get_app_preference("market_ai_analysis_depth_v1", None)
    if saved_depth in {"Fast Briefing", "Deep Research"}:
        if st.session_state.get("deepseek_market_mode") != saved_depth:
            st.session_state["deepseek_market_mode"] = saved_depth
    elif not st.session_state.get("_market_analysis_depth_default_initialized"):
        # Migrate the old first-radio-option default (Fast Briefing) once. Any
        # subsequent user selection is persisted by the radio callback.
        st.session_state["deepseek_market_mode"] = "Deep Research"
        st.session_state["_market_analysis_depth_default_initialized"] = True
    if not st.session_state.get("market_ai_spending_initialized_v2"):
        # Reload the persisted policy once per session so an explicitly changed
        # cap takes effect in an already-open Streamlit session. Saved limits
        # remain authoritative; these fallbacks apply only when none are saved.
        st.session_state["market_ai_request_cap"] = request_default
        st.session_state["market_ai_daily_cap"] = daily_default
        st.session_state["market_ai_spending_initialized_v2"] = True
    run_clicked = False
    with st.sidebar.expander("AI Market Research", expanded=True):
        depth = st.radio("Analysis depth", ("Fast Briefing", "Deep Research"),
            key="deepseek_market_mode", on_change=_save_market_analysis_depth,
            help="Every run analyzes the full canonical Market Intelligence snapshot. Deep Research is the default.")
        selected_model = st.session_state.get("market_ai_model", "deepseek-flash")
        model = market_analysis_model(depth, selected_model)
        if depth == "Deep Research":
            st.caption(f"Provider: DeepSeek · Deep Research synthesis model: {model} · no fallback")
        else:
            st.caption(f"Provider: DeepSeek · Fast Briefing synthesis model: {model}")
        evidence_label = st.radio("Evidence source", ("Dashboard Analysis", "Dashboard + Web Verification"),
            key="deepseek_market_analysis_mode", help="Web verification runs only after you explicitly request a full analysis.")
        search_mode = "dashboard_analysis" if evidence_label == "Dashboard Analysis" else "dashboard_web"
        with st.expander("Advanced Settings", expanded=False):
            st.selectbox("Fast Briefing / section-chat model", ("deepseek-flash", "deepseek-v4-pro"),
                         key="market_ai_model", on_change=_save_market_ai_model,
                         help="Selects the Fast Briefing and section-chat model. Full-market Deep Research always requests deepseek-v4-pro and will stop with an access error if your account does not expose it; there is no fallback.")
            request_limit = st.number_input("Per-request cap · USD", min_value=.01, max_value=10.0,
                step=.25, key="market_ai_request_cap", on_change=_save_market_spending_defaults)
            daily_limit = st.number_input("Daily cap · USD", min_value=.01, max_value=50.0,
                step=.5, key="market_ai_daily_cap", on_change=_save_market_spending_defaults)
        reasoning_effort, output_token_limit = market_research_mode_settings(depth)
        try:
            estimate = estimate_market_research_cost(snapshot, model, output_token_limit=output_token_limit,
                                                       search_mode=search_mode)
        except (ValueError, TypeError) as exc:
            estimate = None
            preflight_error = str(exc)
        else:
            preflight_error = ""
        daily_spend = deepseek_market_research_daily_spend()
        estimate_fits = estimate is not None and estimate <= request_limit and daily_spend + estimate <= daily_limit
        if estimate is not None:
            st.caption(f"Estimated upper bound ${estimate:.4f} · spent today ${daily_spend:.4f} · limits ${request_limit:.2f}/request, ${daily_limit:.2f}/day.")
        with st.expander("Advanced Diagnostics", expanded=False):
            st.caption(f"Requested model: {model} · reasoning: {reasoning_effort} · automatically selected output allowance: {output_token_limit:,} tokens (reasoning + visible answer).")
            st.caption("The DeepSeek /models response is checked on explicit requests for account-specific output and context limits. Official API model metadata is not queried on normal reruns.")
        daily_refresh_analysis = bool(st.session_state.pop("_daily_market_refresh_run_ai", False))
        # Selecting the opt-in checkbox before clicking Refresh is explicit consent to
        # analyze the resulting available-data snapshot. A provider gap should be
        # visible in that snapshot, but must not silently turn the one-button flow
        # into a second approval step.
        st.session_state.pop("_daily_market_refresh_ai_needs_confirmation", None)
        if daily_refresh_analysis:
            gaps = _daily_market_refresh_gaps(st.session_state.get("daily_market_refresh_result", {}))
            if gaps:
                st.warning("DeepSeek will analyze the refreshed snapshot with these source gaps: " +
                           "; ".join(gaps) + ".")
        run_clicked = st.button("Run Full Market Analysis", key="deepseek_market_research", type="primary")
        force_clicked = (st.button("Force New Analysis", key="deepseek_market_research_force",
                                  help="Bypass a compatible cached report and make a new paid request.")
                         or st.session_state.pop("_market_force_refresh_requested", False))
        st.caption("This analyzes the entire canonical snapshot. Reruns and navigation make no AI calls.")
        last_run = st.session_state.get("market_full_analysis_run")
        retry_clicked = False
        if last_run:
            st.markdown(f"**{last_run.get('status','Failed')}** · run `{last_run.get('run_id','—')}`")
            st.caption(f"Stage: {last_run.get('stage','—')} · requested: {last_run.get('model','deepseek-flash')} · actual: {last_run.get('actual_model') or 'not reported'} · snapshot: {last_run.get('snapshot_as_of') or 'unavailable'}")
            if last_run.get("error"):
                st.error(last_run["error"])
            with st.expander("Technical diagnostics", expanded=False):
                st.json({key: value for key, value in last_run.items() if key not in {"error", "run_id"}})
                if last_run.get("error_type"):
                    st.caption(f"Sanitized exception type: {last_run['error_type']}")
            can_retry = (last_run.get("status") == "Incomplete model output" and
                         not last_run.get("synthesis_only") and
                         last_run.get("retry_cache_key") and
                         (last_run.get("source_count", 0) > 0 or search_mode == "dashboard_analysis") and
                         last_run.get("retry_search_mode") == search_mode and
                         last_run.get("analysis_depth") == depth and
                         last_run.get("model") == model and
                         last_run.get("snapshot_fingerprint") == snapshot.get("fingerprint"))
            if can_retry:
                retry_reasoning, retry_output = market_research_retry_settings(depth)
                retry_estimate = estimate_market_research_cost(snapshot, model,
                    output_token_limit=retry_output, search_mode=search_mode, search_cached=True)
                remaining = max(0.0, daily_limit - deepseek_market_research_daily_spend())
                st.caption(f"Synthesis-only retry estimate ≤${retry_estimate:.4f} · output {retry_output:,} tokens · no new search. Remaining daily budget ${remaining:.4f}.")
                retry_clicked = st.button("Retry synthesis only · larger automatic budget",
                    key="deepseek_market_synthesis_retry")
        if run_clicked or retry_clicked or force_clicked or daily_refresh_analysis:
            request_reasoning, request_output = ((market_research_retry_settings(depth) if retry_clicked
                                                  else market_research_mode_settings(depth)))
            try:
                request_estimate = estimate_market_research_cost(snapshot, model,
                    output_token_limit=request_output, search_mode=search_mode,
                    search_cached=retry_clicked)
            except (ValueError, TypeError) as exc:
                request_estimate = None
                request_preflight_error = str(exc)
            else:
                request_preflight_error = ""
            request_fits = (request_estimate is not None and request_estimate <= request_limit and
                            deepseek_market_research_daily_spend() + request_estimate <= daily_limit)
            run_id = uuid.uuid4().hex[:12]
            run_record = {"run_id": run_id, "stage": "budget preflight", "model": model,
                          "snapshot_as_of": snapshot.get("as_of"), "snapshot_fingerprint": snapshot.get("fingerprint"),
                          "status": "Running", "error": "", "reasoning_effort": request_reasoning,
                          "automatic_output_token_limit": request_output,
                          "estimated_cost_usd": request_estimate, "synthesis_only": retry_clicked,
                          "analysis_depth": depth, "retry_search_mode": search_mode}
            st.session_state["market_full_analysis_run"] = run_record
            if not request_fits:
                run_record.update(status="Blocked by budget", error=request_preflight_error or
                    "Estimated upper-bound cost does not fit the configured request and remaining daily caps.",
                    estimated_ceiling_usd=request_estimate, daily_spend_usd=daily_spend)
            else:
                try:
                    result = run_market_research(topic="Full Market Intelligence Snapshot", snapshot=snapshot,
                        research_question=DEEPSEEK_TOPICS["Full Market Intelligence Snapshot"],
                        request_limit_usd=request_limit, daily_limit_usd=daily_limit,
                        force=retry_clicked or force_clicked,
                        synthesis_only=retry_clicked,
                        cached_report_key=last_run.get("retry_cache_key") if retry_clicked and last_run else None,
                        model=model, search_mode=search_mode, reasoning_effort=request_reasoning,
                        output_token_limit=request_output)
                    run_record["stage"] = "persistence verification"
                    persisted = get_deepseek_market_research_by_key(result.get("cache_key", ""))
                    if result.get("research_status") in {"complete", "complete_cost_warning"} and persisted and persisted.get("report_text"):
                        run_record.update(status="Cached result reused" if result.get("cached") else "Completed",
                                          stage="saved report loaded", report_id=persisted.get("id"),
                    actual_model=(persisted.get("usage") or {}).get("actual_model") or "Not reported by provider",
                                          source_count=len(persisted.get("sources", [])), cached=bool(result.get("cached")))
                    elif result.get("research_status") in {"complete", "complete_cost_warning"}:
                        run_record.update(status="Persistence failure", error="The completed report could not be reloaded from SQLite.")
                    else:
                        usage = result.get("usage") or {}
                        run_record.update(status=_run_status_label(result),
                                          stage=usage.get("completion_status", result.get("research_status", "analysis")),
                                          error=result.get("error_message") or "The analysis did not produce a saved completed briefing.",
                                          source_count=len(result.get("sources", [])), completion_reason=result.get("completion_reason"),
                                          retry_cache_key=result.get("cache_key") if result.get("research_status") == "incomplete" and result.get("sources") else None,
                                          diagnostics={key: usage.get(key) for key in ("completion_status", "completion_reason", "error_category", "request_duration_seconds", "input_tokens", "output_tokens", "reasoning_tokens", "visible_output_tokens", "actual_model") if usage.get(key) is not None})
                except Exception as exc:
                    from dashboard.deepseek_market_research import transport_error_category
                    message = str(exc) if type(exc).__name__ == "MarketResearchError" else ""
                    known_categories = ("dns_failure", "tls_failure", "proxy_failure", "connect_timeout",
                                        "read_timeout", "network_connection_failure", "authentication_error",
                                        "rate_limit", "insufficient_balance", "spending_cap")
                    category = ("missing_credentials" if "not configured" in message.lower()
                                else next((item for item in known_categories if item in message.lower()),
                                          transport_error_category(exc)))
                    logger.error("Market full analysis failed run_id=%s stage=%s exception_type=%s",
                                 run_id, run_record.get("stage", "request"), type(exc).__name__)
                    run_record.update(status="Network/API or persistence failure", stage="request/persistence",
                                      error=message or f"Request failed ({category}). Check connection, credentials, budget and SQLite access, then retry.",
                                      error_type=type(exc).__name__, error_category=category)
            st.session_state["market_full_analysis_run"] = run_record
            st.rerun()

    st.session_state["_market_saved_report_for_page"] = saved_report
    st.session_state["_market_snapshot_for_page"] = snapshot

    with st.sidebar.expander("AI Usage & Diagnostics", expanded=False):
        st.caption("Local request metadata only. Prompts, secrets and portfolio records are not stored here. Actual model is shown only when the provider returns an ID.")
        audit_rows = list_market_ai_request_audit(limit=20)
        if not audit_rows:
            st.caption("No audited requests yet. New full analyses and section chats will appear here.")
        for row in audit_rows:
            actual = row.get("actual_model") or "Not reported by provider"
            st.caption(f"{row.get('requested_at', '')} · {row.get('purpose', 'request')} · {row.get('evidence_mode') or 'mode unknown'} · {row.get('completion_status', '')}")
            st.caption(f"Requested {row.get('requested_model')} · actual {actual} · {row.get('endpoint')}")
            st.caption(f"Reasoning {row.get('reasoning_effort') or '—'} · allowance {row.get('output_token_allowance') or '—'} · tokens {row.get('input_tokens') or '—'} in / {row.get('output_tokens') or '—'} out ({row.get('reasoning_tokens') or '—'} reasoning, {row.get('visible_output_tokens') or '—'} visible)")
            st.caption(f"Search {row.get('search_requests', 0)} · estimated ${row.get('estimated_cost_usd', 0):.5f} · reported {row.get('reported_cost_usd') if row.get('reported_cost_usd') is not None else 'not supplied'} · error {row.get('error_category') or 'none'}")

    with st.sidebar.expander("Manage Ask AI chat history", expanded=False):
        chat_groups = list_market_section_chat_groups()
        if not chat_groups:
            st.caption("No saved Ask AI conversations.")
        else:
            all_label = f"All saved chat histories ({sum(int(row['message_count']) for row in chat_groups)} messages)"
            labels_to_group = {}
            for row in chat_groups:
                models = (row.get("models") or "DeepSeek").replace(",", ", ")
                label = (f"{row['section_id']} · {str(row['snapshot_fingerprint'])[:10]} · "
                         f"{row['message_count']} messages · {row['last_message_at']} · {models}")
                labels_to_group[label] = (row["section_id"], row["snapshot_fingerprint"])
            epoch = int(st.session_state.get("_market_chat_manager_epoch", 0))
            selection_key = f"market_chat_manager_selection_{epoch}"
            selected = st.multiselect("Select histories to delete", [all_label, *labels_to_group],
                key=selection_key, help="Each item represents one section and frozen snapshot/model context.")
            confirm = st.checkbox("I understand selected messages will be permanently deleted.",
                                  key=f"market_chat_manager_confirm_{epoch}")
            all_selected = all_label in selected
            targets = ([(row["section_id"], row["snapshot_fingerprint"]) for row in chat_groups]
                       if all_selected else [labels_to_group[label] for label in selected if label in labels_to_group])
            if st.button("Delete selected chat history", key=f"market_chat_manager_delete_{epoch}",
                         type="secondary", disabled=not (targets and confirm)):
                deleted = clear_market_section_chat_groups(targets)
                st.session_state["_market_chat_manager_epoch"] = epoch + 1
                st.session_state["_market_chat_manager_feedback"] = f"Deleted {deleted} messages from {len(targets)} selected history group(s)."
                st.rerun()
            if st.session_state.get("_market_chat_manager_feedback"):
                st.success(st.session_state["_market_chat_manager_feedback"])

def _metric_ref_summaries(snapshot: dict, references, maximum: int = 2) -> list[str]:
    summaries = []
    sections = (snapshot or {}).get("sections") or {}
    for reference in references or []:
        if not isinstance(reference, str) or "." not in reference:
            continue
        section_id, metric_id = reference.split(".", 1)
        metric = ((sections.get(section_id) or {}).get("metrics") or {}).get(metric_id)
        if not isinstance(metric, dict) or metric.get("value") is None:
            continue
        label = metric_id.replace("_", " ").replace("  ", " ").strip().title()
        value = metric.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            shown = f"{value:+.2f}" if metric.get("unit") in {"pp", "percentage_points", "bp", "%"} else f"{value:.2f}"
        else:
            shown = str(value)
        unit = metric.get("unit")
        window = metric.get("window")
        suffix = f" {unit}" if unit else ""
        if window:
            suffix += f" · {window}"
        if metric.get("as_of"):
            suffix += f" · {metric['as_of']}"
        summaries.append(f"{label}: {shown}{suffix}")
        if len(summaries) >= maximum:
            break
    return summaries


def _scenario_label(scenario_id: str) -> str:
    return {
        "current_leadership_continues": "Continuation of current leadership",
        "broader_risk_on_participation": "Broader risk-on recovery",
        "leadership_handoff_without_broad_improvement": "Leadership handoff",
        "stress_spreading_across_sectors": "Broadening market stress",
    }.get(scenario_id, scenario_id.replace("_", " ").title())


def _regime_support_color(regime: str) -> str:
    return {
        "Risk-on Expansion": "#55c7a2",
        "Rotation / Mixed": "#e5b45c",
        "Risk-off Contraction": "#ef6b73",
    }.get(regime, "#929ba8")


def _render_regime_evidence_monitor(evidence: dict, snapshot: dict,
                                    risk_appetite: dict, transition: dict) -> None:
    """Render retained quantitative regime evidence only inside advanced details."""
    st.markdown("**Deterministic regime evidence · not a forecast**")
    st.caption("Relative evidence support is descriptive, not a probability. Correlated measurements are aggregated within groups.")
    with st.popover("ⓘ How to read this monitor"):
        st.write(REGIME_EVIDENCE_TOOLTIPS["regime"])
        st.write(REGIME_EVIDENCE_TOOLTIPS["relative_evidence"])
        st.caption(f"Method: {evidence.get('methodology_version', REGIME_EVIDENCE_VERSION)} · snapshot observations through {evidence.get('as_of') or 'unavailable'}.")
    shares = evidence.get("relative_evidence_support")
    if not shares:
        st.info(evidence.get("explanation") or "Insufficient aligned evidence for relative regime support.")
    else:
        score_rows = []
        for regime in EVIDENCE_REGIMES:
            share = float(shares.get(regime, 0.0))
            factors = evidence.get("support_factors", {}).get(regime, {})
            supporting = factors.get("supporting_factors") or []
            contradicting = factors.get("contradictory_factors") or []
            score_rows.append({"Regime": regime, "Relative evidence support": f"{share:.0f}%",
                "Supporting groups": ", ".join(supporting) or "No dominant measured group",
                "Contradictory groups": ", ".join(contradicting) or "None identified"})
        st.dataframe(score_rows, hide_index=True, width="stretch")
        st.caption(f"Evidence date: {evidence.get('as_of') or '—'} · Methodology {evidence.get('methodology_version')} · "
                   f"Voting groups {len(evidence.get('voting_groups') or [])}/4; rates and macro shown as non-voting context.")
        transition_characteristics = evidence.get("transition_characteristics") or {}
        st.caption("Transition characteristic · " + str(transition_characteristics.get("stress_easing_state", "Insufficient Evidence")) +
                   (" · Recovery from stress confirmed (prior stress documented)" if transition_characteristics.get("recovery_from_stress_confirmed") else ""))

    groups = evidence.get("groups") or {}
    st.markdown("**Evidence groups & definitions**")
    for key, tooltip_key in (("equity_participation", "equity_participation"),
                                 ("sector_leadership", "sector_leadership"),
                                 ("credit", "credit"), ("volatility", "volatility"),
                                 ("rates_macro", "rates_macro")):
        item = groups.get(key) or {}
        title = REGIME_EVIDENCE_GROUP_LABELS.get(key, key.replace("_", " ").title())
        state = "Context only" if key == "rates_macro" else (
            f"direction {item.get('direction'):+.2f}" if item.get("direction") is not None else "unavailable")
        with st.popover(f"{title} · {state}"):
            st.write(REGIME_EVIDENCE_TOOLTIPS[tooltip_key])
            st.caption(f"Observation date: {item.get('as_of') or 'unavailable'}")
            if item.get("metrics"):
                st.json(item["metrics"])
            if item.get("factors"):
                for fact in item["factors"]:
                    st.write("• " + str(fact))
    st.markdown("**Initial evidence weights**")
    st.caption("Equity participation 30% · Sector leadership 25% · Credit 25% · Volatility 20%. Rates & macro are displayed as context but excluded from directional scoring because their interpretation is ambiguous.")
    st.caption("Transformations: existing Python group directions are clamped to [−1, +1]; XLK/SMH/MAGS relative returns are averaged after dividing percentage-point spreads by 2.5 and clamping; available OAS changes use a 5 bp deadband and a 20 bp full scale, then combine as one credit sub-signal with HYG proxies. Correlated observations are first aggregated into their named group.")
    st.caption("Three-regime v2 rebuilds support directly from current signed groups: P=Σw·max(d,0); N=Σw·max(−d,0); Z=Σw·(1−|d|); disagreement D=2·min(P,N). Raw Risk-on=P, Risk-off=N, Rotation=Z+D, then these three fresh raw totals are normalized. The former Stress Recovery score is not redistributed; stress easing and prior-stress-qualified recovery are separate transition characteristics.")
    st.caption("At least current equity participation plus two other current, date-aligned groups are required. Missing/stale groups are excluded and remaining weights renormalize. Rates/macro values remain dated context, with no directional vote. Stress easing may be described without prior stress; 'recovery from stress confirmed' requires prior elevated VIX plus easing in breadth, credit and VIX. Sensitivity changes only hypothetical inputs and recalculates this methodology.")
    with st.popover("Future validated forecast import contract"):
        st.caption("Interface specification only. This dashboard does not import or display predicted probabilities; validation belongs in Market Prediction Lab.")
        st.json(FUTURE_PREDICTION_IMPORT_SCHEMA)

    st.markdown("**Explore Market Scenarios · hypothetical local calculation**")
    with st.container(border=True):
        st.caption("Hypothetical local recalculation only. These assumptions do not alter observed data, history, portfolio state, or forecasts.")
        controls = {
            "scenario_breadth": ("Equity breadth", ("Unchanged · use current reading", "Weakens", "Strengthens")),
            "scenario_technology": ("Technology leadership", ("Unchanged · use current reading", "Weakens", "Rebounds")),
            "scenario_credit": ("Credit appetite", ("Unchanged · use current reading", "Deteriorates", "Stabilizes")),
            "scenario_vix": ("VIX pressure", ("Unchanged · use current reading", "Increases", "Eases")),
            "scenario_defensive": ("Defensive leadership", ("Unchanged · use current reading", "Strengthens", "Weakens")),
        }
        if st.button("Reset to actual market snapshot", key="regime_scenario_reset"):
            for key, (_, options) in controls.items():
                st.session_state[key] = options[0]
        selections = {}
        control_cols = st.columns(5, gap="small")
        for index, (key, (label, options)) in enumerate(controls.items()):
            with control_cols[index]:
                selections[key] = st.selectbox(label, options, key=key, help=REGIME_EVIDENCE_TOOLTIPS["scenario"])
        overrides = {}
        to_direction = {
            "Weakens": -1.0, "Strengthens": 1.0, "Rebounds": 1.0,
            "Deteriorates": -1.0, "Stabilizes": 1.0, "Increases": -1.0,
            "Eases": 1.0,
        }
        for key, selected in selections.items():
            if selected.startswith("Unchanged"):
                continue
            if key == "scenario_breadth":
                overrides["equity_participation"] = to_direction[selected]
            elif key == "scenario_technology":
                overrides["technology_leadership"] = to_direction[selected]
            elif key == "scenario_credit":
                overrides["credit"] = to_direction[selected]
            elif key == "scenario_vix":
                overrides["volatility"] = to_direction[selected]
            elif key == "scenario_defensive":
                # The UI describes strength of defensives; the scorer converts
                # it into cyclical-versus-defensive orientation.
                overrides["defensive_leadership"] = (1.0 if selected == "Strengthens" else -1.0)
        hypothetical = build_regime_evidence(risk_appetite, transition,
            ((snapshot.get("sections") or {}).get("rates_macro") or {}), overrides=overrides)
        comparison = compare_scenario_support(evidence, hypothetical)
        before = comparison.get("baseline")
        after = comparison.get("hypothetical")
        if before and after:
            scenario_cols = st.columns(2, gap="large")
            for title, values, column in (("Actual snapshot", before, scenario_cols[0]),
                                          ("Hypothetical scenario", after, scenario_cols[1])):
                with column:
                    st.markdown(f"**{title}**")
                    for regime in EVIDENCE_REGIMES:
                        value = float(values.get(regime, 0.0))
                        st.caption(f"{regime} · {value:.0f}% relative evidence support")
            changed = comparison.get("changed_groups") or []
            if changed:
                st.markdown("**Evidence groups changed by assumptions**")
                for item in changed:
                    before_direction = "unavailable" if item["before"] is None else f"{item['before']:+.2f}"
                    after_direction = "unavailable" if item["after"] is None else f"{item['after']:+.2f}"
                    st.caption(f"{item['label']}: {before_direction} → {after_direction}")
            else:
                st.caption("No voting evidence group changed from the actual readings.")
            if not (evidence.get("transition_characteristics") or {}).get("recovery_from_stress_confirmed"):
                st.caption("Stress easing is a separate transition characteristic; recovery from stress is not confirmed without documented prior stress and aligned improvement in VIX, breadth and credit.")
        else:
            st.info("The actual snapshot lacks enough aligned groups to compare regime support. Scenario assumptions remain hypothetical and are not persisted.")


def _render_integrated_market_outlook(payload: dict | None, sources: list[dict], report: dict | None,
                                      current_snapshot: dict, deterministic_transition: dict,
                                      risk_appetite: dict, *, stale: bool = False) -> None:
    """Render the saved AI research as a readable, evidence-led workstation."""
    briefing = (payload or {}).get("briefing") or {}
    transition = briefing.get("transition_outlook") or {}
    report_snapshot = (report or {}).get("snapshot") or {}
    source_urls = {f"S{index}": source.get("url") for index, source in enumerate(sources, 1)
                   if isinstance(source.get("url"), str) and source["url"].startswith("https://")}

    def rich(text: str) -> str:
        escaped = html.escape(str(text or ""))
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        for source_id, url in source_urls.items():
            label = html.escape(f"[{source_id}]")
            anchor = f'<a href="{html.escape(url, quote=True)}" target="_blank">{label}</a>'
            escaped = escaped.replace(label, anchor)
        return escaped.replace("\n", "<br>")

    def paragraph(text, *, color="#e2e7ef", size=16):
        if text:
            st.markdown(f"<div style='font-size:{size}px;line-height:1.65;color:{color};overflow-wrap:anywhere'>{rich(text)}</div>", unsafe_allow_html=True)

    def section_heading(title, icon, color="#b8c7dc"):
        st.markdown(f"<div style='font-size:21px;font-weight:750;line-height:1.3;margin:1.2rem 0 .6rem;color:{color}'>{icon}&nbsp; {html.escape(title)}</div>", unsafe_allow_html=True)

    if not payload:
        local_state = (risk_appetite or {}).get("classification") or "Insufficient local evidence"
        st.markdown(f"<div style='border-left:4px solid #68a9ff;padding:.6rem 1rem;background:rgba(104,169,255,.07);font-size:16px;line-height:1.6'>No compatible completed AI briefing is saved. <strong>Local quantitative observation only:</strong> {html.escape(str(local_state))} as of {html.escape(str(current_snapshot.get('as_of') or 'unavailable'))}. Use the sidebar action when you want a new AI interpretation.</div>", unsafe_allow_html=True)
        return

    usage = (report or {}).get("usage") or {}
    model = usage.get("actual_model") or usage.get("requested_model") or (report or {}).get("model") or "DeepSeek"
    report_date = report_snapshot.get("as_of") or "unavailable"
    provider = (report or {}).get("provider") or "DeepSeek"
    accent = provider_accent(provider.lower())
    research_date = report.get("generated_at", "date unavailable")
    with st.container(border=True):
        st.markdown(f"<div style='border-left:4px solid {accent};padding-left:.8rem;font-size:14px;font-weight:700;color:{accent}'>◈ {html.escape(provider.title())} · {html.escape(str(model))}</div>", unsafe_allow_html=True)
        st.caption(f"Market snapshot · {report_date}   |   Research · {research_date}   |   {'STALE · retained for reference' if stale else 'Snapshot match'}")
        if stale:
            st.caption("The saved research is shown with its original as-of date. Current local measurements are separate.")

    section_heading("1 · Current Market Thesis", "◈", accent)
    if briefing.get("executive_takeaway"):
        with st.container(border=True):
            headline = str(briefing["executive_takeaway"]).strip()
            parts = re.split(r"(?<=[.!?])\s+", headline, maxsplit=1)
            lead = parts[0] if len(parts) > 1 else headline
            rest = parts[1] if len(parts) > 1 else ""
            paragraph(f"**{lead}**", color="#f3f6fb", size=19)
            paragraph(rest)
    else:
        paragraph("This report version has no executive thesis; the original response remains available in Full Research.", color="#aeb8c6")

    changed = transition.get("emerging_direction") or transition.get("current_regime_interpretation")
    if isinstance(changed, str) and changed.strip():
        section_heading("2 · What Changed and Why It Matters", "↗")
        with st.container(border=True):
            paragraph(changed)
        if transition.get("python_alignment") == "disagrees" and transition.get("disagreement_explanation"):
            st.caption("AI interpretation differs from Python's descriptive state: " + str(transition["disagreement_explanation"]))

    hypotheses = briefing.get("hypotheses") or []
    if not hypotheses and transition.get("scenarios"):
        hypotheses = [{
            "title": _scenario_label(item.get("scenario_id", "Market scenario")),
            "supporting_evidence": item.get("interpretation", ""),
            "competing_explanation": transition.get("main_uncertainty", "See conflicting evidence in the saved research."),
            "confirm_or_invalidate": item.get("monitor_next", ""),
            "sector_implications": "See the scenario's stated market context.",
            "metric_refs": item.get("metric_refs", []),
            "review_horizon": item.get("review_horizon", "Not specified in the saved report."),
        } for item in transition.get("scenarios", [])]
    section_heading("3 · Key Research Hypotheses", "⌕", "#b9a1ff")
    if hypotheses:
        st.caption("Exploratory explanations from the saved report · qualitative, not probability-ranked.")
        columns = st.columns(2, gap="medium")
        for index, item in enumerate(hypotheses[:4], 1):
            with columns[(index - 1) % 2].container(border=True):
                st.markdown(f"<div style='font-size:18px;font-weight:750;color:#b9a1ff;line-height:1.4;margin-bottom:.7rem'>{index}. {rich(item.get('title', 'Market hypothesis'))}</div>", unsafe_allow_html=True)
                for key, label in (("supporting_evidence", "Evidence and cross-market read"),
                                   ("competing_explanation", "Other plausible explanations"),
                                   ("confirm_or_invalidate", "What would distinguish them"),
                                   ("sector_implications", "Forward implications")):
                    if item.get(key):
                        st.markdown(f"<div style='font-size:14px;font-weight:750;color:#b9c5d6;margin:.65rem 0 .2rem'>{label}</div>", unsafe_allow_html=True)
                        paragraph(item[key], size=15)
                horizon = item.get("review_horizon") or "Next 5–20 sessions where the report specifies conditions"
                st.caption(f"Review horizon · {horizon}")
                refs = _metric_ref_summaries(report_snapshot, item.get("metric_refs"), maximum=5)
                if refs:
                    with st.expander("Local evidence references", expanded=False):
                        for ref in refs:
                            st.write(ref)
    else:
        paragraph("No structured hypotheses were saved in this report version. The original response remains available below.", color="#aeb8c6")

    section_heading("4 · Risks and Supportive Developments", "◇")
    risk_col, support_col = st.columns(2, gap="medium")
    for column, title, key, color in (
        (risk_col, "Risks to the thesis", "risks", "#ef6b73"),
        (support_col, "Supportive developments", "supportive", "#55c7a2"),
    ):
        with column.container(border=True):
            st.markdown(f"<span style='font-weight:750;color:{color}'>{title}</span>", unsafe_allow_html=True)
            entries = briefing.get(key) or []
            if entries:
                for entry in entries[:3]:
                    paragraph("• " + str(entry), color="#f1a0a4" if key == "risks" else "#75d4b5", size=15)
            else:
                st.caption("No separate material item in this saved report.")

    sector_implications = briefing.get("sector_implications") or []
    if sector_implications:
        section_heading("5 · Sector and Cross-Asset Implications", "⇄")
        for entry in sector_implications[:4]:
            paragraph("• " + str(entry), size=15)
    catalysts = briefing.get("catalysts") or []
    if sector_implications:
        pass
    monitor = list(dict.fromkeys(entry for entry in (briefing.get("conditions") or [])
                                 if isinstance(entry, str) and entry.strip()))
    if transition.get("confirmation_invalidation"):
        monitor.append(transition["confirmation_invalidation"])
    watch = list(dict.fromkeys([*catalysts, *monitor]))
    if watch:
        section_heading("6 · What to Watch Next", "◷")
        for entry in watch[:4]:
            paragraph("• " + str(entry), size=15)
    local_state = ((risk_appetite or {}).get("classification") or
                   ((((current_snapshot.get("sections") or {}).get("sector_rotation") or {}).get("regime_evidence") or {}).get("classification")) or
                   "Unavailable")
    st.caption(f"Latest local snapshot: {current_snapshot.get('as_of') or 'unavailable'} · Python-derived regime: {local_state}. AI prose is interpretation; it does not modify local calculations.")


def _render_full_market_evidence(payload: dict, sources: list[dict], report: dict,
                                 snapshot: dict, deterministic_transition: dict) -> None:
    briefing = payload.get("briefing") or {}
    for key, title in (("risks", "Risks"), ("supportive", "Supportive developments"),
        ("sector_implications", "Sector implications"), ("conflicting_evidence", "Conflicting evidence"),
        ("catalysts", "Catalysts"), ("conditions", "Conditions that would change the assessment")):
        values = briefing.get(key) or []
        if values:
            st.markdown(f"**{title}**")
            for value in values[:5]:
                st.markdown("- " + str(value))
    hypotheses = briefing.get("hypotheses") or []
    if hypotheses:
        st.markdown("**Main hypotheses and tests**")
        for item in hypotheses[:3]:
            implications = item.get("sector_implications") or "Not specified in the saved response."
            st.markdown(f"- **{item['title']}** · Supports: {item['supporting_evidence']} · Alternative: {item['competing_explanation']} · Test: {item['confirm_or_invalidate']} · Implications: {implications}")
    transition = briefing.get("transition_outlook")
    if transition:
        st.markdown("**Regime transition details**")
        st.markdown(f"- AI/Python alignment: {transition.get('python_alignment', 'inconclusive')}")
        st.markdown(f"- Interpretation: {transition.get('current_regime_interpretation', '')}")
        st.markdown(f"- Emerging direction: {transition.get('emerging_direction', '')}")
        if transition.get("python_alignment") == "disagrees":
            st.markdown(f"- AI disagreement explanation: {transition.get('disagreement_explanation', '')}")
        st.markdown(f"- Confirm / invalidate: {transition.get('confirmation_invalidation', '')}")
        st.markdown(f"- Main uncertainty: {transition.get('main_uncertainty', '')}")
        st.json(transition.get("turning_point_interpretations", []))
    st.markdown("**Local metric references**")
    st.json(briefing.get("metric_refs", {}))
    report_transition = (((snapshot.get("sections") or {}).get("sector_rotation") or {}).get("regime_transition") or {})
    if report_transition:
        st.markdown("**Python transition evidence and rules**")
        st.caption(f"State: {report_transition.get('current_regime', 'unavailable')} · {report_transition.get('transition_status', 'unavailable')} · as of {report_transition.get('as_of') or '—'} · {report_transition.get('methodology_version', 'unknown methodology')}")
        for key, label in (("supporting_evidence", "Supporting"), ("contradictory_evidence", "Contradictory"),
                           ("confirm_conditions", "Confirmation conditions"), ("invalidate_conditions", "Invalidation conditions")):
            rows = report_transition.get(key) or []
            if rows:
                st.markdown(f"*{label}* · " + "; ".join(str(row) for row in rows[:5]))
        st.markdown("*Transition thresholds and feature timestamps*")
        st.json({"rules": (report_transition.get("features") or {}).get("rules"),
                 "turning_point_rules": (report_transition.get("features") or {}).get("turning_point_rules"),
                 "data_coverage": report_transition.get("data_coverage"),
                 "source_fingerprint": report_transition.get("source_fingerprint")})
    feature_export = report_transition.get("feature_export")
    if feature_export:
        st.markdown("**Feature-only transition export**")
        st.json(feature_export)
    elif deterministic_transition.get("feature_export"):
        st.markdown("**Current feature-only transition export**")
        st.json(deterministic_transition["feature_export"])
    st.markdown("**Full saved research response and source records**")
    st.caption(f"Saved {report.get('generated_at', 'date unavailable')} · requested {((report.get('usage') or {}).get('requested_model') or report.get('model') or 'unknown')} · actual {((report.get('usage') or {}).get('actual_model') or 'not reported')} · {len(sources)} sources")
    st.code(report.get("report_text", ""), language="json")
    for index, source in enumerate(sources, 1):
        url = source.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            st.markdown(f"[S{index} · {source.get('title') or url}]({url}) · {source.get('date') or 'date unavailable'}")
            if source.get("summary") or source.get("snippet"):
                st.caption(source.get("summary") or source.get("snippet"))


MARKET_DESK_CHAT_KEY = "market-intelligence-desk-conversation-v1"
DEFAULT_MARKET_AI_SPENDING_LIMITS = {"per_request_usd": 0.50, "daily_usd": 2.00}
MARKET_DESK_LAYOUT = {"column_ratio": (3, 1), "chat_history_height_px": 480, "responsive_stack": True}


def _render_market_wide_chat(snapshot: dict, structured: dict | None, report: dict | None, *, stale: bool) -> None:
    """Persistent market-wide Ask AI conversation with bounded, dated context."""
    provider = "DeepSeek"
    evidence_label = st.session_state.get("deepseek_market_analysis_mode", "Dashboard Analysis")
    chat_mode = "dashboard_analysis" if evidence_label == "Dashboard Analysis" else "dashboard_web"
    depth = st.session_state.get("deepseek_market_mode", "Deep Research")
    selected_model = st.session_state.get("market_ai_model", "deepseek-flash")
    chat_model = market_analysis_model(depth, selected_model)
    accent = provider_accent(provider.lower())
    st.markdown(f"<div style='border-left:4px solid {accent};padding:.1rem 0 .1rem .65rem;color:{accent};font-size:18px;font-weight:750'>Market Ask AI</div>", unsafe_allow_html=True)
    st.caption(f"{provider} · {chat_model}")
    context_meta = get_app_preference("market_intelligence_desk_context_v1", {}) or {}
    messages = list_market_section_chat("market_wide", MARKET_DESK_CHAT_KEY, limit=60)
    observations = [item for item in list_market_user_observations(limit=50)
                    if item.get("origin_conversation_id") == MARKET_DESK_CHAT_KEY]
    scenarios = list_market_scenarios(include_archived=False)
    current_report_id = (report or {}).get("id")
    with st.expander("Conversation context and settings", expanded=False):
        st.caption(f"Evidence mode · {evidence_label} · local snapshot {snapshot.get('as_of') or 'unavailable'}")
        if context_meta.get("snapshot_fingerprint") and context_meta.get("snapshot_fingerprint") != snapshot.get("fingerprint"):
            st.caption(f"This conversation began with snapshot {context_meta.get('snapshot_as_of') or 'date unavailable'}. Earlier messages are retained; new answers use the current snapshot.")
        if stale:
            st.caption("The saved briefing is older than current measurements. The report and local snapshot dates are passed separately in chat context.")
        if context_meta.get("report_id") and context_meta.get("report_id") != current_report_id and current_report_id:
            st.caption("A newer full-market briefing is available; previous messages remain unchanged.")
        saved_limits = get_app_preference("market_ai_spending_limits_v1", DEFAULT_MARKET_AI_SPENDING_LIMITS) or DEFAULT_MARKET_AI_SPENDING_LIMITS
        st.caption(f"Shared spend limits · ${float(saved_limits.get('per_request_usd', 0.50)):.2f}/request · ${float(saved_limits.get('daily_usd', 2.00)):.2f}/day")
        if st.button(f"Clear market-wide chat · {len(messages)} messages", key="market_desk_clear_chat", disabled=not bool(messages)):
            clear_market_section_chat("market_wide", MARKET_DESK_CHAT_KEY)
            st.session_state.pop("market_desk_chat_error", None)
            st.rerun()
        st.markdown("**Optional user-supplied observation · unverified**")
        observation_text = st.text_area("Observation", key="market_desk_observation_text", max_chars=2000,
                                         placeholder="For example: SMH is down 2% premarket.")
        observed_local = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None, second=0, microsecond=0)
        observed_date = st.date_input("Observed date · America/New_York", value=observed_local.date(), key="market_desk_observation_date")
        observed_time = st.time_input("Observed time · America/New_York", value=observed_local.time(), key="market_desk_observation_time")
        session_type = st.selectbox("Session", ("unknown", "premarket", "regular", "after_hours"), key="market_desk_observation_session")

    with st.container(height=MARKET_DESK_LAYOUT["chat_history_height_px"], border=True):
        if not messages:
            st.caption("Ask a question about the full market briefing to start this conversation.")
        for message in messages[-12:]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                model = message.get("model") or ("You" if message["role"] == "user" else provider)
                st.caption(f"{model} · {message.get('created_at', '')}")
                if message["role"] == "assistant":
                    for source in message.get("source_metadata", []):
                        if isinstance(source, dict) and str(source.get("url", "")).startswith("https://"):
                            st.markdown(f"[{source.get('title') or source['url']}]({source['url']}) · {source.get('date') or 'date unavailable'}")
                    if st.button("Save as Research Note", key=f"market_desk_note_{message['id']}"):
                        note = save_market_research_note(conversation_id=MARKET_DESK_CHAT_KEY,
                            message_id=int(message["id"]), note_text=message.get("content", ""),
                            snapshot_fingerprint=snapshot.get("fingerprint"),
                            source_metadata=message.get("source_metadata", []))
                        st.success(f"Research note saved · {note['created_at']}")

    question = st.text_area("Ask about the market", key="market_desk_question", height=68,
                             placeholder="Ask about a fresh development, an economic mechanism, or a conditional scenario…")
    limits = get_app_preference("market_ai_spending_limits_v1", DEFAULT_MARKET_AI_SPENDING_LIMITS) or DEFAULT_MARKET_AI_SPENDING_LIMITS
    request_cap = float(limits.get("per_request_usd", 0.50))
    daily_cap = float(limits.get("daily_usd", 2.00))
    preview_context = build_market_chat_context(snapshot, "market_wide", None, report, messages,
        question or "Question preview", user_observations=observations,
        scenario_notes=[{key: item.get(key) for key in ("title", "hypothesis", "status", "if_conditions", "then_interpretation", "indicators_to_monitor")} for item in scenarios])
    try:
        estimate = estimate_market_chat_cost(preview_context, search_mode=chat_mode, model=chat_model)
    except ValueError as exc:
        estimate = None
        st.caption(f"Ask AI unavailable: {exc}")
    daily_spend = deepseek_market_research_daily_spend()
    if estimate is not None:
        st.caption(f"Estimate ≤${estimate:.4f} · daily spend ${daily_spend:.4f}")
    disabled = []
    if not question.strip():
        disabled.append("Enter a question.")
    if not snapshot.get("fingerprint"):
        disabled.append("The local market snapshot is unavailable.")
    if not deepseek_chat_configured():
        disabled.append("DeepSeek is not configured.")
    if estimate is not None and estimate > request_cap:
        disabled.append(f"Estimated ceiling exceeds the ${request_cap:.2f} request cap.")
    if estimate is not None and daily_spend + estimate > daily_cap:
        disabled.append(f"Estimated ceiling exceeds remaining daily budget ${max(0, daily_cap-daily_spend):.4f}.")
    if st.button("Ask AI", key="market_desk_ask_ai", type="primary", disabled=bool(disabled)):
        request_id = uuid.uuid4().hex
        full_user_text = question.strip()
        if observation_text.strip():
            full_user_text += "\n\nUser-supplied observation (unverified): " + observation_text.strip()
        user_id = save_market_section_chat_message(section_id="market_wide", snapshot_fingerprint=MARKET_DESK_CHAT_KEY,
            report_id=(report or {}).get("id"), request_id=request_id, role="user", content=full_user_text)
        try:
            observation = None
            if observation_text.strip():
                observation = validate_user_observation(
                    observation_text=observation_text,
                    observed_at=datetime.combine(observed_date, observed_time),
                    session_type=session_type,
                    symbols=extract_market_symbols(observation_text,
                        (*BROAD_EQUITY_INDEX_SYMBOLS, *SECTOR_SYMBOLS.keys(),
                         *CREDIT_PROXY_SYMBOLS, *MACRO_PROXY_SYMBOLS)),
                )
                save_market_user_observation(observation=observation,
                    origin_conversation_id=MARKET_DESK_CHAT_KEY, origin_message_id=user_id)
            context_history = [*messages, {"role": "user", "content": full_user_text}]
            context = build_market_chat_context(snapshot, "market_wide", None, report, context_history,
                question.strip(), user_observations=([*observations, observation] if observation else observations),
                scenario_notes=[{key: item.get(key) for key in ("title", "hypothesis", "status", "if_conditions", "then_interpretation", "indicators_to_monitor")} for item in scenarios])
            answer = answer_market_section_question(context, search_mode=chat_mode,
                request_limit_usd=request_cap, daily_limit_usd=daily_cap, model=chat_model)
            save_market_section_chat_message(section_id="market_wide", snapshot_fingerprint=MARKET_DESK_CHAT_KEY,
                report_id=(report or {}).get("id"), request_id=request_id, role="assistant", content=answer["text"],
                model=answer.get("model", chat_model), source_metadata=answer.get("sources", []),
                input_tokens=answer.get("input_tokens"), output_tokens=answer.get("output_tokens"),
                cost_usd=answer.get("actual_cost_usd"), cost_kind=answer.get("cost_kind", "upper-bound estimate"))
            save_app_preference("market_intelligence_desk_context_v1", {
                "snapshot_fingerprint": snapshot.get("fingerprint"), "snapshot_as_of": snapshot.get("as_of"),
                "report_id": (report or {}).get("id"), "updated_at": datetime.now(timezone.utc).isoformat()})
            st.session_state.pop("market_desk_chat_error", None)
            st.rerun()
        except Exception as exc:
            logger.error("Market-wide Ask AI failed run_id=%s exception_type=%s", request_id[:12], type(exc).__name__)
            st.session_state["market_desk_chat_error"] = f"Ask AI failed ({type(exc).__name__}): {str(exc)[:300]}"
    if disabled:
        st.caption("Ask AI unavailable: " + " ".join(disabled))
    if st.session_state.get("market_desk_chat_error"):
        st.error(st.session_state["market_desk_chat_error"])
    with st.expander("Saved research notes and prior scenario records", expanded=False):
        notes = [item for item in list_market_research_notes(limit=50)
                 if item.get("conversation_id") == MARKET_DESK_CHAT_KEY]
        if notes:
            for note in notes:
                st.markdown(f"**Saved note · {note.get('created_at', 'date unavailable')}**")
                st.write(note.get("note_text", ""))
        else:
            st.caption("No research notes saved from this conversation.")
        old_scenarios = list_market_scenarios(include_archived=True)
        if old_scenarios:
            st.markdown("**Legacy scenario records · preserved**")
            for item in old_scenarios[:8]:
                with st.expander(f"{item.get('title')} · {item.get('status')} · updated {item.get('updated_at')}", expanded=False):
                    st.write(item.get("hypothesis", ""))
                    st.caption(f"IF {item.get('if_conditions', '—')} · THEN {item.get('then_interpretation', '—')}")
                    for revision in list_market_scenario_revisions(item["scenario_id"]):
                        st.caption(f"Revision {revision['revision_number']} · {revision['created_at']}")


PREMARKET_CHAT_KEY = "market-premarket-what-if-v1"


def _premarket_path_payload(text: str) -> dict | None:
    try:
        return parse_premarket_what_if(text)
    except ValueError:
        return None


def _render_premarket_answer(text: str) -> bool:
    """Render validated conditional paths; preserve legacy/non-JSON answers verbatim."""
    result = _premarket_path_payload(text)
    if not result:
        st.markdown(text)
        return False
    st.markdown(f"**What this setup may mean**\n\n{result['setup_meaning']}")
    paths = result["paths"]
    columns = st.columns(2, gap="medium")
    for index, item in enumerate(paths):
        with columns[index % 2].container(border=True):
            st.markdown(f"### {index + 1}. {item['name']}")
            st.markdown(f"**IF** {item['if_condition']}\n\n**THEN** {item['then_interpretation']}")
            st.markdown(f"**Why this could happen**\n\n{item['why_it_might_happen']}")
            st.markdown(f"**Evidence to compare**\n\n{item['supporting_or_contradicting_evidence']}")
            st.markdown(f"**Monitor**\n\n{item['indicators_to_watch']}")
    st.markdown(f"**Most important uncertainty**\n\n{result['most_important_uncertainty']}")
    st.markdown("**During-session checks**")
    for item in result["session_watch"]:
        st.markdown(f"- {item}")
    return True


def _render_premarket_what_if(snapshot: dict, report: dict | None, *, stale: bool) -> None:
    """Explicit, persistent conditional research using the existing DeepSeek chat service."""
    st.markdown("##### ◇ Premarket What-If with AI")
    st.caption("Describe a setup in your own words. Your observations are saved as user-reported and unverified; they never update cached prices. No live premarket quote feed is assumed.")
    st.caption(f"Local completed-session snapshot · {snapshot.get('as_of') or 'unavailable'}" +
               (" · saved briefing is stale" if stale else ""))
    messages = list_market_section_chat("market_premarket", PREMARKET_CHAT_KEY, limit=60)
    for message in messages[-10:]:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                _render_premarket_answer(message.get("content", ""))
                for source in message.get("source_metadata", []):
                    if isinstance(source, dict) and str(source.get("url", "")).startswith("https://"):
                        st.markdown(f"[{source.get('title') or source['url']}]({source['url']}) · {source.get('date') or 'date unavailable'}")
                if st.button("Save as Research Note", key=f"premarket_note_{message['id']}"):
                    note = save_market_research_note(conversation_id=PREMARKET_CHAT_KEY,
                        message_id=int(message["id"]), note_text=message.get("content", ""),
                        snapshot_fingerprint=snapshot.get("fingerprint"),
                        source_metadata=message.get("source_metadata", []))
                    st.success(f"Research note saved · {note['created_at']}")
            else:
                st.markdown(message.get("content", ""))
            st.caption(f"{message.get('model') or ('You' if message['role'] == 'user' else 'DeepSeek')} · {message.get('created_at', '')}")

    user_text = st.text_area("Premarket or developing setup", key="premarket_what_if_text", height=100,
        placeholder="Example: SMH is down 2% premarket while most sectors are rising. QQQ lags SPY and VIX is lower.")
    evidence_label = st.session_state.get("deepseek_market_analysis_mode", "Dashboard Analysis")
    chat_mode = "dashboard_analysis" if evidence_label == "Dashboard Analysis" else "dashboard_web"
    depth = st.session_state.get("deepseek_market_mode", "Deep Research")
    selected_model = st.session_state.get("market_ai_model", "deepseek-flash")
    chat_model = market_analysis_model(depth, selected_model)
    limits = get_app_preference("market_ai_spending_limits_v1", DEFAULT_MARKET_AI_SPENDING_LIMITS) or DEFAULT_MARKET_AI_SPENDING_LIMITS
    request_cap, daily_cap = float(limits.get("per_request_usd", 0.50)), float(limits.get("daily_usd", 2.00))
    messages_for_context = [*messages, {"role": "user", "content": user_text.strip()}] if user_text.strip() else messages
    observations = [item for item in list_market_user_observations(limit=50)
                    if item.get("origin_conversation_id") == PREMARKET_CHAT_KEY]
    legacy_scenarios = list_market_scenarios(include_archived=False)
    saved_notes = list_market_research_notes(limit=20)
    research_context = [
        {"title": item.get("title"), "hypothesis": item.get("hypothesis"), "status": item.get("status"),
         "if_conditions": item.get("if_conditions"), "then_interpretation": item.get("then_interpretation"),
         "indicators_to_monitor": item.get("indicators_to_monitor", [])}
        for item in legacy_scenarios[:8]
    ] + [{"title": "Saved research note", "hypothesis": item.get("note_text", "")[:1200]}
         for item in saved_notes[:5]]
    context = build_market_chat_context(snapshot, "market_premarket", None, report,
        messages_for_context, user_text.strip() or "Premarket setup preview", user_observations=observations,
        scenario_notes=research_context)
    context["conversation_mode"] = "premarket_what_if"
    try:
        estimate = estimate_market_chat_cost(context, search_mode=chat_mode,
            output_tokens=PREMARKET_OUTPUT_TOKENS, model=chat_model)
        estimate_error = ""
    except ValueError as exc:
        estimate, estimate_error = None, str(exc)
    daily_spend = deepseek_market_research_daily_spend()
    if estimate is not None:
        st.caption(f"{evidence_label} · {chat_model} · estimated ceiling ${estimate:.4f} · shared daily spend ${daily_spend:.4f}")
    disabled = []
    if not user_text.strip(): disabled.append("Describe a setup first.")
    if not snapshot.get("fingerprint"): disabled.append("The local market snapshot is unavailable.")
    if not deepseek_chat_configured(): disabled.append("DeepSeek is not configured.")
    if estimate_error: disabled.append(estimate_error)
    if estimate is not None and estimate > request_cap: disabled.append(f"Estimated ceiling exceeds the ${request_cap:.2f} request cap.")
    if estimate is not None and daily_spend + estimate > daily_cap: disabled.append(f"Estimated ceiling exceeds remaining daily budget ${max(0, daily_cap-daily_spend):.4f}.")
    label = "Analyze Premarket Setup" if not messages else "Analyze Premarket Setup / Ask Follow-up"
    if st.button(label, key="premarket_analyze_setup", type="primary", disabled=bool(disabled)):
        request_id = uuid.uuid4().hex
        now_local = datetime.now(ZoneInfo("America/New_York")).replace(second=0, microsecond=0)
        lowered = user_text.lower()
        session = "premarket" if "premarket" in lowered or "pre-market" in lowered else "after_hours" if "after-hours" in lowered or "after hours" in lowered else "regular" if "during the session" in lowered or "at the open" in lowered else "unknown"
        user_id = save_market_section_chat_message(section_id="market_premarket", snapshot_fingerprint=PREMARKET_CHAT_KEY,
            report_id=(report or {}).get("id"), request_id=request_id, role="user", content=user_text.strip())
        try:
            observation = validate_user_observation(observation_text=user_text.strip(), observed_at=now_local,
                session_type=session, symbols=extract_market_symbols(user_text,
                    (*BROAD_EQUITY_INDEX_SYMBOLS, *SECTOR_SYMBOLS.keys(), *CREDIT_PROXY_SYMBOLS, *MACRO_PROXY_SYMBOLS)))
            save_market_user_observation(observation=observation, origin_conversation_id=PREMARKET_CHAT_KEY,
                origin_message_id=user_id)
            history = [*messages, {"role": "user", "content": user_text.strip()}]
            context = build_market_chat_context(snapshot, "market_premarket", None, report, history,
                user_text.strip(), user_observations=[*observations, observation],
                scenario_notes=research_context)
            context["conversation_mode"] = "premarket_what_if"
            answer = answer_market_section_question(context, search_mode=chat_mode,
                request_limit_usd=request_cap, daily_limit_usd=daily_cap,
                output_tokens=PREMARKET_OUTPUT_TOKENS, model=chat_model)
            save_market_section_chat_message(section_id="market_premarket", snapshot_fingerprint=PREMARKET_CHAT_KEY,
                report_id=(report or {}).get("id"), request_id=request_id, role="assistant", content=answer["text"],
                model=answer.get("model", chat_model), source_metadata=answer.get("sources", []),
                input_tokens=answer.get("input_tokens"), output_tokens=answer.get("output_tokens"),
                cost_usd=answer.get("actual_cost_usd"), cost_kind=answer.get("cost_kind", "upper-bound estimate"))
            st.session_state.pop("premarket_chat_error", None)
            st.rerun()
        except Exception as exc:
            logger.error("Premarket What-If failed run_id=%s exception_type=%s", request_id[:12], type(exc).__name__)
            st.session_state["premarket_chat_error"] = f"Premarket analysis failed ({type(exc).__name__}): {str(exc)[:300]}"
    if disabled:
        st.caption("Analysis unavailable: " + " ".join(disabled))
    if st.session_state.get("premarket_chat_error"):
        st.error(st.session_state["premarket_chat_error"])
    if st.button(f"Clear Premarket What-If history · {len(messages)} messages", key="premarket_clear_chat", disabled=not bool(messages)):
        clear_market_section_chat("market_premarket", PREMARKET_CHAT_KEY)
        st.session_state.pop("premarket_chat_error", None)
        st.rerun()


def render_saved_market_briefing(deterministic_transition=None, risk_appetite=None):
    """Render the single Market Intelligence Desk from saved research and local data."""
    _section_anchor("ai-market-briefing")
    st.markdown("#### AI Market Intelligence Desk")
    if st.session_state.get("deepseek_market_research_error"):
        st.error(st.session_state["deepseek_market_research_error"])
    report = st.session_state.get("_market_saved_report_for_page")
    current_snapshot = st.session_state.get("_market_snapshot_for_page") or {}
    current_sector = ((current_snapshot.get("sections") or {}).get("sector_rotation") or {})
    deterministic_transition = deterministic_transition or current_sector.get("regime_transition") or {}
    risk_appetite = risk_appetite or current_sector.get("risk_appetite") or {}
    valid_report = bool(report and report.get("research_status") in {"complete", "complete_cost_warning"}
                        and report.get("report_text"))
    stale = False
    structured = None
    report_snapshot = (report or {}).get("snapshot") or {}
    usage = (report or {}).get("usage") or {}
    if valid_report:
        stale = saved_report_is_stale(report, current_snapshot.get("fingerprint"), MARKET_ANALYST_PROMPT_VERSION)
        stale_reasons = []
        if report_snapshot.get("fingerprint") != current_snapshot.get("fingerprint"):
            if (report_snapshot.get("schema_version") != current_snapshot.get("schema_version") or
                    (report_snapshot.get("configurations") or {}) != (current_snapshot.get("configurations") or {})):
                stale_reasons.append("snapshot schema or calculation methodology changed")
            else:
                stale_reasons.append("underlying measured market observations changed")
        if usage.get("prompt_version") != MARKET_ANALYST_PROMPT_VERSION:
            stale_reasons.append("AI output contract changed")
        actual_model = usage.get("actual_model") or usage.get("requested_model") or report.get("model", "DeepSeek")
        st.caption(f"Saved {report.get('generated_at', 'date unavailable')} · snapshot {report_snapshot.get('as_of') or 'unavailable'} · {actual_model} · {usage.get('search_mode', 'saved')} · {len(report.get('sources', []))} sources · {'stale' if stale else 'current'}")
        if stale:
            st.info("This completed briefing is retained from " + str(report_snapshot.get("as_of") or "an earlier snapshot") +
                    ". The current deterministic observations below are separate. " +
                    ("Reason: " + "; ".join(stale_reasons) + "." if stale_reasons else ""))
            if st.button("Refresh Full Analysis", key="refresh_integrated_market_ai", help="Explicitly run the existing full-market analysis using current data and budget controls."):
                st.session_state["_market_force_refresh_requested"] = True
                st.rerun()
        try:
            structured = parse_market_analysis_json(report["report_text"], report_snapshot, report.get("sources", []))
        except ValueError as error:
            st.warning("The saved AI report cannot be rendered in the current structured view. Its original text remains preserved below.")
            st.caption(f"Saved report validation detail: {error}")
    briefing_column, chat_column = st.columns(MARKET_DESK_LAYOUT["column_ratio"], gap="large", vertical_alignment="top")
    with briefing_column:
        if structured:
            _render_integrated_market_outlook(structured, report.get("sources", []), report,
                current_snapshot, deterministic_transition, risk_appetite, stale=stale)
        else:
            _render_integrated_market_outlook(None, [], report, current_snapshot,
                                              deterministic_transition, risk_appetite)
            if report and not valid_report:
                st.caption("The last attempt did not complete. Any previous completed briefing remains available when present.")
    with chat_column:
        _render_market_wide_chat(current_snapshot, structured, report if valid_report else None, stale=stale)

    # Premarket analysis has its own persistent conversation and remains below
    # the briefing/chat row at the full available page width.
    _render_premarket_what_if(current_snapshot, report if valid_report else None, stale=stale)

    with st.expander("Full Research, Quantitative Evidence & Sources", expanded=False):
        if structured and valid_report:
            _render_full_market_evidence(structured, report.get("sources", []), report,
                report_snapshot, deterministic_transition)
            st.caption(f"Usage {usage.get('input_tokens', '—')} in / {usage.get('output_tokens', '—')} out · estimated ${usage.get('estimated_cost_usd', 0):.5f} · completion {usage.get('completion_status', report.get('research_status'))}")
        elif report and report.get("report_text"):
            st.code(report.get("report_text", ""), language="json")
        else:
            st.caption("No completed full-market research is saved yet.")
        evidence = (((current_snapshot.get("sections") or {}).get("sector_rotation") or {}).get("regime_evidence") or {})
        transition_now = deterministic_transition or {}
        with st.expander("Quantitative regime evidence, methods and feature export", expanded=False):
            _render_regime_evidence_monitor(evidence, current_snapshot,
                risk_appetite or {}, transition_now)


def _render_compact_briefing(parsed: dict, sources: list[dict]) -> None:
    """Render parsed saved text only; never issue a model call to reformat it."""
    import re
    sections = parsed["sections"]
    source_urls = {f"S{index}": source.get("url") for index, source in enumerate(sources, 1) if source.get("url", "").startswith("https://")}

    def render_section(column, key, *, icon, color):
        items = sections.get(key, [])[:3]
        if not items:
            return
        card = column.container(border=True)
        with card:
            st.markdown(f"<span style='color:{color};font-size:18px;font-weight:700'>{icon} {BRIEFING_LABELS[key]}</span>", unsafe_allow_html=True)
            for item in items:
                rendered = _briefing_item(item)
                rendered = re.sub(r"\[(S\d+)(,\s*[^\]]+)?\]", lambda match: f"[{match.group(1)}]({source_urls[match.group(1)]}){match.group(2) or ''}" if match.group(1) in source_urls else match.group(0), rendered)
                st.markdown(f"• {rendered}")

    render_section(st, "market_regime", icon="◈", color="#e5b45c")
    left, right = st.columns(2, gap="small")
    render_section(left, "risks", icon="●", color="#ef6b73")
    render_section(right, "supportive", icon="●", color="#55c7a2")
    left, right = st.columns(2, gap="small")
    render_section(left, "sector", icon="◆", color="#8ab4f8")
    render_section(right, "catalysts", icon="◷", color="#8ab4f8")
    left, right = st.columns(2, gap="small")
    render_section(left, "conflicts", icon="◇", color="#e5b45c")
    render_section(right, "conditions", icon="○", color="#aeb8c6")


BRIEFING_LABELS = {
    "market_regime": "Market regime / key takeaway", "risks": "Top 3 accumulating risks",
    "supportive": "Top 3 supportive developments", "sector": "Sector rotation implications",
    "catalysts": "Key market catalysts", "conflicts": "Conflicting evidence",
    "conditions": "Conditions that would change the assessment",
}


def _render_data_health(universe, histories, provider, basis, vix_state, treasury, calendar_rows, fred_macro, health, diagnostic):
    latest_etf = max((row.get("trading_date") for rows in histories.values() for row in rows if row.get("trading_date")), default=None)
    expected_session = (vix_state or {}).get("completed_session") or latest_completed_weekday()
    latest_treasury = treasury[-1].get("observation_date") if treasury else None
    latest_vix = vix_state.get("as_of")
    calendar_refreshed = max((row.get("last_verified_at") for row in calendar_rows if row.get("last_verified_at")), default=None)
    unavailable = []
    for name, value in (("ETF price history", latest_etf), ("VIX current close", latest_vix), ("Treasury yields", latest_treasury)):
        if not value:
            unavailable.append(name)
    statuses = [
        {"Source": "ETF price history", "Latest observation / verification": latest_etf or "Unavailable", "Provider": provider,
         "Status": ("Current" if latest_etf == expected_session else "Provider data not yet available" if latest_etf and latest_etf < expected_session else "Unavailable")},
        {"Source": "VIX", "Latest observation / verification": latest_vix or (vix_state.get("latest_candidate") or {}).get("observation_date", "Unavailable"), "Provider": vix_state.get("source") or "Current unavailable", "Status": "Current" if vix_state.get("current_available") else "Historical only / stale"},
        {"Source": "U.S. Treasury par yields", "Latest observation / verification": latest_treasury or "Unavailable", "Provider": "U.S. Treasury XML", "Status": "Available" if latest_treasury else "Unavailable"},
        {"Source": "FRED optional history", "Latest observation / verification": max((rows[-1]["observation_date"] for rows in fred_macro.values() if rows), default="Unavailable"), "Provider": "FRED", "Status": "Optional historical"},
        {"Source": "Economic calendar", "Latest observation / verification": calendar_refreshed or "Unavailable", "Provider": "Configured calendar adapters", "Status": "Cached events" if calendar_rows else "No upcoming cached events"},
    ]
    summary = f"Data health · {len(unavailable)} source(s) unavailable" if unavailable else "Data health · primary market sources available"
    with st.expander(summary, expanded=False):
        st.dataframe(statuses, hide_index=True, width="stretch")
        _render_fred_status(fred_macro, health, diagnostic)
        fred_vix = load_fred_vix()
        cboe_history = load_cboe_volatility().get("VIX", [])
        yahoo_history = list_volatility_observations(symbols=("VIX",), provider="yfinance_unofficial").get("VIX", [])
        st.dataframe([
            {"Series": "Cboe VIX", "Latest observation": cboe_history[-1]["observation_date"] if cboe_history else "—", "Last refresh": cboe_history[-1].get("retrieved_at", "—") if cboe_history else "—", "Status": "Historical cache"},
            {"Series": "Yahoo ^VIX fallback", "Latest observation": yahoo_history[-1]["observation_date"] if yahoo_history else "—", "Last refresh": yahoo_history[-1].get("retrieved_at", "—") if yahoo_history else "—", "Status": "Opt-in unofficial fallback"},
            {"Series": "FRED VIXCLS", "Latest observation": fred_vix[-1]["observation_date"] if fred_vix else "—", "Last refresh": fred_vix[-1].get("retrieved_at", "—") if fred_vix else "—", "Status": "Historical reference only"},
        ], hide_index=True, width="stretch")
        st.caption("Optional FRED series are refreshed from Data Sources & Diagnostics above. A failed refresh never removes saved observations.")
        st.caption("Cboe is the preferred published close. The optional Yahoo fallback is unofficial and manually enabled. FRED VIXCLS is shown as historical reference only. Provider failure does not remove cached values.")
        coverage = list_daily_price_coverage(universe, provider, basis)
        coverage_rows = []
        for symbol in universe:
            info = coverage.get(symbol, {})
            bars = histories.get(symbol, [])
            cached_date = info.get("latest_date")
            freshness = ("missing" if not cached_date else "current" if cached_date == expected_session
                         else "provider data not yet available" if cached_date < expected_session
                         else "observation ahead of expected session")
            coverage_rows.append({"Symbol": symbol, "Observations": str(info.get("observations") or "No data"),
                                  "Coverage start": info.get("first_date") or "—", "Latest date": info.get("latest_date") or "—",
                                  "Expected session": expected_session,
                                  "Provider": provider if bars else "Unavailable",
                                  "Adjustment": "Split + dividend adjusted" if bars else "—",
                                  "Status": freshness})
        st.dataframe(coverage_rows, hide_index=True, width="stretch")


def _sector_rotation_section(histories, timeframe=20, vix_rows=(), expected_session=None,
                             vix_current_available=None, risk_appetite=None,
                             regime_transition=None, basket_config=None):
    _section_anchor("sector-rotation-breadth")
    st.markdown("#### 3 · Sector Rotation & Breadth")
    horizon = st.radio("Sector return horizon", (5, 20, 60, 120), index=(5, 20, 60, 120).index(timeframe) if timeframe in (5, 20, 60, 120) else 1, horizontal=True,
                       format_func=lambda value: f"{value}D", key="monitor_sector_horizon")
    metrics = sector_participation_metrics(histories, timeframe=horizon)
    metrics["timeframe"] = horizon
    if metrics.get("status") != "available":
        st.info("Sector analysis is unavailable until cached SPY and standard-sector observations overlap.")
    else:
        cols = st.columns(4)
        outperformers = metrics["outperformers"].get(horizon, {})
        absolute = (metrics.get("absolute_breadth") or {}).get(horizon, {})
        rsp_spy = (metrics.get("rsp_spy_returns") or {}).get(horizon, {})
        cols[0].metric(f"Positive sector ETF returns · {horizon}D", f"{absolute.get('positive_count')}/{absolute.get('universe', 11)}" if absolute.get("positive_count") is not None else "—", help="Count of standard sector ETFs with a positive absolute cumulative return. This is an ETF participation proxy, not constituent-level breadth.")
        cols[1].metric(f"Sector ETFs outperforming SPY · {horizon}D", f"{outperformers.get('count')}/11" if outperformers.get("count") is not None else "—", help="Relative performance count. A sector can outperform SPY while still declining.")
        if rsp_spy.get("status") == "available":
            cols[2].metric(f"RSP / SPY absolute · {horizon}D", f"{rsp_spy['rsp_return']*100:+.2f}% / {rsp_spy['spy_return']*100:+.2f}%", delta=f"RSP−SPY {rsp_spy['excess_pp']:+.2f} pp", delta_color="off", help=f"Matched adjusted-close returns from {rsp_spy['start_date']} through {rsp_spy['as_of']}; excess is a relative-return spread, not absolute breadth.")
        else:
            cols[2].metric(f"RSP / SPY · {horizon}D", "Unavailable")
        cols[3].metric("Above 50D average", f"{metrics['above_sma50']['count']}/{metrics['above_sma50']['available']}" if metrics['above_sma50']['count'] is not None else "—")
        changes = metrics.get("participation_change", {})
        change_text = " · ".join(f"{w}D {changes.get(w, {}).get('positive_count_change'):+d}" for w in (5, 10, 20) if changes.get(w, {}).get("status") == "available")
        st.caption(f"Positive sector ETF count change versus the preceding equal-length window: {change_text or 'insufficient aligned history'} · above SMA200 {metrics['above_sma200']['count']}/{metrics['above_sma200']['available'] if metrics['above_sma200']['count'] is not None else 0} · absolute-return dispersion {metrics['dispersion_pp']:.2f} pp" if metrics.get("dispersion_pp") is not None else f"Positive sector ETF count change versus the preceding equal-length window: {change_text or 'insufficient aligned history'} · above SMA200 unavailable")

    risk_window = timeframe if timeframe in (5, 20, 60) else 20
    risk_appetite = risk_appetite or calculate_risk_appetite(histories, vix_rows, timeframe=risk_window,
        expected_session=expected_session, vix_current_available=vix_current_available,
        basket_config=basket_config)
    metrics["risk_appetite"] = risk_appetite
    if regime_transition is None:
        regime_transition = calculate_regime_transition(histories, vix_rows, timeframe=risk_window,
            expected_session=expected_session, basket_config=basket_config)
    metrics["regime_transition"] = regime_transition
    st.markdown("##### Risk Appetite Monitor")
    risk_label_colors = {"Risk-on": "#55c7a2", "Risk-off": "#ef6b73",
                         "Rotation / Mixed": "#e5b45c", "Insufficient Evidence": "#9aa4b2"}
    appetite_cols = st.columns(5, gap="small")
    appetite_label = str(risk_appetite.get("classification") or "Insufficient Evidence")
    appetite_cols[0].markdown(f"**Observed regime**  \n<span style='color:{risk_label_colors.get(appetite_label, '#9aa4b2')};font-weight:750'>{html.escape(appetite_label)}</span>", unsafe_allow_html=True)
    appetite_cols[0].caption(f"{risk_appetite.get('as_of') or 'Date unavailable'} · {risk_appetite.get('timeframe_sessions', risk_window)} sessions")
    component_labels = (("equity_participation", "Equity participation"),
                        ("defensive_rotation", "Defensive vs cyclical"),
                        ("credit_risk_appetite", "Credit proxies"),
                        ("volatility_pressure", "VIX pressure"))
    for column, (component_id, label) in zip(appetite_cols[1:], component_labels):
        item = (risk_appetite.get("components") or {}).get(component_id) or {}
        direction = item.get("direction")
        color = "#55c7a2" if direction == 1 else "#ef6b73" if direction == -1 else "#e5b45c" if direction == 0 else "#9aa4b2"
        column.markdown(f"**{label}**  \n<span style='color:{color};font-weight:700'>{html.escape(str(item.get('status') or 'Unavailable'))}</span>", unsafe_allow_html=True)
        value = item.get("evidence") or []
        column.caption((str(value[0]) + " · " if value else "") + f"{item.get('as_of') or 'Date unavailable'}")
    st.caption("Descriptive cross-check only. ETF proxy returns are not fund flows; VIX is implied volatility, not market direction or a recommendation.")
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("##### Sector Performance Heatmap")
        rows = sector_return_rows(histories, SECTOR_SYMBOLS, horizon)
        if rows:
            grid = sector_heatmap_grid(rows)
            heat = go.Figure(go.Heatmap(z=grid["values"], x=list(range(grid["columns"])), y=list(range(grid["height"])),
                text=grid["labels"], customdata=grid["hover"], texttemplate="%{text}", textfont={"size":14},
                colorscale="RdYlGn", zmid=0, zmin=-grid["color_bound"], zmax=grid["color_bound"],
                showscale=False, xgap=5, ygap=5, hoverongaps=False,
                hovertemplate="%{customdata[0]} · %{customdata[1]}<br>" + f"{horizon}D return: " + "%{customdata[2]:+.2f}%<br>As of: %{customdata[3]}<extra></extra>"))
            heat.update_layout(height=465, margin={"l":5,"r":5,"t":10,"b":10}, xaxis={"visible":False},
                               yaxis={"visible":False,"autorange":"reversed"})
            apply_plotly_theme(heat)
            st.plotly_chart(heat, width="stretch", config={"displayModeBar":False})
            latest = max((row.get("as_of") for row in rows if row.get("as_of")), default="unavailable")
            st.caption(f"All 11 standard sector ETFs · absolute return · {horizon} sessions · as of {latest}.")
        else:
            st.info(f"Heatmap needs {horizon + 1} common adjusted closes for all sectors.")

    with right:
        st.markdown("##### Relative Strength & Momentum Quadrant")
        strength_col, momentum_col, trails_col = st.columns([1, 1, 1.15])
        strength_window = strength_col.selectbox("Strength lookback", (20, 60, 120), index=0, format_func=lambda n: f"{n}D", key="rotation_strength_window")
        momentum_window = momentum_col.selectbox("Momentum lookback", (5, 20), index=0, format_func=lambda n: f"{n}D", key="rotation_momentum_window")
        trails = trails_col.checkbox("Trails", value=False, key="rotation_trails")
        rotation = sector_rotation_points(histories, SECTOR_SYMBOLS, strength_window=strength_window, momentum_window=momentum_window)
        if rotation:
            chart = go.Figure()
            if trails:
                for row in rotation:
                    chart.add_trace(go.Scatter(x=[p["relative_strength"]*100 for p in row["trail"]],
                        y=[p["relative_momentum"]*100 for p in row["trail"]], mode="lines",
                        line={"width":1,"color":"#68717d"}, opacity=.45, showlegend=False, hoverinfo="skip"))
            colors = {"Leading":"#35b779","Weakening":"#e3ad4f","Lagging":"#e05a67","Improving":"#57a6d8"}
            for quadrant in ("Leading","Weakening","Lagging","Improving"):
                points=[row for row in rotation if row["quadrant"]==quadrant]
                if points:
                    chart.add_trace(go.Scatter(x=[p["relative_strength"]*100 for p in points],
                        y=[p["relative_momentum"]*100 for p in points], mode="markers+text", name=quadrant,
                        text=[p["symbol"] for p in points], textposition="top center",
                        customdata=[[p["name"],p["trading_date"]] for p in points],
                        marker={"size":11,"color":colors[quadrant]},
                        hovertemplate="%{text} · %{customdata[0]}<br>As of %{customdata[1]}<br>Relative strength %{x:.2f}%<br>Relative momentum %{y:.2f} pp<extra></extra>"))
            chart.add_hline(y=0,line_dash="dot",line_color="#8b929d"); chart.add_vline(x=0,line_dash="dot",line_color="#8b929d")
            max_x=max([abs(r["relative_strength"]*100) for r in rotation]+[.25])*1.25
            max_y=max([abs(r["relative_momentum"]*100) for r in rotation]+[.25])*1.4
            chart.update_layout(height=465,margin={"l":20,"r":15,"t":25,"b":35},
                xaxis_title=f"Sector/SPY relative strength · {strength_window}D (%)",
                yaxis_title=f"Change in relative strength · {momentum_window}D (pp)",
                xaxis={"range":[-max_x,max_x],"zeroline":False,"showgrid":True,"gridcolor":"rgba(150,160,175,.14)"},
                yaxis={"range":[-max_y,max_y],"zeroline":False,"showgrid":True,"gridcolor":"rgba(150,160,175,.14)"},
                legend={"orientation":"h","y":1.05,"x":0})
            for x,y,label in ((-.92,.9,"Improving"),(.92,.9,"Leading"),(-.92,-.9,"Lagging"),(.92,-.9,"Weakening")):
                chart.add_annotation(x=max_x*x,y=max_y*y,text=label,showarrow=False,font={"color":PLOTLY_MUTED_COLOR,"size":12})
            apply_plotly_theme(chart); st.plotly_chart(chart,width="stretch",config={"displayModeBar":False})
            st.caption("Relative performance and momentum are calculated from matched sector/SPY adjusted closes; price divergence does not establish fund flows.")
        else:
            st.info(f"Rotation map needs {strength_window + momentum_window + 1} overlapping closes.")
        with st.expander("Rotation methodology & quadrant definitions"):
            st.caption(f"X = sector/SPY ratio return over {strength_window} sessions; Y = change in X over {momentum_window} sessions. Leading: positive and strengthening; Weakening: positive but losing momentum; Lagging: negative and weakening; Improving: negative but recovering. This is a return-based quadrant map, not a proprietary RRG or forecast.")
    st.caption("Leadership, participation and dispersion use the fixed standard 11-sector ETF universe. ETF performance is a sector proxy, not constituent-level breadth or direct flows.")
    return metrics

def _render_cyclical_defensive_panel(histories, horizon):
    st.markdown("##### Cyclical vs Defensive Basket")
    basket = _render_configurable_baskets(histories, horizon)
    st.caption(f"Global window · {horizon} sessions")
    if basket.get("status") == "available":
        fig = go.Figure()
        for name, values, color in (("Cyclical", basket["series"]["cyclical"], "#8ab4f8"), ("Defensive", basket["series"]["defensive"], "#e4b357")):
            fig.add_trace(go.Scatter(x=basket["dates"], y=values, mode="lines", name=name,
                                     line={"color": color, "width": 2.5},
                                     hovertemplate="%{x}<br>%{fullData.name}: %{y:+.2f}%<extra></extra>"))
        fig.add_hline(y=0, line_color="rgba(180,190,205,.3)", line_dash="dot")
        fig.update_layout(**_paired_market_chart_layout(f"Cumulative return (%) · {horizon}D"))
        apply_plotly_theme(fig)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        diff = basket["defensive_minus_cyclical_pp"]
        st.caption(f"Defensive minus cyclical {diff:+.2f} pp · as of {basket['as_of']}")
    else:
        st.caption("Basket comparison needs aligned cached closes for the selected components.")
    return basket


def _equal_weight_basket(histories, periods):
    sessions = session_calendar(histories)
    required = (*CYCLICAL_SYMBOLS, *DEFENSIVE_SYMBOLS)
    if len(sessions) < periods + 1:
        return None
    dates = sessions[-(periods + 1):]
    maps = {symbol: {str(row["trading_date"]): float(row["close"]) for row in histories.get(symbol, []) if row.get("close")}
            for symbol in required}
    if any(any(day not in maps[symbol] for day in dates) for symbol in required):
        return None
    output = {"dates": dates}
    for name, symbols in (("cyclical", CYCLICAL_SYMBOLS), ("defensive", DEFENSIVE_SYMBOLS)):
        output[name] = [sum((maps[symbol][day] / maps[symbol][dates[0]] - 1) * 100 for symbol in symbols) / len(symbols) for day in dates]
    return output


CYCLICAL_SYMBOLS = ("XLY", "XLI", "XLB", "XLE", "XLF", "XLK", "XLC", "XLRE")
DEFENSIVE_SYMBOLS = ("XLV", "XLP", "XLU")


def _render_vix_candlesticks(container, vix_rows, sessions, show_average):
    candle_data = vix_candlestick_data(vix_rows, sessions=sessions, show_average=show_average)
    if not candle_data["candles"]:
        container.caption("No validated OHLC candles in this window. Close-only dates are not given fabricated wicks.")
        return candle_data
    mini = go.Figure()
    candles = candle_data["candles"]
    mini.add_trace(go.Candlestick(x=[row["date"] for row in candles],
        open=[row["open"] for row in candles], high=[row["high"] for row in candles],
        low=[row["low"] for row in candles], close=[row["close"] for row in candles],
        name="VIX · index points", increasing={"line":{"color":"#ef6b73","width":1.4},"fillcolor":"#ef6b73"},
        decreasing={"line":{"color":"#55c7a2","width":1.4},"fillcolor":"#55c7a2"}, whiskerwidth=.6))
    average = candle_data["five_session_average"]
    if average:
        mini.add_trace(go.Scatter(x=[row["date"] for row in average], y=[row["value"] for row in average],
            name="5-session close average", mode="lines", line={"color":"#8ab4f8","width":2.2}))
    mini.update_layout(height=440, margin={"l":12,"r":12,"t":8,"b":8},
        xaxis_title=None, yaxis_title="VIX index points", xaxis_rangeslider_visible=False,
        hovermode="x unified", legend={"orientation":"h","y":1.12},
        xaxis={"type":"category", "showgrid":False}, yaxis={"gridcolor":"rgba(150,160,175,.12)"})
    apply_plotly_theme(mini)
    container.plotly_chart(mini, width="stretch", config={"displayModeBar":False})
    if candle_data["missing_ohlc_dates"]:
        container.caption(f"{len(candles)} validated OHLC candles; {len(candle_data['missing_ohlc_dates'])} close-only/incomplete sessions omitted. Green/red means VIX fell/rose, not equity direction.")
    return candle_data


def _render_vix_monitor(histories, vix_state):
    """Two-column VIX state-and-chart panel; Streamlit stacks columns on narrow screens."""
    _section_anchor("volatility-risk-monitor")
    st.markdown("##### Volatility Risk Monitor")
    rows = (vix_state or {}).get("history", [])
    expected = (vix_state or {}).get("completed_session") or latest_completed_weekday()
    from zoneinfo import ZoneInfo
    monitor = build_vix_risk_monitor(rows, completed_session=expected,
        today=datetime.now(ZoneInfo("America/New_York")).date())
    saved = get_app_preference("vix_absolute_regimes_v1", {"subdued_max": 15.0, "normal_max": 20.0, "elevated_max": 30.0})
    for key, value in (("vix_subdued_max", saved.get("subdued_max", 15.0)),
                       ("vix_normal_max", saved.get("normal_max", 20.0)),
                       ("vix_elevated_max", saved.get("elevated_max", 30.0))):
        if key not in st.session_state:
            st.session_state[key] = float(value)
    try:
        regime = absolute_vix_regime(monitor.get("close"),
            subdued_max=float(st.session_state["vix_subdued_max"]),
            normal_max=float(st.session_state["vix_normal_max"]),
            elevated_max=float(st.session_state["vix_elevated_max"]))
    except ValueError:
        regime = "Invalid threshold order"
    signals = build_vix_descriptive_signals(rows, histories, tuple(SECTOR_SYMBOLS.keys()))
    window = st.session_state.get("vix_cycle_window", 10)
    if window not in (5, 10, 15, 20):
        window = 10
    cycle = analyze_volatility_cycle(rows, window=window)
    five = monitor.get("five_session_ohlc", {})
    change5 = monitor.get("change_5_sessions")
    # The outer Market layout already reserves a separate 25% AI column. Stack
    # metrics and candles in the remaining content area to keep the chart wide.
    left, right = st.columns(VIX_SIGNAL_LAYOUT["column_ratio"], gap="large")
    with left:
        if monitor.get("current_available"):
            st.markdown(f'<div style="font-size:34px;font-weight:720;line-height:1.15;margin:5px 0 2px">{monitor["close"]:.2f} <span style="font-size:15px;font-weight:500;color:#aeb8c6">{VIX_SIGNAL_LAYOUT["primary_headline"]}</span></div>', unsafe_allow_html=True)
            st.caption(f"{monitor['observation_date']} · {monitor.get('source', 'Cboe')} · completed session")
        else:
            st.markdown("**Current VIX unavailable**")
            st.caption(f"Expected completed session {expected}; cached observations through {monitor.get('observation_date', 'unavailable')} remain historical context.")
        st.markdown(f"**{VIX_SIGNAL_LAYOUT['state_heading']}**")

        def metric_card(column, label, value, caption, *, delta=None, delta_color="off", help_text=None):
            with column.container(border=True):
                st.metric(label, value, delta=delta, delta_color=delta_color, help=help_text)
                st.caption(caption)

        percentile = monitor.get("percentile_1y")
        regime_col, percentile_col = st.columns(2, gap="small")
        metric_card(regime_col, "Absolute VIX regime", regime,
                    f"As of {monitor.get('observation_date', '—')} · point thresholds",
                    help_text="Descriptive VIX point thresholds only; this is not an equity-direction signal.")
        metric_card(percentile_col, "1-year historical percentile", f"{percentile:.0f}th" if percentile is not None else "Unavailable",
                    "Secondary historical ranking; not a probability.")
        z = signals.get("z_score", {})
        if z.get("value") is not None:
            empirical = z.get("empirical_percentile")
            normal_pct = z.get("normal_equivalent_percentile")
            empirical_text = f"empirical trailing-60 rank {empirical:.1f}th" if empirical is not None else "empirical rank unavailable"
            normal_text = f"normal-equivalent {normal_pct:.1f}th" if normal_pct is not None else "normal-equivalent unavailable"
            st.caption(f"60-session context · z {z['value']:+.2f} · {empirical_text} · {normal_text} (Gaussian reference, not a forecast)",
                       help=z.get("basis", "Trailing sample through the observation date; current close is included."))
        else:
            st.caption(f"60-session standardized rank unavailable ({z.get('status', 'insufficient history')}); the separate 1-year empirical percentile above uses its own historical window.")

        st.markdown("**Latest Session**")
        prev_col, surge_col = st.columns(2, gap="small")
        metric_card(prev_col, "Previous close", f"{monitor['prior_close']:.2f}" if monitor.get("prior_close") is not None else "Unavailable",
                    f"{monitor.get('prior_close_date') or 'Matched previous observation needed'} · VIX points")
        metric_card(surge_col, "Peak surge vs previous close", f"+{monitor['peak_surge_percent']:.1f}%" if monitor.get("peak_surge_percent") is not None else "No upward spike" if monitor.get("spike_status") == "no_upward_spike" else "Unavailable",
                    f"Session high {monitor.get('high'):.2f} vs prior close" if monitor.get("high") is not None else "Needs validated OHLC and prior close",
                    delta_color="inverse", help_text="(session high / previous close − 1) × 100; calculated only when high exceeds previous close.")
        unwind_col, net_col = st.columns(2, gap="small")
        metric_card(unwind_col, "Peak surge unwound by close", f"{monitor['peak_unwound_percent']:.1f}%" if monitor.get("peak_unwound_percent") is not None else "Unavailable",
                    f"Remaining vs prior close: {monitor.get('peak_retained_percent'):+.1f}%" if monitor.get("peak_retained_percent") is not None else monitor.get("spike_status", "Missing reference" ).replace("_", " ").title(),
                    help_text="Peak surge unwound = (high − close)/(high − previous close). Values above 100% mean the close fell below the prior close; this is a descriptive proxy, not risk eliminated.")
        daily = monitor.get("daily_change_percent")
        metric_card(net_col, "Net close-to-close change", f"{daily:+.1f}%" if daily is not None else "Unavailable",
                    f"{monitor.get('daily_change_points'):+.2f} VIX points" if monitor.get("daily_change_points") is not None else "Matched prior close unavailable",
                    delta_color="inverse", help_text="Close-to-close percentage change. An increase means higher implied volatility, not bullish equity direction.")

        if monitor.get("spike_status") == "partially_reversed":
            st.caption("Intraday stress interpretation: the VIX session peak partially reversed but still closed above the previous close.")
        elif monitor.get("spike_status") == "fully_reversed_below_prior":
            st.caption("Intraday stress interpretation: the spike fully reversed and the close fell below the previous close; this does not confirm an equity bottom.")
        elif monitor.get("spike_status") == "spike_persisted":
            st.caption("Intraday stress interpretation: the VIX closed at its session high; the upward spike persisted.")
        elif monitor.get("spike_status") == "no_upward_spike":
            st.caption("Intraday stress interpretation: no upward VIX spike above the previous close was observed.")

        with st.expander("Statistical signals & equity context", expanded=False):
            z = signals.get("z_score", {})
            var = signals.get("return_variability", {})
            rev = signals.get("reversal_frequency", {})
            interpretation = summarize_vix_statistical_signals(signals, monitor, cycle)
            with st.container(border=True):
                st.markdown(f"**{interpretation['headline']}** · as of {interpretation.get('as_of') or 'unavailable'}")
                st.markdown(f"**Historical context:** {interpretation['historical']}")
                st.markdown(f"**Variability:** {interpretation.get('variability', 'Unavailable')}")
                st.markdown(f"**Implication:** {interpretation['implication']}")
                st.markdown(f"**Equity confirmation:** {interpretation['confirmation']}")
                st.caption(f"Separate longer reference: 1-year empirical VIX percentile {monitor.get('percentile_1y'):.1f}th as of {monitor.get('observation_date')}" if monitor.get("percentile_1y") is not None else "Separate 1-year empirical VIX percentile unavailable.")
                st.caption("Z-score = (latest close − mean of the trailing sample) / population standard deviation. The rolling sample includes the as-of close and uses only observations through that date. Empirical percentile uses the same window and the midrank method: (observations below + half of ties) / sample size. A normal-equivalent percentile maps z through a standard normal CDF; VIX is skewed and heavy-tailed, so that theoretical rank can differ substantially from the empirical rank. Neither rank is a probability of a crash, recovery, or mean reversion.")
                st.caption(interpretation.get("methodology", ""))
                st.caption(interpretation.get("intraday", ""))
            if z.get("value") is None:
                st.caption(f"VIX z-score unavailable: {z.get('status', 'insufficient history')} ({z.get('observations', 0)} observations).")
            if var.get("status") == "available":
                pctl = var.get("historical_percentile")
                status = ("unusually turbulent" if var.get("unusually_turbulent") else
                          "within historical comparison range" if pctl is not None else
                          "not ranked due to short comparison history")
                rank = f"{pctl:.0f}th percentile of {var['prior_windows']} prior rolling windows" if pctl is not None else f"historical rank unavailable ({var['prior_windows']} prior windows; need at least 60)"
                st.caption(f"Recent variability · {var['window']}-return log-return standard deviation {var['daily_log_return_std_percent']:.2f}% per session; {rank} ({status}).")
            else:
                st.caption(f"Recent variability unavailable: {var.get('status')}; complete price history is required.")
            if rev.get("status") == "available":
                frac = rev.get("reversal_fraction")
                ratio = f"{frac:.0%} of {rev['eligible_spike_sessions']} qualifying spike sessions" if frac is not None else "no qualifying spikes"
                st.caption(f"Intraday reversals · {rev['significant_reversals']} / {rev['sessions']} sessions; {ratio}. A qualifying spike is high ≥10% above previous close, with ≥50% of the surge unwound. Large ranges ≥10% of prior close are counted separately: {rev['large_intraday_ranges']}.")
            else:
                st.caption(f"Intraday reversal frequency unavailable: {rev.get('status')}; incomplete OHLC is not imputed.")
            ret3 = monitor.get("change_3_sessions")
            accel = cycle.get("acceleration_pp")
            st.caption(f"Momentum · 3-session cumulative close change {ret3['percent']:+.1f}%" if ret3 else "Momentum · 3-session close change unavailable")
            st.caption(f"5-session acceleration vs preceding 5-session return: {accel:+.1f} percentage points · pressure {cycle.get('upward_momentum')} · rule-based cycle {cycle.get('state')}" if accel is not None else f"Momentum acceleration unavailable · cycle {cycle.get('state', 'unavailable')}")
            st.caption(f"Large VIX close moves in selected {window}-session window: +{cycle.get('large_up_moves', 0)} / −{cycle.get('large_down_moves', 0)}; broad-range reversals {cycle.get('daily_high_close_reversals', 0)}. No probability is estimated.")
            equity = signals.get("equity_confirmation", {})
            st.markdown("**Equity context · matched observation dates**")
            if equity.get("status") == "available":
                def trend(value):
                    return "above" if value is True else "below" if value is False else "unavailable vs"
                st.caption(f"As of {equity['as_of']} (VIX data through {signals['as_of']}): SPY is {trend(equity.get('spy_above_sma50'))} SMA50 and {trend(equity.get('spy_above_sma200'))} SMA200; 5-session SPY return {equity['spy_return_5d_percent']:+.1f}%" if equity.get("spy_return_5d_percent") is not None else f"SPY trend through {equity['as_of']}; 5-session return unavailable.")
                st.caption(f"Standard sector ETFs above SMA50: {equity['sector_above_sma50']}/{equity['sector_sma50_coverage']} covered; above SMA200: {equity['sector_above_sma200']}/{equity['sector_sma200_coverage']} covered. This is sector-ETF proxy data, not constituent breadth.")
                relative = equity.get("sector_relative_vs_spy_pp", {})
                if relative:
                    st.caption(f"5-session relative leadership on common dates: {sum(v > 0 for v in relative.values())}/{len(relative)} covered sectors outperformed SPY.")
                vix_rising = bool(change5 and change5["percent"] > 0)
                equity_weak = equity.get("spy_above_sma50") is False or (equity.get("sector_sma50_coverage", 0) >= 6 and equity.get("sector_above_sma50", 0) <= equity.get("sector_sma50_coverage", 0) / 2)
                if vix_rising and equity_weak:
                    st.info("Mixed evidence: VIX rose over five sessions while SPY trend or sector participation is weak.")
                elif vix_rising:
                    st.info("VIX rose over five sessions; available equity trend measures are not uniformly weak.")
                elif change5 and equity_weak:
                    st.info("VIX eased over five sessions while SPY trend or sector participation remains weak.")
                elif change5:
                    st.info("VIX eased over five sessions; available equity trend measures are not uniformly weak.")
            else:
                st.caption(f"SPY/sector comparison unavailable: {equity.get('reason', 'no matched local observations')}.")
            st.caption("Signals are descriptive observations only. A VIX reversal does not establish a market bottom or causation.")

    with right:
        st.markdown(f"**{VIX_SIGNAL_LAYOUT['chart_heading']}**")
        current_window = st.session_state.get("vix_cycle_window", window)
        if current_window not in (5, 10, 15, 20):
            current_window = 10
        chart_window = st.radio("Candlestick window", (5, 10, 15, 20), index=(5, 10, 15, 20).index(current_window), horizontal=True,
                                format_func=lambda n: f"{n} sessions", key="vix_cycle_window")
        average = st.checkbox("Show 5-session close average", value=True, key="vix_show_five_session_sma")
        selected_expansion = vix_selected_window_expansion(rows, sessions=chart_window)
        st.markdown(f"**Selected-window VIX high/low expansion · {chart_window} sessions**")
        if selected_expansion["status"] == "available":
            expansion_cols = st.columns(2, gap="small")
            expansion_cols[0].metric("High / low expansion", f"{selected_expansion['expansion_percent']:.1f}%")
            expansion_cols[0].caption(f"High {selected_expansion['highest_high']:.2f} · {selected_expansion['highest_high_date']}  /  low {selected_expansion['lowest_low']:.2f} · {selected_expansion['lowest_low_date']}")
            expansion_cols[1].metric("Selected window", f"{chart_window} sessions")
            expansion_cols[1].caption(f"{selected_expansion['start_date']} → {selected_expansion['end_date']}")
        else:
            st.info(f"Selected-window expansion unavailable: {selected_expansion['status'].replace('_', ' ')}. A complete set of {chart_window} valid OHLC observations is required.")
        _render_vix_candlesticks(right, rows, chart_window, average)
        st.caption("Candles use only validated Cboe OHLC. Green/red means VIX fell/rose, not equity direction.")

    with st.expander("VIX methodology, regime thresholds & source notes", expanded=False):
        a, b, c = st.columns(3)
        subdued = a.number_input("Subdued below", min_value=1.0, max_value=50.0, step=1.0, key="vix_subdued_max")
        normal = b.number_input("Normal below", min_value=1.0, max_value=60.0, step=1.0, key="vix_normal_max")
        elevated = c.number_input("Elevated below", min_value=1.0, max_value=80.0, step=1.0, key="vix_elevated_max")
        updated = {"subdued_max": subdued, "normal_max": normal, "elevated_max": elevated}
        if 0 < subdued < normal < elevated and updated != saved:
            save_app_preference("vix_absolute_regimes_v1", updated)
        st.caption("Default boundaries are 15, 20, and 30 VIX points. Labels are descriptive conventions, not a market direction model.")
        st.caption("Z-score: trailing 60 closes through the as-of date; population standard deviation; zero variance or incomplete window is unavailable. Variability: population standard deviation of 20 daily log returns, ranked versus up to 252 earlier rolling windows (current excluded). Reversal fraction: trailing 20 sessions, only highs ≥10% over prior close qualify; at least 50% of the peak surge must unwind. Large intraday ranges (high−low)/prior close ≥10% are tallied separately. SPY/sector series are cut off at or before the VIX as-of date.")
        st.caption("Potential future data: Cboe lists downloadable historical VVIX and VIX9D index series; SKEW has a Cboe index dashboard and may require separate history/licensing review. VIX versus realized volatility can be calculated from compatible dated equity-index closes, but horizons and instruments must be reconciled. VIX alone does not measure SPX put expensiveness. VIX futures term structure requires dated futures settlements; Cboe describes futures feeds as market-data products with licensing options, so this app assumes no freely reusable authorized futures history.")
        st.caption("VIX is an options-implied volatility measure, not a forecast of market direction. An intraday reversal is not evidence of a stock-market bottom.")


def _render_treasury_monitor_content(histories, treasury, vix_state, timeframe, macro=None):
    _section_anchor("rates-macro")
    st.markdown("#### 5 · Rates & Macro")
    rows = sorted(treasury or [], key=lambda row: row["observation_date"])
    freshness = treasury_freshness(rows, completed_session=max((r.get("trading_date") for r in histories.get("SPY", []) if r.get("trading_date")), default=None))
    st.markdown("##### Treasury Yield Monitor")
    window = st.radio("Treasury window", TREASURY_WINDOWS, index=1, horizontal=True,
                      format_func=lambda n: f"{n}D", key="treasury_monitor_window")
    summary = treasury_window_summary(rows, window)
    treasury_left, treasury_right = st.columns([1.05, 1.6], gap="medium")
    cards = treasury_left.columns(2)
    cards_second = treasury_left.columns(2)
    if not rows:
        for col, label in zip((*cards, *cards_second), ("2Y par yield", "10Y par yield", "10Y–2Y spread", "Curve change")):
            col.metric(label, "Unavailable")
        cards[0].caption("Use Refresh Treasury to retrieve the official daily par yield feed.")
    else:
        cards[0].metric("2Y par yield", f"{summary['two_year']:.2f}%", delta=f"{summary['two_year_change_bp']:+.0f} bp / {window}D" if summary['two_year_change_bp'] is not None else "Insufficient history", delta_color="off")
        cards[1].metric("10Y par yield", f"{summary['ten_year']:.2f}%", delta=f"{summary['ten_year_change_bp']:+.0f} bp / {window}D" if summary['ten_year_change_bp'] is not None else "Insufficient history", delta_color="off")
        cards_second[0].metric("10Y−2Y curve", f"{summary['spread_bp']:+.0f} bp", delta=f"{summary['spread_change_bp']:+.0f} bp / {window}D" if summary['spread_change_bp'] is not None else "Insufficient history", delta_color="off")
        cards_second[1].metric("Yield direction", f"2Y {summary['two_year_trend']} · 10Y {summary['ten_year_trend']}", delta=f"as of {summary['as_of']}", delta_color="off")
        st.caption(f"U.S. Treasury par yields · {freshness['status']} · {summary['observations']}/{summary['required_observations']} observations for {window}D · yield levels are percentage points; changes are basis points.")
        ex_cols = st.columns(2)
        ex_cols[0].caption(f"2Y window range: {summary['two_year_trough']:.2f}% low → {summary['two_year_peak']:.2f}% high · {summary['two_year_trend']}")
        ex_cols[1].caption(f"10Y window range: {summary['ten_year_trough']:.2f}% low → {summary['ten_year_peak']:.2f}% high · {summary['ten_year_trend']}")
        figure = go.Figure()
        chart_rows = rows[-(window + 1):]
        figure.add_trace(go.Scatter(x=[r["observation_date"] for r in chart_rows], y=[r["two_year"] for r in chart_rows], name="2Y", mode="lines+markers", line={"color":"#8ab4f8","width":2.4}, marker={"size":5}))
        figure.add_trace(go.Scatter(x=[r["observation_date"] for r in chart_rows], y=[r["ten_year"] for r in chart_rows], name="10Y", mode="lines+markers", line={"color":"#e6b566","width":2.4}, marker={"size":5}))
        figure.update_layout(height=245, hovermode="x unified", yaxis_title="Par yield (%)", margin={"l":8,"r":8,"t":8,"b":8}, legend={"orientation":"h","y":1.12})
        apply_plotly_theme(figure)
        treasury_right.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
        st.caption(f"Treasury publication freshness: {freshness['status']} · latest observation {freshness['observation_date']}. Par yields are not the effective fed funds rate or market-implied policy expectations.")
        with st.expander("Analyze Fed / CPI reaction · descriptive, not causal", expanded=False):
            official = [event for event in list_market_events()
                        if event.get("category") in {"Fed", "Macro"}
                        and event.get("source_verification") == "Official source"
                        and event.get("status") == "Confirmed"
                        and (event.get("event_date") or event.get("start_utc", "")[:10]) <= summary["as_of"]]
            options = {f"{e.get('event_date') or e['start_utc'][:10]} · {e['name']} (official)": e.get("event_date") or e["start_utc"][:10] for e in official}
            options["Enter historical date"] = "manual"
            selected = st.selectbox("Event", list(options), key="treasury_event_study_select")
            event_day = options[selected]
            if event_day == "manual":
                event_day = st.date_input("User-defined event date", key="treasury_user_event_date").isoformat()
                st.caption("User-entered date · unverified. Daily observations do not establish event causality.")
            study = treasury_event_study(rows, event_day)
            if study["status"] != "available":
                st.info("Cached yields do not cover observations on both sides of this date and the follow-up horizon.")
            else:
                immediate = study["immediate_change"]
                st.caption(f"Before {study['before_date']} → next observation {study['after_date']} · 2Y {immediate['two_year_bp']:+.0f} bp · 10Y {immediate['ten_year_bp']:+.0f} bp.")
                event_rows = []
                for horizon, label in (("forward_5", "5 sessions"), ("forward_10", "10 sessions")):
                    forward = study[horizon]
                    event_rows.append({"Horizon": label, "Observed": forward['observation_date'] if forward else "Incomplete",
                        "2Y change since first post-event observation (bp)": f"{forward['two_year_bp']:+.0f}" if forward else "—",
                        "10Y change since first post-event observation (bp)": f"{forward['ten_year_bp']:+.0f}" if forward else "—",
                        "Direction vs initial reaction": (f"2Y {study['persistence']['two_year_5' if horizon == 'forward_5' else 'two_year_10']} · "
                                                          f"10Y {study['persistence']['ten_year_5' if horizon == 'forward_5' else 'ten_year_10']}")})
                st.dataframe(event_rows, hide_index=True, width="stretch")
                st.caption("Daily yield movement after an event is temporal association only, not evidence the event caused the change.")
        with st.expander("Long Treasury history", expanded=False):
            long_figure = go.Figure()
            long_figure.add_trace(go.Scatter(x=[r["observation_date"] for r in rows], y=[r["two_year"] for r in rows], name="2Y", mode="lines"))
            long_figure.add_trace(go.Scatter(x=[r["observation_date"] for r in rows], y=[r["ten_year"] for r in rows], name="10Y", mode="lines"))
            long_figure.update_layout(height=360, hovermode="x unified", yaxis_title="Par yield (%)", margin={"l":8,"r":8,"t":8,"b":8})
            apply_plotly_theme(long_figure)
            st.plotly_chart(long_figure, width="stretch", config={"displayModeBar":False})

    st.markdown("##### Macro & commodity proxies")
    proxy_cols = st.columns(3)
    for col, symbol in zip(proxy_cols, ("TLT", "GLD", "USO")):
        result = _pair_return_summary(histories, symbol, "SPY", int(window))
        if result.get("status") == "available":
            col.metric(f"{symbol} · {window}D", f"{result['absolute_return_pct']:+.2f}%",
                       delta=f"{result['relative_return_pp']:+.2f} pp vs SPY", delta_color="off")
            col.caption(f"As of {result['as_of']} · Tiingo adjusted close" + (" · futures-based oil ETF proxy" if symbol == "USO" else " · ETF price proxy"))
        else:
            col.metric(f"{symbol} · {window}D", "Unavailable")
            col.caption(f"Needs {window + 1} common SPY dates")
    st.caption("TLT and GLD are adjusted ETF-price proxies. USO is a futures-based oil ETF proxy; fund return can differ from spot WTI because of futures rolls, expenses, and tracking effects. Actual WTI benchmark questions require dated external evidence.")


def _render_rates_credit(histories, treasury, vix_state, timeframe, macro, highlights, report, snapshot):
    _render_market_section("rates_macro", highlights, report, snapshot,
        lambda: _render_treasury_monitor_content(histories, treasury, vix_state, timeframe, macro))

def _render_regime_summary(metrics, histories, vix_state, timeframe):
    _section_anchor("market-regime-summary")
    st.markdown("#### 2 · Market Regime Summary")
    if metrics.get("status") != "available":
        st.info("Regime inputs are incomplete; no combined market interpretation is available.")
        _render_broad_index_comparison(histories)
        return
    ratios = metrics.get("ratio_returns", {})
    vix_rows = (vix_state or {}).get("history", [])
    cards = st.columns(7)
    cards[0].metric("Sector ETFs > 50D", _fraction_text(metrics["above_sma50"]))
    cards[1].metric("Sector ETFs > 200D", _fraction_text(metrics["above_sma200"]))
    out = metrics["outperformers"].get(timeframe, {})
    cards[2].metric(f"Outperform SPY · {timeframe}D", f"{out['count']}/11" if out.get("count") is not None else "—")
    cards[3].metric(f"RSP / SPY · {timeframe}D", _signed_percent(ratios.get("RSP/SPY")))
    cards[4].metric(f"SMH / SPY · {timeframe}D", _signed_percent(ratios.get("SMH/SPY")))
    cards[5].metric(f"XLY / XLP · {timeframe}D", _signed_percent(ratios.get("XLY/XLP")))
    defensive = metrics.get("defensive_vs_cyclical_pp")
    cards[6].metric(f"Defensive − cyclical · {timeframe}D", f"{defensive:+.2f} pp" if defensive is not None else "—",
                    delta="equal-weight basket spread" if defensive is not None else "unavailable", delta_color="off")

    _render_broad_index_comparison(histories)

    # A short rule-based read uses multiple independent observations and allows disagreement.
    above50 = metrics["above_sma50"].get("count")
    breadth_window = timeframe if timeframe in (5, 10, 20, 60) else 20
    breadth_read = classify_breadth_condition(metrics, window=breadth_window)
    defensive_outperforming = defensive is not None and defensive > 0
    st.markdown("**Rule-based local read · not AI generated**")
    st.markdown(f"**{breadth_read['status']}** — {breadth_read['explanation']} As of {breadth_read.get('as_of') or metrics['as_of']}; {breadth_read.get('positive_sectors') if breadth_read.get('positive_sectors') is not None else '—'}/11 sector ETFs positive, {breadth_read.get('outperforming_sectors') if breadth_read.get('outperforming_sectors') is not None else '—'}/11 outperforming SPY.")
    st.caption("Breadth counts here are sector-ETF participation proxies, not constituent-level advances/declines. Relative resilience, SMA participation and absolute gains are distinct measurements; ETF returns do not establish flows or causation.")
    st.caption("Rule guide: healthy broadening requires SPY and RSP positive, at least 7/11 sector ETFs positive, and the positive-sector count rising versus the prior equal-length window. Relative resilience requires both indexes negative with RSP less negative and at least 6/11 sectors beating SPY.")
    with st.expander("SPY trend, drawdown and historical context"):
        volatility = volatility_summary({"VIX": vix_rows}) if vix_state and vix_state.get("current_available") else {"status": "unavailable"}
        features = build_regime_features(histories, volatility,
            expected_session=(vix_state or {}).get("completed_session"))
        spy = features["spy"]
        trend = lambda value: "Above" if value is True else "Below" if value is False else "Unavailable"
        detail_cols = st.columns(5)
        detail_cols[0].metric("Rule-based regime", features["regime_classification"])
        detail_cols[1].metric("SPY vs SMA50", trend(spy["above_sma50"]))
        detail_cols[2].metric("SPY vs SMA200", trend(spy["above_sma200"]))
        detail_cols[3].metric("252-session drawdown", f"{spy['drawdown_252']:.2%}" if spy["drawdown_252"] is not None else "Insufficient history")
        detail_cols[4].metric("Drawdown percentile", f"{spy['drawdown_252_percentile']:.1f}th" if spy["drawdown_252_percentile"] is not None else "Insufficient history")
        detail_cols[0].caption(f"SPY data as of {features['as_of'] or 'unavailable'} · descriptive thresholds")
        if vix_state and vix_state.get("current_available"):
            analog = historical_analog_distribution(vix_rows, histories.get("SPY", []))
            if analog["status"] == "available":
                st.caption(f"Historical analog only: {analog['sample_size']} past VIX-percentile matches with fully observed {analog['horizon_sessions']}-session SPY outcomes. Median {_signed_percent(analog['median_return'])}; this is not a forecast.")
            else:
                st.caption(f"Historical VIX analog unavailable ({analog['status']}).")
        else:
            st.caption("Historical VIX analog is not presented as current because no fresh VIX close matches the latest completed SPY session.")


def _render_broad_index_comparison(histories):
    """Broad equity comparison; chart controls are view state, persisted locally."""
    st.markdown("##### Broad Equity Index Comparison")
    defaults = {"window": 20, "mode": "Normalized cumulative performance",
                "visible_symbols": list(BROAD_EQUITY_INDEX_SYMBOLS)}
    saved = get_app_preference("market_broad_index_view_v1", {}) or {}
    config = {**defaults, **{key: saved[key] for key in defaults if key in saved}}
    config["window"] = config["window"] if config["window"] in BROAD_EQUITY_WINDOWS else 20
    config["mode"] = config["mode"] if config["mode"] in {
        "Normalized cumulative performance", "Relative cumulative performance vs SPY"} else defaults["mode"]
    config["visible_symbols"] = [symbol for symbol in BROAD_EQUITY_INDEX_SYMBOLS
                                 if symbol in set(config["visible_symbols"])]
    if not config["visible_symbols"]:
        config["visible_symbols"] = list(BROAD_EQUITY_INDEX_SYMBOLS)
    for key, value in (("broad_index_window", config["window"]),
                       ("broad_index_mode", config["mode"]),
                       ("broad_index_visible", config["visible_symbols"])):
        if key not in st.session_state:
            st.session_state[key] = value
    controls = st.columns([.8, 1.5, 2.3])
    window = controls[0].selectbox("Window", BROAD_EQUITY_WINDOWS,
        format_func=lambda value: f"{value}D", key="broad_index_window")
    mode = controls[1].selectbox("Display", ("Normalized cumulative performance",
        "Relative cumulative performance vs SPY"), key="broad_index_mode")
    visible = controls[2].multiselect("Visible indexes", BROAD_EQUITY_INDEX_SYMBOLS,
        key="broad_index_visible", help="SPY remains in all benchmark calculations when hidden.")
    new_config = {"window": int(window), "mode": mode, "visible_symbols": list(visible)}
    if new_config != saved:
        save_app_preference("market_broad_index_view_v1", new_config)

    chart = broad_index_chart_data(histories, int(window), mode, visible)
    if chart.get("status") != "available":
        st.info(f"Broad index comparison unavailable: {chart.get('status', 'missing data').replace('_', ' ')}. "
                f"Requires {int(window) + 1} aligned adjusted-close observations; use the manual history refresh if an index is missing.")
    else:
        figure = go.Figure()
        palette = {"SPY":"#8ab4f8", "RSP":"#6cc5a1", "QQQ":"#e6b566", "DIA":"#bb9af7", "IWM":"#ef7c73"}
        if mode == "Normalized cumulative performance":
            for symbol, values in chart["series"].items():
                figure.add_trace(go.Scatter(x=chart["dates"], y=values, mode="lines", name=symbol,
                    line={"width":2.2,"color":palette[symbol]},
                    hovertemplate="%{x}<br>%{fullData.name}: %{y:+.2f}%<extra></extra>"))
            figure.add_hline(y=0, line_dash="dot", line_color="rgba(180,190,205,.45)")
            y_title = "Cumulative return (%)"
        else:
            for symbol, (dates, values) in chart["series"].items():
                figure.add_trace(go.Scatter(x=dates, y=values, mode="lines", name=symbol,
                    line={"width":2.2,"color":palette[symbol]},
                    hovertemplate="%{x}<br>%{fullData.name} excess vs SPY: %{y:+.2f} pp<extra></extra>"))
            figure.add_hline(y=0, line_dash="dot", line_color="rgba(180,190,205,.6)",
                             annotation_text="SPY benchmark", annotation_position="top left")
            y_title = "Cumulative excess return vs SPY (pp)"
        figure.update_layout(height=335, hovermode="x unified", margin={"l":12,"r":12,"t":22,"b":28},
            yaxis_title=y_title, legend={"orientation":"h","y":1.12,"x":0,"font":{"size":13}},
            xaxis={"showgrid":False}, yaxis={"gridcolor":"rgba(150,160,175,.12)"})
        apply_plotly_theme(figure)
        st.plotly_chart(figure, width="stretch", config={"displayModeBar":False})
        st.caption(f"{window} completed sessions · {chart['start_date']} to {chart['as_of']} · "
                   f"Tiingo split-and-dividend-adjusted daily closes · exact matched dates; no fill-forward.")
        with st.expander("Index observations and coverage", expanded=False):
            rows = []
            library = broad_index_comparison_library(histories, (int(window),))
            for symbol in BROAD_EQUITY_INDEX_SYMBOLS:
                record = library.get(f"{symbol}_SPY_{int(window)}D", {})
                rows.append({"Index":"SPY (cap weighted)" if symbol == "SPY" else
                             "RSP (equal weight)" if symbol == "RSP" else
                             "QQQ (Nasdaq-100)" if symbol == "QQQ" else
                             "DIA (price weighted)" if symbol == "DIA" else "IWM (small caps)",
                    "Return": f"{record['cumulative_return_pct']:+.2f}%" if record.get("status")=="available" else "—",
                    "Excess vs SPY": f"{record['excess_return_pp']:+.2f} pp" if record.get("status")=="available" else "—",
                    "Recent relative change": record.get("recent_change", "unavailable").replace("_", " "),
                    "As of": record.get("as_of", "—"),
                    "Coverage": f"{record.get('observations',0)}/{record.get('required_observations',int(window)+1)}"})
            st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("SPY is capitalization weighted; RSP equal weighted; QQQ tracks Nasdaq-100; DIA is price weighted; IWM represents smaller U.S. companies. ETF returns are price comparisons, not fund-flow evidence.")


def _fraction_text(row):
    return f"{row['count']}/{row['available']}" if row and row.get("count") is not None else "—"


def _signed_percent(value):
    return f"{value:+.2%}" if value is not None else "—"


def _snap_metric(value, unit, *, as_of=None, window=None, universe=None, definition=None, source=None, coverage=None):
    if value is None:
        return None
    return {"value": value, "unit": unit, "as_of": as_of, "window": window,
            "universe": universe, "definition": definition, "source": source,
            "coverage": coverage}


def _current_pair_result(histories):
    favorites = list_market_explorer_favorites()
    presets = [f"{first}/{second}" for first, second in (("XLY", "XLP"), ("XLY", "SPY"), ("RSP", "SPY"), ("SMH", "SPY"), ("SMH", "QQQ"))]
    selected = st.session_state.get("rs_pair_preset", "Custom")
    first, second = st.session_state.get("rs_custom_first", "SMH"), st.session_state.get("rs_custom_second", "SPY")
    mode = st.session_state.get("rs_custom_mode", "Cumulative excess return")
    window = st.session_state.get("rs_pair_window", 20)
    if selected in presets:
        first, second, mode = selected.split("/")[0], selected.split("/")[1], "Cumulative excess return"
    elif selected.startswith("Saved · "):
        labels = [f"Saved · {item['first']}/{item['second']} · {item['window']}D" for item in favorites]
        if selected in labels:
            favorite = favorites[labels.index(selected)]
            first, second, window = favorite["first"], favorite["second"], favorite["window"]
            mode = {"excess": "Cumulative excess return", "relative_ratio": "Normalized relative-price ratio",
                    "two_line": "Two-line normalized performance"}.get(favorite.get("mode"), mode)
    mode_key = {"Cumulative excess return": "excess", "Normalized relative-price ratio": "relative_ratio",
                "Two-line normalized performance": "two_line"}.get(mode, "excess")
    try:
        result = compare_etfs(histories, first, second, int(window), mode=mode_key)
        return result, {"first": first, "second": second, "window": int(window), "mode": mode_key}
    except (ValueError, TypeError):
        return {"status": "unavailable"}, {"first": first, "second": second, "window": window, "mode": mode_key}


def _public_market_catalyst_rows(calendar_rows):
    """Keep market-wide events plus a fixed public earnings subset; never enumerate user holdings."""
    event_rows = []
    for event in calendar_rows or []:
        category = event.get("category")
        related = event.get("related_symbols") or []
        if isinstance(related, str):
            related = [part.strip().upper() for part in related.replace(";", ",").split(",") if part.strip()]
        related = {str(symbol).strip().upper() for symbol in related}
        public_earnings = category == "Earnings" and bool(related & PUBLIC_BENCHMARK_EARNINGS)
        if category not in {"Macro", "Fed", "Political/Other"} and not public_earnings:
            continue
        item = {key: event.get(key) for key in (
            "name", "category", "start_utc", "end_utc", "status", "provider", "source_url",
            "source_verification", "last_verified_at", "event_date", "earnings_session", "related_symbols"
        ) if event.get(key) is not None}
        manually_confirmed = (str(event.get("provider", "")).startswith("manual")
                              and event.get("status") == "Confirmed")
        source_verified = event.get("source_verification") == "Official source"
        item["user_confirmed"] = manually_confirmed
        item["source_verified"] = source_verified
        item["confirmation_state"] = ("User-confirmed" if manually_confirmed else
                                      "Source-verified" if source_verified else "Unconfirmed")
        event_rows.append(item)
    event_rows.sort(key=lambda row: (row.get("start_utc") or row.get("event_date") or ""))
    return event_rows[:12]


def _build_market_snapshot(histories, timeframe, macro, vix_state, treasury, calendar_rows):
    """Build a view-independent canonical snapshot from locally cached observations."""
    from dashboard.market_explorer import (BROAD_EQUITY_INDEX_SYMBOLS, BROAD_EQUITY_WINDOWS,
        COMPARISON_WINDOWS, STANDARD_COMPARISONS, broad_index_comparison_library,
        standard_comparison_library)
    analysis_window = 20
    metrics = sector_participation_metrics(histories, timeframe=analysis_window)
    basket_cfg = get_app_preference("market_rotation_baskets_v1", DEFAULT_BASKETS)
    comparison_library = standard_comparison_library(histories, COMPARISON_WINDOWS)
    broad_index_library = broad_index_comparison_library(histories, BROAD_EQUITY_WINDOWS)
    vix_rows = (vix_state or {}).get("history", [])
    completed = (vix_state or {}).get("completed_session") or latest_completed_weekday()
    risk_window = timeframe if timeframe in (5, 20, 60) else 20
    risk_appetite = calculate_risk_appetite(histories, vix_rows, timeframe=risk_window,
        expected_session=completed, vix_current_available=(vix_state or {}).get("current_available"),
        basket_config=basket_cfg)
    vix = build_vix_risk_monitor(vix_rows, completed_session=completed)
    vix_signals = build_vix_descriptive_signals(vix_rows, histories, tuple(SECTOR_SYMBOLS))
    vix_cycle = analyze_volatility_cycle(vix_rows, window=10)
    treasury_rows = sorted(treasury or [], key=lambda row: row.get("observation_date", ""))
    transition = calculate_regime_transition(histories, vix_rows, timeframe=risk_window,
        expected_session=completed, treasury_rows=treasury_rows, fred_rows=macro,
        basket_config=basket_cfg)
    if transition.get("as_of"):
        save_market_regime_transition_record(transition)
    treasury_summaries = {window: treasury_window_summary(treasury_rows, window) for window in (5, 20, 60, 120)} if treasury_rows else {}
    treasury_summary = treasury_summaries.get(20, {"status": "unavailable"})
    sector_map = sector_rotation_points(histories, SECTOR_SYMBOLS, strength_window=20, momentum_window=5)

    event_rows = _public_market_catalyst_rows(calendar_rows)
    catalyst_cache = load_recent_catalysts()
    event_asof = max((event.get("last_verified_at", "")[:10] for event in event_rows), default=None)
    catalyst_metrics = {"event_count": _snap_metric(len(event_rows), "events", as_of=event_asof,
        window="upcoming public schedule", source="SQLite calendar")}
    for index, event in enumerate(event_rows[:6], start=1):
        event_date = event.get("event_date") or event.get("start_utc")
        if event_date:
            event_key = f"event_{index:02d}_date"
            catalyst_metrics[event_key] = _snap_metric(
                event_date, "calendar date/time",
                window="upcoming", universe=f"{event.get('name')} · {event.get('category')}",
                definition="Date listed in the dashboard calendar; treat as a planning date, not a market outcome.",
                source="SQLite calendar")
    sections = {"catalysts": {
        "status": "available" if event_rows else "unavailable", "as_of": event_asof,
        "source": "SQLite public calendar events", "window": "upcoming cached events",
        "universe": "market-wide schedule", "coverage": {"events": len(event_rows)},
        "events": event_rows,
        "recent_developments": catalyst_cache.get("records", []),
        "recent_developments_status": catalyst_cache.get("status", "unavailable"),
        "recent_developments_refreshed_at": catalyst_cache.get("refreshed_at"),
        "metrics": catalyst_metrics,
        "gaps": [] if event_rows else ["No upcoming public market-wide catalysts in local calendar"],
    }}

    regime_metrics = {}
    broad_index_metrics = {}
    for comparison_id, row in broad_index_library.items():
        if row.get("status") != "available":
            continue
        symbol, _benchmark, window_text = comparison_id.split("_")
        window = int(window_text.removesuffix("D"))
        prefix = f"broad_{symbol.lower()}_{window}d"
        shared = {"as_of": row.get("as_of"), "window": f"{window} sessions",
                  "universe": f"{symbol}/SPY", "source": row.get("price_basis"),
                  "coverage": {"observations": row.get("observations"),
                               "required": row.get("required_observations")}}
        for suffix, value, unit, definition in (
            ("return", row.get("cumulative_return_pct"), "%", "matched-date cumulative adjusted-close return"),
            ("excess_vs_spy", row.get("excess_return_pp"), "pp", "index cumulative return minus SPY cumulative return"),
            ("prior_excess", row.get("previous_window_excess_pp"), "pp", "immediately preceding equal-window excess return"),
            ("relative_momentum_change", row.get("momentum_change_pp"), "pp", "current excess minus prior equal-window excess"),
        ):
            metric = _snap_metric(value, unit, definition=definition, **shared)
            broad_index_metrics[f"{prefix}_{suffix}"] = metric
            if metric:
                broad_index_metrics[f"{prefix}_{suffix}"]["direction"] = row.get("direction")
                broad_index_metrics[f"{prefix}_{suffix}"]["recent_change"] = row.get("recent_change")
    regime_metrics.update(broad_index_metrics)
    if metrics.get("status") == "available":
        asof = metrics.get("as_of")
        regime_metrics["breadth_condition"] = _snap_metric(
            classify_breadth_condition(metrics, window=20).get("status"), "descriptive sector-ETF participation state",
            as_of=asof, window="20 sessions plus preceding matched 20-session participation change",
            universe="SPY, RSP and fixed standard 11 sector ETFs",
            definition="Rule-based distinction between healthy broadening, relative resilience during a selloff, broadening selling pressure, narrow leadership recovery, mixed, or insufficient data; not constituent breadth or a forecast.",
            source="deterministic local calculation")
        for window, item in metrics.get("outperformers", {}).items():
            regime_metrics[f"sectors_outperforming_spy_{window}d"] = _snap_metric(
                item.get("count"), "sectors", as_of=asof, window=f"{window} sessions",
                universe="fixed standard 11 sectors", definition="sector ETF return greater than SPY return",
                source="Tiingo adjusted close", coverage=item.get("available"))
        for window, item in metrics.get("absolute_breadth", {}).items():
            regime_metrics[f"sectors_positive_absolute_{window}d"] = _snap_metric(
                item.get("positive_count"), "sector ETFs", as_of=item.get("as_of"),
                window=f"{window} sessions", universe="fixed standard 11 sector ETFs",
                definition="count of sector ETF cumulative adjusted-close returns greater than zero; ETF proxy, not constituent breadth",
                source="Tiingo adjusted close", coverage=item.get("available"))
        for window, item in metrics.get("rsp_spy_returns", {}).items():
            shared = {"as_of": item.get("as_of"), "window": f"{window} sessions",
                      "universe": "RSP and SPY, matched sector-calendar dates", "source": "Tiingo adjusted close",
                      "coverage": {"status": item.get("status"), "start_date": item.get("start_date")}}
            for suffix, value, unit, definition in (
                ("rsp_return", item.get("rsp_return"), "%", "RSP absolute cumulative adjusted-close return"),
                ("spy_return", item.get("spy_return"), "%", "SPY absolute cumulative adjusted-close return"),
                ("excess_pp", item.get("excess_pp"), "pp", "RSP absolute return minus SPY absolute return; relative only"),
            ):
                regime_metrics[f"rsp_spy_{window}d_{suffix}"] = _snap_metric(value * 100 if value is not None and unit == "%" else value,
                    unit, definition=definition, **shared)
        for window, item in metrics.get("participation_change", {}).items():
            shared = {"as_of": item.get("current_end_date"), "window": f"current vs prior {window}-session window",
                      "universe": "fixed standard 11 sector ETFs", "source": "Tiingo adjusted close",
                      "coverage": {"status": item.get("status"), "current_start": item.get("current_start_date"),
                                   "prior_start": item.get("prior_start_date"), "available_sectors": item.get("available_sectors")}}
            regime_metrics[f"positive_sector_participation_change_{window}d"] = _snap_metric(
                item.get("positive_count_change"), "sector ETFs", definition="change in count with positive absolute returns between matched equal-length windows", **shared)
            regime_metrics[f"relative_sector_outperformance_count_change_{window}d"] = _snap_metric(
                item.get("relative_count_change"), "sector ETFs", definition="change in count of sector ETFs outperforming SPY; relative breadth proxy, not absolute participation", **shared)
        for key, item in (("above_sma50", metrics.get("above_sma50")), ("above_sma200", metrics.get("above_sma200"))):
            if item:
                regime_metrics[key] = _snap_metric(item.get("count"), "sector ETFs", as_of=asof,
                    window=key.upper().replace("SMA", " sessions"), universe="fixed standard 11 sectors",
                    definition="sector ETF close above same-date simple moving average", source="Tiingo adjusted close",
                    coverage=item.get("available"))
        for pair, value in metrics.get("ratio_returns", {}).items():
            regime_metrics[f"relative_{pair.lower().replace('/', '_')}"] = _snap_metric(
                value * 100 if value is not None else None, "%", as_of=asof, window="20 sessions",
                universe=pair, definition="change in adjusted-close ratio", source="Tiingo adjusted close")
        for window, returns in metrics.get("sector_returns", {}).items():
            for symbol, value in returns.items():
                regime_metrics[f"absolute_return_{symbol}_{window}d"] = _snap_metric(
                    value * 100, "%", as_of=asof, window=f"{window} sessions", universe=symbol,
                    definition="cumulative absolute sector ETF return", source="Tiingo adjusted close")
    broad_available = any(row.get("status") == "available" for row in broad_index_library.values())
    regime_as_of = max([metrics.get("as_of") or "", *(row.get("as_of") or "" for row in broad_index_library.values())], default="") or None
    broad_available_count = sum(row.get("status") == "available" for row in broad_index_library.values())
    historical_context = {
        "seasonality": completed_month_seasonality(histories, as_of=regime_as_of),
        "same_month_relative_history": completed_month_relative_history(histories, as_of=regime_as_of),
        "vix_stress_history": summarize_vix_stress_history(vix_rows, as_of=vix.get("observation_date")),
        "scope": "Local historical descriptions only; no forecast, analog win rate, or causal inference.",
    }
    sections["regime"] = {"status": "available" if metrics.get("status") == "available" or broad_available else "unavailable", "as_of": regime_as_of,
        "source": "Tiingo adjusted daily closes", "window": "5/20/60/120 sessions; fixed 11-sector baseline",
        "universe": "SPY/RSP/QQQ/DIA/IWM; fixed 11-sector baseline", "coverage": {"sector_returns": len(metrics.get("sector_returns", {})), "sectors": 11,
        "broad_index_comparisons": len(broad_index_library), "broad_index_comparisons_available": broad_available_count,
        "broad_index_symbols": len(BROAD_EQUITY_INDEX_SYMBOLS), "broad_index_windows": list(BROAD_EQUITY_WINDOWS)},
        "metrics": regime_metrics, "broad_index_comparisons": broad_index_library,
        "breadth_interpretation": classify_breadth_condition(metrics, window=20),
        "historical_context": historical_context,
        "gaps": ([] if metrics.get("status") == "available" else ["Insufficient aligned SPY and standard-sector history"])
            + [f"{symbol} broad-index history unavailable or incomplete" for symbol in BROAD_EQUITY_INDEX_SYMBOLS
               if not any(row.get("status") == "available" for key, row in broad_index_library.items() if key.startswith(f"{symbol}_SPY_"))]}

    quadrants = {row["symbol"]: row for row in sector_map}
    sector_rotation_metrics = {}
    sector_returns = metrics.get("sector_returns", {})
    excess_by_window = metrics.get("outperformers", {})
    for window, returns in sector_returns.items():
        excess = excess_by_window.get(window, {}).get("excess_pp", {})
        for symbol, value in returns.items():
            sector_rotation_metrics[f"absolute_return_{symbol}_{window}d"] = _snap_metric(
                value * 100, "%", as_of=metrics.get("as_of"), window=f"{window} sessions", universe=symbol,
                definition="absolute adjusted-close return", source="Tiingo adjusted close")
            if symbol in excess:
                sector_rotation_metrics[f"relative_return_{symbol}_{window}d"] = _snap_metric(
                    excess[symbol], "pp", as_of=metrics.get("as_of"), window=f"{window} sessions",
                    universe=f"{symbol} minus SPY", definition="sector ETF return minus SPY return",
                    source="Tiingo adjusted close")
    for symbol, row in quadrants.items():
        sector_rotation_metrics[f"quadrant_{symbol}"] = _snap_metric(
            row["quadrant"], "quadrant", as_of=row.get("trading_date"), window="strength 20D; momentum change 5D",
            universe=symbol, definition="sector/SPY ratio return and change in its rolling return",
            source="Tiingo adjusted close")
        sector_rotation_metrics[f"relative_strength_{symbol}"] = _snap_metric(
            row["relative_strength"] * 100, "%", as_of=row.get("trading_date"), window="20 sessions",
            universe=f"{symbol}/SPY", definition="20-session ratio return", source="Tiingo adjusted close")
        sector_rotation_metrics[f"relative_momentum_{symbol}"] = _snap_metric(
            row["relative_momentum"] * 100, "pp", as_of=row.get("trading_date"), window="5-session change",
            universe=f"{symbol}/SPY", definition="change in relative strength", source="Tiingo adjusted close")
    sector_rotation_metrics["dispersion_pp"] = _snap_metric(metrics.get("dispersion_pp"), "pp",
        as_of=metrics.get("as_of"), window="20 sessions", universe="standard sector ETFs",
        definition="population standard deviation of absolute sector returns", source="Tiingo adjusted close")
    sector_rotation_metrics["above_sma50_count"] = _snap_metric((metrics.get("above_sma50") or {}).get("count"),
        "sector ETFs", as_of=metrics.get("as_of"), window="50 sessions", universe="standard 11 sectors",
        definition="sector closes above SMA50", source="Tiingo adjusted close")
    sector_rotation_metrics["above_sma200_count"] = _snap_metric((metrics.get("above_sma200") or {}).get("count"),
        "sector ETFs", as_of=metrics.get("as_of"), window="200 sessions", universe="standard 11 sectors",
        definition="sector closes above SMA200", source="Tiingo adjusted close")
    sector_rotation_metrics["risk_appetite_classification"] = _snap_metric(
        risk_appetite.get("classification"), "descriptive regime", as_of=risk_appetite.get("as_of"),
        window=f"{risk_window} sessions", universe="equity, sector, credit ETF proxies, VIX",
        definition=f"rule-based classification · {risk_appetite.get('methodology_version')}", source="local cached observations")
    for component_id, component in risk_appetite.get("components", {}).items():
        sector_rotation_metrics[f"risk_appetite_{component_id}_status"] = _snap_metric(
            component.get("status"), "descriptive signal", as_of=component.get("as_of"),
            window=f"{risk_window} sessions" if component_id != "volatility_pressure" else "latest and 5 sessions",
            universe=component_id, definition=component.get("explanation"), source="local cached observations")
        sector_rotation_metrics[f"risk_appetite_{component_id}_direction"] = _snap_metric(
            component.get("direction"), "appetite direction (-1/0/+1)", as_of=component.get("as_of"),
            window=f"{risk_window} sessions" if component_id != "volatility_pressure" else "latest and 5 sessions",
            universe=component_id, definition="+1 constructive, -1 defensive/stress, 0 mixed; not a score", source="local cached observations")
    for metric_key, metric_value, unit, definition in (
        ("risk_appetite_spy_return_pct", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("spy_return_pct"), "%", "SPY absolute cumulative adjusted-close return"),
        ("risk_appetite_rsp_return_pct", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("rsp_return_pct"), "%", "RSP absolute cumulative adjusted-close return"),
        ("risk_appetite_sectors_positive_absolute", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("sectors_positive_absolute"), "sector ETFs", "Number of standard sector ETFs with positive absolute return; not constituent breadth"),
        ("risk_appetite_rsp_excess_pp", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("rsp_excess_pp"), "pp", "RSP return minus SPY return on matched dates"),
        ("risk_appetite_iwm_excess_pp", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("iwm_excess_pp"), "pp", "IWM return minus SPY return on matched dates"),
        ("risk_appetite_cyclical_minus_defensive_pp", (risk_appetite.get("components", {}).get("defensive_rotation", {}).get("value") or {}).get("cyclical_minus_defensive_pp"), "pp", "Equal-weight cyclical basket return minus defensive basket return"),
        ("risk_appetite_hyg_minus_lqd_pp", (risk_appetite.get("components", {}).get("credit_risk_appetite", {}).get("value") or {}).get("hyg_minus_lqd_pp"), "pp", "Matched HYG return minus LQD return; ETF proxy, not a spread"),
        ("risk_appetite_hyg_minus_ief_pp", (risk_appetite.get("components", {}).get("credit_risk_appetite", {}).get("value") or {}).get("hyg_minus_ief_pp"), "pp", "Matched HYG return minus IEF return; ETF proxy"),
        ("risk_appetite_vix_5d_change_percent", (risk_appetite.get("components", {}).get("volatility_pressure", {}).get("value") or {}).get("five_session_change_percent"), "%", "VIX close-to-close cumulative change over 5 sessions"),
        ("risk_appetite_sectors_outperforming_spy", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("sectors_outperforming_spy"), "sector ETFs", "Number of standard sector ETFs outperforming SPY"),
        ("risk_appetite_sectors_above_sma50", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("sectors_above_sma50"), "sector ETFs", "Number of standard sector ETFs above SMA50"),
        ("risk_appetite_sectors_above_sma200", (risk_appetite.get("components", {}).get("equity_participation", {}).get("value") or {}).get("sectors_above_sma200"), "sector ETFs", "Number of standard sector ETFs above SMA200"),
    ):
        sector_rotation_metrics[metric_key] = _snap_metric(metric_value, unit,
            as_of=risk_appetite.get("as_of"), window=f"{risk_window} sessions",
            universe="matched local market data", definition=definition, source="local cached observations")
    transition_features = transition.get("features", {})
    transition_metric_values = (
        ("regime_transition_status", transition.get("transition_status"), "descriptive transition state", "Rule-based change in leadership, breadth, credit and volatility; not predictive."),
        ("regime_transition_emerging_direction", (transition.get("signal_strip") or {}).get("emerging_direction"), "descriptive direction", "Python-derived summary of measured leadership and momentum changes."),
        ("regime_transition_confirmation_status", (transition.get("signal_strip") or {}).get("confirmation_status"), "rule confirmation", "Confirmation status from deterministic persistence and cross-group evidence rules."),
        ("regime_transition_current_leadership", transition.get("current_leadership"), "leadership pattern", "Deterministic current relative sector/index leadership."),
        ("regime_transition_rsp_momentum_change_pp", (transition_features.get("pair_relative_performance", {}).get("RSP") or {}).get("relative_momentum_change_pp"), "pp", "Change in RSP minus SPY relative return over 5 sessions."),
        ("regime_transition_iwm_momentum_change_pp", (transition_features.get("pair_relative_performance", {}).get("IWM") or {}).get("relative_momentum_change_pp"), "pp", "Change in IWM minus SPY relative return over 5 sessions."),
        ("regime_transition_qqq_momentum_change_pp", (transition_features.get("pair_relative_performance", {}).get("QQQ") or {}).get("relative_momentum_change_pp"), "pp", "Change in QQQ minus SPY relative return over 5 sessions."),
        ("regime_transition_cyclical_defensive_momentum_change_pp", transition_features.get("cyclical_defensive_momentum_change_pp"), "pp", "Change in cyclical minus defensive basket return spread."),
        ("regime_transition_sector_breadth_change", transition_features.get("sector_breadth_change"), "sector ETFs", "Change in count of sector ETFs with positive absolute returns versus five sessions earlier."),
        ("regime_transition_sector_relative_outperformance_count_change", transition_features.get("sector_relative_outperformance_count_change"), "sector ETFs", "Change in count of sector ETFs outperforming SPY; relative measure, not absolute breadth."),
    )
    for metric_id, value, unit, definition in transition_metric_values:
        sector_rotation_metrics[metric_id] = _snap_metric(value, unit,
            as_of=transition.get("as_of"), window=f"{risk_window} sessions; {transition.get('transition_momentum_sessions', 5)}D transition momentum",
            universe="date-aligned local market data", definition=definition, source="deterministic local calculation")
    turning_watch = transition_features.get("turning_point_watch") or {}
    turning_signals = turning_watch.get("signals") or {}
    turning_metrics = []
    for signal_id in ("volatility_stabilization", "technology_mag7_recovery",
                      "broad_market_participation", "defensive_leadership", "credit_confirmation"):
        signal = turning_signals.get(signal_id) or {}
        turning_metrics.append((f"turning_point_{signal_id}_state", signal.get("state"), "descriptive current state",
            signal.get("headline") or signal.get("question") or "Rule-based turning-point watch state."))
    turning_metrics.extend((
        ("turning_point_vix_5d_change_pct", (turning_signals.get("volatility_stabilization") or {}).get("details", {}).get("five_session_change_percent"), "%", "Cumulative VIX close change over five sessions; VIX points are not percentages."),
        ("turning_point_technology_xlk_spy_5d_pp", ((transition_features.get("short_horizon_pair_relative_performance") or {}).get("XLK") or {}).get("relative_return_pp"), "pp", "XLK minus SPY cumulative return over matched five-session dates."),
        ("turning_point_technology_xlk_spy_window_pp", ((transition_features.get("pair_relative_performance") or {}).get("XLK") or {}).get("relative_return_pp"), "pp", "XLK minus SPY cumulative return over the transition leadership window."),
        ("turning_point_technology_mags_spy_5d_pp", ((transition_features.get("short_horizon_pair_relative_performance") or {}).get("MAGS") or {}).get("relative_return_pp"), "pp", "MAGS minus SPY cumulative return over matched five-session dates; MAGS is a mega-cap growth proxy."),
        ("turning_point_technology_mags_spy_window_pp", ((transition_features.get("pair_relative_performance") or {}).get("MAGS") or {}).get("relative_return_pp"), "pp", "MAGS minus SPY cumulative return over the transition leadership window."),
        ("turning_point_rsp_spy_5d_pp", ((transition_features.get("short_horizon_pair_relative_performance") or {}).get("RSP") or {}).get("relative_return_pp"), "pp", "RSP minus SPY cumulative return over matched five-session dates."),
        ("turning_point_iwm_spy_5d_pp", ((transition_features.get("short_horizon_pair_relative_performance") or {}).get("IWM") or {}).get("relative_return_pp"), "pp", "IWM minus SPY cumulative return over matched five-session dates."),
        ("turning_point_sector_breadth_count", (transition_features.get("sector_breadth_current") or {}).get("sectors_positive_absolute"), "sector ETFs", "Number of standard sector ETFs with positive absolute returns; ETF proxy, not constituent breadth."),
        ("turning_point_sector_relative_outperformance_count", (transition_features.get("sector_breadth_current") or {}).get("sectors_outperforming_spy"), "sector ETFs", "Number of standard sector ETFs outperforming SPY; relative return measure."),
        ("turning_point_sector_breadth_change", transition_features.get("sector_breadth_change"), "sector ETFs", "Change in count of sector ETFs with positive absolute returns versus five sessions earlier."),
        ("turning_point_cyclical_defensive_5d_pp", transition_features.get("five_session_cyclical_minus_defensive_pp"), "pp", "Configured cyclical basket return less defensive basket return over five sessions."),
        ("turning_point_cyclical_defensive_window_pp", transition_features.get("current_cyclical_minus_defensive_pp"), "pp", "Configured cyclical basket return less defensive basket return over the leadership window."),
        ("turning_point_credit_hyg_minus_lqd_pp", (risk_appetite.get("components", {}).get("credit_risk_appetite", {}).get("value") or {}).get("hyg_minus_lqd_pp"), "pp", "Matched HYG less LQD cumulative adjusted ETF return; credit-sensitive price proxy, not a spread."),
        ("turning_point_credit_hyg_minus_ief_pp", (risk_appetite.get("components", {}).get("credit_risk_appetite", {}).get("value") or {}).get("hyg_minus_ief_pp"), "pp", "Matched HYG less IEF cumulative adjusted ETF return; credit-sensitive price proxy, not a spread."),
    ))
    for metric_id, value, unit, definition in turning_metrics:
        if value is not None:
            signal_id = metric_id.removeprefix("turning_point_").split("_state")[0]
            signal_data = turning_signals.get(signal_id) or {}
            sector_rotation_metrics[metric_id] = _snap_metric(value, unit,
                as_of=signal_data.get("as_of") or turning_watch.get("as_of"),
                window=f"5D transition momentum / {risk_window}D leadership",
                universe="date-aligned local market observations", definition=definition,
                source="deterministic local calculation")
    sections["sector_rotation"] = {"status": "available" if sector_rotation_metrics else "unavailable",
        "as_of": metrics.get("as_of"), "source": "Tiingo adjusted closes; local ratio methodology",
        "window": "absolute/excess return 5/20/60/120D; rotation strength 20D, momentum change 5D",
        "universe": "all standard 11 S&P 500 sector ETFs", "coverage": {"sectors": len(SECTOR_SYMBOLS), "quadrants": len(quadrants)},
        "metrics": sector_rotation_metrics, "risk_appetite": risk_appetite,
        "regime_transition": transition,
        "turning_point_watch": turning_watch,
        "quadrants": {s: row.get("quadrant") for s, row in quadrants.items()},
        "gaps": [] if sector_rotation_metrics else ["Sector return and rotation data unavailable"]}

    relative_metrics, saved_comparisons = {}, {}
    for comparison_id, row in comparison_library.items():
        comparison_values = {}
        for field, unit, definition in (
            ("first_return_pct", "%", "first ETF cumulative adjusted return"),
            ("second_return_pct", "%", "second ETF cumulative adjusted return"),
            ("excess_pp", "pp", "first ETF return minus second ETF return"),
            ("previous_window_excess_pp", "pp", "prior equal-window excess return"),
            ("relative_momentum_change_pp", "pp", "current minus prior-window excess return"),
        ):
            value = row.get(field)
            metric_id = f"{comparison_id.lower()}_{field}"
            comparison_values[field] = _snap_metric(value, unit, as_of=row.get("as_of"),
                window=f"{row.get('window')} sessions", universe="/".join(row.get("symbols", [])),
                definition=definition, source=row.get("price_basis", "Tiingo adjusted close"))
            relative_metrics[metric_id] = comparison_values[field]
        saved_comparisons[comparison_id] = {**row, "metrics": comparison_values}
    for window in COMPARISON_WINDOWS:
        result = basket_performance(histories, basket_cfg, window)
        for name in ("cyclical", "defensive"):
            relative_metrics[f"basket_{name}_{window}d"] = _snap_metric(
                (result.get("returns_pct") or {}).get(name), "%", as_of=result.get("as_of"),
                window=f"{window} sessions", universe=basket_cfg.get(name),
                definition=f"configured weighted {name} basket return", source="Tiingo adjusted close")
        relative_metrics[f"basket_defensive_minus_cyclical_{window}d"] = _snap_metric(
            result.get("defensive_minus_cyclical_pp"), "pp", as_of=result.get("as_of"),
            window=f"{window} sessions", universe="configured cyclical and defensive baskets",
            definition="defensive minus cyclical return", source="Tiingo adjusted close")
    sections["relative_strength"] = {"status": "available" if saved_comparisons else "unavailable",
        "as_of": metrics.get("as_of"), "source": "Tiingo split-and-dividend-adjusted close",
        "window": "standard comparison library: 5/20/60/120/252 sessions",
        "universe": "standard sector ETFs and saved market comparison presets",
        "coverage": {"standard_comparisons": len(STANDARD_COMPARISONS), "comparison_windows": list(COMPARISON_WINDOWS)},
        "metrics": relative_metrics, "comparisons": saved_comparisons,
        "ai_comparison_ids": [key for key in saved_comparisons if key.endswith("_20D")],
        "basket_configuration": basket_cfg,
        "gaps": [] if saved_comparisons else ["No aligned observations for standard market comparisons"]}

    rates_metrics, macro_proxy_comparisons = {}, {}
    for window, summary in treasury_summaries.items():
        if summary.get("status") != "available":
            continue
        for key, unit in (("two_year", "%"), ("ten_year", "%"), ("spread_bp", "bp"),
                          ("two_year_change_bp", "bp"), ("ten_year_change_bp", "bp"), ("spread_change_bp", "bp")):
            rates_metrics[f"{key}_{window}d"] = _snap_metric(summary.get(key), unit, as_of=summary.get("as_of"),
                window=f"{window} sessions" if "change" in key else None, universe="U.S. Treasury par curve",
                definition="official par-yield curve; yield levels in percent, changes in basis points",
                source="U.S. Treasury XML")
    fred_context, fred_dates = {}, []
    for series_id, series_rows in (macro or {}).items():
        if not series_rows:
            continue
        latest = series_rows[-1]
        value, asof = latest.get("value"), latest.get("observation_date")
        if value is None:
            continue
        fred_dates.append(asof)
        spec = SERIES.get(series_id, {})
        fred_context[series_id] = _snap_metric(float(value), spec.get("units", "provider units"), as_of=asof,
            universe=series_id, definition=spec.get("name", "FRED observation"), source="FRED initial-release data")
    rates_metrics.update({f"fred_{key}": value for key, value in fred_context.items()})
    for symbol in MACRO_PROXY_SYMBOLS:
        proxy = {}
        for window in COMPARISON_WINDOWS:
            result = _pair_return_summary(histories, symbol, "SPY", window)
            proxy[f"{window}d"] = result
            for field in ("absolute_return_pct", "relative_return_pp", "previous_relative_return_pp", "momentum_change_pp"):
                rates_metrics[f"{symbol.lower()}_{field}_{window}d"] = _snap_metric(
                    result.get(field), "%" if field == "absolute_return_pct" else "pp",
                    as_of=result.get("as_of"), window=f"{window} sessions", universe=symbol if field == "absolute_return_pct" else f"{symbol}/SPY",
                    definition={"absolute_return_pct":"cumulative adjusted return", "relative_return_pp":"ETF return minus SPY return",
                                "previous_relative_return_pp":"prior equal-window relative return", "momentum_change_pp":"current less prior-window relative return"}[field],
                    source="Tiingo adjusted close")
        macro_proxy_comparisons[symbol] = proxy
    for first, second in (("HYG", "LQD"), ("HYG", "IEF")):
        result = _pair_return_summary(histories, first, second, 20)
        rates_metrics[f"{first.lower()}_{second.lower()}_ratio_return_20d"] = _snap_metric(
            result.get("relative_return_pp"), "pp", as_of=result.get("as_of"), window="20 sessions",
            universe=f"{first}/{second}", definition="ETF relative-price proxy, not a credit spread", source="Tiingo adjusted close")
    event_reactions = []
    official_events = [event for event in list_market_events() if event.get("category") in {"Macro", "Fed"}
                       and event.get("source_verification") == "Official source" and event.get("status") == "Confirmed"]
    for event in sorted(official_events, key=lambda e: (e.get("event_date") or e.get("start_utc", "")), reverse=True)[:5]:
        event_day = event.get("event_date") or event.get("start_utc", "")[:10]
        study = treasury_event_study(treasury_rows, event_day) if treasury_rows else {"status":"unavailable"}
        if study.get("status") == "available":
            event_reactions.append({"name":event.get("name"), "event_date":study["event_date"],
                "before_date":study["before_date"], "after_date":study["after_date"],
                "immediate_change_bp":study["immediate_change"], "forward_5":study.get("forward_5"),
                "forward_10":study.get("forward_10"), "persistence":study.get("persistence"),
                "interpretation_limit":"Temporal association only; not causal attribution."})
    proxy_dates = [r.get("as_of") for pairs in macro_proxy_comparisons.values() for r in pairs.values() if r.get("as_of")]
    rates_asof = max([treasury_summary.get("as_of") or "", *fred_dates, *proxy_dates], default="") or None
    sections["rates_macro"] = {"status": "available" if rates_metrics else "unavailable", "as_of": rates_asof,
        "source": "U.S. Treasury daily par yield XML, FRED optional series, Tiingo adjusted ETF closes",
        "window": "Treasury 5/20/60/120 sessions; macro ETF comparisons 5/20/60/120/252 sessions",
        "universe": "2Y/10Y curve; TLT/GLD/USO ETF proxies; HYG/LQD and HYG/IEF proxies",
        "coverage": {"treasury_observations": len(treasury_rows), "fred_series": len(fred_context), "macro_proxy_symbols": len(macro_proxy_comparisons)},
        "metrics": rates_metrics, "macro_proxy_comparisons": macro_proxy_comparisons, "event_reactions": event_reactions,
        "gaps": ([] if treasury_rows else ["Official Treasury observations unavailable"])
            + ([] if fred_context else ["Optional FRED policy-rate/credit history unavailable"]) }

    vix_metrics = {}
    five_summary = vix.get("five_session_ohlc", {})
    for key, value, unit, window, definition in (
        ("close", vix.get("close"), "VIX points", None, "daily close"),
        ("previous_close", vix.get("prior_close"), "VIX points", "1 session", "matched previous close"),
        ("session_open", vix.get("open"), "VIX points", "latest session", "observed OHLC"),
        ("session_high", vix.get("high"), "VIX points", "latest session", "observed OHLC"),
        ("session_low", vix.get("low"), "VIX points", "latest session", "observed OHLC"),
        ("daily_change_percent", vix.get("daily_change_percent"), "%", "1 session", "close-to-close change"),
        ("peak_surge_percent", vix.get("peak_surge_percent"), "%", "latest session", "session high relative to prior close"),
        ("peak_surge_unwound_percent", vix.get("peak_unwound_percent"), "%", "latest session", "peak surge proportion unwound by close"),
        ("peak_surge_retained_percent", vix.get("peak_retained_percent"), "%", "latest session", "peak surge retained at close"),
        ("change_3d_percent", (vix.get("change_3_sessions") or {}).get("percent"), "%", "3 sessions", "cumulative close change"),
        ("change_5d_percent", (vix.get("change_5_sessions") or {}).get("percent"), "%", "5 sessions", "cumulative close change"),
        ("percentile_1y", vix.get("percentile_1y"), "percentile", "252 sessions", "historical empirical rank"),
        ("five_session_low", five_summary.get("lowest_vix"), "VIX points", "5 sessions", "lowest observed daily low"),
        ("five_session_high", five_summary.get("highest_vix"), "VIX points", "5 sessions", "highest observed daily high"),
        ("five_session_range_expansion_percent", five_summary.get("range_expansion_percent"), "%", "5 sessions", "highest high / lowest low - 1"),
        ("five_session_qualifying_spikes", five_summary.get("qualifying_upward_spikes"), "sessions", "5 sessions", "high at least 10% above previous close"),
        ("five_session_qualifying_reversals", five_summary.get("qualifying_spike_reversals"), "sessions", "5 sessions", "qualified peak surge unwound at least 50%"),
        ("five_session_peak_surge_unwound_percent", (five_summary.get("peak_surge_unwound") or {}).get("peak_surge_unwound_percent"), "%", "5 sessions", "window maximum high versus pre-window close, ending latest close"),
        ("z_score_60d", (vix_signals.get("z_score") or {}).get("value"), "standard deviations", "60 closes", "trailing population-standardized close"),
        ("vix_return_variability", (vix_signals.get("return_variability") or {}).get("daily_log_return_std_percent"), "% log-return std/session", "20 returns", "rolling population std of daily log returns"),
    ):
        vix_metrics[key] = _snap_metric(value, unit, as_of=vix.get("observation_date"), window=window, universe="Cboe VIX", definition=definition, source=vix.get("source"))
    try:
        regime = absolute_vix_regime(vix.get("close")) if vix.get("close") is not None else None
    except ValueError:
        regime = None
    vix_metrics["absolute_regime"] = _snap_metric(regime, "descriptive regime", as_of=vix.get("observation_date"), window="absolute VIX point thresholds", universe="Cboe VIX", definition="user-configurable thresholds; not a forecast", source=vix.get("source"))
    known_vix_labels = ("Volatility subdued", "Volatility normal", "Volatility elevated", "Volatility extreme", "Volatility accelerating", "Volatility easing", "Intraday spike partially reversed")
    active_vix_labels = set(vix.get("classifications", []))
    for label in known_vix_labels:
        slug = re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
        vix_metrics[f"classification_{slug}"] = _snap_metric(label in active_vix_labels if vix.get("current_available") else None, "rule flag", as_of=vix.get("observation_date"), window="latest completed session", universe="Cboe VIX", definition=str(label), source=vix.get("source"))
    vix_metrics["volatility_cycle_state"] = _snap_metric(vix_cycle.get("state"), "rule state", as_of=vix.get("observation_date"), window="10 sessions", universe="Cboe VIX", definition="rule-based descriptive state; mixed state may be unavailable", source=vix.get("source"))
    sections["vix"] = {"status": "available" if vix.get("current_available") else "historical_only", "as_of": vix.get("observation_date"), "source": vix.get("source", "Cboe"), "window": "latest session; rolling 3/5 and trailing 60/252", "universe": "VIX index", "coverage": {"ohlc_sessions": len(vix_rows), "return_variability_prior_windows": (vix_signals.get("return_variability") or {}).get("prior_windows")}, "metrics": vix_metrics, "classifications": vix.get("classifications", []), "cycle": vix_cycle, "reversals": vix_signals.get("reversal_frequency"), "gaps": [] if vix.get("current_available") else ["No VIX close matching the latest completed session"]}

    # Compact deterministic regime evidence is part of the canonical snapshot,
    # so the existing single DeepSeek request sees exactly the displayed data.
    regime_evidence = build_regime_evidence(risk_appetite, transition, sections.get("rates_macro"))
    sector_section = sections.get("sector_rotation") or {}
    sector_section["regime_evidence"] = regime_evidence
    sector_section.setdefault("metrics", {})
    sector_section["metrics"]["regime_evidence_classification"] = _snap_metric(
        regime_evidence.get("classification"), "descriptive evidence alignment",
        as_of=regime_evidence.get("as_of"), window=f"{risk_window} sessions",
            universe="three descriptive regimes", definition=f"{REGIME_EVIDENCE_VERSION}; not a forecast",
        source="deterministic local evidence groups")
    for regime_name, share in (regime_evidence.get("relative_evidence_support") or {}).items():
        slug = re.sub(r"[^a-z0-9]+", "_", regime_name.lower()).strip("_")
        sector_section["metrics"][f"regime_evidence_{slug}_share"] = _snap_metric(
            share, "% relative evidence support (not probability)",
            as_of=regime_evidence.get("as_of"), window=f"{risk_window} sessions",
            universe="three descriptive regimes",
            definition="normalized rule-based compatibility support over available weighted evidence groups",
            source="deterministic local evidence groups")
    sections["sector_rotation"] = sector_section

    configs = {"canonical_windows": list(COMPARISON_WINDOWS), "broad_index_windows": list(BROAD_EQUITY_WINDOWS),
                "sector_rotation_method": {"strength_sessions": 20, "momentum_sessions": 5},
                "risk_appetite_methodology": risk_appetite.get("methodology_version"),
                "regime_evidence_methodology": REGIME_EVIDENCE_VERSION,
                "regime_transition_methodology": TRANSITION_METHODOLOGY_VERSION,
                "basket_weights": basket_cfg, "market_data_sources": {"prices":"Tiingo adjusted close","vix":vix.get("source"),"treasury":"U.S. Treasury XML","uso":"Tiingo adjusted close; futures-based oil ETF proxy"}}
    configs["historical_context_method"] = "completed_month_intramonth_return_v1"
    configs["historical_relative_method"] = "matched_date_monthly_relative_price_change_v1"
    snapshot = build_market_snapshot(sections, configurations=configs,
        view_state={"market_timeframe": timeframe, "relative_strength": _current_pair_result(histories)[1],
                    "broad_index_chart": get_app_preference("market_broad_index_view_v1", {"window":20,"mode":"Normalized cumulative performance","visible_symbols":list(BROAD_EQUITY_INDEX_SYMBOLS)}),
                    "sector_chart_window": st.session_state.get("monitor_sector_horizon", timeframe),
                    "treasury_chart_window": st.session_state.get("treasury_monitor_window",20),
                    "vix_chart_window": st.session_state.get("vix_cycle_window",10)})
    dates = [section.get("as_of") for section in snapshot["sections"].values() if section.get("as_of")]
    snapshot["as_of"] = max(dates) if dates else None
    stable_for_hash = {key:value for key,value in snapshot.items() if key not in {"fingerprint","view_state","view_fingerprint","as_of"}}
    stable_for_hash["as_of"] = snapshot["as_of"]
    snapshot["fingerprint"] = hashlib.sha256(json.dumps(stable_for_hash,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str).encode()).hexdigest()
    for prior in list_deepseek_market_research_reports(250):
        prior_snapshot = prior.get("snapshot") or {}
        prior_fingerprint = prior_snapshot.get("fingerprint")
        if prior.get("research_status") in {"complete", "complete_cost_warning"} and prior_fingerprint and prior_fingerprint != snapshot["fingerprint"]:
            if prior_snapshot.get("schema_version") in {"market-intelligence-snapshot-v1", "market-intelligence-snapshot-v2", "market-intelligence-snapshot-v3"}:
                snapshot["previous_comparison"] = compare_market_snapshots(snapshot, prior_snapshot)
                snapshot["previous_comparison"]["previous_generated_at"] = prior.get("generated_at")
                break
    else:
        snapshot["previous_comparison"] = {"status":"no_previous_snapshot","changes":[],"incompatible":[]}
    # Retain the latest canonical state locally only when its underlying-data
    # fingerprint changes; chart selection/view state does not create versions.
    prior_snapshot = get_app_preference("canonical_market_snapshot_v1", {}) or {}
    if prior_snapshot.get("fingerprint") != snapshot.get("fingerprint"):
        save_app_preference("canonical_market_snapshot_v1", {
            "fingerprint": snapshot.get("fingerprint"),
            "as_of": snapshot.get("as_of"),
            "snapshot": snapshot,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
    return snapshot


def _pair_return_summary(histories, first, second, window):
    """Return aligned current and prior-window relative returns for a pair."""
    from dashboard.cross_market import price_map
    first_prices, second_prices = price_map(histories.get(first, [])), price_map(histories.get(second, []))
    dates = sorted(set(first_prices) & set(second_prices))
    n = int(window)
    if len(dates) < n + 1:
        return {"status":"insufficient_history","symbols":[first,second],"window":n,"observations":len(dates),"as_of":dates[-1] if dates else None}
    recent = dates[-(n+1):]
    r_first = first_prices[recent[-1]] / first_prices[recent[0]] - 1
    r_second = second_prices[recent[-1]] / second_prices[recent[0]] - 1
    excess = (r_first-r_second)*100
    previous = None
    if len(dates) >= 2*n+1:
        prior=dates[-(2*n+1):-n]
        previous=((first_prices[prior[-1]]/first_prices[prior[0]]-1)-(second_prices[prior[-1]]/second_prices[prior[0]]-1))*100
    momentum=excess-previous if previous is not None else None
    return {"status":"available","symbols":[first,second],"window":n,"start_date":recent[0],"as_of":recent[-1],
        "absolute_return_pct":r_first*100,"relative_return_pp":excess,"previous_relative_return_pp":previous,
        "momentum_change_pp":momentum,"trend":"improving" if momentum is not None and momentum>.05 else "deteriorating" if momentum is not None and momentum<-.05 else "steady" if momentum is not None else "insufficient_history"}

def _load_highlights_for_snapshot(snapshot):
    reports = list_deepseek_market_research_reports(250)
    report = select_saved_market_report(reports, snapshot.get("fingerprint"))
    if not report:
        return {}, {}, None
    try:
        saved = report.get("snapshot") or {}
        payload = parse_market_analysis_json(report["report_text"], saved, report.get("sources", []))
        source_urls = {f"S{index}": source["url"] for index, source in enumerate(report.get("sources", []), 1)
                       if isinstance(source, dict) and isinstance(source.get("url"), str)
                       and source["url"].startswith("https://")}
        highlights = {item["section_id"]: {**item, "_source_urls": source_urls}
                      for item in payload["highlights"]}
        if saved.get("schema_version") in {"market-intelligence-snapshot-v1", "market-intelligence-snapshot-v2"}:
            highlights = _remap_legacy_market_highlights(highlights)
        comparisons = {item["comparison_id"]: {**item, "_source_urls": source_urls}
                       for item in payload.get("comparison_highlights", [])}
        return highlights, comparisons, report
    except ValueError:
        return {}, {}, report


def _remap_legacy_market_highlights(legacy):
    """Safe display-only merge of old highlights; saved reports remain untouched."""
    mapped = {key: legacy[key] for key in ("catalysts", "regime", "sector_rotation", "vix") if key in legacy}
    relative_parts = [legacy[key] for key in ("cyclical_defensive", "relative_strength") if key in legacy]
    if relative_parts:
        mapped["relative_strength"] = {
            "headline": "Legacy relative and basket analysis",
            "category": relative_parts[0].get("category", "mixed") if len({x.get("category") for x in relative_parts}) == 1 else "mixed",
            "insight": " ".join(part.get("insight", "") for part in relative_parts if part.get("insight")),
            "why_it_matters": " ".join(part.get("why_it_matters", "") for part in relative_parts if part.get("why_it_matters")),
            "monitor_next": " ".join(part.get("monitor_next", "") for part in relative_parts if part.get("monitor_next")),
            "metric_refs": list(dict.fromkeys(ref for part in relative_parts for ref in part.get("metric_refs", []))),
            "_source_urls": relative_parts[0].get("_source_urls", {}),
            "_legacy_combined": True,
        }
    rates_parts = [legacy[key] for key in ("treasury", "cross_market") if key in legacy]
    if rates_parts:
        mapped["rates_macro"] = {
            "headline": "Legacy rates and cross-market analysis",
            "category": rates_parts[0].get("category", "mixed") if len({x.get("category") for x in rates_parts}) == 1 else "mixed",
            "insight": " ".join(part.get("insight", "") for part in rates_parts if part.get("insight")),
            "why_it_matters": " ".join(part.get("why_it_matters", "") for part in rates_parts if part.get("why_it_matters")),
            "monitor_next": " ".join(part.get("monitor_next", "") for part in rates_parts if part.get("monitor_next")),
            "metric_refs": list(dict.fromkeys(ref for part in rates_parts for ref in part.get("metric_refs", []))),
            "_source_urls": rates_parts[0].get("_source_urls", {}),
            "_legacy_combined": True,
        }
    return mapped


def _render_section_highlight(section_id, highlights, report, snapshot, comparison_highlights=None,
                              comparison_result=None, comparison_id=None):
    """Show one report-derived highlight and a snapshot-bound local chat."""
    chat_model = st.session_state.get("market_ai_model", "deepseek-flash")
    colors = {"constructive":"#55c7a2", "caution":"#ef6b73", "mixed":"#e5b45c", "insufficient evidence":"#9aa6b2"}
    item = (highlights or {}).get(section_id)
    if section_id == "relative_strength" and comparison_id:
        item = (comparison_highlights or {}).get(comparison_id)
        if item:
            item = {**item, "why_it_matters": item.get("why_it_matters", item.get("insight", ""))}
        elif comparison_result and comparison_result.get("status") == "available":
            first, second = comparison_result.get("symbols", ("ETF A", "ETF B"))
            excess = comparison_result.get("excess_pp")
            item = {"headline": f"Calculated summary — not AI analysis · {first} vs {second}",
                    "category": "mixed" if excess is None or abs(excess) < .05 else "constructive" if excess > 0 else "caution",
                    "insight": f"Over {comparison_result.get('window')} sessions, {first} returned {comparison_result.get('return_first', 0):+.2%} and {second} {comparison_result.get('return_second', 0):+.2%}, a {excess:+.2f} pp difference.",
                    "why_it_matters": "This is the matched-date price comparison only; it does not explain the cause of relative performance.",
                    "monitor_next": f"Check the next matched {comparison_result.get('window')}-session comparison and whether the direction persists.",
                    "metric_refs": [], "_deterministic": True}
    deterministic = bool(item and item.get("_deterministic"))
    has_saved_highlight = bool(item and not deterministic)
    report_id = report.get("id") if report else None
    if not item:
        item = {"headline": "No AI Highlight saved", "category": "insufficient evidence",
                "insight": "No AI analysis saved for this section. Run Full Market Analysis when you want a new interpretation.",
                "why_it_matters": "No saved section-level analysis is available.",
                "monitor_next": "Run analysis explicitly when ready.", "metric_refs": []}

    base_fingerprint = snapshot.get("fingerprint") or "no-snapshot"
    evidence_mode = st.session_state.get("deepseek_market_analysis_mode", "Dashboard Analysis")
    comparison_context = comparison_id if section_id == "relative_strength" else ""
    context_key = hashlib.sha256(
        f"{base_fingerprint}|report:{report_id or (report or {}).get('generated_at','none')}|{MARKET_ANALYST_PROMPT_VERSION}|{chat_model}|{evidence_mode}|{comparison_context}".encode()
    ).hexdigest()
    context_key = context_key[:64]
    sources = (report or {}).get("sources", [])
    source_urls = item.get("_source_urls", {})
    def cite(text):
        return re.sub(r"\[(S\d+)\]", lambda m: f"[{m.group(1)}]({source_urls[m.group(1)]})" if m.group(1) in source_urls else m.group(0), text)

    with st.container(border=True):
        provider = (report or {}).get("provider", "deepseek")
        accent = provider_accent(provider)
        if deterministic:
            st.markdown("**Calculated summary — not AI analysis**")
        elif has_saved_highlight:
            st.markdown(f"<div style='border-left:4px solid {accent};padding:2px 0 2px 10px;color:{accent};font-weight:650'>"
                        f"{provider.title()} · AI Highlight</div>", unsafe_allow_html=True)
        if has_saved_highlight and report:
            saved_asof = (report.get("snapshot") or {}).get("as_of") or "date unavailable"
            research_date = report.get("generated_at", "date unavailable")
            st.caption(f"Snapshot {saved_asof} · research {research_date}")
            if saved_report_is_stale(report, base_fingerprint, MARKET_ANALYST_PROMPT_VERSION):
                st.warning(f"Stale analysis · saved snapshot {saved_asof}. Current measurements are shown separately.")
        st.markdown(f"<div style='font-size:17px;line-height:1.4;font-weight:700;overflow-wrap:anywhere'>{cite(item['headline'])}</div>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:{colors[item['category']]};font-weight:650'>{item['category'].title()}</span>", unsafe_allow_html=True)
        st.markdown(f"<div style='font-size:15.5px;line-height:1.6;overflow-wrap:anywhere'>{cite(item['insight'])}</div>", unsafe_allow_html=True)
        with st.expander("Evidence and monitor next", expanded=False):
            st.markdown(cite(item["why_it_matters"]))
            st.markdown(f"**Monitor:** {cite(item['monitor_next'])}")
            st.caption("Local metrics: " + (", ".join(item.get("metric_refs", [])) or "none available"))
            if report:
                st.caption(f"Report {report.get('generated_at', 'date unavailable')} · snapshot {report.get('snapshot', {}).get('as_of', 'unavailable')}")
            elif not deterministic:
                st.caption("No report timestamp · no saved full-market analysis")
        st.markdown("**Ask AI about this section**")
        latest_context = latest_market_section_chat_context(section_id)
        if latest_context and latest_context.get("snapshot_fingerprint") != context_key:
            st.warning("The market snapshot, saved briefing, or research mode changed since this conversation. Start a new conversation to use the current context.")
        legacy_ids = {
            "relative_strength": ("cyclical_defensive",),
            "rates_macro": ("treasury", "cross_market"),
        }.get(section_id, ())
        all_chat_groups = list_market_section_chat_groups()
        archived_groups = earlier_market_chat_groups(
            all_chat_groups, (section_id, *legacy_ids), context_key
        )
        archived = []
        for group in archived_groups:
            old_id = group["section_id"]
            old_fingerprint = group["snapshot_fingerprint"]
            old_rows = list_market_section_chat(old_id, old_fingerprint, limit=60)
            if old_rows:
                archived.append((old_id, group, old_rows))
        if archived:
            with st.expander("Earlier conversations · retained from previous analysis", expanded=False):
                # Put the archive controls beside the archive itself. The sidebar
                # manager remains available for users who want to manage all tabs.
                archive_labels = {}
                for old_id, old_context, old_rows in archived:
                    models = (old_context.get("models") or "DeepSeek").replace(",", ", ")
                    label = (f"{old_id} · {str(old_context['snapshot_fingerprint'])[:10]} · "
                             f"{len(old_rows)} messages · {old_context.get('last_message_at', '')} · {models}")
                    archive_labels[label] = (old_id, old_context["snapshot_fingerprint"])
                archive_epoch = int(st.session_state.get("_market_archive_manager_epoch", 0))
                selected_archive = st.multiselect(
                    "Select earlier conversations to delete",
                    list(archive_labels),
                    key=f"market_archive_selection_{section_id}_{archive_epoch}",
                    help="Select one or more archived conversations for this Market section.",
                )
                confirm_archive = st.checkbox(
                    "I understand selected conversations will be permanently deleted.",
                    key=f"market_archive_confirm_{section_id}_{archive_epoch}",
                )
                if st.button(
                    "Delete selected earlier conversations",
                    key=f"market_archive_delete_{section_id}_{archive_epoch}",
                    disabled=not (selected_archive and confirm_archive),
                    type="secondary",
                ):
                    targets = [archive_labels[label] for label in selected_archive if label in archive_labels]
                    deleted = clear_market_section_chat_groups(targets)
                    st.session_state["_market_archive_manager_epoch"] = archive_epoch + 1
                    st.session_state["_market_archive_feedback"] = (
                        f"Deleted {deleted} messages from {len(targets)} selected earlier conversation(s)."
                    )
                    st.rerun()
                for old_id, old_context, old_rows in archived:
                    st.caption(f"{old_id} · {old_context.get('first_message_at', '')} – "
                               f"{old_context.get('last_message_at', '')} · prior snapshot context")
                    for message in old_rows[-12:]:
                        with st.chat_message(message["role"]):
                            st.markdown(message["content"])
                            st.caption(message.get("model") or "DeepSeek")
        archive_feedback = st.session_state.pop("_market_archive_feedback", None)
        if archive_feedback:
            st.success(archive_feedback)
        with st.expander("Manage all saved Ask AI conversations", expanded=False):
            if not all_chat_groups:
                st.caption("No saved Ask AI conversations.")
            else:
                total_messages = sum(int(group.get("message_count", 0)) for group in all_chat_groups)
                all_label = f"All saved conversations ({total_messages} messages)"
                labels_to_group = {}
                for group in all_chat_groups:
                    models = (group.get("models") or "DeepSeek").replace(",", ", ")
                    label = (f"{group['section_id']} · {str(group['snapshot_fingerprint'])[:10]} · "
                             f"{group['message_count']} messages · {group.get('last_message_at', '')} · {models}")
                    labels_to_group[label] = (group["section_id"], group["snapshot_fingerprint"])
                manager_epoch = int(st.session_state.get("_market_chat_manager_epoch", 0))
                selected_chats = st.multiselect(
                    "Select any saved conversations",
                    [all_label, *labels_to_group],
                    key=f"market_inline_chat_selection_{section_id}_{manager_epoch}",
                    help="Each item is one section and frozen market-context conversation.",
                )
                confirm_chats = st.checkbox(
                    "I understand selected messages will be permanently deleted.",
                    key=f"market_inline_chat_confirm_{section_id}_{manager_epoch}",
                )
                if all_label in selected_chats:
                    delete_targets = [(group["section_id"], group["snapshot_fingerprint"])
                                      for group in all_chat_groups]
                else:
                    delete_targets = [labels_to_group[label] for label in selected_chats
                                      if label in labels_to_group]
                if st.button(
                    "Delete selected conversations",
                    key=f"market_inline_chat_delete_{section_id}_{manager_epoch}",
                    type="secondary",
                    disabled=not (delete_targets and confirm_chats),
                ):
                    deleted = clear_market_section_chat_groups(delete_targets)
                    st.session_state["_market_chat_manager_epoch"] = manager_epoch + 1
                    st.session_state["_market_chat_manager_feedback"] = (
                        f"Deleted {deleted} messages from {len(delete_targets)} saved conversation group(s)."
                    )
                    st.rerun()
        manager_feedback = st.session_state.pop("_market_chat_manager_feedback", None)
        if manager_feedback:
            st.success(manager_feedback)
        chat_messages = list_market_section_chat(section_id, context_key, limit=60)
        if st.button(f"Clear chat history · {len(chat_messages)} messages", key=f"market_chat_clear_{section_id}_{context_key[:10]}",
                     disabled=not bool(chat_messages), help="Clears only this section's conversation for the current snapshot."):
            clear_market_section_chat(section_id, context_key)
            st.session_state.pop(f"market_chat_error_{section_id}_{context_key[:10]}", None)
            st.rerun()
        st.caption("Use the controls above to select earlier conversations or clear all saved Ask AI chats.")
        if chat_messages:
            with st.expander(f"Conversation history · {len(chat_messages)} messages", expanded=False):
                for message in chat_messages[-12:]:
                    with st.chat_message(message["role"]):
                        st.markdown(message["content"])
                        if message["role"] == "user":
                            st.caption(f"You · {message.get('created_at', '')}")
                        if message["role"] == "assistant":
                            cost_text = (f" · {message.get('cost_kind', 'estimate')} ${message['cost_usd']:.5f}"
                                         if message.get("cost_usd") is not None else "")
                            st.caption(f"{message.get('model') or 'DeepSeek'} · {message.get('created_at', '')} · {message.get('input_tokens') or '—'} in / {message.get('output_tokens') or '—'} out{cost_text}")
                            for source in message.get("source_metadata", []):
                                if isinstance(source, dict) and str(source.get("url", "")).startswith("https://"):
                                    st.markdown(f"[{source.get('title') or source['url']}]({source['url']}) · {source.get('date') or 'date unavailable'}")
        error = st.session_state.get(f"market_chat_error_{section_id}_{context_key[:10]}")
        if error:
            st.error(error)
        question_key = f"market_chat_question_{section_id}_{context_key[:10]}"
        if st.session_state.pop(f"market_chat_clear_input_{section_id}_{context_key[:10]}", False):
            # Clear before the widget is instantiated on this rerun.
            st.session_state[question_key] = ""
        question = st.text_input("Your question", key=question_key,
                                 placeholder="Ask about this section or related indicators…")
        evidence_mode = st.session_state.get("deepseek_market_analysis_mode", "Dashboard Analysis")
        chat_mode = "dashboard_analysis" if evidence_mode == "Dashboard Analysis" else "dashboard_web"
        limits = get_app_preference("market_ai_spending_limits_v1", DEFAULT_MARKET_AI_SPENDING_LIMITS) or DEFAULT_MARKET_AI_SPENDING_LIMITS
        req_cap = float(limits.get("per_request_usd", 0.50))
        day_cap = float(limits.get("daily_usd", 2.00))
        daily_spend = deepseek_market_research_daily_spend()
        preview_context = build_market_chat_context(snapshot, section_id, item, report, chat_messages, question or "Question preview")
        try:
            chat_estimate = estimate_market_chat_cost(preview_context, search_mode=chat_mode, model=chat_model)
            st.caption(f"{evidence_mode} · {chat_model} · estimated ceiling ${chat_estimate:.4f} · shared daily ledger")
        except ValueError as exc:
            chat_estimate = None
            st.caption(f"Ask AI unavailable: {exc}")
        disabled_reasons = []
        if not question.strip():
            disabled_reasons.append("Enter a question.")
        if not snapshot.get("fingerprint"):
            disabled_reasons.append("The local market snapshot is unavailable.")
        if not deepseek_chat_configured():
            disabled_reasons.append("DeepSeek is not configured; add DEEPSEEK_API_KEY to .env or Streamlit secrets.")
        if chat_estimate is not None and chat_estimate > req_cap:
            disabled_reasons.append(f"Estimated ceiling ${chat_estimate:.4f} exceeds the ${req_cap:.2f} request limit.")
        if chat_estimate is not None and daily_spend + chat_estimate > day_cap:
            disabled_reasons.append(f"Estimated ceiling exceeds remaining daily budget ${max(0, day_cap-daily_spend):.4f}.")
        submitted = st.button("Ask AI", key=f"market_chat_submit_{section_id}_{context_key[:10]}",
                              disabled=bool(disabled_reasons))
        if disabled_reasons:
            st.caption("Ask AI disabled: " + " ".join(disabled_reasons))
        if submitted:
            request_id = uuid.uuid4().hex
            user_question = question.strip()
            st.session_state[f"market_chat_clear_input_{section_id}_{context_key[:10]}"] = True
            save_market_section_chat_message(section_id=section_id, snapshot_fingerprint=context_key,
                report_id=report_id, request_id=request_id, role="user", content=user_question)
            try:
                context = build_market_chat_context(snapshot, section_id, item, report, chat_messages, user_question)
                answer = answer_market_section_question(context, search_mode=chat_mode,
                    request_limit_usd=req_cap, daily_limit_usd=day_cap, model=chat_model)
                save_market_section_chat_message(section_id=section_id, snapshot_fingerprint=context_key,
                    report_id=report_id, request_id=request_id, role="assistant", content=answer["text"],
                    model=answer.get("model", "deepseek-flash"), source_metadata=answer.get("sources", []),
                    input_tokens=answer.get("input_tokens"), output_tokens=answer.get("output_tokens"),
                    cost_usd=answer.get("actual_cost_usd"), cost_kind=answer.get("cost_kind", "upper-bound estimate"))
                st.session_state.pop(f"market_chat_error_{section_id}_{context_key[:10]}", None)
            except Exception as exc:
                logger.error("Market section chat failed section=%s report_id=%s exception_type=%s",
                             section_id, report_id, type(exc).__name__)
                st.session_state[f"market_chat_error_{section_id}_{context_key[:10]}"] = (
                    f"Ask AI failed ({type(exc).__name__}): {str(exc)[:300]}")
            st.rerun()


def _render_market_section(section_id, highlights, report, snapshot, render_content,
                           comparison_highlights=None, comparison_result=None, comparison_id=None):
    """Render each quantitative section with its matching saved highlight beside it.

    Streamlit's column layout stacks these regions on narrow screens. Rendering
    is read-only; the Ask AI button inside the highlight is the only chat trigger.
    """
    main, contextual = st.columns([3, 1], gap="large", vertical_alignment="top")
    with main:
        render_content()
    with contextual:
        _render_section_highlight(section_id, highlights, report, snapshot,
                                  comparison_highlights, comparison_result, comparison_id)

def _render_overview(histories, timeframe, macro, vix_state, treasury, universe, provider, basis, calendar_rows, health=None, diagnostic=None, highlight_container=None):
    metrics = sector_participation_metrics(histories, timeframe=timeframe)
    snapshot = _build_market_snapshot(histories, timeframe, macro, vix_state, treasury, calendar_rows)
    highlights, comparison_highlights, matching_report = _load_highlights_for_snapshot(snapshot)
    pair_result, pair_config = _current_pair_result(histories)
    pair_id = f"{pair_config['first']}_{pair_config['second']}_{pair_config['window']}D"
    snapshot["view_state"]["relative_strength"] = pair_config
    snapshot["view_state"]["selected_comparison_result"] = {
        key: pair_result.get(key) for key in (
            "status", "symbols", "window", "as_of", "return_first", "return_second",
            "excess_pp", "price_basis", "mode")
        if pair_result and key in pair_result
    }
    snapshot["view_fingerprint"] = hashlib.sha256(json.dumps(snapshot["view_state"],sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
    st.session_state["_market_highlights_current"] = highlights
    st.session_state["_market_comparison_highlights_current"] = comparison_highlights
    st.session_state["_market_report_current"] = matching_report
    st.session_state["_market_snapshot_current"] = snapshot

    unavailable = []
    if not vix_state or not vix_state.get("current_available"):
        unavailable.append("Current VIX close for latest completed SPY session")
    if not treasury:
        unavailable.append("Official Treasury daily par yields")
    vix_context = build_vix_risk_monitor((vix_state or {}).get("history", []),
        completed_session=(vix_state or {}).get("completed_session") or latest_completed_weekday())
    # Keep AI settings and the explicit full-analysis action in the sidebar.
    _render_gpt_research(comparison=pair_result,
        regime_snapshot={"timeframe": timeframe, "sector_metrics": metrics,
                         "vix": vix_context, "treasury": treasury},
        unavailable=unavailable, calendar_rows=calendar_rows, snapshot=snapshot,
        saved_report=matching_report)
    sector_rotation_snapshot = (snapshot.get("sections") or {}).get("sector_rotation") or {}

    from dashboard.event_calendar_ui import render_upcoming_market_catalysts
    _section_anchor("upcoming-market-catalysts")
    _render_market_section("catalysts", highlights, matching_report, snapshot,
                           lambda: render_upcoming_market_catalysts(show_refresh_button=False))
    _render_market_section("regime", highlights, matching_report, snapshot,
        lambda: _render_regime_summary(metrics, histories, vix_state, timeframe))

    sector_metrics = {}
    def render_sector_rotation():
        nonlocal sector_metrics
        sector_metrics = _sector_rotation_section(histories, timeframe,
            vix_rows=(vix_state or {}).get("history", []),
            expected_session=(vix_state or {}).get("completed_session") or latest_completed_weekday(),
            vix_current_available=(vix_state or {}).get("current_available"),
            risk_appetite=snapshot.get("sections", {}).get("sector_rotation", {}).get("risk_appetite"),
            regime_transition=snapshot.get("sections", {}).get("sector_rotation", {}).get("regime_transition"),
            basket_config=snapshot.get("configurations", {}).get("basket_weights"))
    _render_market_section("sector_rotation", highlights, matching_report, snapshot, render_sector_rotation)

    def render_relative_and_defensive():
        _section_anchor("relative-strength-defensive-rotation")
        st.markdown("#### 4 · Relative Strength & Defensive Rotation")
        horizon = st.session_state.get("market_analysis_timeframe", 20)
        cyclical_col, comparison_col = st.columns(2, gap="large")
        with cyclical_col:
            _render_cyclical_defensive_panel(histories, horizon)
        with comparison_col:
            _render_relative_strength_explorer(histories, compact=True)
    _render_market_section("relative_strength", highlights, matching_report, snapshot,
        render_relative_and_defensive, comparison_highlights, pair_result, pair_id)
    _render_rates_credit(histories, treasury, vix_state, timeframe, macro, highlights, matching_report, snapshot)
    _render_market_section("vix", highlights, matching_report, snapshot,
        lambda: _render_vix_monitor(histories, vix_state))

    with st.expander("Methodology and data limitations", expanded=False):
        st.caption("The canonical AI snapshot uses fixed 5/20/60/120/252-session comparisons and fixed 20D sector metrics; chart selections are view state and do not stale the integrated report. Standard sector measures use all 11 ETFs. ETF performance is a proxy, not constituent breadth, fund flows, or causation. Defensive/cyclical weights are documented in the basket control. The quadrant map uses matched sector/SPY ratios: 20D strength and 5D change in strength. Treasury observations are official par yields; TLT/GLD/USO are market-price proxies.")
    _render_data_health(universe, histories, provider, basis, vix_state, treasury, calendar_rows, macro, health, diagnostic)
    with st.expander("Advanced Quantitative Analysis", expanded=False):
        render_relationship_explorer(histories, macro or {}, (vix_state or {}).get("history", []), timeframe)
    # The user reviews all quantitative market evidence before the integrated AI desk.
    render_saved_market_briefing(
        deterministic_transition=sector_rotation_snapshot.get("regime_transition"),
        risk_appetite=sector_rotation_snapshot.get("risk_appetite"))

def _render_technology_capital_cycle():
    _section_anchor("technology-ai-capital-cycle")
    st.markdown("#### Technology & AI Capital Cycle")
    st.caption("Company capex guidance, cash flow, financing, ratings and earnings revisions are not integrated. The dashboard will not infer AI financing stress from ETF price action alone.")

def _ratio_caption(label, result, window):
    if result["status"] == "available":
        st.caption(f"{label} · {window}D: {result['return']:+.2%} · as of {result['as_of']}")
    else:
        st.caption(f"{label} · {window}D: unavailable ({result['status'].replace('_', ' ')})")


def render_relationship_explorer(histories, macro, vix_history, timeframe):
    st.markdown("#### Advanced Quantitative Analysis")
    st.caption("The broad-index performance chart lives in Market Regime Summary. These optional views retain cached cross-asset correlations, yield relationships and exploratory lead/lag; association does not establish causation.")
    observations = _series_observations(macro, vix_history)
    mode = st.selectbox("Relationship view", CROSS_ASSET_RELATIONSHIP_VIEWS, key="cross_market_view")
    sessions = session_calendar(histories)
    if not sessions:
        st.info("Cross-market analysis needs cached SPY sessions. Use the manual historical refresh first.")
        return

    if mode == "ETF returns vs macro changes":
        c1, c2 = st.columns(2)
        symbol = c1.selectbox("ETF", [s for s in ETF_CANDIDATES if histories.get(s)], key="cross_market_rate_scatter_etf")
        rate = c2.selectbox("Macro series", ["DGS2", "DGS10", "10Y-2Y", "BAMLH0A0HYM2", "BAMLC0A0CM", "VIX"],
                            format_func=lambda item: RATE_LABELS[item], key="cross_market_rate_scatter_rate")
        series = _changes_by_session(observations, sessions, rate)
        returns = daily_returns_by_session(histories.get(symbol, []), sessions)
        points = pair_observations(series, returns, sessions)
        if len(points) < 3:
            st.info("At least three aligned daily ETF returns and newly available macro changes are needed. Same-day post-close observations are excluded.")
            return
        figure = go.Figure(go.Scatter(x=[p["x"] for p in points], y=[p["y"] * 100 for p in points],
                                      mode="markers", name=symbol,
                                      customdata=[[p["date"]] for p in points],
                                      marker={"size": 9, "color": "#8ab4f8", "opacity": .78},
                                      hovertemplate="%{customdata[0]}<br>Macro change: %{x:+.1f}<br>ETF return: %{y:+.2f}%<extra></extra>"))
        figure.update_layout(height=420, xaxis_title=f"{RATE_LABELS[rate]} daily change ({'points' if rate == 'VIX' else 'bp'})",
                             yaxis_title=f"{symbol} daily adjusted return (%)", xaxis={"zeroline": True},
                             yaxis={"zeroline": True}, showlegend=False)
        apply_plotly_theme(figure)
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
        st.caption(f"n={len(points)} aligned observations. Macro changes are assigned to the first SPY session strictly after their observation date; missing releases are not converted to zero.")
        return

    if mode == "Rolling correlations":
        c1, c2, c3 = st.columns(3)
        equity_symbols = [s for s in ETF_CANDIDATES if histories.get(s)]
        asset = c1.selectbox("ETF returns", equity_symbols, key="cross_market_roll_asset")
        other_options = ["SPY", "RSP", "QQQ", "IWM", "SMH", "HYG", "LQD", "IEF", "DGS2", "DGS10", "10Y-2Y", "BAMLH0A0HYM2", "BAMLC0A0CM", "VIX"]
        other = c2.selectbox("Compare with", other_options, key="cross_market_roll_other")
        windows = c3.multiselect("Rolling windows", (20, 60, 120, 252), default=[20, 60], format_func=lambda n: f"{n} sessions", key="cross_market_roll_windows")
        asset_returns = daily_returns_by_session(histories.get(asset, []), sessions)
        is_etf = other in equity_symbols
        other_series = (daily_returns_by_session(histories.get(other, []), sessions)
                        if is_etf else _changes_by_session(observations, sessions, other))
        figure = go.Figure()
        latest = []
        for window in windows:
            rows = rolling_correlations(asset_returns, other_series, sessions, window)
            if rows:
                figure.add_trace(go.Scatter(x=[r["date"] for r in rows], y=[r["correlation"] for r in rows],
                                            mode="lines", name=f"{window} sessions", connectgaps=False))
                latest.append({"Window": f"{window} sessions", "Correlation": f"{rows[-1]['correlation']:+.2f}", "As of": rows[-1]["date"], "n": rows[-1]["observations"]})
        if not figure.data:
            st.info("Insufficient contiguous, aligned observations for the selected windows. Missing sessions are not skipped or filled.")
            return
        figure.add_hline(y=0, line_color="rgba(180,190,205,.35)")
        figure.update_layout(height=420, hovermode="x unified", yaxis_title="Pearson correlation", yaxis={"range": [-1, 1], "gridcolor": "rgba(150,160,175,.12)"}, xaxis={"showgrid": False}, legend={"orientation": "h", "y": 1.1})
        apply_plotly_theme(figure)
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
        st.dataframe(latest, hide_index=True, width="stretch")
        if is_etf:
            regimes = correlation_by_spy_200d_regime(asset_returns, other_series, histories, sessions)
            st.caption(f"SPY SMA200 context (above: r={_corr_text(regimes['above_200d'])}, n={regimes['above_200d']['observations']}; below: r={_corr_text(regimes['below_200d'])}, n={regimes['below_200d']['observations']}). This is a historical split, not a causal or forecast claim.")
        st.caption("Rates and OAS changes use only observations available before each equity session. Historical FRED values use initial-release data where the provider returns them.")
        return

    if mode == "Scatter relationship":
        c1, c2, c3 = st.columns(3)
        symbols = [s for s in ETF_CANDIDATES if histories.get(s)]
        left = c1.selectbox("X series", symbols, key="cross_market_scatter_left")
        right_options = [*symbols, "DGS2", "DGS10", "10Y-2Y", "BAMLH0A0HYM2", "BAMLC0A0CM", "VIX"]
        right = c2.selectbox("Y series", right_options, index=right_options.index("SPY") if "SPY" in right_options else 0, key="cross_market_scatter_right")
        window = c3.selectbox("Recent observations", (60, 120, 252), index=0, format_func=lambda n: f"{n} sessions", key="cross_market_scatter_window")
        left_returns = daily_returns_by_session(histories.get(left, []), sessions)
        right_returns = (daily_returns_by_session(histories.get(right, []), sessions)
                         if right in symbols else _changes_by_session(observations, sessions, right))
        points = pair_observations(left_returns, right_returns, sessions)[-window:]
        if len(points) < 3:
            st.info("Insufficient aligned observations for this scatter view.")
            return
        figure = go.Figure(go.Scatter(x=[p["x"] * 100 for p in points], y=[p["y"] * (100 if right in symbols else 1) for p in points],
                                      mode="markers", name=f"{left}/{right}", customdata=[[p["date"]] for p in points],
                                      hovertemplate="%{customdata[0]}<br>X: %{x:+.2f}<br>Y: %{y:+.2f}<extra></extra>"))
        figure.update_layout(height=420, xaxis_title=f"{left} daily return (%)", yaxis_title=f"{right} daily return (%)" if right in symbols else f"{RATE_LABELS[right]} change ({'points' if right == 'VIX' else 'bp'})", showlegend=False)
        apply_plotly_theme(figure)
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
        st.caption(f"n={len(points)} pairs. No imputation; {right} macro observations are lagged to the next SPY session.")
        return

    # Optional lead/lag exploration. Positive lag means the X observation is
    # paired with Y's return that many SPY sessions later.
    c1, c2, c3, c4 = st.columns(4)
    x_options = [*ETF_CANDIDATES, "DGS2", "DGS10", "10Y-2Y", "BAMLH0A0HYM2", "BAMLC0A0CM", "VIX"]
    x_symbol = c1.selectbox("X series", x_options, index=x_options.index("DGS2"), key="cross_market_lag_x")
    y_symbol = c2.selectbox("Y ETF return", [s for s in ETF_CANDIDATES if histories.get(s)], key="cross_market_lag_y")
    observation_window = c3.selectbox("Observation window", (20, 60, 120, 252), index=1,
                                      format_func=lambda n: f"last {n} sessions", key="cross_market_lag_window")
    requested_lag = c4.selectbox("Maximum shift", (3, 5, 10), index=1,
                                 format_func=lambda n: f"±{n} sessions", key="cross_market_lag_max")
    sessions = sessions[-observation_window:]
    x_series = (daily_returns_by_session(histories.get(x_symbol, []), sessions)
                if x_symbol in ETF_CANDIDATES else _changes_by_session(observations, sessions, x_symbol))
    y_series = daily_returns_by_session(histories.get(y_symbol, []), sessions)
    diagnostics = lead_lag_sample_diagnostics(x_series, y_series, sessions,
                                               max_lag=requested_lag, minimum_observations=20)
    st.caption(f"X: {x_symbol} · Y: {y_symbol} · selected window {diagnostics['selected_start'] or '—'} to {diagnostics['selected_end'] or '—'} · common zero-lag dates {diagnostics['overlap_start'] or '—'} to {diagnostics['overlap_end'] or '—'} · matched n={diagnostics['matched_observations_zero_lag']} · requested lag range {diagnostics['requested_lags'][0]:+d}…{diagnostics['requested_lags'][1]:+d} · minimum n={diagnostics['minimum_required']}.")
    rows = diagnostics["per_lag"]
    available_rows = [row for row in rows if row["status"] == "available" and row["correlation"] is not None]
    st.dataframe([{"X leads Y by sessions": row["lag_sessions"], "Matched n": row["observations"],
                   "Status": row["status"], "Correlation": row["correlation"]} for row in rows],
                 hide_index=True, width="stretch")
    if not available_rows:
        st.info("Analysis unavailable: " + diagnostics["reason"])
        return
    figure = go.Figure(go.Bar(x=[row["lag_sessions"] for row in available_rows], y=[row["correlation"] for row in available_rows],
                              marker_color=["#8ab4f8" if row["correlation"] >= 0 else "#d9828b" for row in available_rows],
                              customdata=[[row["observations"]] for row in available_rows],
                              hovertemplate="X leads Y by %{x} SPY sessions<br>r=%{y:+.2f}<br>n=%{customdata[0]}<extra></extra>"))
    figure.add_hline(y=0, line_color="rgba(180,190,205,.35)")
    figure.update_layout(height=380, xaxis_title="X leads Y by sessions (positive means X first)", yaxis_title="Pearson correlation", yaxis={"range": [-1, 1]})
    apply_plotly_theme(figure)
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    best = max(available_rows, key=lambda row: abs(row["correlation"]))
    st.caption(f"Largest absolute sample correlation: {best['correlation']:+.2f} at X lead/lag {best['lag_sessions']:+d} sessions (n={best['observations']}). Exploratory association only; not evidence of causation or a reliable trade signal.")


def _corr_text(row):
    return f"{row['correlation']:+.2f}" if row.get("correlation") is not None else "unavailable"


def render_phase_one_monitor(histories, timeframe, macro, vix_state, treasury, *, universe, provider, basis, calendar_rows=(), health=None, diagnostic=None, highlight_container=None):
    _render_overview(histories, timeframe, macro, vix_state, treasury, universe, provider, basis, calendar_rows, health, diagnostic, highlight_container)


from datetime import date, datetime
from zoneinfo import ZoneInfo
import json

import streamlit as st
import plotly.graph_objects as go

from dashboard.analytics import momentum
from dashboard.data_access import (
    add_chart_symbol, add_market_symbol, list_cached_quotes, list_market_symbols,
    create_research_workspace,
    list_holdings, list_watchlist,
    list_market_leadership_symbols, list_sector_display_symbols,
    list_daily_price_coverage, move_market_symbol, remove_market_symbol,
    remove_chart_symbol, move_chart_symbol, restore_chart_symbol_defaults,
    save_sector_display_symbols,
    get_latest_market_briefing, list_market_briefings, save_market_briefing,
    get_app_preference, save_app_preference,
    list_volatility_observations, volatility_history_status,
    list_treasury_yields,
    list_upcoming_market_events,
)
from dashboard.ai_market_analyst import (
    AssessmentValidationError, build_market_portfolio_context, build_market_snapshot, generate_market_briefing,
)
from dashboard.ai_providers import AIProviderError, DEFAULT_PROVIDER
from dashboard.history_providers import TiingoDailyProvider
from dashboard.market_data import quote_is_stale
from dashboard.market_history import (
    BROAD_MARKET_SYMBOLS, MACRO_PROXY_SYMBOLS, MARKET_ETF_SYMBOLS, SECTOR_SYMBOLS,
    load_history, market_etf_universe, refresh_historical_data,
)
from dashboard.daily_market_refresh import run_daily_market_refresh
from dashboard.recent_catalysts import load_recent_catalysts
from dashboard.cboe_volatility import load_cboe_volatility, refresh_cboe_volatility, volatility_summary
from dashboard.fred_volatility import fred_api_configured, load_fred_vix, refresh_fred_vix
from dashboard.fred_macro import diagnose_fred_connection, fred_macro_configured, load_fred_macro, refresh_fred_macro
from dashboard.market_monitor_ui import render_phase_one_monitor, render_market_sidebar_navigation, render_saved_market_briefing
from dashboard.vix_sources import YFINANCE_PROVIDER, refresh_vix_sources, select_vix_series
from dashboard.vix_risk import latest_completed_weekday
from dashboard.treasury_yields import refresh_treasury_yields
from dashboard.market_regime import build_regime_features, historical_analog_distribution
from dashboard.market_visuals import (
    breadth_proxy_summary, market_leadership_observation,
    leadership_calculation_symbols, market_leadership_series, sector_heatmap_grid, sector_return_rows,
    sector_rotation_points, sector_rotation_pulse,
)
from dashboard.pages import market_risk
from dashboard.theme import PLOTLY_MUTED_COLOR, apply_plotly_theme


def _pct(value) -> str:
    return f"{value:+.2%}" if value is not None else "Unavailable"


def _save_daily_refresh_ai_default() -> None:
    save_app_preference("daily_market_refresh_run_ai_v1",
                        bool(st.session_state.get("daily_market_refresh_run_ai", False)))


def _render_market_refresh_result(result: dict) -> None:
    if not result:
        return
    history = result.get("historical", {})
    vix = result.get("vix", {})
    treasury = result.get("treasury", {})
    price_diagnostics = [row for row in history.get("diagnostics", [])
                         if row.get("symbol") in MARKET_ETF_SYMBOLS]
    latest_price = max((row.get("new_cached_date") for row in price_diagnostics
                        if row.get("new_cached_date")), default="Unavailable")
    vix_date = ((vix.get("current_candidate") or vix.get("latest_cached") or {}).get("date")
                or "Unavailable")
    cols = st.columns(5)
    cols[0].metric("Completed U.S. session", result.get("expected_session", "Unavailable"))
    cols[1].metric("Latest ETF history", latest_price)
    cols[2].metric("ETF symbols updated", sum(1 for symbol in history.get("updated", {}) if symbol in MARKET_ETF_SYMBOLS))
    cols[3].metric("VIX candidate", vix_date)
    cols[4].metric("Treasury observation", treasury.get("latest_date") or "Unavailable")
    if result.get("run_ai_requested"):
        ai_run = st.session_state.get("market_full_analysis_run") or {}
        ai_status = ai_run.get("status", "queued after refresh")
        if ai_run.get("estimated_cost_usd") is not None:
            ai_status += f" · estimate ≤${ai_run['estimated_cost_usd']:.4f}"
    else:
        ai_status = "not requested"
    st.caption(f"Refresh finished {result.get('completed_at', 'time unavailable')} · "
               f"canonical snapshot rebuilds from SQLite on the next render · AI: {ai_status}")
    if result.get("run_ai_requested"):
        ai_run = st.session_state.get("market_full_analysis_run") or {}
        if ai_run.get("status") == "Blocked by budget":
            st.warning("DeepSeek did not run because the estimated request exceeds your saved spending limit. "
                       f"{ai_run.get('error', 'Review the budget preflight in AI Market Research settings.')}")
    with st.expander("Per-symbol historical refresh diagnostics", expanded=False):
        rows = price_diagnostics
        if rows:
            st.dataframe([{
                "Symbol": row.get("symbol"),
                "Previous observation": row.get("previous_cached_date") or "—",
                "Expected session": row.get("expected_session_date"),
                "Attempted": "Yes" if row.get("refresh_attempted") else "No",
                "Provider status": row.get("provider_request_status"),
                "Latest returned": row.get("latest_date_returned") or "—",
                "Rows upserted": row.get("rows_upserted", 0),
                "New cached observation": row.get("new_cached_date") or "—",
                "Freshness": row.get("final_freshness_state"),
                "Error category": row.get("error_category") or "—",
                "Details": row.get("error_message") or "—",
            } for row in rows], hide_index=True, width="stretch")
        else:
            st.caption("No symbol diagnostics were returned.")
    with st.expander("Market refresh source diagnostics", expanded=False):
        for stage_name, stage in result.get("stages", {}).items():
            st.caption(f"**{stage_name.replace('_', ' ').title()} · {stage.get('status')}** — {stage.get('detail', '')}")
        for source_name in ("historical", "vix", "treasury", "calendar"):
            source = result.get(source_name, {})
            errors = source.get("errors", {})
            if source_name == "historical":
                errors = {symbol: detail for symbol, detail in errors.items()
                          if symbol in MARKET_ETF_SYMBOLS}
            if errors:
                st.markdown(f"**{source_name.title()} errors**")
                st.dataframe([{"Source / symbol": key, "Error": value} for key, value in errors.items()],
                             hide_index=True, width="stretch")


def _render_daily_market_refresh(universe: list[str]) -> None:
    """One prominent workflow plus explicit source-by-source diagnostics."""
    if "daily_market_refresh_run_ai" not in st.session_state:
        st.session_state["daily_market_refresh_run_ai"] = bool(
            get_app_preference("daily_market_refresh_run_ai_v1", False)
        )
    primary, option = st.columns([1.5, 2.5], vertical_alignment="center")
    run_ai = option.checkbox("Run DeepSeek Analysis After Refresh",
        key="daily_market_refresh_run_ai", on_change=_save_daily_refresh_ai_default,
        help="The source refresh completes first. If selected, the existing full-snapshot workflow runs afterward, subject to saved spend limits.")
    clicked = primary.button("↻ Refresh Market Data", key="daily_market_refresh", type="primary",
                             use_container_width=True)
    if clicked and not st.session_state.get("_daily_market_refresh_running", False):
        st.session_state["_daily_market_refresh_running"] = True
        with st.status("Refreshing Market data sources…", expanded=True) as status:
            def report_stage(name, state, detail):
                status.update(label=f"{name.replace('_', ' ').title()} · {state}", state="running")
                status.write(detail)
            def report_symbol(symbol, detail):
                status.write(f"Tiingo · {symbol}: {detail}")
            try:
                result = run_daily_market_refresh(
                    universe,
                    allow_yfinance=bool(st.session_state.get("allow_yfinance_vix", False)),
                    on_stage=report_stage, on_history_symbol=report_symbol,
                )
                result["run_ai_requested"] = bool(run_ai)
                st.session_state["daily_market_refresh_result"] = result
                if run_ai:
                    st.session_state["_daily_market_refresh_run_ai"] = True
                status.update(label="Daily Market Refresh complete · inspect source freshness below", state="complete")
            except Exception as error:
                st.session_state["daily_market_refresh_error"] = f"{type(error).__name__}: {error}"
                status.update(label="Daily Market Refresh failed", state="error")
            finally:
                st.session_state["_daily_market_refresh_running"] = False
        st.rerun()
    if st.session_state.get("daily_market_refresh_error"):
        st.error("Market refresh could not complete all stages. Cached observations were preserved.")
        with st.expander("Refresh error details", expanded=False):
            st.code(st.session_state.pop("daily_market_refresh_error"))
    _render_market_refresh_result(st.session_state.get("daily_market_refresh_result"))

    with st.expander("Data Sources & Diagnostics", expanded=False):
        st.caption("Individual source refreshes are available here for troubleshooting. All requests are explicit and cached records are preserved on errors.")
        configured = TiingoDailyProvider.from_environment() is not None
        st.caption("Tiingo configured · adjusted daily OHLCV" if configured else "Tiingo not configured · set TIINGO_API_KEY in local .env")
        st.checkbox("Allow unofficial Yahoo ^VIX fallback", value=False, key="allow_yfinance_vix",
                    help="Only used when a manual VIX refresh runs and Cboe lacks the latest completed session.")
        columns = st.columns(4)
        if columns[0].button("Refresh Historical Prices", key="diagnostic_refresh_history"):
            result = refresh_historical_data(market_etf_universe())
            st.session_state["diagnostic_historical_result"] = result
        if columns[1].button("Refresh VIX", key="diagnostic_refresh_vix"):
            completed = latest_completed_weekday(datetime.now(ZoneInfo("America/New_York")))
            st.session_state["diagnostic_vix_result"] = refresh_vix_sources(
                completed_session=completed, allow_yfinance=st.session_state.get("allow_yfinance_vix", False))
        if columns[2].button("Refresh Treasury", key="diagnostic_refresh_treasury"):
            completed = latest_completed_weekday(datetime.now(ZoneInfo("America/New_York")))
            st.session_state["diagnostic_treasury_result"] = refresh_treasury_yields(today=date.fromisoformat(completed))
        if columns[3].button("Refresh Calendar", key="diagnostic_refresh_calendar"):
            from dashboard.event_calendar import refresh_official_calendars
            completed = latest_completed_weekday(datetime.now(ZoneInfo("America/New_York")))
            st.session_state["diagnostic_calendar_result"] = refresh_official_calendars(today=date.fromisoformat(completed))
        second = st.columns(3)
        if second[0].button("Refresh optional FRED series", key="diagnostic_refresh_fred"):
            st.session_state["diagnostic_fred_result"] = refresh_fred_macro()
        if second[1].button("Refresh FRED VIX reference", key="diagnostic_refresh_fred_vix"):
            st.session_state["diagnostic_fred_vix_result"] = refresh_fred_vix()
        if second[2].button("Test FRED DGS10", key="diagnostic_test_fred"):
            st.session_state["diagnostic_fred_test_result"] = diagnose_fred_connection("DGS10")
        for key in ("diagnostic_historical_result", "diagnostic_vix_result", "diagnostic_treasury_result", "diagnostic_calendar_result", "diagnostic_fred_result", "diagnostic_fred_vix_result", "diagnostic_fred_test_result"):
            value = st.session_state.get(key)
            if value:
                with st.expander(f"{key.replace('diagnostic_', '').replace('_result', '').replace('_', ' ').title()} result", expanded=False):
                    st.json(value)
        cache = load_recent_catalysts()
        st.caption(f"Recent catalyst evidence: {cache.get('status', 'unavailable')} · {len(cache.get('records', []))} cached items. It refreshes through explicitly requested Dashboard + Web Verification research; no search is run here.")


def render() -> None:
    render_market_sidebar_navigation()
    st.header("Market Intelligence")
    st.caption("Local cached daily data · adjusted-close analytics · refreshes are manual")
    universe = market_etf_universe()
    _render_daily_market_refresh(universe)
    timeframe = st.radio(
        "Analysis timeframe", (5, 20, 60), index=1, horizontal=True,
        format_func=lambda n: f"{n}D", key="market_analysis_timeframe",
    )
    provider = "tiingo"
    basis = TiingoDailyProvider.adjustment_basis
    histories = {
        symbol: load_history(symbol, provider, basis)
        for symbol in universe
    }
    # Rendering and selector changes only read SQLite. Network access is confined
    # to the explicit refresh buttons above.
    completed_session = latest_completed_weekday(datetime.now(ZoneInfo("America/New_York")))
    cboe_history = load_cboe_volatility()
    yahoo_history = list_volatility_observations(("VIX",), provider=YFINANCE_PROVIDER)
    fred_reference = load_fred_vix()
    vix_state = select_vix_series(completed_session=completed_session, cboe=cboe_history,
                                  yahoo=yahoo_history, fred=fred_reference)
    vix_state["completed_session"] = completed_session
    calendar_rows = list_upcoming_market_events(limit=100)
    render_phase_one_monitor(
        histories, timeframe, load_fred_macro(), vix_state, list_treasury_yields(),
        universe=universe, provider=provider, basis=basis, calendar_rows=calendar_rows,
        health=st.session_state.get("fred_macro_last_result"),
        diagnostic=st.session_state.get("fred_macro_diagnostic_result"),
    )
    # Keep the existing Market Home shortcut to a ticker workspace, without
    # restoring the superseded standalone leadership chart section.
    research_symbols = sorted(
        set(list_market_leadership_symbols()) | set(list_market_symbols())
        | {item["symbol"].upper() for item in list_holdings()}
        | {item["symbol"].upper() for item in list_watchlist()}
    )
    if research_symbols:
        research_col, open_col = st.columns([4, 1], vertical_alignment="bottom")
        selected_research_symbol = research_col.selectbox(
            "Open ticker research workspace", research_symbols,
            key="market_home_research_symbol", help="Open a ticker's saved research workspace.")
        if open_col.button("Open workspace", key="market_home_open_workspace"):
            from dashboard.navigation import activate_research_workspace
            create_research_workspace(selected_research_symbol)
            activate_research_workspace(st.session_state, selected_research_symbol)
            st.rerun()
    # Section composition and the single saved briefing are owned by the monitor.
    # The legacy heatmap/leadership renderers remain available as reusable helpers,
    # but are intentionally not appended as duplicate visible sections here.


def _render_archived_market_assessments() -> None:
    """Keep legacy AI assessment records readable without presenting a second live conclusion."""
    with st.expander("Archived Market Assessments · legacy provider history", expanded=False):
        reports = list_market_briefings(25)
        if not reports:
            st.caption("No archived assessments.")
            return
        for report in reports:
            st.markdown(f"**{report.get('generated_at')} · {report.get('provider', 'unknown').title()} / `{report.get('model', 'unknown')}`** · market date {report.get('market_data_as_of') or 'unavailable'} · retained historical record")
            _render_assessment_text(report.get("report_text", ""))
            if report.get("source_metadata"):
                st.caption("Archived source links: " + " · ".join(f"[{item.get('title') or item['url']}]({item['url']})" for item in report["source_metadata"]))


def _render_market_analyst(histories: dict[str, list[dict]], vix_state=None) -> None:
    st.markdown("#### AI Market Analyst")
    st.caption("Creates a briefing only when requested. It interprets cached data; Python calculates every metric.")
    latest = get_latest_market_briefing()
    if latest:
        st.markdown(f"**Latest saved assessment** · generated {latest['generated_at']} · market date {latest.get('market_data_as_of') or 'Unavailable'} · data retrieved {latest.get('market_data_as_of_timestamp') or 'Unavailable'} · {latest.get('provider', 'openai').title()} / `{latest['model']}`" +
                    (" · web search enabled" if latest["web_search_enabled"] else " · price data only"))
        _render_assessment_text(latest["report_text"])
        if latest["source_metadata"]:
            st.caption("Web sources")
            st.markdown(" · ".join(f"[{item.get('title') or item['url']}]({item['url']})" for item in latest["source_metadata"]))
        usage = latest.get("total_tokens")
        cost = latest.get("estimated_cost_usd")
        if usage is not None:
            st.caption(f"Usage: {usage:,} tokens" + (f" · estimated ${cost:.6f} token cost" if cost is not None else " · cost estimate unavailable") +
                       (" · excludes separate web-search charges" if latest["web_search_enabled"] and cost is not None else ""))
        with st.expander("Saved indicator snapshot and supporting data"):
            st.json(latest.get("indicator_snapshot") or latest["input_snapshot"].get("regime_features", {}))
        _render_assessment_comparison()
    else:
        st.caption("No saved briefing yet.")

    provider = st.session_state.get("ai_provider", DEFAULT_PROVIDER)
    model = st.session_state.get("ai_model")
    max_tokens = st.session_state.get("ai_max_output_tokens", 1400)
    include_portfolio = st.session_state.get("ai_include_portfolio", False)
    if provider == "openai":
        web_search = st.checkbox("Use current web search (additional usage may apply)", value=False, key="ai_market_web_search")
    else:
        web_search = False
        st.caption("Current web search is available when OpenAI is selected in AI settings.")
    if st.button("Generate / Refresh AI Briefing", type="primary", key="generate_market_briefing"):
        cboe_reference = load_cboe_volatility()
        volatility_histories = {"VIX": (vix_state or {}).get("history", []), "VIX9D": cboe_reference.get("VIX9D", [])}
        portfolio_context = build_market_portfolio_context(histories) if include_portfolio else None
        previous = get_latest_market_briefing()
        upcoming_events = list_upcoming_market_events(limit=100)
        snapshot = build_market_snapshot(histories, portfolio_context, volatility_histories, previous, upcoming_events)
        if not any(item is not None for item in snapshot["as_of_dates"].values()):
            st.warning("There is no cached market history to analyze. Use the manual historical refresh first.")
        else:
            try:
                with st.spinner("Generating the requested market briefing…"):
                    briefing = generate_market_briefing(
                        snapshot, web_search=web_search, provider=provider,
                        model=model, max_output_tokens=max_tokens,
                    )
                save_market_briefing(briefing)
                st.rerun()
            except AIProviderError as error:
                st.error(str(error))
            except AssessmentValidationError as error:
                st.error(str(error))
            except Exception as error:
                st.error("The briefing could not be saved or generated. Check local storage and provider connectivity, then retry.")
                st.caption(f"Error type: {type(error).__name__}")


def _render_volatility_dashboard(histories: dict[str, list[dict]]) -> None:
    """Render cached FRED VIX observations; network requests remain explicit."""
    fred_rows = load_fred_vix()
    cboe_reference = load_cboe_volatility()
    summary = volatility_summary({"VIX": fred_rows, "VIX9D": cboe_reference.get("VIX9D", [])})
    st.markdown("#### Market Risk & Volatility")
    st.caption("FRED VIXCLS · Cboe daily close · delayed end-of-day observation, not a live quote · VIX measures implied volatility, not market direction.")
    st.caption("This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis. VIXCLS is copyrighted Cboe data; the linked FRED terms and series-owner restrictions apply.")
    if summary["status"] == "unavailable":
        st.caption("No FRED VIXCLS observations cached. Configure FRED_API_KEY, then use **Refresh VIX (FRED)** above. Cboe reference data is not substituted automatically.")
    else:
        if summary["status"] == "stale":
            st.warning(f"FRED cache is stale · latest VIX close {summary['as_of']}. Refresh manually to update.")
        cols = st.columns(6)
        cols[0].metric("Latest VIX close", f"{summary['close']:.2f}", delta=(f"{summary['previous_change_points']:+.2f} pts · {summary['previous_change_percent']:+.1f}%" if summary["previous_change_points"] is not None else None), delta_color="off")
        cols[1].metric("Previous close", f"{summary['previous_close']:.2f}" if summary["previous_close"] is not None else "Unavailable")
        cols[2].metric("5D change", f"{summary['change_5d_points']:+.2f} pts" if summary["change_5d_points"] is not None else "Insufficient history", delta_color="off")
        cols[3].metric("20D change", f"{summary['change_20d_points']:+.2f} pts" if summary["change_20d_points"] is not None else "Insufficient history", delta_color="off")
        cols[4].metric("252-session percentile", f"{summary['percentile_252']:.1f}th" if summary["percentile_252"] is not None else "Insufficient history")
        cols[5].metric("VIX vs 20D average", f"{summary['vs_sma20'].replace('_', ' ')} · {summary['vs_sma20_points']:+.2f}" if summary["vs_sma20_points"] is not None else "Insufficient history")
        st.caption(f"Observation {summary['as_of']} · {summary['source']} · retrieved {summary.get('retrieved_at') or 'unknown'} · descriptive level: {summary['condition']} (below 20 normal, 20–30 elevated, 30+ stressed; conventions only)")

        vix9d = summary.get("vix9d_ratio")
        if vix9d is not None:
            suffix = " · stale" if summary.get("vix9d_status") == "stale" else ""
            st.caption(f"Optional term comparison: Cboe VIX9D / FRED VIX = {vix9d:.2f} at common date {summary.get('vix9d_as_of')}{suffix}. The sources remain separately labeled.")

        normalized = st.checkbox("Overlay normalized SPY comparison (both series start at 100)", value=False, key="vix_normalized_spy")
        figure = go.Figure()
        vix_rows = summary.get("history", [])[-504:]
        if normalized:
            spy_map = {row.get("trading_date"): row.get("close") for row in histories.get("SPY", [])}
            vix_map = {row["observation_date"]: row["close"] for row in vix_rows}
            common = sorted(set(spy_map) & set(vix_map))
            if len(common) < 2:
                st.info("A normalized overlay needs at least two common VIX and SPY observations. No values were filled.")
            else:
                vix_base, spy_base = float(vix_map[common[0]]), float(spy_map[common[0]])
                figure.add_trace(go.Scatter(x=common, y=[float(vix_map[d]) / vix_base * 100 for d in common], mode="lines", name="VIX · rebased", line={"color": "#d6a84f", "width": 2}, hovertemplate="%{x}<br>VIX rebased: %{y:.2f}<extra></extra>"))
                figure.add_trace(go.Scatter(x=common, y=[float(spy_map[d]) / spy_base * 100 for d in common], mode="lines", name="SPY · rebased", line={"color": "#8ab4f8", "width": 2}, hovertemplate="%{x}<br>SPY rebased: %{y:.2f}<extra></extra>"))
                figure.add_hline(y=100, line_dash="dot", line_color="rgba(180,190,205,.35)")
        else:
            figure.add_trace(go.Scatter(x=[row["observation_date"] for row in vix_rows], y=[row["close"] for row in vix_rows], mode="lines", name="VIX", line={"color": "#d6a84f", "width": 1.8}, hovertemplate="%{x}<br>VIX close: %{y:.2f}<extra></extra>"))
            figure.add_hline(y=20, line_dash="dot", line_color="rgba(180,190,205,.35)")
            figure.add_hline(y=30, line_dash="dot", line_color="rgba(210,100,100,.4)")
        figure.update_layout(template="plotly_dark", height=380, margin={"l": 10, "r": 10, "t": 60, "b": 18}, hovermode="x unified", xaxis={"showgrid": False}, yaxis={"title": "Rebased index (100 = start)" if normalized else "VIX index points", "gridcolor": "rgba(150,160,175,.12)"}, legend={"orientation": "h", "y": 1.12})
        apply_plotly_theme(figure)
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})

    with st.expander("Cboe reference history and data terms"):
        st.caption("Cboe’s official historical CSV remains available as a separately refreshed reference. It is not an automatic fallback for missing FRED observations.")
        if st.button("Refresh Cboe reference history", key="cboe_reference_refresh"):
            with st.spinner("Refreshing Cboe reference CSVs…"):
                st.session_state["cboe_reference_refresh_result"] = refresh_cboe_volatility()
            st.rerun()
        result = st.session_state.get("cboe_reference_refresh_result")
        if result:
            for symbol, count in result.get("updated", {}).items():
                st.caption(f"{symbol}: {count:,} cached rows from Cboe.")
            for symbol, error in result.get("errors", {}).items():
                st.warning(f"{symbol}: {error}")
        reference_vix = cboe_reference.get("VIX", [])
        if reference_vix:
            latest_reference = reference_vix[-1]
            st.caption(f"Reference only: Cboe VIX close {latest_reference['close']:.2f} · {latest_reference['observation_date']} · retrieved {latest_reference['retrieved_at']}.")
        st.markdown("**FRED API notice:** This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis. VIXCLS is Cboe data, identified as copyrighted and reprinted with permission; follow the series-owner restrictions before sharing or redistributing.")
        st.markdown("[FRED API Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html) · [VIXCLS series notes](https://fred.stlouisfed.org/series/VIXCLS) · [Cboe historical VIX data](https://www.cboe.com/tradable-products/vix/vix-historical-data)")
        st.caption("VIX is an options-implied measure of expected volatility, not a forecast of direction. VIX is never treated as a tradable ETF or adjusted equity return. FRED updates vary by release calendar; VIXCLS is usually posted on the next business day and the observation date remains the measure date.")


def _render_regime_summary(histories: dict[str, list[dict]]) -> None:
    """Visible deterministic features. The panel reads cache only and doesn't call an API."""
    cboe_reference = load_cboe_volatility()
    volatility = volatility_summary({"VIX": load_fred_vix(), "VIX9D": cboe_reference.get("VIX9D", [])})
    features = build_regime_features(histories, volatility)
    st.markdown("#### Deterministic market regime")
    st.caption(f"Rule-based descriptive classification · {features.get('as_of') or 'SPY data unavailable'} · not a probability or forecast")
    spy = features["spy"]
    trend = lambda value: "Above" if value is True else "Below" if value is False else "Unavailable"
    cols = st.columns(5)
    cols[0].metric("Regime", features["regime_classification"])
    cols[1].metric("SPY vs SMA50", trend(spy["above_sma50"]))
    cols[2].metric("SPY vs SMA200", trend(spy["above_sma200"]))
    cols[3].metric("SPY drawdown · 252D", f"{spy['drawdown_252']:.2%}" if spy["drawdown_252"] is not None else "Unavailable")
    cols[4].metric("Drawdown percentile", f"{spy['drawdown_252_percentile']:.1f}th" if spy["drawdown_252_percentile"] is not None else "Insufficient history")
    relative = features["relative_strength_trends"]
    rows = []
    for symbol in ("QQQ", "RSP"):
        item = relative[symbol]
        rows.append({"Indicator": f"{symbol}/SPY relative return", "Current": _pct(item["recent_20d"]),
                     "Context": f"Prior 20D {_pct(item['prior_20d'])}; acceleration {item['change_vs_prior_pp']:+.2f} pp" if item["change_vs_prior_pp"] is not None else "Insufficient history"})
    dispersion = features["sector_dispersion_20d"]
    rows.append({"Indicator": "11-sector 20D return dispersion", "Current": f"{dispersion['stddev_pp']:.2f} pp SD" if dispersion["stddev_pp"] is not None else "Unavailable",
                 "Context": f"{dispersion['stddev_percentile_252']:.1f}th percentile" if dispersion["stddev_percentile_252"] is not None else "Insufficient history"})
    breadth = features["broad_etf_20d_proxy"]
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption(f"Equal-weight defensive vs cyclical sector ETF returns: {_pp(features['defensive_vs_cyclical_20d_pp'])}. Broad ETF proxy: {breadth['positive']}/{breadth['available']} positive over 20D (of {breadth['universe']} symbols in the fixed proxy universe).")
    with st.expander("Historical analogs · VIX percentile matches"):
        vix_rows = load_fred_vix()
        analog = historical_analog_distribution(vix_rows, histories.get("SPY", []))
        if analog["status"] == "available":
            st.caption(f"Past readings within ±{analog['tolerance_pp']:.0f} percentile points of the current VIX percentile ({analog['current_vix_percentile']:.1f}th): n={analog['sample_size']} fully observed outcomes · SPY forward {analog['horizon_sessions']} sessions. Mean {_pct(analog['mean_return'])}; median {_pct(analog['median_return'])}; range {_pct(analog['range_return'][0])} to {_pct(analog['range_return'][1])}.")
            st.caption(analog["caveat"])
        else:
            st.caption(f"Historical analogs unavailable ({analog['status']}); longer local VIX/SPY history is required.")
    with st.expander("Regime rules and indicator definitions"):
        st.caption(features["methodology"]["regime_thresholds"])
        st.caption("Relative strength is the change in the same-date adjusted-close ETF/SPY ratio over 20 sessions; acceleration compares the latest 20D ratio return with the preceding 20D return.")
        st.caption(features["methodology"]["breadth"])
        st.caption("Historical percentiles are empirical ranks of cached historical observations; the available window and sample length limit interpretation. The analog is an unvalidated association, not a reliable forecast. Optional credit-spread indicators are not connected because no reliable source is configured.")


def _pp(value):
    return f"{value:+.2f} pp" if value is not None else "Unavailable"


def _render_assessment_text(report: str) -> None:
    """Keep the executive summary visible and place full supporting detail in an expander."""
    import re
    match = re.search(r"(?im)^\s*#{0,3}\s*(?:1[.)]\s*)?Market Character\s*:?.*$", report)
    if not match:
        st.markdown(report)
        return
    remainder = report[match.end():]
    next_heading = re.search(r"(?im)^\s*#{0,3}\s*(?:2[.)]\s*)?Top 3 Market Signals", remainder)
    summary = remainder[:next_heading.start()] if next_heading else remainder[:1200]
    st.markdown(summary.strip())
    with st.expander("Supporting evidence and full assessment"):
        st.markdown(report)


def _render_assessment_comparison() -> None:
    reports = list_market_briefings(10)
    if len(reports) < 2:
        st.caption("Assessment comparison will appear after a second assessment is saved.")
        return
    latest, previous = reports[0], reports[1]
    current_ind, prior_ind = latest.get("indicator_snapshot", {}), previous.get("indicator_snapshot", {})
    prior_regime = previous.get("regime_classification") or "Unavailable"
    current_regime = latest.get("regime_classification") or "Unavailable"
    changes = [{"Indicator": "Regime", "Previous": prior_regime, "Current": current_regime,
                "Change": "Regime changed" if prior_regime != current_regime else "No change"}]
    for label, section, key, threshold, units, scale in (
        ("SPY 252-session drawdown", "spy", "drawdown_252", 3.0, "pp", 100.0),
        ("VIX close", "volatility", "close", 3.0, "points", 1.0),
        ("20D sector dispersion (pp)", "sector_dispersion_20d", "stddev_pp", 2.0, "pp", 1.0),
    ):
        # Drawdown is stored as a fraction; dispersion is already percentage points.
        old = prior_ind.get(section, {}).get(key)
        new = current_ind.get(section, {}).get(key)
        delta = (new - old) * scale if old is not None and new is not None else None
        changes.append({"Indicator": label,
                        "Previous": f"{old * scale:.2f}" if old is not None else "Unavailable",
                        "Current": f"{new * scale:.2f}" if new is not None else "Unavailable",
                        "Change": (f"Material · {delta:+.2f} {units}" if abs(delta) >= threshold else f"{delta:+.2f} {units}") if delta is not None else "Unavailable"})
    st.caption(f"Latest versus previous · {latest.get('market_data_as_of') or 'date unavailable'} vs {previous.get('market_data_as_of') or 'date unavailable'} · material differences are shown, not forecast.")
    st.dataframe(changes, width="stretch", hide_index=True)
    with st.expander("Saved assessment history"):
        st.dataframe([{"Generated": row["generated_at"], "Data as of": row.get("market_data_as_of") or "Unavailable", "Retrieved": row.get("market_data_as_of_timestamp") or "Unavailable", "Regime": row.get("regime_classification") or "Unavailable", "Provider / model": f"{row.get('provider')} / {row.get('model')}"} for row in reports], width="stretch", hide_index=True)
        if len(reports) > 1:
            selected_id = st.selectbox(
                "Open a previous assessment", [row["id"] for row in reports[1:]],
                format_func=lambda report_id: next(
                    f"{row['generated_at']} · {row.get('regime_classification') or 'Regime unavailable'}"
                    for row in reports if row["id"] == report_id
                ), key="saved_assessment_history_select",
            )
            selected = next(row for row in reports if row["id"] == selected_id)
            st.markdown(selected["report_text"])
            st.markdown("**Saved indicator snapshot**")
            st.json(selected.get("indicator_snapshot", {}))
            if selected.get("source_metadata"):
                st.markdown("Sources: " + " · ".join(f"[{item.get('title') or item['url']}]({item['url']})" for item in selected["source_metadata"]))


def _render_leadership_customizer(symbols, histories, provider, basis) -> None:
    st.caption("Choose which ETFs appear in the chart. SPY stays in calculations as the benchmark when hidden.")
    st.caption("Displayed: " + (" · ".join(symbols) if symbols else "none"))
    with st.form("leadership_add_ticker", clear_on_submit=True):
        ticker = st.text_input("Add ETF", placeholder="IGV or MAGS", label_visibility="collapsed")
        if st.form_submit_button("Add ticker"):
            try:
                add_chart_symbol("leadership", ticker)
                st.rerun()
            except ValueError as error:
                st.error(str(error))
    if symbols:
        remove_col, reorder_col = st.columns(2)
        selected = remove_col.selectbox("Remove", symbols, key="leadership_remove_ticker")
        if remove_col.button("Remove", key="leadership_remove_button"):
            remove_chart_symbol("leadership", selected)
            st.rerun()
        selected_move = reorder_col.selectbox("Reorder", symbols, key="leadership_reorder_ticker")
        current_index = symbols.index(selected_move)
        up_col, down_col = reorder_col.columns(2)
        if up_col.button("↑", disabled=current_index == 0, key="leadership_move_up"):
            move_chart_symbol("leadership", selected_move, -1)
            st.rerun()
        if down_col.button("↓", disabled=current_index == len(symbols) - 1, key="leadership_move_down"):
            move_chart_symbol("leadership", selected_move, 1)
            st.rerun()
    if st.button("Restore default list", key="leadership_restore_defaults"):
        restore_chart_symbol_defaults("leadership")
        st.rerun()
    _render_history_fetch_controls(symbols, histories, provider, basis, "leadership")


def _render_sector_customizer(symbols, histories, provider, basis) -> None:
    standard = list(SECTOR_SYMBOLS)
    currently_selected = [symbol for symbol in symbols if symbol in SECTOR_SYMBOLS]
    selected_standard = st.multiselect(
        "Visible standard sectors", standard, default=currently_selected,
        format_func=lambda ticker: f"{ticker} · {SECTOR_SYMBOLS[ticker]}",
        key="sector_standard_selection_" + "_".join(symbols),
    )
    custom = [symbol for symbol in symbols if symbol not in SECTOR_SYMBOLS]
    with st.form("sector_add_custom_ticker", clear_on_submit=True):
        ticker = st.text_input("Add industry/theme ETF", placeholder="IGV or MAGS", label_visibility="collapsed")
        if st.form_submit_button("Add custom ETF"):
            try:
                add_chart_symbol("sector_display", ticker)
                st.rerun()
            except ValueError as error:
                st.error(str(error))
    if custom:
        st.caption("Custom ETFs: " + " · ".join(custom))
        selected_custom = st.selectbox("Remove custom ETF", custom, key="sector_remove_custom")
        if st.button("Remove selected custom ETF", key="sector_remove_custom_button"):
            remove_chart_symbol("sector_display", selected_custom)
            st.rerun()
    if st.button("Save sector visibility", key="sector_save_visibility"):
        save_sector_display_symbols([*selected_standard, *custom])
        st.rerun()
    if st.button("Restore standard 11-sector view", key="sector_restore_defaults"):
        restore_chart_symbol_defaults("sector_display")
        st.rerun()
    _render_history_fetch_controls(symbols, histories, provider, basis, "sectors")


def _render_history_fetch_controls(symbols, histories, provider, basis, key_prefix) -> None:
    missing = [symbol for symbol in symbols if not histories.get(symbol)]
    if not missing:
        return
    st.divider()
    st.caption("Historical data needed · fetching is manual and uses the configured Tiingo provider.")
    selected = st.selectbox("Ticker", missing, key=f"{key_prefix}_history_needed")
    if st.button("Fetch History", key=f"{key_prefix}_fetch_history"):
        with st.spinner(f"Requesting cached history for {selected}…"):
            st.session_state["symbol_history_refresh_result"] = refresh_historical_data([selected])
        st.rerun()


def _rank(rows: list[dict], symbol: str) -> str:
    for index, row in enumerate(rows, 1):
        if row["symbol"] == symbol and row["return"] is not None:
            return str(index)
    return "Unavailable"


def _render_data_status(symbols: list[str], histories: dict[str, list[dict]], provider: str, basis: str) -> None:
    coverage = list_daily_price_coverage(symbols, provider, basis)
    st.markdown("#### Historical data status")
    st.caption("All analytics below use this provider and adjustment basis only. A blank/Unavailable value means data is not cached or insufficient.")
    rows = []
    for symbol in symbols:
        info = coverage.get(symbol, {})
        bars = histories.get(symbol, [])
        rows.append({
            "Symbol": symbol,
            "Observations": str(info.get("observations", 0) or "No data"),
            "Coverage start": info.get("first_date") or "Unavailable",
            "Latest date": info.get("latest_date") or "Unavailable",
            "Provider": provider if bars else "Not available",
            "Adjustment": "Split + dividend adjusted" if bars else "Unavailable",
            "Status": _history_freshness(info.get("latest_date")),
        })
    with st.expander(f"Coverage by symbol · {len(symbols)} symbols", expanded=False):
        st.dataframe(rows, width="stretch", hide_index=True)


def _history_freshness(latest_date: str | None) -> str:
    if not latest_date:
        return "Missing"
    try:
        age = (date.today() - date.fromisoformat(latest_date)).days
    except ValueError:
        return "Invalid date"
    if age < 0:
        return "Future date"
    return "Recent" if age <= 4 else "Stale"


def _render_quote_cache() -> None:
    left, _, right = st.columns([2, 3, 1])
    with left.popover("⚙ Customize symbols", use_container_width=True):
        with st.form("add_market_symbol_form", clear_on_submit=True):
            symbol = st.text_input("Add quote symbol", placeholder="AAPL")
            if st.form_submit_button("Add"):
                if symbol.strip():
                    add_market_symbol(symbol)
                    st.rerun()
                else:
                    st.warning("Enter a symbol first.")
        symbols = list_market_symbols()
        if symbols:
            selected = st.selectbox("Reorder", symbols, key="market_reorder_symbol")
            index = symbols.index(selected)
            up, down = st.columns(2)
            if up.button("↑", disabled=index == 0, key="market_move_up"):
                move_market_symbol(selected, -1)
                st.rerun()
            if down.button("↓", disabled=index == len(symbols) - 1, key="market_move_down"):
                move_market_symbol(selected, 1)
                st.rerun()
            remove = st.selectbox("Remove", symbols, key="market_remove_symbol")
            if st.button("Remove selected", key="market_remove_button"):
                remove_market_symbol(remove)
                st.rerun()
    if right.button("↻ Refresh quotes", key="spot_quote_refresh"):
        from dashboard.quote_service import refresh_quotes
        st.session_state["spot_quote_refresh_result"] = refresh_quotes(list_market_symbols())
        st.rerun()
    quote_result = st.session_state.pop("spot_quote_refresh_result", None)
    if quote_result:
        for symbol, message in quote_result["errors"].items():
            st.warning(f"{symbol}: {message}")
        if quote_result.get("updated"):
            st.caption("Updated quotes: " + ", ".join(quote_result["updated"]))
    quotes = list_cached_quotes()
    symbols = list_market_symbols()
    if not symbols:
        st.caption("No quote symbols configured.")
    for start in range(0, len(symbols), 3):
        cols = st.columns(3)
        for col, symbol in zip(cols, symbols[start:start + 3]):
            quote = quotes.get(symbol.upper())
            if quote:
                change = quote.get("change_percent")
                col.metric(symbol, f"${quote['price']:,.2f}", f"{change:+.2f}%" if change is not None else "Change unavailable")
                col.caption(("Stale" if quote_is_stale(quote) else "Saved") + f" · {quote['quote_date']} · not real-time")
            else:
                col.metric(symbol, "Unavailable", "No saved quote")

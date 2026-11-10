"""Portfolio health, exposures, position monitoring, and local risk analysis."""

from datetime import date
import plotly.graph_objects as go
import streamlit as st

from dashboard.calculations import holding_metrics, portfolio_totals
from dashboard.data_access import (
    delete_holding, get_cash_balance, get_portfolio_strategy_horizon,
    get_portfolio_monitor_settings, get_strategy_record, list_cached_quotes,
    ensure_portfolio_classifications, list_holdings, list_portfolio_classification_records,
    list_research_strategy_scenarios, list_strategy_revisions,
    list_portfolio_position_order, move_portfolio_position,
    save_cash_balance, save_holding, save_portfolio_strategy_horizon,
    save_portfolio_monitor_settings, save_portfolio_classification_records,
    save_strategy_record,
    get_position_plan, get_position_planner_policy, save_position_planner_policy,
    latest_strategy_revision_id,
    record_position_plan_execution, list_position_plan_executions,
)
from dashboard.history_providers import TiingoDailyProvider
from dashboard.market_data import quote_is_stale
from dashboard.market_history import SECTOR_SYMBOLS, load_history, refresh_historical_data
from dashboard.market_visuals import sector_rotation_points
from dashboard.navigation import activate_research_workspace
from dashboard.pages import market_risk
from dashboard.portfolio_risk import (
    aggregate_downside_to_stops, correlation_matrix, portfolio_risk_analysis,
    position_reward_risk, sector_exposure,
)
from dashboard.portfolio_monitor import (
    monitor_alerts, order_holdings, price_position_status, resolve_approved_strategy,
    scenario_levels, target_range_text,
)
from dashboard.position_planner import (
    calculate_position_capacity, plan_entry_tranches, simulate_exit_tranches,
)
from dashboard.setup_calculations import calculate_long_setup, format_return_range
from dashboard.quote_service import refresh_quotes
from dashboard.sector_classification import (
    ASSET_TYPES, BENCHMARK_TYPES, CLASSIFICATION_SOURCES, SECTOR_CHOICES,
    VERIFICATION_STATUSES,
)
from dashboard.theme import PLOTLY_MUTED_COLOR, apply_plotly_theme


def _money(value):
    return f"${value:,.2f}" if value is not None else "Unavailable"


def _signed_money(value):
    if value is None:
        return "Unavailable"
    label = f"{'+' if value >= 0 else '−'}${abs(value):,.2f}"
    return f":{'green' if value > 0 else 'red' if value < 0 else 'gray'}[{label}]"


def _signed_pct(value):
    if value is None:
        return "Unavailable"
    label = f"{value:+.2%}"
    return f":{'green' if value > 0 else 'red' if value < 0 else 'gray'}[{label}]"


def _refresh() -> None:
    symbols = [item["symbol"] for item in list_holdings()] + ["SPY"]
    st.session_state["portfolio_refresh_result"] = refresh_quotes(symbols)


@st.dialog("Add position")
def _add_position_dialog() -> None:
    with st.form("add_position_form", clear_on_submit=True):
        symbol = st.text_input("Ticker", placeholder="e.g. AAPL").strip().upper()
        left, right = st.columns(2)
        shares = left.number_input("Shares", min_value=0.0, step=1.0, format="%.4f")
        cost = right.number_input("Average cost per share", min_value=0.0, step=1.0)
        horizon = st.selectbox("Investment horizon", ("Long", "Medium", "Short"), index=1)
        status = st.selectbox("Thesis status", ("Active", "Review Needed", "Invalidated"))
        if st.form_submit_button("Save position", type="primary"):
            if not symbol:
                st.error("Enter a ticker symbol.")
            else:
                save_holding(symbol, shares, cost, investment_horizon=horizon, thesis_status=status)
                st.rerun()


@st.dialog("Edit position")
def _edit_position_dialog(symbol: str) -> None:
    holding = next((row for row in list_holdings() if row["symbol"].upper() == symbol), None)
    if not holding:
        st.info("This position is no longer available.")
        return
    with st.form(f"edit_position_{symbol}"):
        st.caption(symbol)
        left, right = st.columns(2)
        shares = left.number_input("Shares", min_value=0.0, value=float(holding["shares"]), step=1.0, format="%.4f")
        cost = right.number_input("Average cost per share", min_value=0.0, value=float(holding["average_cost"]), step=1.0)
        horizon = st.selectbox("Investment horizon", ("Long", "Medium", "Short"), index=("Long", "Medium", "Short").index(holding["investment_horizon"]))
        status = st.selectbox("Thesis status", ("Active", "Review Needed", "Invalidated"), index=("Active", "Review Needed", "Invalidated").index(holding["thesis_status"]))
        if st.form_submit_button("Save changes", type="primary"):
            save_holding(symbol, shares, cost, investment_horizon=horizon, thesis_status=status)
            st.rerun()
    if st.button("Delete position", key=f"confirm_delete_{symbol}"):
        delete_holding(symbol)
        st.rerun()


@st.dialog("Edit saved strategy levels")
def _edit_strategy_levels_dialog(symbol: str) -> None:
    holding = next((row for row in list_holdings() if row["symbol"].upper() == symbol), None)
    if not holding:
        st.info("This position is no longer available.")
        return
    saved = get_strategy_record(symbol) or {}
    with st.form(f"portfolio_strategy_levels_{symbol}"):
        st.caption(f"{symbol} · saves to the same strategy record and revision history used by Research.")
        target = st.number_input("Target price (0 clears)", min_value=0.0, value=float(saved.get("target_price") or 0), step=0.5)
        stop = st.number_input("Stop / invalidation price (0 clears)", min_value=0.0, value=float(saved.get("invalidation_price") or 0), step=0.5)
        submitted = st.form_submit_button("Save strategy levels", type="primary")
        if submitted:
            target_value, stop_value = target or None, stop or None
            entry_low, entry_high = saved.get("entry_price_low"), saved.get("entry_price_high")
            entry = None
            if entry_low is not None and entry_high is not None:
                if target_value is not None and target_value <= float(entry_high):
                    st.error("Target must be above the upper bound of the planned entry range.")
                    return
                if stop_value is not None and stop_value >= float(entry_low):
                    st.error("Stop must be below the lower bound of the planned entry range.")
                    return
                if target_value is not None and stop_value is not None:
                    setup = calculate_long_setup(target_price=target_value, stop_price=stop_value,
                                                 entry_low=entry_low, entry_high=entry_high)
                    if setup["status"] != "valid":
                        st.error(setup["message"])
                        return
            else:
                entry = float(holding["average_cost"])
                if target_value is not None and stop_value is not None:
                    setup = calculate_long_setup(target_price=target_value, stop_price=stop_value,
                                                 entry_price=entry)
                    if setup["status"] != "valid":
                        st.error(setup["message"])
                        return
                elif target_value is not None and target_value <= entry:
                    st.error("Target must be above the holding's average cost.")
                    return
                elif stop_value is not None and stop_value >= entry:
                    st.error("Stop must be below the holding's average cost.")
                    return
            updated = {**saved, "target_price": target_value, "invalidation_price": stop_value}
            save_strategy_record(symbol, updated)
            st.rerun()
    st.caption("These are planning levels only. A saved stop is not a brokerage order or guaranteed execution price.")


@st.dialog("Simulate position")
def _simulate_position_dialog(holding: dict, quote: dict | None, strategy: dict | None, portfolio_value) -> None:
    symbol = holding["symbol"].upper()
    current = quote.get("price") if quote and not quote_is_stale(quote) else None
    old_value = holding_metrics(holding, quote).get("market_value")
    st.caption(f"{symbol} · hypothetical only · no changes are saved")
    cols = st.columns(3)
    shares = cols[0].number_input("Hypothetical shares", min_value=0.0, value=float(holding["shares"]), step=1.0, format="%.4f", key=f"sim_shares_{symbol}")
    levels = scenario_levels(strategy.get("scenario") if strategy else None)
    target_default = float(levels["target_low"] or 0)
    stop_default = float(levels["stop"] or 0)
    target = cols[1].number_input("Hypothetical target", min_value=0.0, value=target_default, step=1.0, key=f"sim_target_{symbol}")
    stop = cols[2].number_input("Hypothetical stop", min_value=0.0, value=stop_default, step=1.0, key=f"sim_stop_{symbol}")
    result = position_reward_risk(
        shares=shares, entry_price=holding.get("average_cost"), current_price=current,
        target_price=target, stop_price=stop,
    )
    exposure = shares * float(current) if current is not None else None
    simulated_total = (portfolio_value - old_value + exposure
                       if portfolio_value is not None and old_value is not None and exposure is not None else None)
    simulated_alloc = exposure / simulated_total if simulated_total and simulated_total > 0 else None
    current_alloc = old_value / portfolio_value if portfolio_value and old_value is not None else None
    metrics = st.columns(4)
    metrics[0].metric("Position exposure", _money(exposure))
    metrics[1].metric("Remaining RRR", f"{result['remaining_rrr']:.2f}×" if result["remaining_rrr"] is not None else result["message"])
    metrics[2].metric("Potential upside", _money(result["potential_upside_dollars"]))
    metrics[3].metric("Downside to stop", _money(result["potential_downside_to_stop_dollars"]))
    if simulated_alloc is not None:
        delta = (simulated_alloc - current_alloc) * 100 if current_alloc is not None else None
        st.caption(f"Estimated allocation after resizing: {simulated_alloc:.1%}" + (f" · change {delta:+.2f} pp" if delta is not None else ""))
    else:
        st.caption("Allocation unavailable because current valuation is incomplete.")
    if quote:
        st.caption(f"Uses cached quote dated {quote.get('quote_date', 'unknown')}" + (" · stale" if quote_is_stale(quote) else ""))
    if quote and strategy and not strategy.get("quote_basis_compatible", True):
        st.caption(strategy.get("quote_basis_note", "Quote and strategy price basis are incompatible."))
    st.caption("A stop is a trigger, not a guaranteed execution price; gaps can produce larger losses.")


def render() -> None:
    holdings = list_holdings()
    ensure_portfolio_classifications(row["symbol"] for row in holdings)
    quotes = list_cached_quotes()
    cash = get_cash_balance()
    totals = portfolio_totals(holdings, quotes, cash)

    st.header("Portfolio")
    _render_position_policy_settings()
    actions = st.columns([1.0, 1.05, 1.15, 1.25])
    if actions[0].button("＋ Add position", type="primary", use_container_width=True):
        _add_position_dialog()
    with actions[1].popover("✎ Edit cash", use_container_width=True):
        with st.form("cash_balance_form"):
            new_cash = st.number_input("Available cash (USD)", min_value=0.0, value=float(cash), step=100.0)
            if st.form_submit_button("Save cash"):
                save_cash_balance(new_cash)
                st.rerun()
    actions[2].button("↻ Refresh quotes", on_click=_refresh, use_container_width=True)
    lookback = actions[3].selectbox("Risk lookback", (60, 126, 252), index=2, format_func=lambda n: f"{n} trading days", key="portfolio_risk_lookback")

    refresh_result = st.session_state.pop("portfolio_refresh_result", None)
    if refresh_result:
        for symbol, message in refresh_result["errors"].items():
            st.warning(f"{symbol}: {message}")
        if refresh_result.get("updated"):
            st.toast("Saved fresh quotes for " + ", ".join(refresh_result["updated"]))
        holdings, quotes = list_holdings(), list_cached_quotes()
        cash = get_cash_balance()
        totals = portfolio_totals(holdings, quotes, cash)

    active_symbols = [row["symbol"].upper() for row in holdings if float(row["shares"]) > 0]
    history_symbols = list(dict.fromkeys([*active_symbols, "SPY", *SECTOR_SYMBOLS]))
    provider, basis = "tiingo", TiingoDailyProvider.adjustment_basis
    histories = {symbol: load_history(symbol, provider, basis) for symbol in history_symbols}
    risk = portfolio_risk_analysis(holdings, quotes, cash, histories, lookback=lookback)
    strategies = {}
    for holding in holdings:
        symbol = holding["symbol"].upper()
        record = get_strategy_record(symbol)
        strategies[symbol] = resolve_approved_strategy(
            record, list_research_strategy_scenarios(symbol),
            active_horizon=get_portfolio_strategy_horizon(symbol),
            current_price=(quotes.get(symbol) or {}).get("price"),
            current_quote_stale=quote_is_stale(quotes[symbol]) if symbol in quotes else True,
        )
    classifications = list_portfolio_classification_records()
    exposure = sector_exposure(holdings, quotes, cash, classifications)
    _render_portfolio_plan_warnings(holdings, quotes, totals, cash, classifications, exposure)

    st.markdown("#### Portfolio health")
    health = st.columns(4)
    health[0].metric("Total portfolio value", _money(totals["total_value"]))
    health[1].metric("Cash allocation", f"{totals['cash_allocation']:.1%}" if totals["cash_allocation"] is not None else "Unavailable")
    health[2].metric("Daily P/L", _money(totals["daily_pl"]))
    health[3].metric("Unrealized P/L", _money(totals["unrealized_pl"]))
    health2 = st.columns(4)
    partial_mark = "~" if risk["status"] == "partial" else ""
    health2[0].metric("Portfolio beta vs SPY", f"{partial_mark}{risk['portfolio_beta']:.2f}" if risk["portfolio_beta"] is not None else "Unavailable")
    health2[1].metric("Invested-equity beta", f"{partial_mark}{risk['invested_equity_beta']:.2f}" if risk["invested_equity_beta"] is not None else "Unavailable")
    health2[2].metric("Annualized volatility", f"{partial_mark}{risk['annualized_volatility']:.1%}" if risk["annualized_volatility"] is not None else "Unavailable")
    top_three = _top_three_concentration(holdings, quotes, totals["total_value"])
    health2[3].metric("Top 3 concentration", f"{top_three:.1%}" if top_three is not None else "Unavailable")
    stop_coverage = _approved_stop_coverage(holdings, quotes, strategies)
    health3 = st.columns(2)
    health3[0].metric("Estimated downside to stops", _money(stop_coverage["downside"]))
    health3[1].metric("Invested value with approved stops",
                      f"{stop_coverage['coverage']:.1%}" if stop_coverage["coverage"] is not None else "Unavailable",
                      help="Priced invested value backed by an approved numeric stop. Downside estimate excludes stale quotes.")
    st.caption(f"Historical simulation · cached quote weights where available, aligned Tiingo close fallback · {lookback}-session lookback · cash modeled at zero return · not realized account performance.")
    if risk["status"] == "insufficient_history":
        st.caption(f"Risk estimates unavailable: {risk['observations']}/{lookback} aligned observations; at least 2 are needed to estimate covariance.")
    elif risk["status"] == "partial":
        missing = risk.get("missing_history_symbols", [])
        detail = " Missing history: " + ", ".join(missing) + "." if missing else ""
        st.caption(f"Partial coverage estimate · {risk['covered_positions']}/{risk['position_count']} positions included · {risk['observations']}/{lookback} observations. Missing data is excluded, never treated as zero-return exposure.{detail}")
    elif risk["status"] == "cash_only":
        st.caption("Cash-only portfolio: modeled beta and volatility are 0; invested-equity beta is unavailable.")
    if totals["missing_prices"]:
        st.caption(f"Partial valuation: {totals['missing_prices']} holding quote(s) missing. Portfolio total and total-value percentages are unavailable.")
    if totals["missing_changes"]:
        st.caption(f"Daily P/L unavailable for {totals['missing_changes']} holding(s) because cached daily-change data is missing.")
    stale_quotes = [symbol for symbol, quote in quotes.items() if quote_is_stale(quote)]
    if stale_quotes:
        st.caption("Stale cached quotes are marked in the position monitor; historical risk weights use aligned Tiingo closes instead: " + ", ".join(stale_quotes))
    if risk.get("aligned_dates"):
        risk_as_of = risk["aligned_dates"][-1]
        try:
            history_stale = (date.today() - date.fromisoformat(risk_as_of)).days > 4
        except ValueError:
            history_stale = True
        st.caption(f"Aligned Tiingo return window ends {risk_as_of} · adjusted-close historical estimates, not live risk measures." + (" **Cached history is stale.**" if history_stale else ""))

    _render_history_status(holdings, histories, lookback, risk)
    _render_sector_exposure(holdings, exposure)
    _render_rotation_connection(histories, exposure, classifications, holdings, quotes)
    monitor_settings = _render_monitor_settings()
    _render_position_monitor(holdings, quotes, strategies, totals, monitor_settings, classifications)
    _render_downside_summary(holdings, quotes, strategies)
    _render_sector_classification_editor(holdings, classifications)
    _render_risk_attribution(holdings, risk, histories, lookback)

    with st.expander("Manual market-risk rules"):
        market_risk.render(show_title=False)


def _render_history_status(holdings, histories, lookback, risk):
    symbols = list(dict.fromkeys([*(row["symbol"].upper() for row in holdings if float(row["shares"]) > 0), "SPY"]))
    required_closes = lookback + 1
    status_rows = []
    missing = []
    for symbol in symbols:
        bars = histories.get(symbol, [])
        latest = max((str(row.get("trading_date", "")) for row in bars), default="—")
        status_rows.append({"Ticker": symbol, "Cached adjusted closes": len(bars), "Latest date": latest,
                            "Status": "Ready" if len(bars) >= required_closes else "History needed"})
        if len(bars) < required_closes:
            missing.append(symbol)
    if risk.get("observations", 0) < lookback and not missing and symbols:
        # The refresh service checks stale tails and internal holes before it
        # contacts Tiingo, so this can repair alignment without forcing requests.
        missing = symbols
    with st.expander("Historical risk data coverage and refresh"):
        st.caption(f"Provider: Tiingo · split and dividend adjusted daily closes · {required_closes} close observations are needed for {lookback} returns. Viewing this section makes no network requests.")
        st.dataframe(status_rows, width="stretch", hide_index=True)
        if missing:
            st.warning(("Historical data needed for: " if any(len(histories.get(s, [])) < required_closes for s in missing) else "Common-date history may need repair for: ") + ", ".join(missing))
            if st.button(f"Check / fetch history for {len(missing)} symbol(s)", key="portfolio_fetch_missing_history"):
                try:
                    provider = TiingoDailyProvider.from_environment()
                    result = refresh_historical_data(missing, provider)
                except Exception as error:
                    result = {"errors": {"Refresh": str(error)}, "updated": {}, "cached": [], "limited": False}
                st.session_state["portfolio_history_refresh_result"] = result
                st.rerun()
        else:
            st.caption("Every position and SPY has enough individual cached closes for this lookback. Date alignment may still reduce the shared observation count.")
        result = st.session_state.get("portfolio_history_refresh_result")
        if result:
            for symbol, message in result.get("errors", {}).items():
                st.error(f"{symbol}: {message}")
            updated = result.get("updated", {})
            if updated:
                st.success("Saved Tiingo history: " + ", ".join(f"{symbol} ({count} rows)" for symbol, count in updated.items()))
            if result.get("limited"):
                st.warning("Provider request limits paused the refresh. Existing cached history remains available.")
            if not updated and not result.get("errors"):
                st.info("The selected histories were already current in SQLite.")


def _top_three_concentration(holdings, quotes, total_value):
    if total_value is None or total_value <= 0:
        return None
    values = []
    for holding in holdings:
        value = holding_metrics(holding, quotes.get(holding["symbol"].upper()))["market_value"]
        if value is None:
            return None
        values.append(value)
    return sum(sorted(values, reverse=True)[:3]) / total_value


def _render_sector_exposure(holdings, exposure):
    st.markdown("#### Sector exposure")
    if not holdings:
        st.caption("No positions yet; cash is shown separately.")
    rows = exposure["rows"]
    categories = [row["category"] for row in rows]
    figure = go.Figure()
    figure.add_trace(go.Bar(
        y=categories,
        x=[row["pct_total_portfolio"] * 100 if row["pct_total_portfolio"] is not None else None for row in rows],
        name="% of total portfolio",
        customdata=[[row["value"], ", ".join(row["symbols"])] for row in rows],
        orientation="h", marker_color="#66b3a1",
        hovertemplate="%{y}<br>%{x:.1f}% of total portfolio<br>Value $%{customdata[0]:,.2f}<br>Holdings: %{customdata[1]}<extra></extra>",
    ))
    figure.add_trace(go.Bar(
        y=categories,
        x=[row["pct_invested_equity"] * 100 if row["pct_invested_equity"] is not None else None for row in rows],
        name="% of invested equity",
        customdata=[[row["value"], ", ".join(row["symbols"])] for row in rows],
        orientation="h", marker_color="#7596d2",
        hovertemplate="%{y}<br>%{x:.1f}% of priced invested equity<br>Value $%{customdata[0]:,.2f}<br>Holdings: %{customdata[1]}<extra></extra>",
    ))
    figure.update_layout(
        template="plotly_dark", barmode="group", height=max(320, 56 * len(rows)),
        margin=dict(l=5, r=10, t=50, b=10), xaxis_title="Allocation (%)",
        xaxis={"showgrid": True, "gridcolor": "rgba(150,160,175,.14)"},
        yaxis={"autorange": "reversed"}, legend={"orientation": "h", "y": 1.12},
    )
    apply_plotly_theme(figure)
    event = st.plotly_chart(figure, width="stretch", on_select="rerun", selection_mode="points", config={"displayModeBar": False}, key="portfolio_sector_exposure_chart")
    selected_from_chart = None
    try:
        points = event.selection.points
        if points:
            selected_from_chart = points[-1].get("y")
    except (AttributeError, KeyError, TypeError):
        pass
    if selected_from_chart in categories:
        st.session_state["portfolio_sector_detail"] = selected_from_chart
    current_selection = st.session_state.get("portfolio_sector_detail")
    selected_default = current_selection if current_selection in categories else categories[0]
    if st.session_state.get("portfolio_sector_detail") not in categories:
        st.session_state["portfolio_sector_detail"] = selected_default
    selected_category = st.selectbox("Inspect sector holdings", categories, index=categories.index(selected_default), key="portfolio_sector_detail")
    selected_row = next(row for row in rows if row["category"] == selected_category)
    if selected_row["symbols"]:
        st.caption(f"{selected_category}: " + " · ".join(selected_row["symbols"]))
    else:
        st.caption("Cash is not invested equity and has no underlying tickers.")
    if exposure["missing_symbols"]:
        st.caption("Exposure includes priced holdings only: " + ", ".join(exposure["missing_symbols"]) + " lack a current quote.")
    coverage = exposure.get("verified_sector_classification_coverage")
    st.caption((f"Verified sector-classification coverage: {coverage:.1%} of priced invested value." if coverage is not None else "Verified sector-classification coverage is unavailable.") + " Diversified ETFs remain separate unless verified holdings look-through is entered; no fund look-through feed is connected.")


def _render_rotation_connection(histories, exposure, classifications, holdings, quotes):
    st.markdown("#### Market rotation connection")
    rotation = sector_rotation_points(histories, SECTOR_SYMBOLS, strength_window=20, momentum_window=5)
    if not rotation:
        st.caption("No rotation map: cached adjusted closes do not provide enough aligned history for the standard sector ETFs and SPY.")
        return
    # Overlay only manually verified or verified look-through sector assignments.
    value_by_sector = {}
    for holding in holdings:
        symbol = holding["symbol"].upper()
        classification = classifications.get(symbol, {})
        benchmark = classification.get("sector_benchmark")
        user_approved = (classification.get("verification_status") == "Verified"
                         or classification.get("classification_source") == "Manual")
        if not user_approved or benchmark not in SECTOR_SYMBOLS:
            continue
        value = holding_metrics(holding, quotes.get(symbol)).get("market_value")
        if value is not None:
            label = SECTOR_SYMBOLS[benchmark]
            value_by_sector[label] = value_by_sector.get(label, 0.0) + value
    total = exposure["total_value"]
    figure = go.Figure()
    colors = {"Leading": "#35b779", "Weakening": "#e3ad4f", "Lagging": "#e05a67", "Improving": "#57a6d8"}
    for quadrant in ("Leading", "Weakening", "Lagging", "Improving"):
        points = [row for row in rotation if row["quadrant"] == quadrant]
        if not points:
            continue
        sizes, customdata = [], []
        for point in points:
            value = value_by_sector.get(point["name"], 0.0)
            weight = value / total * 100 if total and total > 0 else None
            sizes.append(min(46, 12 + (weight or 0) * 2.2))
            customdata.append([point["name"], point["trading_date"], weight])
        figure.add_trace(go.Scatter(
            x=[row["relative_strength"] * 100 for row in points],
            y=[row["relative_momentum"] * 100 for row in points],
            text=[row["symbol"] for row in points], mode="markers+text", name=quadrant,
            textposition="top center", customdata=customdata,
            marker={"size": sizes, "color": colors[quadrant], "opacity": .88,
                    "line": {"width": [2.5 if item[2] is not None and item[2] >= 5 else .5 for item in customdata],
                             "color": ["#f5ca68" if item[2] is not None and item[2] >= 5 else colors[quadrant] for item in customdata]}},
            hovertemplate="%{text} · %{customdata[0]}<br>As of %{customdata[1]}<br>Relative strength %{x:.2f}%<br>Relative momentum change %{y:.2f} pp<br>Portfolio exposure %{customdata[2]:.1f}%<extra></extra>",
        ))
    figure.add_hline(y=0, line_dash="dot", line_color="#8b929d")
    figure.add_vline(x=0, line_dash="dot", line_color="#8b929d")
    max_x = max([abs(row["relative_strength"] * 100) for row in rotation] + [.25]) * 1.25
    max_y = max([abs(row["relative_momentum"] * 100) for row in rotation] + [.25]) * 1.4
    for label, x_sign, y_sign in (("Improving", -1, 1), ("Leading", 1, 1),
                                  ("Lagging", -1, -1), ("Weakening", 1, -1)):
        figure.add_annotation(x=x_sign * max_x * .9, y=y_sign * max_y * .9,
                              text=label, showarrow=False,
                              font={"color": PLOTLY_MUTED_COLOR, "size": 14})
    figure.update_layout(
        template="plotly_dark", height=490, margin=dict(l=10, r=10, t=58, b=16),
        xaxis_title="Relative strength · 20D sector/SPY ratio return (%)",
        yaxis_title="Relative momentum · 5D change in relative strength (pp)",
        xaxis={"range": [-max_x, max_x], "zeroline": False, "showgrid": True, "gridcolor": "rgba(150,160,175,.14)"},
        yaxis={"range": [-max_y, max_y], "zeroline": False, "showgrid": True, "gridcolor": "rgba(150,160,175,.14)"},
        legend={"orientation": "h", "y": 1.12},
    )
    apply_plotly_theme(figure)
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    exposed_weak = [
        row for row in rotation if row["quadrant"] in ("Weakening", "Lagging")
        and total and value_by_sector.get(row["name"], 0) / total >= .05
    ]
    if exposed_weak:
        st.warning("At least 5% portfolio exposure is in weakening/lagging sectors: " + ", ".join(row["symbol"] for row in exposed_weak))
    st.caption(("Bubble size reflects verified portfolio exposure to the corresponding sector ETF benchmark." if value_by_sector else "No verified sector benchmark assignments are available for overlay sizing; circles use equal sizes.") + " Relative momentum is a contextual historical signal, not a prediction.")


def _format_rrr(value):
    if not value or value[0] is None or value[1] is None:
        return "Unavailable"
    low, high = value
    return f"{low:.2f}×" if abs(low - high) < 1e-9 else f"{low:.2f}–{high:.2f}×"


def _format_pct_range(value):
    return format_return_range(value)


def _render_monitor_settings():
    saved = get_portfolio_monitor_settings()
    saved["remaining_rrr_below"] = get_position_planner_policy()["rrr_monitoring_threshold"]
    with st.expander("Position alert thresholds"):
        with st.form("portfolio_monitor_alert_settings"):
            columns = st.columns(2)
            stop_pct = columns[0].number_input("Near stop (%)", 0.0, 50.0, float(saved["stop_near_pct"]), .5)
            target_pct = columns[1].number_input("Near target (%)", 0.0, 50.0, float(saved["target_near_pct"]), .5)
            if st.form_submit_button("Save thresholds"):
                save_portfolio_monitor_settings(stop_pct, target_pct, saved["remaining_rrr_below"])
                st.rerun()
        st.caption("Alerts are informational; they never change strategy levels or place orders.")
    return saved


def _strategy_levels_changed(symbol, horizon):
    if not horizon:
        return False
    history = list_research_strategy_scenarios(symbol, history=True)
    versions = [row for row in history if row.get("horizon") == horizon]
    if len(versions) >= 2:
        now, before = (scenario_levels(row["scenario"]) for row in versions[:2])
        return any(now[key] != before[key] for key in ("target_low", "target_high", "stop"))
    legacy = list_strategy_revisions(symbol)
    return (len(legacy) >= 2 and any(legacy[0].get(field) != legacy[1].get(field)
                                    for field in ("target_price", "invalidation_price")))


def _render_position_monitor(holdings, quotes, strategies, totals, settings, classifications):
    heading = st.columns([4, 1])
    heading[0].markdown("#### Holdings")
    sort_mode = heading[1].selectbox(
        "Sort holdings", ("Custom Order", "Position Size", "Ticker"),
        index=0, key="portfolio_holding_sort_mode", label_visibility="collapsed",
    )
    if sort_mode == "Position Size":
        st.caption("Diversified ETFs are grouped last; positions within each group are ordered by market value.")
    if not holdings:
        st.caption("No positions yet. Add a position to start tracking your portfolio.")
        return
    custom_order = list_portfolio_position_order()
    ordered_holdings = order_holdings(holdings, sort_mode, quotes=quotes,
                                      custom_order=custom_order, classifications=classifications)
    alerts = {}
    for holding in holdings:
        symbol = holding["symbol"].upper()
        quote = quotes.get(symbol)
        approved = strategies.get(symbol) or {}
        alerts[symbol] = monitor_alerts(
            holding, approved, quote.get("price") if quote else None,
            stale=(quote_is_stale(quote) if quote else True) or not approved.get("quote_basis_compatible", True),
            stop_near_pct=settings["stop_near_pct"], target_near_pct=settings["target_near_pct"],
            remaining_rrr_limit=settings["remaining_rrr_below"],
            levels_changed=_strategy_levels_changed(symbol, approved.get("horizon")),
        )
    active_alerts = [f"{symbol}: {', '.join(items)}" for symbol, items in alerts.items() if items]
    if active_alerts:
        st.warning("Position alerts · " + "  |  ".join(active_alerts))
    else:
        st.caption("No active price or thesis alerts.")

    left, right = st.columns(2)
    for i, holding in enumerate(ordered_holdings):
        symbol = holding["symbol"].upper()
        quote, approved = quotes.get(symbol), strategies.get(symbol) or {"available": False}
        with (left if i % 2 == 0 else right).container(border=True):
            head = st.columns([1.1, .28, .28, 1] if sort_mode == "Custom Order" else [1.15, 1])
            name = symbol + (f" · {approved['company_name']}" if approved.get("company_name") else "")
            head[0].markdown(f"### {name}")
            if sort_mode == "Custom Order":
                if head[1].button("↑", key=f"position_up_{symbol}", disabled=i == 0, help="Move up"):
                    move_portfolio_position(symbol, -1)
                    st.rerun()
                if head[2].button("↓", key=f"position_down_{symbol}", disabled=i == len(ordered_holdings) - 1, help="Move down"):
                    move_portfolio_position(symbol, 1)
                    st.rerun()
                research_col = head[3]
            else:
                research_col = head[1]
            if research_col.button("Open research", key=f"portfolio_open_{holding['id']}", use_container_width=True):
                activate_research_workspace(st.session_state, symbol)
                st.rerun()

            mark = holding_metrics(holding, quote)
            current = quote.get("price") if quote else None
            stale = quote_is_stale(quote) if quote else True
            weight = mark["market_value"] / totals["total_value"] if mark["market_value"] is not None and totals["total_value"] else None
            pnl_pct = mark["unrealized_pl"] / mark["cost_basis"] if mark["unrealized_pl"] is not None and mark["cost_basis"] else None
            summary = st.columns(3)
            summary[0].markdown(f"**{holding['shares']:g} shares**\n\nAvg {_money(holding['average_cost'])}")
            summary[1].markdown(f"**{_money(current)}**\n\n{quote.get('quote_date', 'date unavailable') if quote else 'No cached quote'}" + (" · :orange[stale]" if stale and quote else ""))
            summary[2].markdown(f"**Value {_money(mark['market_value'])}**\n\nWeight {f'{weight:.1%}' if weight is not None else 'Unavailable'}")
            status = approved.get("thesis_status") or holding.get("thesis_status", "Unavailable")
            color = {"Active": "blue", "Review Needed": "orange", "Invalidated": "red"}.get(status, "gray")
            st.markdown(f"P/L {_signed_money(mark['unrealized_pl'])} · {_signed_pct(pnl_pct)}  |  Thesis :{color}[**{status}**]")

            if approved.get("requires_selection"):
                choices = approved["available_horizons"]
                selected = st.selectbox("Select active approved horizon", choices, index=None,
                                        placeholder="Choose a horizon", key=f"active_horizon_{symbol}")
                if selected and st.button("Use selected horizon", key=f"save_horizon_{symbol}"):
                    save_portfolio_strategy_horizon(symbol, selected)
                    st.rerun()
                st.info("Several approved horizons exist without a saved primary match. Choose one; Portfolio will not guess.")
                continue

            scenario = approved.get("scenario")
            if approved.get("available") and scenario:
                choices = approved["available_horizons"]
                horizon = approved.get("horizon")
                if len(choices) > 1:
                    selected = st.selectbox("Active strategy", choices, index=choices.index(horizon), key=f"active_horizon_{symbol}")
                    if selected != horizon:
                        save_portfolio_strategy_horizon(symbol, selected)
                        st.rerun()
                levels = scenario_levels(scenario)
                detail = st.columns(4)
                detail[0].caption("Horizon")
                detail[0].markdown(f"**{horizon}**")
                detail[1].caption("Target")
                detail[1].markdown(f"**{target_range_text(scenario)}**")
                detail[2].caption("Stop / invalidation")
                detail[2].markdown(f"**{_money(levels['stop'])}**")
                detail[3].caption("Strategy status")
                detail[3].markdown(f"**{approved['analysis']['status']}**")
                calc = approved["analysis"]
                metrics = st.columns(3)
                metrics[0].metric("Planned RRR", _format_rrr(calc.get("rrr_range")))
                compatible = approved.get("quote_basis_compatible", True)
                metrics[1].metric("Remaining RRR", _format_rrr(calc.get("remaining_range")) if not stale and compatible else "Stale quote" if stale else "Price basis mismatch")
                metrics[2].markdown(f"Upside {_format_pct_range(calc.get('upside_percent'))}\n\nDownside {_format_pct_range(calc.get('downside_percent'))}")
                flags = (["Price basis mismatch"] if not compatible else price_position_status(
                    current, scenario, stale=stale, stop_near_pct=settings["stop_near_pct"],
                    target_near_pct=settings["target_near_pct"]))
                if flags:
                    colors = {"Stop breached": "red", "Target reached": "red", "Near stop": "orange",
                              "Near target": "orange", "Stale quote": "orange", "Quote unavailable": "gray",
                              "Price basis mismatch": "gray"}
                    st.markdown(" · ".join(f":{colors.get(flag, 'blue')}[{flag}]" for flag in flags))
                if (levels["stop"] is not None and levels["target_high"] is not None and current is not None
                        and not stale and compatible and levels["target_high"] > levels["stop"]):
                    position = max(0.0, min(1.0, (float(current) - levels["stop"]) / (levels["target_high"] - levels["stop"])))
                    st.progress(position, text="Price location from stop to upper target · distance only, not probability")
                if alerts[symbol]:
                    st.caption("Alerts · " + " · ".join(alerts[symbol]))
                _render_position_plan_summary(holding, quote, approved, totals, cash=get_cash_balance(), classifications=classifications)
                with st.expander("Approved strategy and risk detail"):
                    if approved.get("investment_thesis"):
                        st.write(approved["investment_thesis"])
                    st.caption(f"Entry {_format_entry(scenario)} · Target {target_range_text(scenario)} · Stop {_money(levels['stop'])}")
                    if calc.get("missing"):
                        st.caption("Missing for complete RRR: " + ", ".join(calc["missing"]))
                    if approved.get("quote_basis_note"):
                        st.caption(approved["quote_basis_note"])
                    if current is not None and not stale and compatible and levels["target_low"] is not None and levels["stop"] is not None:
                        shares = float(holding["shares"])
                        rewards = [shares * (target - float(current)) for target in {levels["target_low"], levels["target_high"]}]
                        downside = max(0.0, shares * (float(current) - levels["stop"]))
                        st.caption(f"Potential at {shares:g} shares: upside {_money(min(rewards))}–{_money(max(rewards))} · current-price-to-stop scenario {_money(downside)}.")
                    st.caption("Stops are planning triggers, not guaranteed execution prices; gaps can exceed the estimate.")
                action = st.columns(2)
                if action[0].button("Simulate", key=f"simulate_{symbol}", use_container_width=True):
                    _simulate_position_dialog(holding, quote, approved, totals["total_value"])
                with action[1].popover("⋯ Position actions", use_container_width=True):
                    if st.button("Edit holding", key=f"edit_open_{holding['id']}"):
                        _edit_position_dialog(symbol)
                    if st.button("Delete holding", key=f"delete_open_{holding['id']}"):
                        delete_holding(symbol)
                        st.rerun()
            else:
                st.info("No approved strategy data available. Unapproved drafts are never used in Portfolio.")


def _format_entry(scenario):
    levels = scenario_levels(scenario)
    low, high = levels["entry_low"], levels["entry_high"]
    if low is None or high is None:
        return "not supplied"
    return f"${low:,.2f}" if low == high else f"${low:,.2f}–${high:,.2f}"


def _render_position_policy_settings():
    """User-wide policy defaults; changes do not rewrite plans or holdings."""
    policy = get_position_planner_policy()
    with st.expander("Position-planning policy defaults", expanded=False):
        st.caption("These are user policy limits. Changing them affects future capacity calculations only; approved plans and holdings stay unchanged.")
        with st.form("position_planner_policy_form"):
            columns = st.columns(3)
            max_allocation = columns[0].number_input("Maximum single position (%)", 0.0, 100.0, float(policy["max_position_allocation"]) * 100, 1.0) / 100
            target_allocation = columns[1].number_input("Default target allocation (%)", 0.0, 100.0, float(policy["target_position_allocation"]) * 100, 1.0) / 100
            max_loss = columns[2].number_input("Maximum planned loss / position (%)", 0.0, 100.0, float(policy["max_loss_per_position"]) * 100, .25) / 100
            minimum_reserve = columns[0].number_input("Minimum cash reserve ($)", min_value=0.0, value=float(policy["minimum_cash_reserve"]), step=100.0)
            sector_ceiling = columns[1].number_input("Maximum sector allocation (%)", 0.0, 100.0, float(policy["max_sector_allocation"]) * 100, 1.0) / 100
            rrr_limit = columns[2].number_input("RRR monitoring threshold", min_value=0.0, value=float(policy["rrr_monitoring_threshold"]), step=.1)
            if st.form_submit_button("Save policy defaults"):
                if target_allocation > max_allocation:
                    st.error("Default target allocation cannot exceed the maximum position allocation.")
                else:
                    save_position_planner_policy({"max_position_allocation": max_allocation,
                        "target_position_allocation": target_allocation, "max_loss_per_position": max_loss,
                        "minimum_cash_reserve": minimum_reserve, "max_sector_allocation": sector_ceiling,
                        "rrr_monitoring_threshold": rrr_limit})
                    st.rerun()


def _render_position_plan_summary(holding, quote, approved, totals, *, cash, classifications):
    symbol = holding["symbol"].upper()
    horizon = approved.get("horizon")
    plan_record = get_position_plan(symbol, horizon) if horizon else None
    plan = (plan_record or {}).get("plan")
    if not plan:
        st.caption("Position plan · **Incomplete** · approve a plan in Research to see allocation and tranche capacity.")
        return
    latest_revision = latest_strategy_revision_id(symbol)
    if plan_record.get("strategy_revision_id") != latest_revision:
        st.warning("Position plan needs review: its linked approved strategy revision has changed. Review and approve an updated plan in Research before using its capacity figures.")
        return
    stale = quote_is_stale(quote) if quote else True
    mark = holding_metrics(holding, quote)
    current_value = mark.get("market_value")
    status = "Gray · quote unavailable" if current_value is None else "Amber · cached quote is stale" if stale else "Green · within plan limits"
    if totals.get("total_value") is None or current_value is None:
        st.caption(f"Position plan · {status} · allocation capacity unavailable until all holdings are priced.")
        return
    entry = plan.get("entry_reference")
    stop = plan.get("stop_price")
    target = plan.get("target_reference")
    classification = classifications.get(symbol, {})
    sector = classification.get("sector") if classification.get("verification_status") == "Verified" else None
    sector_value = 0.0
    if sector:
        for item in list_holdings():
            other = classifications.get(item["symbol"].upper(), {})
            other_quote = list_cached_quotes().get(item["symbol"].upper())
            if other.get("verification_status") == "Verified" and other.get("sector") == sector and other_quote:
                sector_value += float(item["shares"]) * float(other_quote["price"])
    result = calculate_position_capacity(
        portfolio_equity=totals["total_value"], current_market_value=current_value,
        current_shares=holding["shares"], average_cost=holding["average_cost"], cash=cash,
        entry_price=entry, stop_price=stop, target_price=target,
        target_allocation=plan.get("target_allocation"), max_allocation=plan.get("max_allocation"),
        max_risk_fraction=plan.get("max_loss_per_position"),
        minimum_cash_reserve=plan.get("minimum_cash_reserve", 0), sector_value=sector_value,
        sector_ceiling=plan.get("max_sector_allocation") if sector else None,
        current_price=(quote or {}).get("price"), quote_stale=stale,
        price_basis_compatible=approved.get("quote_basis_compatible", True),
        position_type=plan.get("position_type", "long_equity"),
    )
    if result.get("status") != "valid":
        st.caption(f"Position plan · **Incomplete** · {result.get('message')}")
        return
    if result.get("allocation_excess_dollars", 0) > 0 or (result.get("total_stop_risk_dollars") is not None and result["total_stop_risk_dollars"] > (result.get("risk_budget_dollars") or float("inf"))):
        status = "Red · approved limit exceeded"
    elif stale:
        status = "Amber · cached quote is stale"
    risk = result.get("total_stop_risk_dollars")
    columns = st.columns(4)
    columns[0].caption(f"Plan · {horizon}\n\nCurrent {result['current_total_allocation']:.1%} · target {result['target_allocation']:.1%} · max {result['maximum_allocation']:.1%}")
    columns[1].caption(f"Allocation gap\n\n${(result.get('target_gap_dollars') or 0):,.0f} · {result['additional_permitted_shares']} more shares permitted")
    risk_text = (f"${risk:,.0f} / {result['stop_risk_portfolio_fraction']:.2%} equity"
                 if risk is not None else "No numeric stop")
    columns[2].caption(f"Stop-risk exposure\n\n{risk_text}")
    next_buy = next((row for row in sorted(plan.get("entry_tranches", []), key=lambda x: x.get("sequence", 0)) if row.get("status", "planned") in {"planned", "eligible"}), None)
    next_sell = next((row for row in sorted(plan.get("exit_tranches", []), key=lambda x: x.get("sequence", 0)) if row.get("status", "planned") in {"planned", "eligible"}), None)
    next_text = "No pending tranche"
    if next_buy:
        trigger = float(next_buy.get("trigger_price") or entry)
        next_text = f"Buy stage · {next_buy.get('fraction', 0):.0%} · ${trigger:,.2f}"
        if quote and not stale and float(quote["price"]) <= trigger:
            next_text += " · price trigger reached; review conditions"
    elif next_sell:
        next_text = f"Sell stage · {next_sell.get('percent', 0):.0%} · trigger {next_sell.get('condition') or next_sell.get('trigger_price') or 'manual review'}"
    columns[3].caption(f"Next stage\n\n{next_text}\n\n{status}")
    tranche_options = [(str(row.get("id") or f"buy-{i+1}"), "buy", row)
                       for i, row in enumerate(plan.get("entry_tranches", []))
                       if row.get("status", "planned") not in {"canceled", "skipped"}]
    tranche_options += [(str(row.get("id") or f"sell-{i+1}"), "sell", row)
                        for i, row in enumerate(plan.get("exit_tranches", []))
                        if row.get("status", "planned") not in {"canceled", "skipped"}]
    if tranche_options:
        with st.popover("Record actual fill"):
            st.caption("Records an actual fill separately. This does not change holdings or cash; reconcile your manual portfolio position after recording.")
            labels = [f"{action.upper()} · {row.get('id') or tranche_id}" for tranche_id, action, row in tranche_options]
            selected_index = st.selectbox("Plan stage", range(len(labels)), format_func=lambda index: labels[index], key=f"plan_fill_stage_{symbol}")
            selected_id, action, selected_tranche = tranche_options[selected_index]
            with st.form(f"plan_actual_fill_form_{symbol}"):
                fill_shares = st.number_input("Actual filled shares", min_value=0.0, step=1.0, format="%.4f")
                fill_price = st.number_input("Actual fill price", min_value=0.0, step=.01, format="%.2f")
                fill_date = st.date_input("Fill date")
                notes = st.text_input("Execution note (optional)")
                confirmed = st.checkbox("I confirm this action was actually executed")
                if st.form_submit_button("Record fill"):
                    try:
                        record_position_plan_execution(plan_record["id"], symbol, selected_id,
                            action, fill_shares, fill_price, fill_date.isoformat(), notes,
                            confirmed=confirmed)
                        st.success("Actual fill recorded in the plan audit. Holdings and cash remain unchanged until reconciled.")
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
        executions = list_position_plan_executions(symbol)
        if executions:
            with st.expander(f"Recorded plan fills · {len(executions)}"):
                st.dataframe(executions, hide_index=True, use_container_width=True)
    if result.get("warnings"):
        st.caption("Plan warnings · " + " · ".join(result["warnings"]))
    stages = plan_entry_tranches(plan.get("entry_tranches", []), capacity_result=result,
                                 entry_price=float(entry), stop_price=stop)
    if stages.get("tranches"):
        first = next((row for row in stages["tranches"] if row.get("status") not in {"canceled", "skipped", "filled"}), None)
        if first:
            st.caption(f"First buy tranche capacity: {first['planned_shares']} shares, up to ${first['maximum_capital_commitment']:,.2f}. Conditional confirmations are not machine-verified.")
    if plan.get("exit_tranches"):
        exit_path = simulate_exit_tranches(plan["exit_tranches"], current_shares=holding["shares"],
            current_price=(quote or {}).get("price"), stop_price=stop,
            portfolio_equity=totals.get("total_value"))
        st.caption(f"Sequential sell plan leaves {exit_path['remaining_shares']:g} shares across all planned stages. Price trigger estimates carry gap risk and are not guaranteed fills.")
    with st.expander("Simulate position-plan changes", expanded=False):
        st.caption("Hypothetical only. Changes here are not saved to the approved plan or actual holdings.")
        controls = st.columns(3)
        sim_entry = controls[0].number_input("Entry reference", min_value=0.0, value=float(entry), step=.5, key=f"plan_sim_entry_{symbol}")
        sim_stop = controls[1].number_input("Stop reference", min_value=0.0, value=float(stop or 0), step=.5, key=f"plan_sim_stop_{symbol}")
        sim_target_alloc = controls[2].number_input("Target allocation (%)", min_value=0.0, max_value=100.0, value=float(plan.get("target_allocation", 0)) * 100, step=1.0, key=f"plan_sim_alloc_{symbol}") / 100
        simulated = calculate_position_capacity(
            portfolio_equity=totals["total_value"], current_market_value=current_value,
            current_shares=holding["shares"], average_cost=holding["average_cost"], cash=cash,
            entry_price=sim_entry, stop_price=sim_stop or None, target_price=target,
            target_allocation=sim_target_alloc, max_allocation=plan.get("max_allocation"),
            max_risk_fraction=plan.get("max_loss_per_position"),
            minimum_cash_reserve=plan.get("minimum_cash_reserve", 0), sector_value=sector_value,
            sector_ceiling=plan.get("max_sector_allocation") if sector else None,
            current_price=(quote or {}).get("price"), quote_stale=stale,
            price_basis_compatible=approved.get("quote_basis_compatible", True),
            position_type=plan.get("position_type", "long_equity"),
        )
        if simulated.get("status") == "valid":
            items = st.columns(4)
            items[0].metric("Maximum total shares", f"{simulated['maximum_total_shares']:g}")
            items[1].metric("Additional shares", f"{simulated['additional_permitted_shares']:g}")
            items[2].metric("Capital required", _money(simulated["additional_capital_required"]))
            items[3].metric("Stop-risk scenario", _money(simulated["total_stop_risk_dollars"]))
            st.caption(f"Binding constraint: {simulated['binding_constraint']} · allocation after purchase {simulated['allocation_after_purchase']:.1%} · remaining deployable cash {_money(simulated['remaining_buying_capacity'])}.")
        else:
            st.warning(simulated.get("message", "Simulation inputs are incomplete."))


def _render_portfolio_plan_warnings(holdings, quotes, totals, cash, classifications, exposure):
    policy = get_position_planner_policy()
    warnings = []
    if cash < policy["minimum_cash_reserve"]:
        warnings.append(f"Cash ${cash:,.0f} is below the ${policy['minimum_cash_reserve']:,.0f} minimum reserve.")
    if totals.get("total_value"):
        for holding in holdings:
            value = holding_metrics(holding, quotes.get(holding["symbol"].upper())).get("market_value")
            if value is not None and value / totals["total_value"] > policy["max_position_allocation"]:
                warnings.append(f"{holding['symbol']} is above the {policy['max_position_allocation']:.0%} single-position policy limit.")
    sector_totals = {}
    for holding in holdings:
        symbol = holding["symbol"].upper()
        classification = classifications.get(symbol, {})
        quote = quotes.get(symbol)
        if (classification.get("verification_status") == "Verified" and quote
                and classification.get("sector")):
            sector_totals[classification["sector"]] = sector_totals.get(classification["sector"], 0.0) + float(holding["shares"]) * float(quote["price"])
    if totals.get("total_value"):
        for sector, value in sector_totals.items():
            if value / totals["total_value"] > policy["max_sector_allocation"]:
                warnings.append(f"Verified {sector} exposure is above the {policy['max_sector_allocation']:.0%} sector policy limit.")
    if warnings:
        st.warning("Position-planning policy review · " + " ".join(warnings))


def _render_downside_summary(holdings, quotes, strategies):
    flat, fresh = {}, {}
    for symbol, approved in strategies.items():
        flat[symbol] = {"invalidation_price": scenario_levels(approved.get("scenario"))["stop"]}
        quote = quotes.get(symbol)
        if quote and not quote_is_stale(quote):
            fresh[symbol] = quote
    result = aggregate_downside_to_stops(holdings, fresh, flat)
    with st.expander("Estimated downside to approved stops"):
        left, right = st.columns([1, 3])
        left.metric("Current value to stop levels", _money(result["dollars"]))
        right.caption(f"Coverage: {result['covered']}/{result['position_count']} positions with a fresh quote above an approved numeric stop.")
        messages = []
        if result["crossed_symbols"]:
            messages.append("Stop already crossed: " + ", ".join(result["crossed_symbols"]))
        if result["missing_symbols"]:
            messages.append("Missing fresh quote/approved stop: " + ", ".join(result["missing_symbols"]))
        if messages:
            right.caption(" · ".join(messages))
        right.caption("Scenario estimate only. Stop orders may execute below the trigger during gaps or illiquid markets; losses are not capped at this estimate.")


def _approved_stop_coverage(holdings, quotes, strategies):
    invested = covered = downside = 0.0
    for holding in holdings:
        symbol = holding["symbol"].upper()
        quote = quotes.get(symbol)
        value = holding_metrics(holding, quote)["market_value"]
        if value is None:
            continue
        invested += value
        stop = scenario_levels((strategies.get(symbol) or {}).get("scenario"))["stop"]
        if stop is not None:
            covered += value
            if not quote_is_stale(quote):
                downside += max(0.0, (float(quote["price"]) - stop) * float(holding["shares"]))
    return {"coverage": covered / invested if invested > 0 else None,
            "downside": downside if covered > 0 else None}


def _render_sector_classification_editor(holdings, classifications):
    with st.expander("Position asset and sector classifications"):
        if not holdings:
            st.caption("Add positions before assigning sector classifications.")
            return
        st.caption("Edit several positions together. Local ticker matches are suggestions marked Unverified; manual verification is never overwritten. Diversified funds stay separate until reliable holdings look-through is available.")
        rows = []
        for holding in holdings:
            symbol = holding["symbol"].upper()
            record = classifications.get(symbol, {})
            rows.append({
                "symbol": symbol,
                "asset_type": record.get("asset_type", "Other"),
                "sector": record.get("sector", "Diversified / unclassified"),
                "sector_benchmark": record.get("sector_benchmark", ""),
                "benchmark_type": record.get("benchmark_type", "None"),
                "classification_source": record.get("classification_source", "Automatic"),
                "verification_status": record.get("verification_status", "Unverified"),
            })
        sector_options = list(SECTOR_CHOICES)
        sector_options.extend(value for value in (row["sector"] for row in rows) if value not in sector_options)
        edited = st.data_editor(
            rows, hide_index=True, width="stretch", key="portfolio_classifications_editor",
            disabled=["symbol"],
            column_config={
                "symbol": st.column_config.TextColumn("Ticker", width="small"),
                "asset_type": st.column_config.SelectboxColumn("Asset type", options=list(ASSET_TYPES), required=True),
                "sector": st.column_config.SelectboxColumn("Sector / exposure", options=sector_options, required=True),
                "sector_benchmark": st.column_config.TextColumn("Sector benchmark", help="Enter an ETF ticker such as XLK or XLC."),
                "benchmark_type": st.column_config.SelectboxColumn("Benchmark type", options=list(BENCHMARK_TYPES), required=True),
                "classification_source": st.column_config.SelectboxColumn("Source", options=list(CLASSIFICATION_SOURCES), required=True),
                "verification_status": st.column_config.SelectboxColumn("Verification", options=list(VERIFICATION_STATUSES), required=True),
            },
        )
        if st.button("Save classifications", type="primary", key="save_portfolio_classifications"):
            records = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
            try:
                save_portfolio_classification_records(records)
            except ValueError as error:
                st.error(str(error))
            else:
                st.toast("Portfolio classifications saved locally.")
                st.rerun()


def _render_risk_attribution(holdings, risk, histories, lookback):
    with st.expander("Portfolio risk attribution"):
        st.caption(f"Covariance-based current-holdings simulation · adjusted returns aligned by date · cached quote weights with Tiingo-close fallback · {lookback}-session lookback · cash return set to zero.")
        st.caption(f"Coverage: {risk['observations']}/{lookback} aligned daily observations · {risk['covered_positions']}/{risk['position_count']} positions included · cached quote coverage {risk.get('quote_covered_positions', 0)}/{risk['position_count']} (not required for historical weights).")
        if risk["status"] == "cash_only":
            st.dataframe([{"Ticker": "Cash", "Capital weight": f"{risk['cash_weight']:.2%}" if risk["cash_weight"] is not None else "Unavailable", "Standalone volatility": "0.0% (assumed)", "Portfolio volatility contribution": "0.0%"}], width="stretch", hide_index=True)
            st.caption("Cash-only current-holdings model: portfolio beta 0.00 and annualized volatility 0.0%; invested-equity beta is unavailable.")
            return
        if risk["status"] == "insufficient_history":
            st.info("Risk attribution is unavailable until at least two aligned daily returns are cached for SPY and one or more positions.")
            return
        if risk["status"] == "partial":
            st.warning("Partial estimate: rows without enough aligned adjusted-close history are excluded. The displayed weights are normalized over included positions plus cash; this is not the full portfolio estimate.")
        rows = []
        for holding in holdings:
            symbol = holding["symbol"].upper()
            detail = risk["risk_contributions"].get(symbol, {})
            if symbol not in risk.get("included_symbols", []):
                continue
            rows.append({
                "Ticker": symbol,
                "Capital weight": f"{risk['capital_weights'].get(symbol, 0):.2%}",
                "Standalone volatility": f"{detail['standalone_annualized_volatility']:.1%}" if detail.get("standalone_annualized_volatility") is not None else "Unavailable",
                "Portfolio volatility contribution": f"{detail['fraction_of_portfolio_volatility']:.1%}" if detail.get("fraction_of_portfolio_volatility") is not None else "Unavailable",
            })
        rows.append({"Ticker": "Cash", "Capital weight": f"{risk['cash_weight']:.2%}", "Standalone volatility": "0.0% (assumed)", "Portfolio volatility contribution": "0.0%"})
        st.dataframe(rows, width="stretch", hide_index=True)
        model_beta = f"{risk['portfolio_beta']:.2f}" if risk["portfolio_beta"] is not None else "unavailable"
        equity_beta = f"{risk['invested_equity_beta']:.2f}" if risk["invested_equity_beta"] is not None else "unavailable"
        st.caption(f"Model beta {model_beta} · covered-equity beta {equity_beta} · portfolio/SPY correlation " + (f"{risk['correlation_to_spy']:.2f}" if risk["correlation_to_spy"] is not None else "unavailable") + (f" · weights as of {risk.get('as_of', 'unknown')}" if risk.get("as_of") else ""))
        fallback_prices = [symbol for symbol, source in risk.get("weight_price_sources", {}).items() if source != "cached quote"]
        if fallback_prices:
            st.caption("Tiingo historical-close weights used where no cached quote existed: " + ", ".join(fallback_prices))
        if st.checkbox("Show aligned position correlation matrix", key="portfolio_show_correlations"):
            symbols = risk.get("included_symbols", [])
            corr = correlation_matrix(histories, symbols, lookback)
            if corr["observations"] < lookback:
                st.caption(f"Correlation matrix unavailable: {corr['observations']}/{lookback} aligned observations.")
            else:
                st.dataframe(corr["correlations"], width="stretch")
        st.caption("Capital weight is the share of portfolio value. Risk contribution uses weight × covariance with the modeled portfolio, divided by modeled portfolio variance; it can be negative when a holding diversifies other positions.")


if __name__ == "__main__":
    render()

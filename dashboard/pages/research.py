from datetime import date
import json
from math import isfinite

import streamlit as st

from dashboard.data_access import (
    add_watchlist_item, append_research_chat_messages, create_research_workspace, delete_watchlist_item,
    get_buy_zone_alert, get_cash_balance, get_strategy_record, list_cached_quotes, list_holdings,
    list_research_chat_messages, list_research_workspaces, list_strategy_revisions, list_watchlist,
    list_research_strategy_scenarios,
    save_buy_zone_alert, save_strategy_record, get_position_plan, save_position_plan,
    latest_strategy_revision_id, get_position_planner_policy, list_portfolio_classification_records,
)
from dashboard.ai_market_analyst import build_limited_portfolio_context
from dashboard.ai_providers import AIProviderError, DEFAULT_PROVIDER
from dashboard.ai_research import answer_research_question
from dashboard.market_data import quote_is_stale
from dashboard.navigation import activate_research_workspace
from dashboard.setup_calculations import (
    calculate_long_setup, calculate_position_sizing,
    format_return_percent, format_return_range,
)
from dashboard.research_context import build_research_context
from dashboard.strategy_import import (
    approve_import, assess_current_quote, chatgpt_export_instructions,
    compatible_current_quote, imported_strategy_record, json_template,
    normalize_strategy_import, preview_import, validate_normalized_strategy,
)
from dashboard.strategy_presentation import (
    LOGIC_GROUPS, analyze_scenario, investment_logic, strategy_scenarios,
    summarize_thesis,
)
from dashboard.calculations import portfolio_totals
from dashboard.position_planner import (calculate_position_capacity, plan_entry_tranches,
    resolve_planner_reference, simulate_exit_tranches)
from dashboard.kelly_sizing import calculate_binary_kelly

_WORKSPACE_KEY = "research_workspace_symbol"


def render() -> None:
    st.header("Research & Watchlist")
    selected = st.session_state.get(_WORKSPACE_KEY)
    if selected and selected in list_research_workspaces():
        _render_workspace(selected)
    else:
        _render_watchlist()


def _choose_workspace(symbol: str) -> None:
    st.session_state.pop("strategy_import_open_symbol", None)
    create_research_workspace(symbol)
    activate_research_workspace(st.session_state, symbol)


def _open_strategy_import(symbol: str) -> None:
    st.session_state["strategy_import_open_symbol"] = symbol


def _empty_strategy(symbol: str) -> dict:
    holding = next((item for item in list_holdings() if item["symbol"].upper() == symbol), None)
    return {
        "investment_thesis": "", "investment_horizon": holding["investment_horizon"] if holding else "Medium",
        "thesis_status": holding["thesis_status"] if holding else "Active", "catalysts": "", "risks": "",
        "entry_price_low": None, "entry_price_high": None, "target_price": None,
        "stop_invalidation": "", "invalidation_price": None, "position_sizing_notes": "",
        "last_review_date": date.today().isoformat(), "valuation_notes": "", "legacy_entry_zone": "",
        "strategy_type": "", "expected_holding_window": "", "max_planned_dollar_risk": None,
        "bullish_scenario": "", "bearish_scenario": "", "assumptions": "",
        "uncertainties": "", "ai_price_estimates": 0, "structured_analysis_json": "{}",
    }


def _render_watchlist() -> None:
    left, right = st.columns([3, 1])
    with left:
        with st.form("research_new_stock_form", clear_on_submit=True):
            cols = st.columns([4, 1])
            symbol = cols[0].text_input("Research new stock", placeholder="Ticker symbol").strip().upper()
            submitted = cols[1].form_submit_button("Open workspace", type="primary", use_container_width=True)
            if submitted:
                if not symbol:
                    st.error("Enter a ticker symbol.")
                else:
                    _choose_workspace(symbol)
                    st.rerun()
    with right:
        with st.popover("Manage watchlist", use_container_width=True):
            with st.form("watchlist_add_form", clear_on_submit=True):
                new_symbol = st.text_input("Add ticker")
                company = st.text_input("Company name (optional)")
                if st.form_submit_button("Add"):
                    if new_symbol.strip():
                        add_watchlist_item(new_symbol.strip().upper(), company)
                        st.rerun()
                    else:
                        st.error("Enter a ticker symbol.")
            items = list_watchlist()
            if items:
                remove_symbol = st.selectbox("Remove from watchlist", [item["symbol"] for item in items], key="watchlist_remove_select")
                if st.button("Remove selected", key="watchlist_remove_button"):
                    delete_watchlist_item(remove_symbol)
                    st.rerun()

    workspaces = list_research_workspaces()
    watchlist = list_watchlist()
    company_names = {item["symbol"].upper(): item.get("company_name", "") for item in watchlist}
    if not workspaces:
        st.caption("Your watchlist and research workspaces will appear here.")
        return

    st.subheader("Strategies & watchlist")
    headers = st.columns([1.35, 1.25, 1, 0.72, 0.72, 0.82, 0.78, 1.12])
    for col, label in zip(headers, ("Ticker / company", "Strategy · horizon", "Entry", "Target", "Stop", "Planned RRR", "Current RRR", "Status · reviewed")):
        col.caption(label)

    quotes = list_cached_quotes()
    saved_records = {symbol: get_strategy_record(symbol) for symbol in workspaces}
    records = {symbol: saved_records[symbol] or _empty_strategy(symbol) for symbol in workspaces}
    for symbol in workspaces:
        strategy = records[symbol]
        quote = quotes.get(symbol.upper())
        stored_scenarios = strategy_scenarios(strategy, list_research_strategy_scenarios(symbol))
        chosen_horizon = str(strategy.get("investment_horizon") or "Medium").lower()
        scenario_key = "short_term" if "short" in chosen_horizon else "long_term" if "long" in chosen_horizon else "medium_term"
        scenario = stored_scenarios[scenario_key]
        try:
            detail = json.loads(strategy.get("structured_analysis_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            detail = {}
        current_price, _ = compatible_current_quote(
            detail, quote, is_stale=bool(quote and quote_is_stale(quote)),
        ) if detail.get("schema_version") in {1, 2} else (None, None)
        result = analyze_scenario(scenario, current_price, quote_is_stale=bool(quote and quote_is_stale(quote)))
        row = st.columns([1.35, 1.25, 1, 0.72, 0.72, 0.82, 0.78, 1.12], vertical_alignment="center")
        row[0].button(symbol, key=f"research_open_{symbol}", on_click=_choose_workspace, args=(symbol,), type="tertiary", help="Open research workspace")
        if company_names.get(symbol):
            row[0].caption(company_names[symbol])
        strategy_name = strategy.get("strategy_type") or "Strategy not set"
        window = strategy.get("expected_holding_window") or strategy.get("investment_horizon") or "Horizon not set"
        row[1].markdown(f"{strategy_name}\n\n:{_horizon_color(strategy.get('investment_horizon'))}[{window}]")
        low, high = scenario.get("entry_price_low"), scenario.get("entry_price_high")
        entry_text = f"${low:,.2f}" if low is not None and high is not None and low == high else (f"${low:,.2f}–${high:,.2f}" if low is not None and high is not None else "Unavailable")
        row[2].write(entry_text)
        row[3].write(_scenario_range(scenario, "target_price_low", "target_price_high", fallback="target_price"))
        row[4].write(_money(scenario.get("stop_price", scenario.get("invalidation_price"))))
        planned = _ratio_text(result.get("planned_rrr"), result.get("rrr_range"))
        row[5].markdown(f"**{planned}**" if planned != "Unavailable" else "Unavailable")
        current_rrr = result.get("remaining_rrr")
        current_range = result.get("remaining_range")
        row[6].markdown(f"**{_ratio_text(current_rrr, current_range)}**" if current_rrr is not None or current_range is not None else "Unavailable")
        if current_rrr is not None and quote:
            row[6].caption(("stale" if quote_is_stale(quote) else "saved") + f" · {quote['quote_date']}")
        status = (strategy.get("thesis_status") or "Active") if saved_records[symbol] else "No strategy"
        row[7].caption(f"{status}\n\n{strategy.get('last_review_date') or 'Not reviewed'}")
        st.divider()
        if result["status"] in {"Incomplete", "Invalidated"} and (scenario.get("target_price_low") or scenario.get("stop_price")):
            st.caption(f"{symbol} setup: {result['message']}")


def _horizon_color(horizon: str | None) -> str:
    return {"Long": "green", "Medium": "blue", "Short": "orange"}.get(horizon, "gray")


@st.dialog("Edit strategy")
def _edit_strategy_dialog(symbol: str) -> None:
    strategy = get_strategy_record(symbol) or _empty_strategy(symbol)
    with st.form(f"strategy_edit_form_{symbol}"):
        st.text_area("Investment thesis", value=strategy["investment_thesis"], key=f"thesis_{symbol}")
        strategy_type = st.text_input("Investment strategy type", value=strategy.get("strategy_type", ""))
        left, right = st.columns(2)
        horizon = left.selectbox("Investment horizon", ("Long", "Medium", "Short"), index=("Long", "Medium", "Short").index(strategy["investment_horizon"]))
        expected_window = right.text_input("Expected holding window", value=strategy.get("expected_holding_window", ""), placeholder="e.g. 6–12 months")
        status = st.selectbox("Thesis status", ("Active", "Review Needed", "Invalidated"), index=("Active", "Review Needed", "Invalidated").index(strategy["thesis_status"]))

        low_saved, high_saved = strategy.get("entry_price_low"), strategy.get("entry_price_high")
        exact_saved = low_saved is not None and high_saved is not None and float(low_saved) == float(high_saved)
        mode = st.radio("Planned entry", ("Single price", "Price range"), horizontal=True, index=0 if exact_saved or (low_saved is None and high_saved is None) else 1)
        if mode == "Single price":
            entry_value = st.number_input("Entry price", min_value=0.0, value=float(low_saved or 0.0) if exact_saved else 0.0, step=0.5)
            entry_low, entry_high = entry_value or None, entry_value or None
        else:
            entry_cols = st.columns(2)
            entry_low_value = entry_cols[0].number_input("Entry range · low", min_value=0.0, value=float(low_saved or 0.0), step=0.5)
            entry_high_value = entry_cols[1].number_input("Entry range · high", min_value=0.0, value=float(high_saved or 0.0), step=0.5)
            entry_low, entry_high = entry_low_value or None, entry_high_value or None

        price_cols = st.columns(2)
        target = price_cols[0].number_input("Target price", min_value=0.0, value=float(strategy.get("target_price") or 0.0), step=0.5)
        invalidation_price = price_cols[1].number_input("Stop / invalidation price", min_value=0.0, value=float(strategy.get("invalidation_price") or 0.0), step=0.5)
        catalysts = st.text_area("Catalysts", value=strategy["catalysts"])
        risks = st.text_area("Risks", value=strategy["risks"])
        valuation = st.text_area("Valuation notes", value=strategy.get("valuation_notes", ""))
        stop_text = st.text_area("Stop or thesis invalidation conditions", value=strategy["stop_invalidation"])
        sizing = st.text_area("Position sizing notes", value=strategy["position_sizing_notes"])
        max_risk = st.number_input("Maximum planned dollar risk (optional)", min_value=0.0, value=float(strategy.get("max_planned_dollar_risk") or 0.0), step=100.0)
        bullish = st.text_area("Bullish scenario", value=strategy.get("bullish_scenario", ""))
        bearish = st.text_area("Bearish scenario", value=strategy.get("bearish_scenario", ""))
        assumptions = st.text_area("Assumptions", value=strategy.get("assumptions", ""))
        uncertainties = st.text_area("Uncertainties", value=strategy.get("uncertainties", ""))
        ai_price_estimates = st.checkbox(
            "Saved price levels are AI estimates, not verified market data",
            value=bool(strategy.get("ai_price_estimates", 0)),
        )
        date_text = strategy.get("last_review_date") or date.today().isoformat()
        try:
            review_default = date.fromisoformat(date_text)
        except ValueError:
            review_default = date.today()
        review_date = st.date_input("Last review date", value=review_default)
        if st.form_submit_button("Save strategy revision", type="primary"):
            candidate = calculate_long_setup(
                entry_price=entry_low if entry_low is not None and entry_high == entry_low else None,
                entry_low=entry_low if entry_low is not None and entry_high != entry_low else None,
                entry_high=entry_high if entry_low is not None and entry_high != entry_low else None,
                target_price=target or None, stop_price=invalidation_price or None,
            )
            if entry_low is not None and entry_high is not None and entry_low > entry_high:
                st.error("Entry range lower bound must not exceed upper bound.")
            elif candidate["status"] in ("invalid_setup", "invalid_input", "zero_risk"):
                st.error(candidate["message"])
            else:
                save_strategy_record(symbol, {
                    "investment_thesis": st.session_state[f"thesis_{symbol}"],
                    "investment_horizon": horizon, "thesis_status": status,
                    "catalysts": catalysts, "risks": risks,
                    "entry_price_low": entry_low, "entry_price_high": entry_high,
                    "target_price": target or None, "invalidation_price": invalidation_price or None,
                    "stop_invalidation": stop_text, "position_sizing_notes": sizing,
                    "max_planned_dollar_risk": max_risk or None,
                    "last_review_date": review_date.isoformat(), "valuation_notes": valuation,
                    "legacy_entry_zone": strategy.get("legacy_entry_zone", ""),
                    "strategy_type": strategy_type, "expected_holding_window": expected_window,
                    "bullish_scenario": bullish, "bearish_scenario": bearish,
                    "assumptions": assumptions, "uncertainties": uncertainties,
                    "ai_price_estimates": int(ai_price_estimates),
                    "structured_analysis_json": strategy.get("structured_analysis_json", "{}"),
                })
                st.rerun()


def _render_workspace(symbol: str) -> None:
    top, back = st.columns([5, 1])
    workspace_info = next((item for item in list_watchlist() if item["symbol"].upper() == symbol), None)
    holding_info = next((item for item in list_holdings() if item["symbol"].upper() == symbol), None)
    company_name = workspace_info.get("company_name") if workspace_info else ""
    top.subheader(f"{symbol}{' · ' + company_name if company_name else ''}")
    if company_name:
        top.caption("Company label from your watchlist; not independently verified by a connected company-data source.")
    if back.button("← Strategies"):
        st.session_state.pop("strategy_import_open_symbol", None)
        st.session_state[_WORKSPACE_KEY] = None
        st.rerun()

    strategy = get_strategy_record(symbol) or _empty_strategy(symbol)
    quote = list_cached_quotes().get(symbol.upper())
    context = build_research_context(symbol, quote, strategy)
    chat_history = list_research_chat_messages(symbol)

    conversation, strategy_panel = st.columns([1.55, 1], gap="large")
    with conversation:
        st.markdown("#### Investment Strategist")
        st.caption(
            f"{st.session_state.get('ai_provider', DEFAULT_PROVIDER).title()} · "
            f"{st.session_state.get('ai_model', 'deepseek-chat')} · only called when you submit a message or request a draft"
        )
        if not context["verified_external_evidence"]:
            st.caption("Verified fundamentals and recent news are not connected. Local technical data is available where cached; model recollection is unverified.")
        for message in chat_history[-12:]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                if message.get("input_tokens") is not None or message.get("output_tokens") is not None:
                    st.caption(f"Provider usage · {message.get('input_tokens') or '—'} input / {message.get('output_tokens') or '—'} output tokens")
        question = st.chat_input("Challenge a thesis, compare horizons, or ask what would change the view", key=f"research_chat_input_{symbol}")
        if question and question.strip():
            provider = st.session_state.get("ai_provider", DEFAULT_PROVIDER)
            include_portfolio = bool(st.session_state.get("ai_include_portfolio", False))
            portfolio_context = build_limited_portfolio_context() if include_portfolio else None
            try:
                with st.spinner(f"Waiting for {provider.title()}…"):
                    answer_result = answer_research_question(
                        symbol, question, chat_history,
                        provider=provider, model=st.session_state.get("ai_model", "deepseek-chat"),
                        max_output_tokens=st.session_state.get("ai_max_output_tokens", 1400),
                        strategy=strategy, portfolio_context=portfolio_context,
                        research_context=context, return_result=True,
                    )
                answer = answer_result.text
                append_research_chat_messages(
                    symbol, [{"role": "user", "content": question}, {"role": "assistant", "content": answer}],
                    provider=provider, model=st.session_state.get("ai_model", "deepseek-chat"),
                    input_tokens=answer_result.input_tokens, output_tokens=answer_result.output_tokens,
                )
                st.rerun()
            except AIProviderError as error:
                st.error(str(error))
            except Exception as error:
                st.error("Research chat failed. Check the selected provider and retry; no strategy or position was changed.")
                st.caption(f"Error type: {type(error).__name__}")
        with st.expander("Research data and limitations"):
            st.json(context, expanded=False)
            for event in context.get("upcoming_earnings_events", []):
                if event.get("source_url"):
                    st.markdown(f"[{event.get('name') or 'Earnings source'} · {event.get('status', 'status unavailable')}]({event['source_url']})")
            for item in context.get("verified_external_evidence", []):
                if item.get("url"):
                    st.markdown(f"[{item.get('title') or item['url']}]({item['url']}) · as of {item.get('as_of') or 'date unavailable'}")

    with strategy_panel:
        _render_strategy_dashboard(symbol, strategy, quote, context, holding_info)


def _scenario_range(scenario: dict, low_key: str, high_key: str, *, fallback: str | None = None) -> str:
    low = scenario.get(low_key)
    high = scenario.get(high_key)
    if low is None and fallback:
        low = scenario.get(fallback)
    if high is None and fallback:
        high = scenario.get(fallback)
    if low is None or high is None:
        return "Not provided"
    if float(low) == float(high):
        return _money(low)
    return f"{_money(low)} – {_money(high)}"


def _ratio_text(value, bounds) -> str:
    if value is not None:
        return f"{value:.2f}×"
    if bounds is not None:
        return f"{bounds[0]:.2f}–{bounds[1]:.2f}×"
    return "Not calculable"


def _percent_text(bounds) -> str:
    formatted = format_return_range(bounds)
    return "Not calculable" if formatted == "Unavailable" else formatted


def _render_strategy_dashboard(symbol: str, strategy: dict, quote: dict | None, context: dict, holding_info: dict | None) -> None:
    """Compact approved-strategy view, with simulations and source text on demand."""
    st.markdown("#### Strategy decision dashboard")
    detail = {}
    try:
        detail = json.loads(strategy.get("structured_analysis_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    current_price, quote_note = compatible_current_quote(
        detail, quote, is_stale=bool(quote and quote_is_stale(quote)),
    ) if detail.get("schema_version") in {1, 2} else (None, "No compatible imported reference is available.")
    if quote:
        stale = quote_is_stale(quote)
        history_date = context.get("historical_source", {}).get("latest_observation_date")
        freshness = "stale cached quote" if stale else "cached Alpha Vantage quote; end of day, not real-time"
        st.caption(f"Latest reference: ${quote['price']:,.2f} · {quote.get('quote_date') or 'date unavailable'} · {freshness} · Technical data through {history_date or 'unavailable'}.")
        if history_date and quote.get("quote_date") != history_date:
            st.caption("Quote and Tiingo technical history have different observation dates; this is a freshness difference. Technical indicators use Tiingo adjusted closes.")
    else:
        st.caption(f"Latest reference price unavailable · Technical data through {context.get('historical_source', {}).get('latest_observation_date') or 'unavailable'}.")
    if quote and current_price is None:
        st.caption("Current RRR unavailable: " + (quote_note or "cached price could not be matched to the approved strategy reference."))

    buttons = st.columns([1.15, 1, 1.25])
    buttons[0].button("Import Strategy", key=f"import_strategy_{symbol}", type="primary", use_container_width=True,
                      on_click=_open_strategy_import, args=(symbol,))
    if buttons[1].button("Copy JSON Template", key=f"strategy_template_{symbol}", use_container_width=True):
        st.session_state[f"show_strategy_template_{symbol}"] = not st.session_state.get(f"show_strategy_template_{symbol}", False)
    if buttons[2].button("Copy ChatGPT Export Instructions", key=f"strategy_instructions_{symbol}", use_container_width=True):
        st.session_state[f"show_strategy_instructions_{symbol}"] = not st.session_state.get(f"show_strategy_instructions_{symbol}", False)
    if st.session_state.get(f"show_strategy_template_{symbol}"):
        st.caption("Complete schema-aligned example. Placeholder prose is explicit and unsupported prices remain null.")
        st.code(json_template(symbol), language="json")
    if st.session_state.get(f"show_strategy_instructions_{symbol}"):
        st.code(chatgpt_export_instructions(symbol), language="text")
    if st.session_state.get("strategy_import_open_symbol") == symbol:
        _strategy_import_dialog(symbol, quote)

    company = strategy.get("company_name") or next((item.get("company_name") for item in list_watchlist() if item["symbol"].upper() == symbol), None)
    meta = st.columns(4)
    meta[0].metric("Strategy type", strategy.get("strategy_type") or "Not specified")
    meta[1].metric("Investment horizon", strategy.get("investment_horizon") or "Not specified")
    meta[2].metric("Thesis status", strategy.get("thesis_status") or "Active")
    meta[3].metric("Last review", strategy.get("last_review_date") or "Not reviewed")
    if company:
        st.caption(f"Company: {company} · name is user supplied unless verified through a connected company-data source.")
    st.markdown("**Thesis at a glance**")
    st.write(summarize_thesis(strategy.get("investment_thesis", "")))

    scenarios = strategy_scenarios(strategy, list_research_strategy_scenarios(symbol))
    complete_count = sum(
        all(scenarios[key].get(field) is not None for field in ("entry_price_low", "entry_price_high", "target_price_low", "target_price_high", "stop_price"))
        for key in ("short_term", "medium_term", "long_term")
    )
    st.caption(f"Price-level coverage: {complete_count} of 3 horizons have entry, target, and stop levels. This is a completeness check, not a quality score.")
    horizon_labels = (("short_term", "Short-term"), ("medium_term", "Medium-term"), ("long_term", "Long-term"))
    columns = st.columns(3, gap="small")
    for (key, label), column in zip(horizon_labels, columns):
        scenario = scenarios[key]
        analysis = analyze_scenario(scenario, current_price, quote_is_stale=bool(quote and quote_is_stale(quote)))
        with column.container(border=True):
            st.markdown(f"**{label}**")
            st.caption(scenario.get("holding_horizon") or scenario.get("holding_window") or "Holding period not specified")
            st.markdown(f"Entry  ·  **{_scenario_range(scenario, 'entry_price_low', 'entry_price_high')}**")
            st.markdown(f"Target  ·  **{_scenario_range(scenario, 'target_price_low', 'target_price_high', fallback='target_price')}**")
            st.markdown(f"Stop / invalidation  ·  **{_money(scenario.get('stop_price', scenario.get('invalidation_price'))) if scenario.get('stop_price', scenario.get('invalidation_price')) is not None else 'Not provided'}**")
            m = st.columns(2)
            m[0].metric("Planned RRR", _ratio_text(analysis.get("planned_rrr"), analysis.get("rrr_range")), help="Python-calculated. Entry/target ranges show endpoint combinations, not an assumed midpoint.")
            remaining = analysis.get("remaining_rrr") if current_price is not None and not (quote and quote_is_stale(quote)) else None
            remaining_range = analysis.get("remaining_range") if current_price is not None and not (quote and quote_is_stale(quote)) else None
            m[1].metric("Remaining RRR", _ratio_text(remaining, remaining_range), help="Uses the latest fresh quote only when price date and basis are compatible.")
            st.caption(f"Upside {_percent_text(analysis.get('upside_percent'))} · downside {_percent_text(analysis.get('downside_percent'))}")
            status_color = {"Actionable": "green", "Conditional": "blue", "Incomplete": "gray", "Invalidated": "red"}.get(analysis["status"], "gray")
            st.markdown(f":{status_color}[**{analysis['status']}**] · {analysis['message']}")
            if analysis.get("missing"):
                st.caption("Missing: " + ", ".join(analysis["missing"]))
            if scenario.get("thesis") or scenario.get("entry_conditions") or scenario.get("invalidation_conditions"):
                with st.expander("Scenario logic"):
                    for label, field in (("Thesis", "thesis"), ("Entry conditions", "entry_conditions"), ("Invalidation", "invalidation_conditions"), ("Fundamental invalidation", "fundamental_invalidation")):
                        if scenario.get(field):
                            st.markdown(f"**{label}**\n\n{scenario[field]}")
    st.caption("Actionable/Conditional status is a mechanical setup state based on these saved levels and available prices, not an investment recommendation. Stops are triggers, not guaranteed execution prices.")

    logic = investment_logic(strategy, detail)
    for start in (0, 2):
        group_cols = st.columns(2, gap="small")
        for i in range(start, min(start + 2, len(LOGIC_GROUPS))):
            key, label = LOGIC_GROUPS[i]
            with group_cols[i - start].container(border=True):
                st.markdown(f"**{label}**")
                items = logic.get(key, [])
                if not items:
                    st.caption("No item provided.")
                for item in items[:3]:
                    st.markdown(f"**{item['label']}** · `{item['evidence_status']}`")
                    st.write(summarize_thesis(item.get("explanation", ""), max_sentences=1, max_chars=180))
                    if item.get("details"):
                        with st.expander(f"Details · {item['label']}"):
                            st.write(item["details"])

    if strategy.get("ai_price_estimates"):
        st.warning("Saved price levels are AI estimates, not verified market data.")
    if holding_info:
        st.caption(f"Held position · {holding_info['shares']:g} shares at average cost {_money(holding_info['average_cost'])}. Position size is not sent to AI unless portfolio context is enabled.")

    with st.expander("Explore hypothetical RRR and position size"):
        st.caption("Inputs here are hypothetical and are never saved to the approved strategy. Cached quote is used as the current reference only when fresh.")
        selected = st.selectbox("Horizon", [key for key, _ in horizon_labels], index=1,
                                format_func=lambda key: dict(horizon_labels)[key], key=f"sim_horizon_{symbol}")
        scenario = scenarios[selected]
        default_entry = scenario.get("entry_price_low") or (quote.get("price") if quote else None) or 0.0
        default_stop = scenario.get("stop_price", scenario.get("invalidation_price")) or 0.0
        default_target = scenario.get("target_price_low", scenario.get("target_price")) or 0.0
        entry_col, stop_col = st.columns(2)
        sim_entry = entry_col.number_input("Hypothetical entry", min_value=0.0, value=float(default_entry), key=f"sim_entry_{symbol}")
        sim_stop = stop_col.number_input("Hypothetical stop", min_value=0.0, value=float(default_stop), key=f"sim_stop_{symbol}")
        target_col, shares_col, budget_col = st.columns(3)
        sim_target = target_col.number_input("Hypothetical target", min_value=0.0, value=float(default_target), key=f"sim_target_{symbol}")
        shares = shares_col.number_input("Hypothetical shares", min_value=0.0, value=float(holding_info["shares"] if holding_info else 0), step=1.0, key=f"sim_shares_{symbol}")
        budget = budget_col.number_input("Risk budget (optional)", min_value=0.0, value=float(strategy.get("max_planned_dollar_risk") or 0.0), step=100.0, key=f"sim_budget_{symbol}")
        portfolio_holdings = list_holdings()
        cached_quotes = list_cached_quotes()
        missing_prices = [item["symbol"] for item in portfolio_holdings if not cached_quotes.get(item["symbol"].upper())]
        stale_prices = [item["symbol"] for item in portfolio_holdings
                        if cached_quotes.get(item["symbol"].upper()) and quote_is_stale(cached_quotes[item["symbol"].upper()])]
        invested_value = sum(float(item["shares"]) * float(cached_quotes[item["symbol"].upper()]["price"])
                             for item in portfolio_holdings if cached_quotes.get(item["symbol"].upper()))
        current_portfolio_value = invested_value + get_cash_balance() if not missing_prices and not stale_prices else None
        current_position_value = (float(holding_info["shares"]) * float(quote["price"])
                                  if holding_info and quote and not quote_is_stale(quote) else 0.0)
        simulated = calculate_long_setup(entry_price=sim_entry or None, stop_price=sim_stop or None,
                                         target_price=sim_target or None,
                                         current_price=quote.get("price") if quote and not quote_is_stale(quote) else None)
        sizing = calculate_position_sizing(entry_price=sim_entry or None, stop_price=sim_stop or None,
                                           target_price=sim_target or None, shares=shares,
                                           current_portfolio_value=current_portfolio_value,
                                           current_position_value=current_position_value)
        max_sizing = calculate_position_sizing(entry_price=sim_entry or None, stop_price=sim_stop or None,
                                               target_price=sim_target or None, risk_budget=budget or None)
        result_cols = st.columns(3)
        result_cols[0].metric("Entry RRR", f"{simulated['planned_rrr']:.2f}×" if simulated.get("planned_rrr") is not None else "Unavailable")
        result_cols[1].metric("Remaining RRR", f"{simulated['current_rrr']:.2f}×" if simulated.get("current_rrr") is not None else "Unavailable")
        result_cols[2].metric("Position exposure", _money(sizing.get("position_exposure")))
        payoff_cols = st.columns(4)
        payoff_cols[0].metric("Potential upside", _money(sizing.get("potential_dollar_upside")))
        payoff_cols[1].metric("Dollar risk to stop", _money(sizing.get("potential_dollar_downside")))
        payoff_cols[2].metric("Max shares at risk budget", str(max_sizing.get("max_shares")) if max_sizing.get("max_shares") is not None else "Set a risk budget")
        payoff_cols[3].metric("Allocation after size", f"{sizing['portfolio_allocation_after_change']:.1%}" if sizing.get("portfolio_allocation_after_change") is not None else "Unavailable")
        st.caption(f"Potential upside {format_return_percent(sizing.get('upside_percent'))} · planned downside {format_return_percent(sizing.get('downside_percent'))}" if sizing.get("upside_percent") is not None else f"Downside {format_return_percent(sizing.get('downside_percent'))} · target is missing, so upside is not calculated." if sizing.get("downside_percent") is not None else sizing.get("message", "Sizing unavailable."))
        st.caption(f"{simulated['message']} {simulated['current_message']} Potential numbers use the hypothetical share count; gap and slippage risk can exceed the planned stop loss.")
        if budget:
            st.caption(f"Maximum shares at risk budget: {max_sizing.get('max_shares') if max_sizing.get('status') == 'valid' else max_sizing.get('message')}")
        if missing_prices:
            st.caption("Portfolio allocation is unavailable because prices are missing for: " + ", ".join(missing_prices) + ".")
        elif stale_prices:
            st.caption("Portfolio allocation is unavailable because quotes are stale for: " + ", ".join(stale_prices) + ".")

    _render_strategy_details_and_history(symbol, strategy, detail)
    with st.expander("Advanced manual editor"):
        st.caption("Full manual editing is optional. Saving creates an approved strategy revision.")
        if st.button("Open full strategy editor", key=f"open_advanced_strategy_{symbol}"):
            _edit_strategy_dialog(symbol)
    with st.expander("Buy-zone alert"):
        _render_buy_zone(symbol)

    _render_position_plan(symbol, strategy, scenarios, quote, holding_info)


def _render_position_plan(symbol, strategy, scenarios, quote, holding_info):
    """Research owns approved plan assumptions; portfolio quantities are recomputed locally."""
    st.markdown("#### Position Plan")
    holdings, quotes, cash = list_holdings(), list_cached_quotes(), get_cash_balance()
    totals = portfolio_totals(holdings, quotes, cash)
    policy = get_position_planner_policy()
    try:
        strategy_detail = json.loads(strategy.get("structured_analysis_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        strategy_detail = {}
    quote_stale = bool(quote and quote_is_stale(quote))
    quote_assessment = assess_current_quote(strategy_detail, quote, is_stale=quote_stale)
    horizon_options = (("short_term", "Swing / short-term"), ("medium_term", "Medium-term"), ("long_term", "Long-term"))
    primary = str(strategy.get("investment_horizon") or "Medium").lower()
    default_key = "short_term" if "short" in primary else "long_term" if "long" in primary else "medium_term"
    approved = bool(strategy.get("investment_thesis") or strategy.get("structured_analysis_json", "{}") != "{}")
    if not approved:
        st.caption("Approve a strategy before saving a position plan. Imported sizing ideas remain proposals until explicitly approved here.")
        return

    with st.expander("Position Planner · Trade Setup · Position Sizing · Staged Execution Plan", expanded=False):
        horizon = st.selectbox("Trade Setup · approved horizon", [key for key, _ in horizon_options],
                               index=[key for key, _ in horizon_options].index(default_key),
                               format_func=lambda key: dict(horizon_options)[key], key=f"planner_horizon_{symbol}")
        horizon_label = dict(horizon_options)[horizon]
        scenario = scenarios[horizon]
        saved_row = get_position_plan(symbol, horizon_label)
        saved = (saved_row or {}).get("plan", {})
        imported = strategy_detail.get("position_sizing") or {}
        defaults = {**imported, **saved}
        if imported.get("target_allocation_range"):
            low_alloc, high_alloc = imported["target_allocation_range"]
            st.caption(f"Imported allocation proposal: {low_alloc:.1%}–{high_alloc:.1%}. This is not a portfolio-derived recommendation.")

        low, high = scenario.get("entry_price_low"), scenario.get("entry_price_high")
        if low is None or high is None:
            st.warning("Trade Setup is incomplete: the approved scenario needs a numeric entry or entry range before sizing.")
            return
        entries = list(dict.fromkeys((float(low), float(high))))
        targets = [value for value in (scenario.get("target_price_low"), scenario.get("target_price_high")) if value is not None]
        setup_cols = st.columns(3)
        entry = setup_cols[0].selectbox("Approved entry reference", entries,
            format_func=lambda value: f"${value:,.2f}", key=f"plan_entry_{symbol}_{horizon}")
        target = setup_cols[1].selectbox("Approved target reference", list(dict.fromkeys(map(float, targets))),
            format_func=lambda value: f"${value:,.2f}", key=f"plan_target_{symbol}_{horizon}") if targets else None
        stop = scenario.get("stop_price", scenario.get("invalidation_price"))
        setup_cols[2].metric("Approved stop / invalidation", _money(stop))
        st.caption("Entry and target ranges are not averaged. Select an endpoint; the planner uses that explicit price scenario.")
        with st.expander("Advanced manual Trade Setup overrides", expanded=False):
            st.caption("Overrides apply only to this proposed plan revision; the approved research strategy remains unchanged.")
            override = st.checkbox("Use manual entry / target / stop for this plan", key=f"plan_override_{symbol}_{horizon}")
            if override:
                entry = st.number_input("Manual planned entry ($)", min_value=0.01, value=float(entry), step=.5, key=f"manual_entry_{symbol}_{horizon}")
                target = st.number_input("Manual planned target ($)", min_value=0.01, value=float(target or entry), step=.5, key=f"manual_target_{symbol}_{horizon}")
                stop = st.number_input("Manual stop / invalidation ($)", min_value=0.01, value=float(stop or entry * .9), step=.5, key=f"manual_stop_{symbol}_{horizon}")

        held_shares = float(holding_info["shares"]) if holding_info else 0.0
        manual_price, manual_date, manual_confirmed = None, None, False
        quote_basis_confirmed = False
        if quote:
            quote_asof = quote.get("quote_date") or "date unavailable"
            if quote_stale:
                st.warning(f"Stale quote: Alpha Vantage quote from {quote_asof} cannot be used for actionable sizing. {quote_assessment['message']}")
            elif quote_assessment["status"] == "compatible":
                st.caption(f"Reference: Alpha Vantage ${quote['price']:,.2f} · {quote_asof}. {quote_assessment['message']}")
            else:
                st.info(quote_assessment["message"])
                quote_basis_confirmed = st.checkbox("I verified this fresh Alpha Vantage quote and the strategy levels use a compatible split/dividend price basis", key=f"confirm_quote_basis_{symbol}_{horizon}")
        else:
            st.info("No cached Alpha Vantage quote is available. Historical Tiingo adjusted closes are for technical indicators and are not substituted for a quote.")

        with st.expander("Manual current-price reference", expanded=quote_stale or not quote):
            st.caption("Use only a price you have checked yourself. This is a planning reference, not a provider quote. Confirm that it uses the same split/dividend basis as the strategy levels.")
            manual_text = st.text_input("Manual reference price ($)", value="", key=f"manual_current_price_{symbol}_{horizon}", placeholder="Enter a checked price")
            manual_date = st.date_input("Manual reference as-of date", value=date.today(), key=f"manual_current_date_{symbol}_{horizon}")
            manual_confirmed = st.checkbox("I checked this price and confirm its basis is compatible with the selected setup", key=f"confirm_manual_reference_{symbol}_{horizon}")
            try:
                manual_price = float(manual_text.strip()) if manual_text.strip() else None
            except ValueError:
                manual_price = None
            if manual_text.strip() and (manual_price is None or manual_price <= 0 or not manual_confirmed):
                st.caption("Enter a positive numeric price and check the confirmation to use this manual reference.")

        reference_choice = resolve_planner_reference(
            quote_assessment, quote, quote_basis_confirmed=quote_basis_confirmed,
            manual_price=manual_price, manual_date=manual_date,
            manual_basis_confirmed=manual_confirmed,
        )
        current_reference = reference_choice.get("price")
        reference_source = reference_choice.get("source")
        reference_confirmed = reference_choice.get("available", False)

        st.markdown("**Position Sizing**")
        if totals.get("total_value") is None:
            st.warning("Portfolio equity is incomplete because one or more holdings lack cached prices; capacity cannot be calculated.")
            return
        if held_shares > 0 and symbol not in quotes:
            st.warning("This holding has no cached quote, so its market value and buying capacity are unavailable.")
            return
        market_value = held_shares * float(quotes[symbol]["price"]) if symbol in quotes else 0.0
        classifications = list_portfolio_classification_records()
        classification = classifications.get(symbol, {})
        sector_value = 0.0
        if classification.get("verification_status") == "Verified":
            for row in holdings:
                other = classifications.get(row["symbol"].upper(), {})
                q = quotes.get(row["symbol"].upper())
                if q and other.get("verification_status") == "Verified" and other.get("sector") == classification.get("sector"):
                    sector_value += float(row["shares"]) * float(q["price"])

        # Kelly is only computed from an explicitly entered probability. Imported
        # AI estimates remain unverified proposals and are not prefilled here.
        with st.expander("Optional conservative Kelly illustration", expanded=False):
            st.caption("Binary target/stop scenario only. A stop can gap; the assumed stop fill is not a guaranteed maximum loss. Kelly is not used unless you explicitly enter and source a probability.")
            kelly_enabled = st.checkbox("Enter an explicit probability for sensitivity analysis", key=f"kelly_enabled_{symbol}_{horizon}")
            kelly_probability = None
            kelly_source = None
            kelly_evidence = ""
            probability_low = probability_high = None
            gap_slippage = st.number_input("Additional gap / slippage loss (% of entry)", 0.0, 100.0, 0.0, .5, key=f"kelly_gap_{symbol}_{horizon}") / 100
            if kelly_enabled:
                probability_source_options = [
                    "User-provided hypothetical assumption",
                    "ChatGPT research estimate (unverified)",
                    "Historical or validated probability model",
                ]
                kelly_source = st.selectbox("Probability source", probability_source_options, key=f"kelly_source_{symbol}_{horizon}")
                probability_percent = st.number_input("P(target before stop) (%)", min_value=0.0, max_value=100.0, value=None, step=1.0, placeholder="Enter an evidenced or hypothetical probability", key=f"kelly_probability_{symbol}_{horizon}")
                kelly_probability = probability_percent / 100 if probability_percent is not None else None
                probability_low_pct = st.number_input("Optional low probability (%)", min_value=0.0, max_value=100.0, value=None, step=1.0, placeholder="Optional", key=f"kelly_low_{symbol}_{horizon}")
                probability_high_pct = st.number_input("Optional high probability (%)", min_value=0.0, max_value=100.0, value=None, step=1.0, placeholder="Optional", key=f"kelly_high_{symbol}_{horizon}")
                probability_low = probability_low_pct / 100 if probability_low_pct is not None else None
                probability_high = probability_high_pct / 100 if probability_high_pct is not None else None
                kelly_evidence = st.text_input("Probability method / evidence (required except hypothetical)", key=f"kelly_evidence_{symbol}_{horizon}")
            kelly_result = None
            if target is not None and stop is not None and kelly_enabled and kelly_probability is not None:
                try:
                    kelly_result = calculate_binary_kelly(
                        entry_price=entry, target_price=target, stop_price=stop,
                        probability=kelly_probability, probability_source=kelly_source,
                        probability_evidence=kelly_evidence,
                        probability_low=probability_low, probability_high=probability_high,
                        fractional_kelly=.25, portfolio_equity=totals["total_value"],
                        gap_slippage_fraction=gap_slippage,
                    )
                    if kelly_result["status"] == "valid":
                        st.caption(f"Full Kelly allocation: {kelly_result['full_kelly_allocation_fraction']:.2%} of invested capital · quarter-Kelly illustration: {kelly_result['quarter_kelly_allocation_fraction']:.2%} ({_money(kelly_result['quarter_kelly_allocation_fraction'] * totals['total_value'])}) · risk-capital fraction: {kelly_result['quarter_kelly_allocation_fraction'] * kelly_result['gap_adjusted_loss_per_invested_dollar']:.2%} of portfolio. Not a validated forecast; hard portfolio caps still apply.")
                        st.caption(f"Break-even probability under this binary model: {kelly_result['break_even_probability']:.1%}. {kelly_result['model_assumption']}")
                        if st.button("Use quarter-Kelly as intended allocation", key=f"apply_quarter_kelly_{symbol}_{horizon}"):
                            saved_max = defaults.get("max_allocation", defaults.get("maximum_allocation", policy["max_position_allocation"]))
                            policy_cap = min(float(saved_max), float(policy["max_position_allocation"]))
                            max_widget_key = f"plan_max_allocation_{symbol}_{horizon}"
                            if max_widget_key in st.session_state:
                                policy_cap = min(policy_cap, float(st.session_state[max_widget_key]) / 100)
                            st.session_state[f"plan_target_allocation_{symbol}_{horizon}"] = min(
                                kelly_result["quarter_kelly_allocation_fraction"], policy_cap
                            ) * 100
                    else:
                        st.info(kelly_result["message"])
                except ValueError as error:
                    st.warning(f"Kelly unavailable: {error}")
            elif not kelly_enabled:
                st.caption("Kelly unavailable until you deliberately enter a probability and its source. No probability is inferred from SPY models or AI text.")
            elif kelly_probability is None:
                st.info("Enter a probability explicitly. The planner does not prefill 50% or infer a win rate.")
            else:
                st.info("Kelly requires a valid selected entry, target, and stop (0 < stop < entry < target).")

        target_default = defaults.get("target_allocation") if defaults.get("target_allocation") is not None else policy["target_position_allocation"]
        max_default = defaults.get("max_allocation") if defaults.get("max_allocation") is not None else defaults.get("maximum_allocation")
        max_default = max_default if max_default is not None else policy["max_position_allocation"]
        risk_default = defaults.get("max_loss_per_position") if defaults.get("max_loss_per_position") is not None else defaults.get("maximum_loss_fraction")
        risk_default = risk_default if risk_default is not None else policy["max_loss_per_position"]
        sizing_cols = st.columns(4)
        target_alloc = sizing_cols[0].number_input("Intended allocation (%)", 0.0, 100.0, float(target_default) * 100, 1.0, key=f"plan_target_allocation_{symbol}_{horizon}") / 100
        max_alloc = sizing_cols[1].number_input("Position maximum (%)", 0.0, 100.0, float(max_default) * 100, 1.0, key=f"plan_max_allocation_{symbol}_{horizon}") / 100
        max_risk = sizing_cols[2].number_input("Stop-risk budget (% equity)", 0.0, 100.0, float(risk_default) * 100, .25, key=f"plan_max_risk_{symbol}_{horizon}") / 100
        reserve = sizing_cols[3].number_input("Minimum cash reserve ($)", min_value=0.0, value=float(defaults.get("minimum_cash_reserve", policy["minimum_cash_reserve"]) or 0), step=100.0, key=f"plan_cash_reserve_{symbol}_{horizon}")
        sector_cap = st.number_input("Maximum verified sector allocation (%)", 0.0, 100.0, float(defaults.get("max_sector_allocation", policy["max_sector_allocation"]) or 0) * 100, 1.0, key=f"plan_sector_cap_{symbol}_{horizon}") / 100
        position_type = st.selectbox("Position type", ("long_equity", "long_etf"), format_func=lambda value: "Long stock" if value == "long_equity" else "Long ETF", key=f"plan_type_{symbol}_{horizon}")
        sizing_summary = st.columns(4)
        sizing_summary[0].metric("Current allocation", f"{market_value / totals['total_value']:.1%}")
        sizing_summary[1].metric("Policy / intended", f"{target_alloc:.1%} · {_money(target_alloc * totals['total_value'])}")
        sizing_summary[2].metric("Maximum allocation", f"{max_alloc:.1%} · {_money(max_alloc * totals['total_value'])}")
        sizing_summary[3].metric("Cash / deployable", f"{_money(cash)} / {_money(max(0, cash - max(reserve, policy['minimum_cash_reserve'])))}")
        st.caption("The intended allocation is a user/policy plan, not an optimized or guaranteed recommendation. The permitted amount is capped by cash, position, stop-risk, sector, and policy constraints.")

        st.markdown("**Staged Execution Plan**")
        default_buys = defaults.get("entry_tranches") or [
            {"id": "buy-1", "sequence": 1, "fraction": .4, "trigger_price": entry, "confirmation_conditions": "", "rationale": "Initial planned tranche"},
            {"id": "buy-2", "sequence": 2, "fraction": .3, "trigger_price": None, "confirmation_conditions": "", "rationale": "Set a distinct price or confirmation condition"},
            {"id": "buy-3", "sequence": 3, "fraction": .3, "trigger_price": None, "confirmation_conditions": "", "rationale": "Set a distinct price or confirmation condition"},
        ]
        try:
            import pandas as pd
            buy_editor_data = pd.DataFrame(default_buys, columns=list(dict.fromkeys([*default_buys[0].keys(), "fraction", "trigger_price", "confirmation_conditions", "rationale"])))
        except ImportError:
            buy_editor_data = default_buys
        st.caption("Starting template: 40% / 30% / 30% of intended position. Later triggers are intentionally blank; price declines alone are not buy signals.")
        buy_rows = st.data_editor(buy_editor_data, key=f"plan_buy_editor_{symbol}_{horizon}", num_rows="dynamic", hide_index=True, use_container_width=True)
        default_sells = defaults.get("exit_tranches") or []
        try:
            sell_editor_data = pd.DataFrame(default_sells) if default_sells else pd.DataFrame(columns=["sequence", "trigger_price", "percent", "percent_basis", "condition", "rationale"])
        except (ImportError, NameError):
            sell_editor_data = default_sells
        sell_rows = st.data_editor(sell_editor_data, key=f"plan_sell_editor_{symbol}_{horizon}", num_rows="dynamic", hide_index=True, use_container_width=True)
        rationale = st.text_area("Allocation rationale and constraints", value=str(defaults.get("allocation_rationale", imported.get("allocation_rationale", ""))), key=f"plan_rationale_{symbol}_{horizon}")

        buys = _clean_editor_rows(buy_rows)
        sells = _clean_editor_rows(sell_rows)
        buys = [{**row, "id": str(row.get("id") or f"buy-{index + 1}"), "sequence": int(row.get("sequence") or index + 1), "action": "buy", "status": row.get("status") or "planned"} for index, row in enumerate(buys)]
        sells = [{**row, "id": str(row.get("id") or f"sell-{index + 1}"), "sequence": int(row.get("sequence") or index + 1), "action": "sell", "status": row.get("status") or "planned"} for index, row in enumerate(sells)]

        result = None
        manual_reference_valid = reference_choice.get("is_manual", False)
        can_calculate = reference_confirmed and current_reference is not None
        if totals.get("total_value") is not None and (held_shares == 0 or symbol in quotes):
            result = calculate_position_capacity(
                portfolio_equity=totals["total_value"], current_market_value=market_value,
                current_shares=held_shares, average_cost=holding_info["average_cost"] if holding_info else 0.0,
                cash=cash, entry_price=entry, stop_price=stop, target_price=target,
                target_allocation=target_alloc,
                max_allocation=min(max_alloc, policy["max_position_allocation"]),
                max_risk_fraction=min(max_risk, policy["max_loss_per_position"]),
                minimum_cash_reserve=max(reserve, policy["minimum_cash_reserve"]),
                sector_value=sector_value,
                sector_ceiling=min(sector_cap, policy["max_sector_allocation"]) if classification.get("verification_status") == "Verified" else None,
                current_price=current_reference if can_calculate else None,
                quote_stale=quote_stale and not manual_reference_valid,
                price_basis_compatible=can_calculate,
                price_compatibility_status=quote_assessment.get("status"),
                price_compatibility_note=(quote_assessment.get("message") if not can_calculate else None),
                current_price_is_manual=manual_reference_valid,
                position_type=position_type)

        if result and result.get("status") == "valid":
            try:
                stages = plan_entry_tranches(buys, capacity_result=result, entry_price=entry, stop_price=stop)
                risk_text = "Unavailable" if result["total_stop_risk_dollars"] is None else f"${result['total_stop_risk_dollars']:,.2f} ({result['stop_risk_portfolio_fraction']:.2%} of equity)"
                st.caption(f"Permitted maximum: {result['maximum_total_shares']} total shares · {result['additional_permitted_shares']} additional · ${result['additional_capital_required']:,.2f} capacity · stop-risk scenario {risk_text} · binding constraint: {result['binding_constraint']}. Gap losses can exceed this scenario.")
                if stages["tranches"]:
                    st.dataframe(stages["tranches"], hide_index=True, use_container_width=True)
                if sells:
                    exits = simulate_exit_tranches(sells, current_shares=held_shares, current_price=current_reference, stop_price=stop, portfolio_equity=totals["total_value"])
                    st.caption(f"Sequential exit simulation leaves {exits['remaining_shares']:g} shares; stop fills are not guaranteed and gaps can exceed modeled risk.")
                    st.dataframe(exits["tranches"], hide_index=True, use_container_width=True)
            except (ValueError, TypeError) as error:
                st.info(f"Staged plan preview incomplete: {error}")
        elif result:
            st.warning(result.get("message", "Sizing is unavailable until the reference is resolved."))

        if st.button("Approve and save plan revision", key=f"save_position_plan_{symbol}_{horizon}", type="primary"):
            missing_triggers = [row.get("id") for row in buys if row.get("status") not in {"canceled", "skipped"} and row.get("trigger_price") is None]
            duplicate_triggers = len([row.get("trigger_price") for row in buys if row.get("trigger_price") is not None]) != len(set(row.get("trigger_price") for row in buys if row.get("trigger_price") is not None))
            if max_alloc < target_alloc:
                st.error("Maximum allocation must be at least the intended allocation.")
            elif missing_triggers:
                st.error("Each active buy stage needs its own explicit trigger price for share and cash calculations. Add a distinct trigger, or remove unused stages.")
            elif duplicate_triggers:
                st.error("Buy-stage trigger prices repeat. Review them and set distinct levels if that is your intention; the planner will not infer different stages from one repeated price.")
            elif sum(float(row.get("fraction", 0)) for row in buys) > 1.000001:
                st.error("Entry-stage fractions together cannot exceed 100% of the intended position.")
            elif not can_calculate:
                st.error("Resolve the current reference first. Use a fresh compatible quote or enter and explicitly confirm a manual reference; stale cached prices are not used for actionable sizing.")
            else:
                classification = list_portfolio_classification_records().get(symbol, {})
                sector = classification.get("sector") if classification.get("verification_status") == "Verified" else None
                plan = {"schema_version": 2, "position_type": position_type, "entry_reference": entry, "target_reference": target, "stop_price": stop,
                        "target_allocation": target_alloc, "max_allocation": max_alloc, "max_loss_per_position": max_risk,
                        "minimum_cash_reserve": reserve, "max_sector_allocation": sector_cap, "classified_sector": sector,
                        "entry_tranches": buys, "exit_tranches": sells, "allocation_rationale": rationale,
                        "assumptions": imported.get("assumptions", ""), "constraints": imported.get("constraints", ""),
                        "evidence": imported.get("evidence", ""), "uncertainty": imported.get("uncertainty", ""),
                        "current_reference": {"price": current_reference, "date": manual_date.isoformat() if manual_reference_valid and manual_date else (quote or {}).get("quote_date"),
                                              "source": reference_source, "basis_confirmed_by_user": bool(manual_reference_valid or reference_source == "User-confirmed Alpha Vantage quote basis")},
                        "kelly_assumption": ({"probability": kelly_probability, "source": kelly_source, "evidence": kelly_evidence,
                                               "gap_slippage_fraction": gap_slippage, "unverified": True} if kelly_enabled and kelly_probability is not None else None)}
                save_position_plan(symbol, horizon_label, plan, strategy_revision_id=latest_strategy_revision_id(symbol))
                st.success("Approved position-plan revision saved. Portfolio holdings, shares, and cash were not changed.")
                st.rerun()

def _clean_editor_rows(value):
    records = value.to_dict("records") if hasattr(value, "to_dict") else value
    cleaned = []
    for row in records or []:
        item = {}
        for key, raw in dict(row).items():
            if raw is None:
                continue
            try:
                if isinstance(raw, float) and not isfinite(raw):
                    continue
            except TypeError:
                pass
            item[key] = raw
        if item:
            cleaned.append(item)
    return cleaned


def _render_strategy_details_and_history(symbol: str, strategy: dict, detail: dict) -> None:
    with st.expander("Full Research & Sources"):
        full_thesis = detail.get("investment_thesis") or strategy.get("investment_thesis")
        if full_thesis:
            st.markdown("**Full thesis**")
            st.write(full_thesis)
        st.caption(f"Research date: {detail.get('research_date') or strategy.get('last_review_date') or 'not provided'} · imported claims and citations are not independently verified.")
        reference = detail.get("market_price_reference") or {}
        if reference:
            st.markdown(f"**Imported price reference** · {_money(reference.get('price'))} · {reference.get('date') or 'date unavailable'} · {reference.get('basis') or 'basis unspecified'}")
            if reference.get("source", "").startswith(("https://", "http://")):
                st.markdown(f"[Open price reference]({reference['source']})")
        for evidence in detail.get("evidence", []) if isinstance(detail.get("evidence"), list) else []:
            if isinstance(evidence, dict) and evidence.get("url", "").startswith(("https://", "http://")):
                title = evidence.get("title") or evidence.get("source") or evidence["url"]
                st.markdown(f"- [{title}]({evidence['url']}) · as of {evidence.get('as_of') or 'date not provided'} · unverified reference")
        if not detail.get("evidence"):
            st.caption("No supporting source links were saved.")
    for label, field, fallback in (
        ("Fundamental Analysis", "fundamental_analysis", ""),
        ("Valuation Analysis", "valuation_analysis", strategy.get("valuation_notes", "")),
        ("Technical Analysis", "technical_analysis", ""),
    ):
        with st.expander(label):
            st.write(detail.get(field) or fallback or "No analysis text saved.")
    with st.expander("Bull / Base / Bear Cases"):
        for label, field in (("Bull", "bull_case"), ("Base", "base_case"), ("Bear", "bear_case")):
            st.markdown(f"**{label}**")
            st.write(detail.get(field) or "No case provided.")
    with st.expander("Assumptions & Uncertainties"):
        for label, value in (("Assumptions", strategy.get("assumptions")),
                             ("Uncertainties", strategy.get("uncertainties")),
                             ("Research gaps", detail.get("research_gaps"))):
            st.markdown(f"**{label}**")
            st.write(value or "None recorded.")
    with st.expander("Strategy Revision History"):
        _render_strategy_history(symbol)
        scenario_history = list_research_strategy_scenarios(symbol, history=True)
        if scenario_history:
            st.markdown("**Horizon scenario revisions**")
            for revision in scenario_history:
                scenario = revision["scenario"]
                st.caption(f"{revision['horizon']} · approved {revision.get('approved_at')} · {revision.get('provider') or 'manual'} / {revision.get('model') or '—'} · target {_scenario_range(scenario, 'target_price_low', 'target_price_high', fallback='target_price')}")
                with st.expander(f"Saved {revision['horizon']} scenario details · {revision.get('approved_at')}"):
                    st.json(scenario, expanded=False)


@st.dialog("Import Strategy", width="large")
def _strategy_import_dialog(symbol: str, quote: dict | None) -> None:
    st.caption("Paste a version 1 or 2 strategy JSON export (version 2 is current). Importing is local and does not call an AI provider. Imported claims and source links are references, not independently verified facts.")
    raw = st.text_area("Strategy JSON", height=300, key=f"strategy_import_json_{symbol}", placeholder="Paste the JSON export here…")
    preview_key = f"strategy_import_preview_{symbol}"
    if st.button("Validate / Preview", key=f"validate_strategy_import_{symbol}"):
        try:
            normalized = normalize_strategy_import(raw)
            canonical = validate_normalized_strategy(normalized, symbol)
            current_quote, quote_note = compatible_current_quote(
                canonical, quote, is_stale=bool(quote and quote_is_stale(quote)),
            )
            current_date = quote.get("quote_date") if quote else None
            prepared = preview_import(
                canonical, current_price=current_quote, current_quote_date=current_date,
            )
            if quote_note:
                prepared["validation_warnings"].append(quote_note)
            st.session_state[preview_key] = prepared
            st.session_state.pop(f"strategy_import_error_{symbol}", None)
        except ValueError as error:
            st.session_state.pop(preview_key, None)
            st.session_state[f"strategy_import_error_{symbol}"] = str(error)
    if st.session_state.get(f"strategy_import_error_{symbol}"):
        st.error(st.session_state[f"strategy_import_error_{symbol}"])
    preview = st.session_state.get(preview_key)
    if preview:
        saved = get_strategy_record(symbol)
        st.markdown(f"### {preview['ticker']} · {preview.get('company_name') or 'Company name not provided'}")
        st.caption(f"Research date: {preview.get('research_date') or 'not provided'} · Strategy type: {preview.get('strategy_type') or 'not specified'} · schema v{preview['schema_version']}")
        if quote:
            st.caption(f"Cached Alpha Vantage quote: {quote.get('quote_date') or 'date unavailable'}. Current RRR is shown only when source, date, and raw price basis match the imported reference." + (" Quote is stale." if quote_is_stale(quote) else ""))
        st.caption("All imported evidence and company names are user supplied. Source URLs are retained as references and are not independently verified.")
        normalization = preview.get("normalization_metadata") or {}
        changes = normalization.get("changes", [])
        if changes:
            st.markdown("**Fields normalized**")
            st.dataframe(changes, hide_index=True, width="stretch")
        preserved = normalization.get("preserved_unrecognized", {})
        if preserved:
            with st.expander("Unmapped content preserved for review"):
                st.json(preserved, expanded=False)
        for evidence in preview.get("evidence", []):
            label = evidence.get("title") or evidence.get("source") or evidence["url"]
            st.link_button(f"Source reference: {label}", evidence["url"])
            st.caption(f"{evidence.get('source') or 'Source not specified'} · as of {evidence.get('as_of') or 'date not provided'} · unverified reference")
        st.markdown("**Investment thesis**")
        st.write(preview["investment_thesis"])
        if preview.get("position_sizing"):
            st.markdown("**Position-sizing proposal · not yet approved**")
            st.json(preview["position_sizing"], expanded=False)
            st.caption("Allocation fractions are suggestions only. The dashboard recalculates all share quantities, dollar amounts, and stop risk from local portfolio data.")
        scenario_rows = []
        for key in ("short_term", "medium_term", "long_term"):
            scenario = preview["scenarios"][key]
            setup = preview["calculated_setups"][key]
            rrr = setup.get("planned_rrr")
            scenario_rows.append({
                "Horizon": scenario["horizon"],
                "Entry": f"{_money(scenario['entry_price_low'])} – {_money(scenario['entry_price_high'])}",
                "Target": f"{_money(scenario['target_price_low'])} – {_money(scenario['target_price_high'])}",
                "Stop": _money(scenario["stop_price"]),
                "Planned RRR · range endpoints": (f"{rrr:.2f}×" if rrr is not None else
                                f"{setup['planned_rrr_range'][0]:.2f}–{setup['planned_rrr_range'][1]:.2f}×" if len(setup.get("planned_rrr_range", [])) == 2 and all(value is not None for value in setup["planned_rrr_range"]) else "Unavailable"),
                "Current RRR": (f"{setup['current_rrr']:.2f}×" if setup.get("current_rrr") is not None else
                                f"{setup['current_rrr_range'][0]:.2f}–{setup['current_rrr_range'][1]:.2f}×" if len(setup.get("current_rrr_range", [])) == 2 and all(value is not None for value in setup["current_rrr_range"]) else "Unavailable"),
            })
        st.dataframe(scenario_rows, hide_index=True, use_container_width=True)
        for key in ("short_term", "medium_term", "long_term"):
            scenario = preview["scenarios"][key]
            with st.expander(f"{scenario['horizon']} details"):
                st.markdown("**Thesis / entry conditions**\n\n" + (scenario.get("thesis") or "—") + "\n\n" + (scenario.get("entry_conditions") or "—"))
                st.markdown("**Bull / base / bear**\n\n" + "\n\n".join(scenario.get(field) or "—" for field in ("bull_case", "base_case", "bear_case")))
                st.markdown("**Invalidation / evidence / uncertainty**\n\n" + "\n\n".join(scenario.get(field) or "—" for field in ("invalidation_conditions", "fundamental_invalidation", "evidence", "assumptions", "uncertainty")))
        gaps = [
            f"{key}: {field.replace('_', ' ')} is null"
            for key, scenario in preview["scenarios"].items()
            for field in ("entry_price_low", "target_price_low", "stop_price")
            if scenario.get(field) is None
        ]
        if gaps:
            st.info("Missing numeric fields: " + "; ".join(gaps))
        st.markdown("**Research summaries**")
        summary_cols = st.columns(2)
        summary_cols[0].markdown("**Fundamental**\n\n" + (preview.get("fundamental_analysis") or "—") + "\n\n**Valuation**\n\n" + (preview.get("valuation_analysis") or "—"))
        summary_cols[1].markdown("**Technical**\n\n" + (preview.get("technical_analysis") or "—") + "\n\n**Bull / base / bear**\n\n" + "\n\n".join(preview.get(field) or "—" for field in ("bull_case", "base_case", "bear_case")))
        st.caption("Catalysts: " + (preview.get("catalysts") or "—") + " · Risks: " + (preview.get("risks") or "—"))
        st.caption("Uncertainties / gaps: " + " · ".join(filter(None, (preview.get("uncertainties"), preview.get("research_gaps")))) if preview.get("uncertainties") or preview.get("research_gaps") else "Uncertainties / gaps: none provided")
        for warning in preview.get("validation_warnings", []):
            st.warning(warning)
        if saved and saved.get("investment_thesis"):
            compare_fields = ("investment_thesis", "strategy_type", "entry_price_low", "entry_price_high", "target_price", "invalidation_price", "catalysts", "risks")
            changes = []
            imported_record = imported_strategy_record(preview, saved)
            for field in compare_fields:
                old, new = saved.get(field), imported_record.get(field)
                if old != new:
                    changes.append({"Field": field.replace("_", " ").title(), "Approved": old if old not in (None, "") else "—", "Import": new if new not in (None, "") else "—"})
            st.markdown("**Differences from approved strategy**")
            st.dataframe(changes or [{"Field": "No differences", "Approved": "—", "Import": "—"}], hide_index=True, use_container_width=True)
            approved_scenarios = {item["horizon"]: item["scenario"] for item in list_research_strategy_scenarios(symbol)}
            scenario_changes = []
            for key, scenario in preview["scenarios"].items():
                approved = approved_scenarios.get(scenario["horizon"], {})
                for field in ("entry_price_low", "entry_price_high", "target_price_low", "target_price_high", "stop_price", "thesis"):
                    imported_value = scenario.get(field)
                    approved_field = {"target_price_low": "target_price", "target_price_high": "target_price", "stop_price": "invalidation_price"}.get(field, field)
                    approved_value = approved.get(approved_field)
                    if imported_value != approved_value and (imported_value is not None or approved_value is not None):
                        scenario_changes.append({"Horizon": scenario["horizon"], "Field": field.replace("_", " "), "Approved": approved_value if approved_value is not None else "—", "Import": imported_value if imported_value is not None else "—"})
            if scenario_changes:
                st.caption("Horizon scenario differences")
                st.dataframe(scenario_changes, hide_index=True, use_container_width=True)
        review_flags = normalization.get("review_flags", [])
        reviewed = st.checkbox(
            "I reviewed the preserved fields that could not be normalized automatically",
            key=f"strategy_import_reviewed_{symbol}",
        ) if review_flags else True
        if review_flags:
            st.warning("Some content remains unrecognized but has been preserved in the strategy record. Review it above and confirm before saving.")
        if st.button("Import & Save", key=f"approve_strategy_import_{symbol}", type="primary", disabled=not reviewed):
            try:
                approve_import(preview)
                st.session_state.pop(preview_key, None)
                st.session_state.pop(f"strategy_import_json_{symbol}", None)
                st.session_state.pop("strategy_import_open_symbol", None)
                st.success("Imported strategy saved as a new approved revision.")
                st.rerun()
            except (ValueError, TypeError) as error:
                st.error(f"Import could not be saved: {error}")
    if st.button("Cancel", key=f"cancel_strategy_import_{symbol}"):
        st.session_state.pop(preview_key, None)
        st.session_state.pop(f"strategy_import_error_{symbol}", None)
        st.session_state.pop(f"strategy_import_json_{symbol}", None)
        st.session_state.pop(f"strategy_import_reviewed_{symbol}", None)
        st.session_state.pop("strategy_import_open_symbol", None)
        st.rerun()


def _money(value) -> str:
    return f"${value:,.2f}" if value is not None else "Unavailable"


def _render_buy_zone(symbol: str) -> None:
    alert = get_buy_zone_alert(symbol)
    with st.popover("Buy-zone alert"):
        with st.form(f"buy_zone_form_{symbol}"):
            low = st.number_input("Lower price", min_value=0.0, value=float(alert["lower_price"]) if alert else 0.0, step=1.0)
            high = st.number_input("Upper price", min_value=0.0, value=float(alert["upper_price"]) if alert else 0.0, step=1.0)
            enabled = st.checkbox("Enable local alert condition", value=bool(alert["enabled"]) if alert else False)
            if st.form_submit_button("Save condition"):
                if enabled and (high <= 0 or low > high):
                    st.error("Enter a positive upper price and a valid range.")
                else:
                    save_buy_zone_alert(symbol, low, high, enabled)
                    st.rerun()
    if alert and alert["enabled"]:
        quote = list_cached_quotes().get(symbol.upper())
        if not quote:
            st.caption("Buy-zone condition enabled; waiting for a saved quote.")
        else:
            in_zone = alert["lower_price"] <= quote["price"] <= alert["upper_price"]
            st.caption(f"Saved quote {quote['quote_date']} {'meets' if in_zone else 'does not meet'} the buy-zone condition. Review manually; no order is placed.")


def _render_strategy_history(symbol: str) -> None:
    revisions = list_strategy_revisions(symbol)
    if not revisions:
        st.caption("No revisions saved yet.")
    for revision in revisions:
        with st.expander(f"{revision['recorded_at']} · review date {revision['last_review_date'] or 'not set'}"):
            st.markdown("**Investment thesis**\n\n" + (revision["investment_thesis"] or "—"))
            left, right = st.columns(2)
            left.markdown("**Catalysts**\n\n" + (revision["catalysts"] or "—"))
            right.markdown("**Risks**\n\n" + (revision["risks"] or "—"))
            st.caption(f"Horizon {revision['investment_horizon']} · {revision['thesis_status']} · Entry {_money(revision['entry_price_low'])}–{_money(revision['entry_price_high'])} · Target {_money(revision['target_price'])} · Stop {_money(revision.get('invalidation_price'))}")
            st.markdown("**Valuation**\n\n" + (revision["valuation_notes"] or "—"))
            st.markdown("**Invalidation**\n\n" + (revision["stop_invalidation"] or "—"))
            if revision.get("legacy_entry_zone"):
                st.markdown("**Prior entry notes**\n\n" + revision["legacy_entry_zone"])
            if revision.get("bullish_scenario"):
                st.markdown("**Bullish scenario**\n\n" + revision["bullish_scenario"])
            if revision.get("bearish_scenario"):
                st.markdown("**Bearish scenario**\n\n" + revision["bearish_scenario"])
            if revision.get("assumptions"):
                st.markdown("**Assumptions**\n\n" + revision["assumptions"])
            if revision.get("uncertainties"):
                st.markdown("**Uncertainties**\n\n" + revision["uncertainties"])
            try:
                historical_detail = json.loads(revision.get("structured_analysis_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                historical_detail = {}
            if historical_detail:
                with st.expander("Structured research saved with this revision"):
                    st.json(historical_detail, expanded=False)
            if revision.get("ai_price_estimates"):
                st.warning("Price levels in this revision are AI estimates, not verified market data.")

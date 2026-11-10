"""Deterministic market snapshot and provider-independent AI market briefing."""

from __future__ import annotations

from datetime import date, datetime, timezone
import os

from dashboard.ai_providers import DEFAULT_PROVIDER, generate_text, load_ai_environment
from dashboard.analytics import momentum
from dashboard.market_history import BROAD_MARKET_SYMBOLS, MACRO_PROXY_SYMBOLS, SECTOR_SYMBOLS
from dashboard.market_visuals import (
    breadth_rows, sector_return_rows, sector_rotation_points, sector_rotation_pulse,
)
from dashboard.market_regime import build_regime_features, historical_analog_distribution
from dashboard.cboe_volatility import volatility_summary


ASSESSMENT_HEADINGS = (
    "Market Character", "Top 3 Market Signals", "What Changed Since the Previous Briefing",
    "Volatility & Market Risk", "Portfolio and RRR Implications", "What to Watch Next",
)


class AssessmentValidationError(ValueError):
    """The model response did not follow the required assessment structure."""


def validate_market_assessment(text: str) -> str:
    import re
    if not isinstance(text, str) or not text.strip():
        raise AssessmentValidationError("The AI provider returned an empty market assessment.")
    missing = [heading for heading in ASSESSMENT_HEADINGS
               if not re.search(rf"(?im)^\s*#+\s*{re.escape(heading)}\s*:??\s*$", text)]
    if missing:
        raise AssessmentValidationError("The AI response is incomplete; missing sections: " + ", ".join(missing))
    return text.strip()


RETURN_WINDOWS = (5, 20, 60)
ROTATION_STRENGTH_WINDOW = 20
ROTATION_MOMENTUM_WINDOW = 5


def _aligned_relative_performance(histories: dict[str, list[dict]], symbols, benchmark="SPY") -> dict:
    """Compute ETF/SPY return differences and ratio acceleration on common dates."""
    benchmark_map = {row.get("trading_date"): row.get("close") for row in histories.get(benchmark, [])}
    output = {}
    for symbol in symbols:
        asset_map = {row.get("trading_date"): row.get("close") for row in histories.get(symbol, [])}
        common = sorted((set(asset_map) & set(benchmark_map)) - {None})
        item = {"as_of": common[-1] if common else None}
        for window in RETURN_WINDOWS:
            required = window + 1
            if len(common) < required:
                item[str(window)] = {"status": "insufficient_history", "required_observations": required,
                                     "available_observations": len(common), "as_of": item["as_of"]}
                continue
            segment = common[-required:]
            if any((date.fromisoformat(after) - date.fromisoformat(before)).days > 4
                   for before, after in zip(segment, segment[1:])):
                item[str(window)] = {"status": "missing_observations", "required_observations": required,
                                     "available_observations": len(segment), "as_of": segment[-1]}
                continue
            try:
                asset = [float(asset_map[day]) for day in segment]
                spy = [float(benchmark_map[day]) for day in segment]
            except (TypeError, ValueError):
                item[str(window)] = {"status": "invalid_price", "as_of": segment[-1]}
                continue
            if min(asset) <= 0 or min(spy) <= 0:
                item[str(window)] = {"status": "invalid_price", "as_of": segment[-1]}
                continue
            asset_return, spy_return = asset[-1] / asset[0] - 1, spy[-1] / spy[0] - 1
            item[str(window)] = {"status": "available", "asset_return": asset_return,
                                 "benchmark_return": spy_return,
                                 "excess_return_pp": (asset_return - spy_return) * 100,
                                 "required_observations": required, "as_of": segment[-1]}
        span = 2 * 20 + 1
        if len(common) >= span:
            segment = common[-span:]
            if any((date.fromisoformat(after) - date.fromisoformat(before)).days > 4
                   for before, after in zip(segment, segment[1:])):
                item["relative_momentum_acceleration_20d_pp"] = None
                item["acceleration_method"] = "Unavailable: a gap exists in the latest 41 common observations."
                output[symbol] = item
                continue
            ratio = [float(asset_map[day]) / float(benchmark_map[day]) for day in segment]
            prior = ratio[20] / ratio[0] - 1
            latest = ratio[-1] / ratio[20] - 1
            item["relative_momentum_acceleration_20d_pp"] = (latest - prior) * 100
            item["acceleration_method"] = "Latest 20-session ETF/SPY ratio return minus preceding 20-session ETF/SPY ratio return, in percentage points."
        else:
            item["relative_momentum_acceleration_20d_pp"] = None
            item["acceleration_method"] = "Unavailable: requires 41 common daily closes."
        output[symbol] = item
    return output


def _sector_regime_snapshot(histories, volatility, previous_snapshot=None) -> dict:
    relative = _aligned_relative_performance(histories, (*SECTOR_SYMBOLS.keys(), "SPY"))
    sector_windows = {}
    for window in RETURN_WINDOWS:
        pulse = sector_rotation_pulse(histories, SECTOR_SYMBOLS, window)
        rows = pulse.get("rows", [])
        values = [row.get("return") for row in rows if row.get("return") is not None]
        sector_windows[str(window)] = {
            "as_of": max((row.get("as_of") for row in rows if row.get("as_of")), default=None),
            "positive_count": pulse.get("positive_count"), "available_count": pulse.get("available_sector_count"),
            "universe_count": pulse.get("sector_count"), "strongest": pulse.get("strongest"),
            "weakest": pulse.get("weakest"), "leadership_spread_pp": pulse.get("leadership_spread_pp"),
            "population_stddev_pp": (sum((value - sum(values)/len(values))**2 for value in values) / len(values))**0.5 * 100 if values else None,
            "defensive_return": pulse.get("defensive_return"), "cyclical_return": pulse.get("cyclical_return"),
            "defensive_vs_cyclical_pp": pulse.get("defensive_vs_cyclical_pp"),
            "participation_change": {"prior_positive": pulse.get("previous_positive_count"),
                                     "current_positive": pulse.get("current_positive_count"),
                                     "status": pulse.get("participation")},
            "source": "Tiingo split-and-dividend-adjusted closes; fixed standard 11-sector universe",
        }

    points = sector_rotation_points(
        histories, SECTOR_SYMBOLS, strength_window=ROTATION_STRENGTH_WINDOW,
        momentum_window=ROTATION_MOMENTUM_WINDOW,
    )
    rotation_symbols = {}
    for point in points:
        trail = point.get("trail", [])
        current_momentum = point.get("relative_momentum")
        prior_momentum = trail[-2].get("relative_momentum") if len(trail) >= 2 else None
        rotation_symbols[point["symbol"]] = {
            "name": point.get("name"), "quadrant": point.get("quadrant"),
            "relative_strength_pct": point.get("relative_strength", 0) * 100,
            "relative_momentum_pp": current_momentum * 100 if current_momentum is not None else None,
            "relative_momentum_acceleration_pp": (current_momentum - prior_momentum) * 100 if current_momentum is not None and prior_momentum is not None else None,
            "as_of": point.get("trading_date"),
        }
    previous_rotation = (previous_snapshot or {}).get("sector_rotation", {}).get("symbols", {})
    transitions = []
    for symbol, current in rotation_symbols.items():
        before = previous_rotation.get(symbol, {}).get("quadrant")
        if before and current.get("quadrant") and before != current["quadrant"]:
            transitions.append({"symbol": symbol, "from": before, "to": current["quadrant"]})
    vix_summary = volatility_summary(volatility)
    return {
        "relative_performance": relative,
        "sector_windows": sector_windows,
        "sector_rotation": {"symbols": rotation_symbols, "transitions_since_previous": transitions,
                            "strength_lookback": ROTATION_STRENGTH_WINDOW,
                            "momentum_lookback": ROTATION_MOMENTUM_WINDOW,
                            "acceleration": "Difference between current 5-session change in the 20-session sector/SPY ratio return and the prior observation's value; in percentage points. This is a descriptive change, not predictive.",
                            "as_of": max((row.get("as_of") for row in rotation_symbols.values() if row.get("as_of")), default=None)},
        "risk_volatility": {
            "index": "Cboe VIXCLS sourced through FRED, not an investable security",
            "close": vix_summary.get("close"), "previous_close": vix_summary.get("previous_close"),
            "previous_change_points": vix_summary.get("previous_change_points"),
            "previous_change_percent": vix_summary.get("previous_change_percent"),
            "change_5d_points": vix_summary.get("change_5d_points"),
            "change_20d_points": vix_summary.get("change_20d_points"),
            "percentile_252": vix_summary.get("percentile_252"), "vs_sma20": vix_summary.get("vs_sma20"),
            "vs_sma20_points": vix_summary.get("vs_sma20_points"), "as_of": vix_summary.get("as_of"),
            "retrieved_at": vix_summary.get("retrieved_at"), "source": vix_summary.get("source"),
            "status": vix_summary.get("status"), "condition": vix_summary.get("condition"),
        },
    }


def compare_market_snapshots(current: dict, previous: dict | None) -> dict:
    """Compare actual saved structured inputs, reporting only computable changes."""
    if not previous:
        return {"status": "no_previous_snapshot", "message": "No previous saved market snapshot was available."}
    prior = previous.get("input_snapshot", previous)
    current_rel, prior_rel = current.get("relative_performance", {}), prior.get("relative_performance", {})
    relative_changes = []
    for symbol, current_item in current_rel.items():
        before_item = prior_rel.get(symbol, {})
        for window in ("5", "20", "60"):
            now, old = current_item.get(window, {}), before_item.get(window, {})
            if now.get("excess_return_pp") is not None and old.get("excess_return_pp") is not None:
                delta = now["excess_return_pp"] - old["excess_return_pp"]
                relative_changes.append({"symbol": symbol, "window": f"{window}D", "change_in_excess_pp": delta,
                                         "direction": "strengthening" if delta > .05 else "weakening" if delta < -.05 else "little change"})
    old_quadrants = prior.get("sector_rotation", {}).get("symbols", {})
    transitions = [{"symbol": symbol, "from": old_quadrants[symbol].get("quadrant"), "to": value.get("quadrant")}
                   for symbol, value in current.get("sector_rotation", {}).get("symbols", {}).items()
                   if symbol in old_quadrants and old_quadrants[symbol].get("quadrant") != value.get("quadrant")]
    now_vix, old_vix = current.get("risk_volatility", {}), prior.get("risk_volatility", {})
    vix_change = (now_vix.get("close") - old_vix.get("close")
                  if now_vix.get("close") is not None and old_vix.get("close") is not None else None)
    return {"status": "compared" if relative_changes or transitions or vix_change is not None else "partial_comparison",
            "previous_as_of": prior.get("market_data_as_of"),
            "previous_generated_at": previous.get("generated_at"),
            "vix_level_change_points": vix_change, "relative_performance_changes": relative_changes,
            "sector_quadrant_transitions": transitions,
            "note": "Changes compare the current structured snapshot with the actual previous saved input snapshot. They are descriptive and do not imply causality."}


def build_market_snapshot(
    histories: dict[str, list[dict]], portfolio_context: list[dict] | None = None,
    volatility_history: dict[str, list[dict]] | None = None,
    previous_assessment: dict | None = None,
    upcoming_events: list[dict] | None = None,
) -> dict:
    """Build a compact, deterministic snapshot from the local adjusted-history cache."""
    symbols = list(dict.fromkeys((*BROAD_MARKET_SYMBOLS, *SECTOR_SYMBOLS, *MACRO_PROXY_SYMBOLS)))
    symbols = [symbol for symbol in symbols if symbol in histories]
    as_of = {symbol: (histories[symbol][-1].get("trading_date") if histories[symbol] else None) for symbol in symbols}
    freshness = {}
    for symbol, observed in as_of.items():
        try:
            age = (datetime.now(timezone.utc).date() - date.fromisoformat(observed)).days
            freshness[symbol] = {"status": "stale" if age > 4 else "recent", "calendar_days_old": age}
        except (TypeError, ValueError):
            freshness[symbol] = {"status": "unavailable", "calendar_days_old": None}
    volatility = volatility_summary(volatility_history or {})
    regime_features = build_regime_features(histories, volatility)
    analogs = historical_analog_distribution(
        (volatility_history or {}).get("VIX", []), histories.get("SPY", []), horizon=20,
    )
    old_snapshot = previous_assessment.get("input_snapshot") if previous_assessment else None
    sector_regime = _sector_regime_snapshot(histories, volatility_history or {}, old_snapshot)
    event_snapshot = []
    for event in upcoming_events or []:
        if event.get("status") not in ("Confirmed", "Estimated", "Tentative"):
            continue
        verified_at = event.get("last_verified_at")
        try:
            verified_age = (datetime.now(timezone.utc) - datetime.fromisoformat(verified_at.replace("Z", "+00:00")).astimezone(timezone.utc)).days
        except (AttributeError, ValueError):
            verified_age = None
        stale_after = 8 if event.get("provider") == "bls_ical" else 30 if event.get("provider") in ("bea_ical", "fec_election_dates", "manual_earnings", "manual_public_event") else 60
        event_snapshot.append({key: event.get(key) for key in ("name", "category", "event_date", "earnings_session", "exact_announcement_time_utc", "start_utc", "source_timezone", "source_url", "provider", "status", "source_verification", "last_verified_at", "related_symbols")}
                              | {"freshness": "unknown" if verified_age is None else "stale" if verified_age > stale_after else "recent"})
    snapshot = {
        "source": "Local SQLite cache; Tiingo daily split-and-dividend-adjusted close",
        "adjustment_basis": "split_and_dividend_adjusted",
        "as_of_dates": as_of,
        "freshness_by_symbol": freshness,
        "market_data_as_of": max((day for day in [*as_of.values(), volatility.get("as_of")] if day), default=None),
        "market_data_as_of_timestamp": max(
            (stamp for rows in [*histories.values(), *(volatility_history or {}).values()]
             for row in rows for stamp in [row.get("retrieved_at")] if stamp), default=None,
        ),
        "regime_features": regime_features,
        **sector_regime,
        "spy_realized_volatility_annualized": regime_features.get("spy_realized_volatility_annualized", {}),
        "historical_context": {"vix_percentile_analogs_spy_20d": analogs},
        "upcoming_market_events": [event for event in event_snapshot
                                    if portfolio_context is not None or event.get("category") != "Earnings"],
        "calculation_windows": {
            "relative_performance": list(RETURN_WINDOWS),
            "sector_participation_dispersion": list(RETURN_WINDOWS),
            "relative_momentum_acceleration": "40 sessions plus current observation (41 aligned closes)",
            "sector_rotation": {"relative_strength": ROTATION_STRENGTH_WINDOW,
                                "relative_momentum": ROTATION_MOMENTUM_WINDOW,
                                "relative_momentum_acceleration": "change in rotation y-coordinate over one daily observation"},
            "spy_realized_volatility": [20, 60, 252],
        },
        "broad_market_etf_breadth_proxies": breadth_rows(histories, BROAD_MARKET_SYMBOLS),
        "sector_performance": {f"{window}_trading_day": sector_return_rows(histories, SECTOR_SYMBOLS, window) for window in RETURN_WINDOWS},
        "macro_proxy_momentum": {
            symbol: {"5_trading_day": momentum(histories.get(symbol, []), 5),
                     "20_trading_day": momentum(histories.get(symbol, []), 20),
                     "as_of": as_of.get(symbol)} for symbol in MACRO_PROXY_SYMBOLS
        },
        "limitations": [
            "ETF advances and declines are proxies, not constituent-level market breadth.",
            "Missing observations remain unavailable; no values are imputed.",
            "VIX9D/VIX carries its own common-date and freshness metadata; VIX3M/VIX is unavailable without an official historical series.",
            "Price changes do not establish investor fund flows.",
            "Possible catalysts are unverified unless a cited web source is provided.",
            "Historical analog feature dates do not look ahead, but Tiingo serves current adjusted history rather than archived point-in-time data vintages; later corporate-action adjustments may restate prior observations.",
        ],
    }
    if portfolio_context:
        snapshot["optional_portfolio_context"] = portfolio_context
    snapshot["input_data_completeness"] = assess_snapshot_completeness(snapshot)
    snapshot["previous_snapshot_comparison"] = compare_market_snapshots(snapshot, previous_assessment)
    return snapshot


def assess_snapshot_completeness(snapshot: dict) -> dict:
    """Report absent/stale required market inputs without treating them as zero."""
    as_of = snapshot.get("as_of_dates", {})
    required = ("SPY", "RSP", "QQQ", *SECTOR_SYMBOLS.keys())
    missing = [symbol for symbol in required if not as_of.get(symbol)]
    stale = [symbol for symbol in required if snapshot.get("freshness_by_symbol", {}).get(symbol, {}).get("status") == "stale"]
    vix = snapshot.get("risk_volatility", {})
    if vix.get("status") == "unavailable":
        missing.append("VIX (FRED VIXCLS)")
    elif vix.get("status") == "stale":
        stale.append("VIX (FRED VIXCLS)")
    return {"status": "complete" if not missing and not stale else "incomplete",
            "missing_symbols": missing, "stale_symbols": stale,
            "message": "All required inputs have recent observations." if not missing and not stale
            else "Missing and stale inputs are excluded from claims; request updates or state the gaps."}


def build_limited_portfolio_context() -> list[dict]:
    """Build opted-in context without quantity, cash, market value, or cost basis."""
    from dashboard.data_access import get_strategy_record, list_cached_quotes, list_holdings
    from dashboard.setup_calculations import calculate_long_setup

    quotes = list_cached_quotes()
    context = []
    from dashboard.data_access import list_portfolio_classification_records
    from dashboard.market_data import quote_is_stale
    classifications = list_portfolio_classification_records()
    for holding in list_holdings():
        symbol = holding["symbol"].upper()
        strategy = get_strategy_record(symbol)
        row = {"symbol": symbol, "horizon": holding.get("investment_horizon"),
               "thesis_status": holding.get("thesis_status")}
        quote = quotes.get(symbol)
        classification = classifications.get(symbol, {})
        verified_sector = (classification.get("sector") if classification.get("verification_status") == "Verified" else None)
        if strategy:
            low, high = strategy.get("entry_price_low"), strategy.get("entry_price_high")
            exact = low if low is not None and high is not None and float(low) == float(high) else None
            setup = calculate_long_setup(
                entry_price=exact, entry_low=low if exact is None else None,
                entry_high=high if exact is None else None,
                stop_price=strategy.get("invalidation_price"), target_price=strategy.get("target_price"),
                current_price=(quote or {}).get("price") if quote and not quote_is_stale(quote) else None,
            )
            row.update({"planned_rrr": setup.get("planned_rrr"), "current_rrr": setup.get("current_rrr"),
                        "rrr_status": setup.get("status"),
                        "missing_strategy_inputs": [key for key in ("entry_price_low", "target_price", "invalidation_price") if strategy.get(key) is None]})
        else:
            row.update({"planned_rrr": None, "current_rrr": None})
        row["current_rrr_quote_date"] = quote.get("quote_date") if quote and not quote_is_stale(quote) else None
        row["current_quote_status"] = "missing" if not quote else "stale" if quote_is_stale(quote) else "recent"
        row["verified_sector"] = verified_sector
        context.append(row)
    return context


def build_market_portfolio_context(histories: dict[str, list[dict]]) -> dict:
    """Opt-in holdings exposure and aggregate cached historical risk, without sizes."""
    from dashboard.data_access import get_cash_balance, list_cached_quotes, list_holdings
    from dashboard.portfolio_risk import portfolio_risk_analysis

    holdings = list_holdings()
    quotes = list_cached_quotes()
    risk = portfolio_risk_analysis(holdings, quotes, get_cash_balance(), histories, lookback=252)
    position_context = build_limited_portfolio_context()
    weights = risk.get("capital_weights", {})
    verified_sector_exposure = {}
    for row in position_context:
        row["capital_weight_percent"] = weights.get(row["symbol"])
        sector = row.get("verified_sector")
        if sector:
            verified_sector_exposure[sector] = verified_sector_exposure.get(sector, 0.0) + (weights.get(row["symbol"]) or 0.0)
    return {
        "position_context": position_context,
        "verified_sector_exposure_percent": verified_sector_exposure,
        "aggregate_risk": {
            "status": risk.get("status"), "lookback_sessions": risk.get("lookback"),
            "observations": risk.get("observations"), "required_observations": risk.get("required_observations"),
            "covered_positions": risk.get("covered_positions"), "position_count": risk.get("position_count"),
            "portfolio_beta_vs_spy": risk.get("portfolio_beta"),
            "invested_equity_beta_vs_spy": risk.get("invested_equity_beta"),
            "annualized_volatility": risk.get("annualized_volatility"),
            "correlation_to_spy": risk.get("correlation_to_spy"),
            "missing_history_symbols": risk.get("missing_history_symbols", []),
        },
        "privacy_note": "No shares, cash balance, cost basis, position values, or dollar exposures included.",
    }


def generate_market_briefing(
    snapshot: dict, web_search: bool = False, *, provider: str | None = None,
    model: str | None = None, max_output_tokens: int = 1400,
) -> dict:
    """Generate and normalize a market report after an explicit UI action."""
    load_ai_environment()
    provider = (provider or os.environ.get("AI_PROVIDER", DEFAULT_PROVIDER)).lower()
    instructions = """You are a careful market analyst. Use only supplied observations, scheduled-event records and, if enabled, cited web-search sources. Never invent prices, headlines, events, event dates, fund flows, probabilities, sample sizes, or dates. Python calculated all numeric indicators; do not recalculate them. Distinguish VERIFIED OBSERVATION, INTERPRETATION, HYPOTHESIS, and UNKNOWN. Do not claim price changes identify causes or investor flows. State material stale/missing inputs. The regime is a descriptive rule label, not a forecast. Historical analogs are unvalidated associations and must retain their sample size and caveat. Scheduled events have explicit Confirmed, Estimated, or Tentative labels; preserve those labels and never turn tentative dates into confirmed dates. If calendar freshness is stale or unknown, do not present the event as currently verified. A calendar event does not establish a market reaction. Portfolio context exists only when explicitly enabled; use verified sector labels and supplied exposure weights, never infer quantities or recommend trades. Current RRR is available only when supplied using recent quotes. Label user monitoring thresholds as analyst-defined unless the input says they were statistically validated.

For breadth, separate absolute returns, relative excess returns, and changes in participation. RSP beating SPY or sectors beating SPY does not alone mean absolute breadth is improving. If the indexes and most sector ETFs decline, call that relative resilience during a selloff, not healthy broadening. Sector and broad-market ETFs are participation proxies, not constituent-level breadth unless actual constituent data is supplied.

Return 400-700 words maximum, organized under exactly these six Markdown headings:
## Market Character — one short headline, 2-3 interpretation sentences, including conflicting evidence.
## Top 3 Market Signals — rank three signals by relevance. For each: Observation, Interpretation, Evidence, and What would weaken it. Avoid repeating the same return values elsewhere.
## What Changed Since the Previous Briefing — use the actual saved-snapshot comparison; clearly say none exists if absent.
## Volatility & Market Risk — interpret VIX alongside SPY, breadth proxies, and rotation; rising volatility alone is not bearish direction.
## Portfolio and RRR Implications — when context is absent say it was not included. When present, connect only valid exposure/verified sector, report missing or stale inputs, and do not recommend trades.
## What to Watch Next — at most three specific measurable indicators, their change to monitor, and how that would strengthen/weaken interpretation; label thresholds analyst-defined or validated.
Possible causes remain unverified unless supported by cited news. If web search was not requested, explicitly say causal explanations remain unverified. Do not invent hypothetical probabilities or forecasts."""
    prompt = "Analyze the compact structured JSON snapshot. Do not repeat a raw-data dump. Follow the six required headings exactly. "
    if web_search:
        prompt += "Use current web search and cite every news-supported explanation. Separate news-supported context from price-only hypotheses. "
    else:
        prompt += "Do not use external news; state that causal explanations remain unverified because news evidence was not requested. "
    import json
    prompt += "\nSNAPSHOT_JSON:\n" + json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    result = generate_text(prompt=prompt, provider=provider, model=model,
                           max_output_tokens=max_output_tokens, instructions=instructions,
                           web_search=web_search)
    # Historical cost estimate is only retained for the former known OpenAI model.
    estimated_cost = None
    if result.provider == "openai" and result.model == "gpt-6-luna" and result.input_tokens is not None and result.output_tokens is not None:
        estimated_cost = (result.input_tokens * 0.10 + result.output_tokens * 0.50) / 1_000_000
    report_text = validate_market_assessment(result.text)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": result.provider, "model": result.model,
        "web_search_enabled": web_search,
        "input_snapshot": snapshot,
        "market_data_as_of": snapshot.get("market_data_as_of"),
        "market_data_as_of_timestamp": snapshot.get("market_data_as_of_timestamp"),
        "regime_classification": snapshot.get("regime_features", {}).get("regime_classification"),
        "indicator_snapshot": snapshot.get("regime_features", {}),
        "report_text": report_text,
        "source_metadata": list(result.source_metadata),
        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens, "estimated_cost_usd": estimated_cost,
    }

"""Phase 1 deterministic market-regime transition features and rules.

This produces descriptive states only. It is not a forecast, probability, or
trading signal. Feature rows are causal as of the requested XNYS session.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math

from dashboard.cross_market import pearson_correlation, price_map
from dashboard.market_history import SECTOR_SYMBOLS
from dashboard.market_explorer import DEFAULT_BASKETS, normalize_basket_config
from dashboard.risk_appetite import calculate_risk_appetite
from dashboard.volatility_cycle import analyze_volatility_cycle
from dashboard.vix_risk import build_vix_risk_monitor

TRANSITION_METHODOLOGY_VERSION = "market-regime-transition-v4"
TRANSITION_EXPORT_SCHEMA = "market-regime-transition-features-v4"
RULES = {
    "leadership_window_sessions": 20,
    "transition_momentum_sessions": 5,
    "relative_weakening_threshold_pp": 0.50,
    "opposing_crossover_threshold_pp": 0.50,
    "breadth_contraction_sectors": 2,
    "crossover_persistence_sessions": 3,
    "confirmed_regime_persistence_sessions": 3,
    "minimum_history_floor_sessions": 21,
    "maximum_inter_observation_gap_calendar_days": 4,
    "rolling_association_window_sessions": 60,
    "minimum_association_observations": 20,
    "prior_stress_vix_points": 25.0,
    "rates_change_windows_sessions": (5, 20),
    "proxy_momentum_windows_sessions": (5, 20),
    "technology_rebound_min_return_pct": 0.0,
    "defensive_stable_min_return_pct": 0.0,
}
TURNING_POINT_RULES = {
    "technology_minimum_matched_pairs": 3,
    "technology_minimum_upward_5d_crossovers": 2,
    "required_large_cap_pair_for_confirmed_technology_change": "XLK",
    "participation_confirmed_crossover_pairs": 2,
    "defensive_confirmed_cyclical_crossovers": 2,
    "vix_easing_five_session_change_percent": -5.0,
    "vix_easing_consecutive_lower_closes": 3,
    "broadening_stress_breadth_contraction_sectors": 2,
}


def _truncate_histories(histories, as_of):
    result = {}
    for symbol, rows in (histories or {}).items():
        result[symbol] = [row for row in rows or [] if str(row.get("trading_date", "")) <= as_of]
    return result


def _valid_sessions(days):
    if len(days) < 2:
        return False
    try:
        return all(0 < (date.fromisoformat(right) - date.fromisoformat(left)).days <=
                   RULES["maximum_inter_observation_gap_calendar_days"]
                   for left, right in zip(days, days[1:]))
    except ValueError:
        return False


def _pair_series(histories, symbol, benchmark, window):
    left, right = price_map(histories.get(symbol, [])), price_map(histories.get(benchmark, []))
    days = sorted(set(left) & set(right))
    if len(days) < window + 1 or not _valid_sessions(days[-(window + 1):]):
        return {"status": "insufficient_or_gapped", "observations": len(days), "as_of": days[-1] if days else None}
    values = []
    for index in range(window, len(days)):
        start, end = days[index - window], days[index]
        excess = ((left[end] / left[start] - 1) - (right[end] / right[start] - 1)) * 100
        values.append({"date": end, "value_pp": excess})
    return {"status": "available", "observations": len(days), "as_of": values[-1]["date"], "series": values}


def _pair_feature(histories, symbol, benchmark, window, momentum_window):
    series = _pair_series(histories, symbol, benchmark, window)
    if series.get("status") != "available" or len(series["series"]) <= momentum_window:
        return {"status": "insufficient_history", "ticker": symbol, "benchmark": benchmark,
                "window_sessions": window, "momentum_sessions": momentum_window,
                "as_of": series.get("as_of"), "coverage": series.get("observations", 0)}
    values = series["series"]
    latest, prior = values[-1], values[-1 - momentum_window]
    momentum_change = latest["value_pp"] - prior["value_pp"]
    threshold = RULES["opposing_crossover_threshold_pp"]
    persistence = RULES["crossover_persistence_sessions"]
    recent = values[-(persistence + 1):]
    crossover = None
    if len(recent) == persistence + 1:
        before = recent[0]["value_pp"]
        tail = [row["value_pp"] for row in recent[1:]]
        if before <= -threshold and all(value >= threshold for value in tail):
            crossover = {"from": "underperforming", "to": "outperforming", "persistent_sessions": persistence}
        elif before >= threshold and all(value <= -threshold for value in tail):
            crossover = {"from": "outperforming", "to": "underperforming", "persistent_sessions": persistence}
    return {"status": "available", "ticker": symbol, "benchmark": benchmark,
            "window_sessions": window, "momentum_sessions": momentum_window,
            "relative_return_pp": latest["value_pp"], "prior_relative_return_pp": prior["value_pp"],
            "relative_momentum_change_pp": momentum_change,
            "as_of": latest["date"], "prior_as_of": prior["date"],
            "coverage": series["observations"], "crossover": crossover,
            "latest_daily_relative_series": [{"date": row["date"], "value_pp": row["value_pp"]}
                                             for row in recent]}


def _basket_return(histories, symbols, window, weights=None):
    maps = {symbol: price_map(histories.get(symbol, [])) for symbol in symbols}
    if not maps or any(not values for values in maps.values()):
        return {"status": "unavailable", "value_pct": None, "as_of": None, "coverage": 0}
    days = sorted(set.intersection(*(set(values) for values in maps.values())))
    if len(days) < window + 1 or not _valid_sessions(days[-(window + 1):]):
        return {"status": "insufficient_history", "value_pct": None, "as_of": days[-1] if days else None, "coverage": len(days)}
    segment = days[-(window + 1):]
    weights = weights or {symbol: 1.0 for symbol in symbols}
    total_weight = sum(float(weights.get(symbol, 0)) for symbol in symbols)
    if total_weight <= 0:
        return {"status": "invalid_weights", "value_pct": None, "as_of": segment[-1], "coverage": len(days)}
    ret = sum((values[segment[-1]] / values[segment[0]] - 1) * float(weights.get(symbol, 0)) / total_weight
              for symbol, values in maps.items())
    return {"status": "available", "value_pct": ret * 100, "as_of": segment[-1], "coverage": len(days)}


def _breadth_at(histories, day, window):
    truncated = _truncate_histories(histories, day)
    from dashboard.cross_market import sector_participation_metrics
    data = sector_participation_metrics(truncated, timeframe=window)
    rsp_spy = (data.get("rsp_spy_returns", {}).get(window) or {})
    absolute = (data.get("absolute_breadth", {}).get(window) or {})
    return {"as_of": data.get("as_of"),
            "sectors_outperforming_spy": (data.get("outperformers", {}).get(window) or {}).get("count"),
            "sectors_positive_absolute": absolute.get("positive_count"),
            "spy_return": rsp_spy.get("spy_return"), "rsp_return": rsp_spy.get("rsp_return"),
            "available": (data.get("outperformers", {}).get(window) or {}).get("available", 0)}


def _daily_correlation(histories, left_symbol, right_symbol, as_of, window=None):
    window = window or RULES["rolling_association_window_sessions"]
    left, right = price_map(histories.get(left_symbol, [])), price_map(histories.get(right_symbol, []))
    days = sorted(d for d in set(left) & set(right) if d <= as_of)
    if len(days) < 2:
        return {"status": "insufficient", "correlation": None, "observations": 0, "as_of": None}
    days = days[-(window + 1):]
    if not _valid_sessions(days):
        return {"status": "inconclusive_gap", "correlation": None, "observations": len(days) - 1, "as_of": days[-1]}
    if not _valid_sessions(days):
        return {"status": "inconclusive_gap", "correlation": None, "observations": 0, "as_of": days[-1]}
    a = [left[b] / left[a] - 1 for a, b in zip(days, days[1:])]
    b = [right[b] / right[a] - 1 for a, b in zip(days, days[1:])]
    count = len(a)
    if count < RULES["minimum_association_observations"]:
        return {"status": "insufficient", "correlation": None, "observations": count, "as_of": days[-1]}
    return {"status": "available", "correlation": pearson_correlation(a, b), "observations": count, "as_of": days[-1]}


def _yield_correlation(histories, treasury_rows, asset_symbol, as_of):
    asset = price_map(histories.get(asset_symbol, []))
    yields = {row.get("observation_date"): row for row in treasury_rows or []
              if row.get("observation_date") and row.get("observation_date") <= as_of}
    days = sorted(d for d in set(asset) & set(yields)
                  if yields[d].get("two_year") is not None and yields[d].get("ten_year") is not None)
    if len(days) < 2:
        return {"status": "insufficient", "observations": 0, "as_of": None}
    days = days[-(RULES["rolling_association_window_sessions"] + 1):]
    if not _valid_sessions(days):
        return {"status": "inconclusive_gap", "observations": len(days) - 1,
                "as_of": days[-1],
                "interpretation_limit": "The matched Treasury/equity observations contain a gap; no daily association is calculated."}
    output = {}
    for tenor, field in (("2Y", "two_year"), ("10Y", "ten_year")):
        matched = [(left, right) for left, right in zip(days, days[1:])
                   if yields[left].get(field) is not None and yields[right].get(field) is not None]
        if len(matched) < RULES["minimum_association_observations"]:
            output[tenor] = {"status": "insufficient", "observations": len(matched), "correlation": None}
            continue
        eq_returns = [asset[right] / asset[left] - 1 for left, right in matched]
        yield_changes_bp = [(float(yields[right][field]) - float(yields[left][field])) * 100 for left, right in matched]
        output[tenor] = {"status": "available", "observations": len(matched),
                         "correlation": pearson_correlation(eq_returns, yield_changes_bp),
                         "as_of": matched[-1][1],
                         "interpretation_limit": "Historical association only; no causal or policy inference."}
    return output


def _rates_context(histories, treasury_rows, as_of, fred_rows=None):
    rows = sorted((row for row in treasury_rows or [] if str(row.get("observation_date", "")) <= as_of),
                  key=lambda row: row.get("observation_date", ""))
    rates = {"status": "unavailable", "as_of": None, "changes": {}, "relationships": {},
             "interpretation_limit": "Descriptive alignment only; not causal attribution or future Fed expectations."}
    if rows:
        latest = rows[-1]
        age = (date.fromisoformat(as_of) - date.fromisoformat(latest["observation_date"])).days
        if age <= 5:
            rates.update(status="available" if latest["observation_date"] == as_of else "lagged",
                         as_of=latest["observation_date"], latest={key: latest.get(key) for key in ("two_year", "ten_year")})
            for sessions in RULES["rates_change_windows_sessions"]:
                if len(rows) > sessions:
                    prior = rows[-sessions - 1]
                    if all(latest.get(key) is not None and prior.get(key) is not None for key in ("two_year", "ten_year")):
                        rates["changes"][f"{sessions}D"] = {
                            "two_year_bp": (float(latest["two_year"]) - float(prior["two_year"])) * 100,
                            "ten_year_bp": (float(latest["ten_year"]) - float(prior["ten_year"])) * 100,
                            "curve_bp": ((float(latest["ten_year"]) - float(latest["two_year"])) -
                                         (float(prior["ten_year"]) - float(prior["two_year"]))) * 100,
                            "prior_as_of": prior.get("observation_date"), "as_of": latest.get("observation_date")}
    for proxy in ("TLT", "GLD", "USO"):
        for window in RULES["proxy_momentum_windows_sessions"]:
            feature = _pair_feature(histories, proxy, "SPY", window, min(5, window - 1))
            absolute = _basket_return(histories, (proxy,), window)
            rates.setdefault("macro_proxies", {}).setdefault(proxy, {})[f"relative_{window}d"] = feature
            rates["macro_proxies"][proxy][f"absolute_{window}d"] = absolute
    rates["relationships"] = {f"{left}_vs_SPY_60d": _daily_correlation(histories, left, "SPY", as_of)
                               for left in ("TLT", "GLD", "USO")}
    rates["relationships"].update({f"{left}_vs_XLK_60d": _daily_correlation(histories, left, "XLK", as_of)
                                   for left in ("TLT", "GLD", "USO")})
    rates["relationships"]["Treasury_yield_changes_vs_equity_returns"] = {
        asset: _yield_correlation(histories, rows, asset, as_of) for asset in ("SPY", "XLK")}
    # FRED OAS is included only if explicitly supplied and sufficiently current.
    credit = {}
    for series in ("BAMLH0A0HYM2", "BAMLC0A0CM"):
        observations = sorted((row for row in (fred_rows or {}).get(series, [])
                               if row.get("observation_date", "") <= as_of and row.get("value") is not None),
                              key=lambda row: row["observation_date"])
        if len(observations) >= 2:
            latest = observations[-1]
            age = (date.fromisoformat(as_of) - date.fromisoformat(latest["observation_date"])).days
            credit[series] = {"status": "available" if age <= 5 else "stale",
                "latest_value_percent": float(latest["value"]), "as_of": latest["observation_date"],
                "change_20_observations_bp": ((float(latest["value"]) - float(observations[-21]["value"])) * 100
                                               if len(observations) >= 21 else None),
                "change_window_prior_date": observations[-21]["observation_date"] if len(observations) >= 21 else None,
                "source": latest.get("provider", "FRED")}
    rates["verified_credit_spreads"] = credit
    return rates


def _transition_explanation(status, leader, supporting, contradictory, confirm, invalidate):
    return {"current_leadership": leader,
            "supporting_evidence": supporting[:4],
            "contradictory_evidence": contradictory[:4],
            "confirm_conditions": confirm[:3],
            "invalidate_conditions": invalidate[:3],
            "summary": {
                "Continuing": f"{leader} remains in place and current relative momentum has not met the weakening threshold.",
                "Weakening": f"{leader} remains positive, but relative momentum or breadth has deteriorated; an opposing leadership change is not yet established.",
                "Possible reversal": f"{leader} is weakening and opposing leadership has early cross-market support, but persistence rules are not yet met.",
                "Transition confirmed by predefined rules": "A new risk-appetite state persisted for the configured sessions and is supported by separate breadth, credit, or volatility evidence. This confirms only the rule-defined transition in observed data, not a future market move.",
                "Mixed / inconclusive": "Leadership, breadth, credit, or volatility signals disagree or remain too close to the configured thresholds.",
                "Insufficient data": "Required observations are missing, stale, gapped, or not aligned closely enough to evaluate the transition.",
            }.get(status, "Transition conditions are not conclusive.")}


def build_turning_point_watch(features: dict, risk: dict, vix_cycle: dict, vix_rows, *, window: int = 20) -> dict:
    """Build four concise, descriptive turning-point monitors from local features.

    These states describe recent measurements only; they are not forecasts or
    probabilities. A short-term rebound remains distinct from a persistent
    relative-strength crossover.
    """
    features, risk = features or {}, risk or {}
    pairs_20 = features.get("pair_relative_performance") or {}
    pairs_5 = features.get("short_horizon_pair_relative_performance") or {}
    components = risk.get("components") or {}
    equity = components.get("equity_participation") or {}
    credit = components.get("credit_risk_appetite") or {}
    volatility = components.get("volatility_pressure") or {}
    defense = components.get("defensive_rotation") or {}
    breadth = features.get("sector_breadth_current") or {}
    breadth_change = features.get("sector_breadth_change")
    expected_session = (features.get("data_coverage") or {}).get("expected_session")

    def pair_value(group, symbol):
        item = group.get(symbol) or {}
        return item if item.get("status") == "available" else None

    def signal(signal_id, label, state, headline, evidence, *, as_of=None, details=None):
        return {"signal_id": signal_id, "label": label, "question": label, "state": state,
                "headline": headline, "evidence": evidence, "as_of": as_of,
                "details": details or {}}

    # A. VIX pressure: use the existing risk-appetite thresholds and the
    # volatility-cycle's explicit reversal/easing descriptors.
    cycle = vix_cycle or {}
    vix_date = (volatility.get("as_of") or cycle.get("as_of"))
    vix_dir = volatility.get("direction")
    five_vix = (volatility.get("value") or {}).get("five_session_change_percent")
    cycle_state = cycle.get("state")
    clean_vix = sorted((row for row in (vix_rows or []) if row.get("close") is not None),
                       key=lambda row: str(row.get("observation_date", "")))
    vix_falling_count = TURNING_POINT_RULES["vix_easing_consecutive_lower_closes"]
    falling_three = (len(clean_vix) >= vix_falling_count + 1 and
        all(float(clean_vix[index]["close"]) < float(clean_vix[index - 1]["close"])
            for index in range(len(clean_vix) - vix_falling_count, len(clean_vix))))
    latest_vix = float(clean_vix[-1]["close"]) if clean_vix else None
    prior_stress = bool(risk.get("prior_stress_observed"))
    if vix_dir is None or five_vix is None or not vix_date:
        vix_signal = signal("volatility_stabilization", "VIX Stress", "Insufficient data",
            "VIX history unavailable", "Aligned VIX closes are not current or do not cover the required window.",
            as_of=vix_date)
    elif prior_stress and five_vix <= TURNING_POINT_RULES["vix_easing_five_session_change_percent"] and falling_three:
        vix_signal = signal("volatility_stabilization", "VIX Stress", "Pressure easing",
            "Easing after observed stress", f"VIX is {five_vix:+.1f}% over five sessions, with three consecutive lower closes after prior stress.",
            as_of=vix_date, details={"five_session_change_percent": five_vix, "rule_status": "confirmed stress easing",
                                     "three_consecutive_lower_closes": True, "prior_stress": prior_stress})
    elif cycle_state == "Turbulent stabilization" or (vix_dir == 1 and cycle.get("failed_highs", 0) > 0):
        vix_signal = signal("volatility_stabilization", "VIX Stress", "Stable / mixed",
            "Spike reversal; easing unconfirmed", f"VIX five-session change is {five_vix:+.1f}%; recent highs/reversals suggest pressure may be easing, not confirmed.",
            as_of=vix_date, details={"five_session_change_percent": five_vix, "cycle_state": cycle_state, "rule_status": "possible turning point",
                                     "failed_highs": cycle.get("failed_highs", 0)})
    elif vix_dir == 1:
        vix_signal = signal("volatility_stabilization", "VIX Stress", "Pressure easing",
            "Easing", f"VIX is {five_vix:+.1f}% over five sessions.", as_of=vix_date,
            details={"five_session_change_percent": five_vix, "cycle_state": cycle_state})
    elif vix_dir == -1:
        vix_signal = signal("volatility_stabilization", "VIX Stress", "Pressure rising",
            "Pressure rising", f"VIX is {five_vix:+.1f}% over five sessions; latest close {latest_vix:.2f} points." if latest_vix is not None else f"VIX is {five_vix:+.1f}% over five sessions.",
            as_of=vix_date, details={"five_session_change_percent": five_vix, "cycle_state": cycle_state})
    else:
        vix_signal = signal("volatility_stabilization", "VIX Stress", "Stable / mixed",
            "Mixed", f"Five-session VIX change is {five_vix:+.1f}%; daily and intraday evidence does not meet an easing or increasing-pressure rule.",
            as_of=vix_date, details={"five_session_change_percent": five_vix, "cycle_state": cycle_state})

    # B. Technology/Mag 7: require XLK plus at least two of SMH, QQQ, MAGS.
    tech_symbols = ("XLK", "SMH", "QQQ", "MAGS")
    tech_rows = {symbol: (pair_value(pairs_20, symbol), pair_value(pairs_5, symbol)) for symbol in tech_symbols}
    tech_available = {symbol: pair for symbol, pair in tech_rows.items()
                      if pair[0] is not None and pair[1] is not None}
    tech_short_positive = [symbol for symbol, (_, short) in tech_available.items()
                           if short.get("relative_return_pp", 0) > 0]
    tech_long_positive = [symbol for symbol, (long, _) in tech_available.items()
                          if long.get("relative_return_pp", 0) > 0]
    tech_momentum_up = [symbol for symbol, (_, short) in tech_available.items()
                        if short.get("relative_momentum_change_pp", 0) > 0]
    tech_up_crossovers = [symbol for symbol, (_, short) in tech_available.items()
                          if (short.get("crossover") or {}).get("to") == "outperforming"]
    required_tech_count = TURNING_POINT_RULES["technology_minimum_matched_pairs"]
    if ("XLK" not in tech_available or "MAGS" not in tech_available
            or len(tech_available) < required_tech_count):
        tech_signal = signal("technology_mag7_recovery", "Technology / Mag 7 Leadership",
            "Insufficient data", "Technology comparison coverage incomplete",
            f"Need current XLK/SPY and MAGS/SPY plus at least one of SMH or QQQ, with matched 5D and {window}D closes.",
            as_of=None, details={"available_symbols": sorted(tech_available), "required_symbols": required_tech_count})
    else:
        short_excess = sum((short["relative_return_pp"] for _, short in tech_available.values())) / len(tech_available)
        long_excess = sum((long["relative_return_pp"] for long, _ in tech_available.values())) / len(tech_available)
        tech_dates = sorted({short.get("as_of") for _, short in tech_available.values() if short.get("as_of")})
        tech_date = min(tech_dates) if len(tech_dates) == 1 else None
        if not tech_date or (expected_session and tech_date != expected_session):
            tech_state, tech_headline = "Insufficient data", "Technology comparison observations are stale or misaligned"
        elif (len(tech_up_crossovers) >= TURNING_POINT_RULES["technology_minimum_upward_5d_crossovers"]
              and TURNING_POINT_RULES["required_large_cap_pair_for_confirmed_technology_change"] in tech_up_crossovers):
            tech_state, tech_headline = "Relative recovery strengthening", "Persistent technology relative-strength crossover"
        elif len(tech_short_positive) >= required_tech_count and len(tech_long_positive) < required_tech_count:
            tech_state, tech_headline = "Recovery unconfirmed", "Short rebound is broader; longer trend still lags"
        elif len(tech_short_positive) >= required_tech_count and len(tech_long_positive) >= required_tech_count and len(tech_momentum_up) >= required_tech_count:
            tech_state, tech_headline = "Relative recovery strengthening", "Technology and mega-cap growth lead in both windows"
        elif len(tech_short_positive) < 2 and len(tech_long_positive) >= required_tech_count:
            tech_state, tech_headline = "Recovery unconfirmed", "Technology's longer-term lead lacks short-term confirmation"
        elif len(tech_short_positive) < 2 and len(tech_long_positive) < 2:
            tech_state, tech_headline = "Underperformance deepening", "Technology underperforms SPY in both windows"
        else:
            tech_state, tech_headline = "Mixed / insufficient data", "Relative leadership signals are mixed"
        xl_long, xl_short = tech_available["XLK"]
        mags_long, mags_short = tech_available.get("MAGS", (None, None))
        fact = (f"XLK/SPY: 5D {xl_short['relative_return_pp']:+.2f} pp, {window}D {xl_long['relative_return_pp']:+.2f} pp; "
                f"{len(tech_short_positive)}/{len(tech_available)} covered comparisons positive over 5D.")
        if mags_long and mags_short:
            fact += f" MAGS/SPY: 5D {mags_short['relative_return_pp']:+.2f} pp, {window}D {mags_long['relative_return_pp']:+.2f} pp."
        tech_signal = signal("technology_mag7_recovery", "Technology / Mag 7 Leadership",
            tech_state, tech_headline, fact, as_of=tech_date,
            details={"positive_5d_symbols": tech_short_positive, "positive_long_window_symbols": tech_long_positive,
                     "upward_crossovers": tech_up_crossovers, "window_sessions": window,
                     "mags_coverage": "available" if mags_long else "unavailable"})

    # C. Participation: separate broad confirmation from a technology-only bounce.
    rsp_long, iwm_long = pair_value(pairs_20, "RSP"), pair_value(pairs_20, "IWM")
    rsp_short, iwm_short = pair_value(pairs_5, "RSP"), pair_value(pairs_5, "IWM")
    breadth_count = breadth.get("sectors_positive_absolute")
    relative_breadth_count = breadth.get("sectors_outperforming_spy")
    breadth_dates = {item.get("as_of") for item in (rsp_short, iwm_short) if item}
    participation_date = min(breadth_dates) if len(breadth_dates) == 1 else None
    if (not all((rsp_long, iwm_long, rsp_short, iwm_short)) or breadth_count is None or breadth_change is None
            or not participation_date or (expected_session and participation_date != expected_session)
            or breadth.get("as_of") != participation_date):
        part_signal = signal("broad_market_participation", "Broad Equity Participation", "Insufficient data",
            "Participation coverage incomplete", "Matched RSP/IWM comparisons and sector-breadth change are required.",
            as_of=participation_date)
    else:
        short_positive = rsp_short["relative_return_pp"] > 0 and iwm_short["relative_return_pp"] > 0
        breadth_improving = breadth_change >= 0
        short_negative = rsp_short["relative_return_pp"] < 0 and iwm_short["relative_return_pp"] < 0
        participation_crossovers = sum((item.get("crossover") or {}).get("to") == "outperforming"
                                       for item in (rsp_short, iwm_short))
        up_cross = participation_crossovers >= TURNING_POINT_RULES["participation_confirmed_crossover_pairs"]
        absolute_returns_positive = (breadth.get("spy_return") is not None and breadth.get("rsp_return") is not None
                                     and breadth["spy_return"] > 0 and breadth["rsp_return"] > 0)
        relative_resilience = (breadth.get("spy_return") is not None and breadth.get("rsp_return") is not None
                               and breadth["spy_return"] < 0 and breadth["rsp_return"] < 0
                               and rsp_short["relative_return_pp"] > 0)
        if relative_resilience:
            part_state, part_head = "Stable / mixed", "Relative resilience during a selloff; absolute breadth is not positive"
        elif up_cross and breadth_improving and absolute_returns_positive:
            part_state, part_head = "Broadening", "Equal-weight, small-cap and sector participation improved persistently"
        elif short_positive and breadth_improving and absolute_returns_positive and (rsp_long["relative_return_pp"] < 0 or iwm_long["relative_return_pp"] < 0):
            part_state, part_head = "Broadening", "Participation is improving; the longer window remains mixed"
        elif short_positive and breadth_improving and absolute_returns_positive:
            part_state, part_head = "Broadening", "Broad participation improving"
        elif short_negative and breadth_change < 0:
            part_state, part_head = "Narrowing", "Broad participation deteriorating"
        else:
            part_state, part_head = "Stable / mixed", "Participation signals are mixed"
        part_rule = ("confirmed" if up_cross and breadth_improving and absolute_returns_positive else
                     "possible" if short_positive and breadth_improving and absolute_returns_positive else "descriptive")
        part_fact = (f"RSP/SPY {rsp_short['relative_return_pp']:+.2f} pp and IWM/SPY {iwm_short['relative_return_pp']:+.2f} pp (5D); "
                     f"{breadth_count}/11 sector ETFs positive absolute ({relative_breadth_count}/11 outperform SPY), positive-count change {breadth_change:+d} over five sessions.")
        part_signal = signal("broad_market_participation", "Broad Equity Participation", part_state,
            part_head, part_fact, as_of=participation_date or breadth.get("as_of"),
            details={"rsp_5d_excess_pp": rsp_short["relative_return_pp"],
                     "iwm_5d_excess_pp": iwm_short["relative_return_pp"],
                     "rsp_window_excess_pp": rsp_long["relative_return_pp"],
                     "iwm_window_excess_pp": iwm_long["relative_return_pp"],
                     "sectors_positive_absolute": breadth_count,
                     "sectors_outperforming_spy": relative_breadth_count, "breadth_change": breadth_change,
                     "window_sessions": window, "rule_status": part_rule})

    # D. Compare configured basket returns and momentum, not inferred flows.
    spread_20 = features.get("current_cyclical_minus_defensive_pp")
    spread_5 = features.get("five_session_cyclical_minus_defensive_pp")
    momentum_change = features.get("cyclical_defensive_momentum_change_pp")
    defensive_as_of = features.get("cyclical_defensive_as_of")
    rotation_pairs = features.get("defensive_rotation_short_horizon_pairs") or {}
    cyclical_crossovers = [key for key, item in rotation_pairs.items()
        if item.get("status") == "available" and (item.get("crossover") or {}).get("to") == "outperforming"]
    if (spread_20 is None or spread_5 is None or not defensive_as_of
            or (expected_session and defensive_as_of != expected_session)):
        defensive_signal = signal("defensive_leadership", "Defensive Leadership",
            "Insufficient data", "Matched cyclical/defensive baskets unavailable",
            "The configured baskets need complete common-date observations for both windows.", as_of=None)
    else:
        if (spread_20 < -RULES["relative_weakening_threshold_pp"] and
                spread_5 > RULES["relative_weakening_threshold_pp"] and
                len(cyclical_crossovers) >= TURNING_POINT_RULES["defensive_confirmed_cyclical_crossovers"]):
            def_state, def_head = "Leadership shifting to cyclicals", "Cyclical relative-strength crossover is persistent"
            def_rule = "confirmed"
        elif spread_20 < -RULES["relative_weakening_threshold_pp"] and spread_5 > RULES["relative_weakening_threshold_pp"]:
            def_state, def_head = "Dominant but fading", "Defensive relative strength is easing"
            def_rule = "possible"
        elif spread_20 < -RULES["relative_weakening_threshold_pp"] and (momentum_change is None or momentum_change <= -RULES["relative_weakening_threshold_pp"]):
            def_state, def_head = "Dominant and strengthening", "Defensive sectors lead and their relative strength is increasing"
            def_rule = "descriptive"
        elif spread_20 > RULES["relative_weakening_threshold_pp"] and spread_5 > 0:
            def_state, def_head = "Leadership shifting to cyclicals", "Cyclicals lead the defensive basket in both windows"
            def_rule = "descriptive"
        elif abs(spread_20) <= RULES["relative_weakening_threshold_pp"]:
            def_state, def_head = "Mixed / insufficient data", "No clear basket leadership"
            def_rule = "mixed"
        else:
            def_state, def_head = "Mixed / insufficient data", "Defensive and cyclical windows disagree"
            def_rule = "mixed"
        def_date = defensive_as_of
        def_fact = f"Cyclical minus defensive: {spread_20:+.2f} pp ({window}D), {spread_5:+.2f} pp (5D)."
        defensive_signal = signal("defensive_leadership", "Defensive Leadership", def_state,
            def_head, def_fact, as_of=def_date,
            details={"cyclical_minus_defensive_window_pp": spread_20,
                     "cyclical_minus_defensive_5d_pp": spread_5,
                     "relative_momentum_change_pp": momentum_change,
                     "persistent_cyclical_crossovers": cyclical_crossovers,
                     "window_sessions": window, "rule_status": def_rule})

    # Credit confirmation is its own signal. It uses aligned HYG/LQD and
    # HYG/IEF adjusted-return proxies; these are not direct credit spreads.
    credit_date = credit.get("as_of")
    credit_value = credit.get("value") or {}
    def credit_pp(key):
        value = credit_value.get(key)
        return f"{float(value):+.2f} pp" if isinstance(value, (int, float)) else "unavailable"
    if (credit.get("direction") is None or not credit_date or
            (expected_session and credit_date != expected_session)):
        credit_state = "Insufficient data"
        credit_headline = "Credit observations unavailable or stale"
        credit_evidence = "Matched HYG/LQD and HYG/IEF observations are required; these ETF proxies are not direct credit spreads."
    elif credit.get("direction") > 0:
        credit_state = "Credit appetite improving"
        credit_headline = "Credit appetite improving"
        credit_evidence = f"HYG−LQD {credit_pp('hyg_minus_lqd_pp')}; HYG−IEF {credit_pp('hyg_minus_ief_pp')} over {window} sessions."
    elif credit.get("direction") < 0:
        credit_state = "Credit deteriorating"
        credit_headline = "Credit appetite deteriorating"
        credit_evidence = f"HYG−LQD {credit_pp('hyg_minus_lqd_pp')}; HYG−IEF {credit_pp('hyg_minus_ief_pp')} over {window} sessions."
    else:
        credit_state = "Mixed / neutral"
        credit_headline = "Credit confirmation is mixed"
        credit_evidence = f"HYG−LQD {credit_pp('hyg_minus_lqd_pp')}; HYG−IEF {credit_pp('hyg_minus_ief_pp')} over {window} sessions."
    participation_direction = (equity.get("direction") if isinstance(equity, dict) else None)
    credit_signal = signal("credit_confirmation", "Credit Confirmation", credit_state,
        credit_headline, credit_evidence, as_of=credit_date,
        details={"proxy_type": "credit-sensitive ETF returns, not a credit spread",
                 "supports_or_contradicts_participation": (
                     "supports" if credit.get("direction") is not None and participation_direction == credit.get("direction")
                     else "contradicts" if credit.get("direction") is not None and participation_direction is not None
                     else "unavailable")})

    signals = {item["signal_id"]: item for item in (vix_signal, tech_signal, part_signal, defensive_signal, credit_signal)}
    # Deterministic cross-signal scenario; never use a single metric as proof.
    tech_rebound = tech_signal["state"] in {"Relative recovery strengthening", "Recovery unconfirmed"}
    defensive_healthy = defensive_signal["state"] == "Dominant and strengthening"
    participation_broadening = part_signal["state"] == "Broadening"
    credit_weak = credit.get("direction") == -1
    vix_stress = vix_dir == -1
    tech_and_defensive_weak = (
        (features.get("technology_5d_absolute_return_pct") is not None and
         features.get("technology_5d_absolute_return_pct") < 0 and
         features.get("defensive_5d_absolute_return_pct") is not None and
         features.get("defensive_5d_absolute_return_pct") < 0 and
         (features.get("sector_breadth_change") is not None and
          features.get("sector_breadth_change") <= -TURNING_POINT_RULES["broadening_stress_breadth_contraction_sectors"]))
        or (tech_signal["state"] == "Underperformance deepening" and
            defensive_signal["state"] in {"Dominant and strengthening", "Mixed / insufficient data"} and
            part_signal["state"] == "Narrowing"))
    if tech_and_defensive_weak:
        scenario = {"id": "broadening_stress", "label": "Technology and defensives both weakening; stress may be broadening"}
    elif tech_rebound and (vix_stress or credit_weak):
        scenario = {"id": "rebound_without_confirmation", "label": "Technology rebound lacks credit or volatility confirmation"}
    elif tech_rebound and defensive_signal["state"] in {"Dominant but fading", "Leadership shifting to cyclicals"}:
        scenario = {"id": "leadership_handoff", "label": "Technology rebound may be a leadership handoff"}
    elif tech_rebound and defensive_healthy and participation_broadening:
        scenario = {"id": "broadening_recovery", "label": "Technology rebound coincides with broader market recovery evidence"}
    elif "Insufficient data" in {item["state"] for item in signals.values()}:
        scenario = {"id": "insufficient_data", "label": "Turning-point combination is incomplete"}
    else:
        scenario = {"id": "mixed", "label": "Turning-point evidence is mixed or inconclusive"}
    return {"as_of": max((item["as_of"] for item in signals.values() if item.get("as_of")), default=None),
            "window_sessions": window, "momentum_window_sessions": 5,
            "signals": signals, "scenario": scenario,
            "methodology_version": TRANSITION_METHODOLOGY_VERSION}


def calculate_regime_transition(histories, vix_rows, *, timeframe=20, expected_session=None,
                                treasury_rows=(), fred_rows=None, basket_config=None):
    """Calculate causal transition features and a conservative deterministic state."""
    window = timeframe if timeframe in (5, 20, 60) else RULES["leadership_window_sessions"]
    momentum_window = RULES["transition_momentum_sessions"]
    spy_days = sorted(price_map((histories or {}).get("SPY", [])).keys())
    requested_asof = str(expected_session) if expected_session else (spy_days[-1] if spy_days else None)
    if not requested_asof:
        return _empty_transition(window, "No completed SPY session is available.")
    local_histories = _truncate_histories(histories, requested_asof)
    local_vix = [row for row in vix_rows or [] if str(row.get("observation_date", "")) <= requested_asof]
    baskets = normalize_basket_config(basket_config or DEFAULT_BASKETS)
    cyclical_sectors, defensive_sectors = tuple(baskets["cyclical"]), tuple(baskets["defensive"])
    actual_spy_days = sorted(price_map(local_histories.get("SPY", [])).keys())
    as_of = actual_spy_days[-1] if actual_spy_days else None
    # Recompute using causally truncated inputs even if a display calculation
    # is supplied by the caller.
    risk = calculate_risk_appetite(local_histories, local_vix,
        timeframe=window, expected_session=requested_asof,
        vix_current_available=bool(local_vix and local_vix[-1].get("observation_date") == requested_asof),
        basket_config=baskets)
    feature_dates = {}
    for symbol in ("SPY", "RSP", "IWM", "QQQ", "XLK", "SMH", "MAGS", *SECTOR_SYMBOLS, "HYG", "LQD", "IEF"):
        dates = price_map(local_histories.get(symbol, []))
        feature_dates[symbol] = max(dates) if dates else None
    required_symbols = ("SPY", "RSP", "IWM", "QQQ", *SECTOR_SYMBOLS)
    stale_symbols = [symbol for symbol in required_symbols if feature_dates.get(symbol) != requested_asof]
    pair_features = {symbol: _pair_feature(local_histories, symbol, "SPY", window, momentum_window)
                     for symbol in ("RSP", "IWM", "QQQ", "XLK", "SMH", "MAGS")}
    short_pair_features = {symbol: _pair_feature(local_histories, symbol, "SPY", 5, 5)
                           for symbol in ("RSP", "IWM", "QQQ", "XLK", "SMH", "MAGS")}
    defensive_rotation_pairs = {
        "XLY_XLP": _pair_feature(local_histories, "XLY", "XLP", 5, 5),
        "XLI_XLU": _pair_feature(local_histories, "XLI", "XLU", 5, 5),
        "XLF_XLV": _pair_feature(local_histories, "XLF", "XLV", 5, 5),
    }
    sector_features = {symbol: _pair_feature(local_histories, symbol, "SPY", window, momentum_window)
                       for symbol in SECTOR_SYMBOLS}
    current_cyc = _basket_return(local_histories, cyclical_sectors, window, baskets["cyclical"])
    current_def = _basket_return(local_histories, defensive_sectors, window, baskets["defensive"])
    prior_window = {}
    if actual_spy_days and len(actual_spy_days) > momentum_window:
        prior_end = actual_spy_days[-1 - momentum_window]
        prior_histories = _truncate_histories(local_histories, prior_end)
        prior_window = {
            "cyclical": _basket_return(prior_histories, cyclical_sectors, window, baskets["cyclical"]),
            "defensive": _basket_return(prior_histories, defensive_sectors, window, baskets["defensive"]),
            "breadth": _breadth_at(local_histories, prior_end, window),
        }
    cyc_minus_def = ((current_cyc["value_pct"] - current_def["value_pct"])
                     if current_cyc.get("value_pct") is not None and current_def.get("value_pct") is not None else None)
    previous_cyc_minus_def = None
    if prior_window.get("cyclical", {}).get("value_pct") is not None and prior_window.get("defensive", {}).get("value_pct") is not None:
        previous_cyc_minus_def = prior_window["cyclical"]["value_pct"] - prior_window["defensive"]["value_pct"]
    basket_momentum_change = (cyc_minus_def - previous_cyc_minus_def
                              if cyc_minus_def is not None and previous_cyc_minus_def is not None else None)
    current_breadth = _breadth_at(local_histories, requested_asof, window)
    breadth_change = (current_breadth["sectors_positive_absolute"] - prior_window["breadth"]["sectors_positive_absolute"]
                      if current_breadth.get("sectors_positive_absolute") is not None and
                         prior_window.get("breadth", {}).get("sectors_positive_absolute") is not None else None)
    relative_breadth_change = (current_breadth["sectors_outperforming_spy"] - prior_window["breadth"]["sectors_outperforming_spy"]
                      if current_breadth.get("sectors_outperforming_spy") is not None and
                         prior_window.get("breadth", {}).get("sectors_outperforming_spy") is not None else None)

    relative_crossovers = {symbol: feature.get("crossover") for symbol, feature in pair_features.items()
                           if feature.get("crossover")}
    sector_crossovers = {symbol: feature.get("crossover") for symbol, feature in sector_features.items()
                         if feature.get("crossover")}
    positive_to_negative_sector_crossovers = {symbol: crossover for symbol, crossover in sector_crossovers.items()
                                              if crossover.get("to") == "underperforming"}
    sector_rank = sorted(((symbol, feature.get("relative_return_pp")) for symbol, feature in sector_features.items()
                          if feature.get("relative_return_pp") is not None), key=lambda item: item[1], reverse=True)
    prior_sector_rank = sorted(((symbol, feature.get("prior_relative_return_pp")) for symbol, feature in sector_features.items()
                                if feature.get("prior_relative_return_pp") is not None), key=lambda item: item[1], reverse=True)
    prior_rank = {symbol: index + 1 for index, (symbol, _) in enumerate(prior_sector_rank)}
    rank_changes = {symbol: prior_rank[symbol] - (index + 1) for index, (symbol, _) in enumerate(sector_rank)
                    if symbol in prior_rank and prior_rank[symbol] != index + 1}
    tech_abs = [_basket_return(local_histories, (symbol,), 5).get("value_pct") for symbol in ("XLK", "SMH")]
    tech_return_5d = sum(value for value in tech_abs if value is not None) / len([v for v in tech_abs if v is not None]) if any(v is not None for v in tech_abs) else None
    tech_relative_5d = [_pair_feature(local_histories, symbol, "SPY", 5, 1).get("relative_return_pp")
                        for symbol in ("XLK", "SMH")]
    tech_relative_5d = [value for value in tech_relative_5d if value is not None]
    tech_leading = bool(tech_relative_5d and sum(tech_relative_5d) / len(tech_relative_5d) > 0)
    defensive_5d = _basket_return(local_histories, defensive_sectors, 5, baskets["defensive"])
    cyclical_5d = _basket_return(local_histories, cyclical_sectors, 5, baskets["cyclical"])
    cyclical_defensive_5d_pp = (cyclical_5d.get("value_pct") - defensive_5d.get("value_pct")
        if cyclical_5d.get("value_pct") is not None and defensive_5d.get("value_pct") is not None else None)
    defensive_lead_weakening = (previous_cyc_minus_def is not None and cyc_minus_def is not None
        and previous_cyc_minus_def < 0 and basket_momentum_change is not None
        and basket_momentum_change >= RULES["relative_weakening_threshold_pp"])
    risk_components = risk.get("components", {})
    equity_direction = (risk_components.get("equity_participation") or {}).get("direction")
    credit_direction = (risk_components.get("credit_risk_appetite") or {}).get("direction")
    volatility_direction = (risk_components.get("volatility_pressure") or {}).get("direction")
    defensive_direction = (risk_components.get("defensive_rotation") or {}).get("direction")
    breadth_value = (risk_components.get("equity_participation") or {}).get("value") or {}
    missing_confirmation_groups = [name for name, direction in
        (("credit", credit_direction), ("volatility", volatility_direction)) if direction is None]

    scenarios = []
    tech_positive = tech_return_5d is not None and tech_return_5d > RULES["technology_rebound_min_return_pct"]
    def_value = defensive_5d.get("value_pct")
    cyc_value_5d = cyclical_5d.get("value_pct")
    if (tech_positive and tech_leading and def_value is not None and def_value >= RULES["defensive_stable_min_return_pct"]
            and not defensive_lead_weakening):
        scenarios.append({"id": "technology_rebound_defensives_stable", "label": "Technology rebound with defensive sectors stable/strong",
            "interpretation": "Potential participation broadening; stronger RSP/IWM, credit proxies, and sector breadth would support this, but the rebound alone does not establish risk-on."})
    if tech_positive and tech_leading and defensive_lead_weakening:
        scenarios.append({"id": "technology_rebound_defensives_weaken", "label": "Technology rebound with defensive leaders weakening",
            "interpretation": "Possible leadership handoff or rotation; overall risk appetite remains unconfirmed until breadth and credit support it."})
    if tech_return_5d is not None and tech_return_5d < 0 and def_value is not None and def_value < 0:
        scenarios.append({"id": "technology_and_defensives_weaken", "label": "Technology and defensive sectors both weakening",
            "interpretation": "Could indicate broadening stress if sector breadth contracts, HYG proxies weaken, and VIX pressure rises; otherwise evidence remains incomplete."})
    if (cyc_value_5d is not None and cyc_value_5d > 0 and tech_return_5d is not None and tech_return_5d > 0
            and risk.get("prior_stress_observed") and volatility_direction == 1 and credit_direction in (0, 1)):
        scenarios.append({"id": "cyclical_technology_post_stress_recovery", "label": "Technology and cyclicals strengthen after stress",
            "interpretation": "Possible stress recovery when VIX eases and credit stabilizes; persistence across additional sessions is needed."})

    current_regime = risk.get("classification", "Insufficient Evidence")
    current_leadership = ("Defensive leadership" if defensive_direction == -1 else
                          "Cyclical leadership" if defensive_direction == 1 else
                          "Technology leadership" if tech_leading else
                          "Broad equity participation" if equity_direction == 1 else "Mixed leadership")
    top_leaders = [symbol for symbol, _ in sector_rank[:3]]
    weak_laggards = [symbol for symbol, _ in sector_rank[-3:]]
    relative_changes = [feature.get("relative_momentum_change_pp") for feature in pair_features.values()
                        if feature.get("status") == "available"]
    weakening_count = sum(value <= -RULES["relative_weakening_threshold_pp"] for value in relative_changes)
    strengthening_count = sum(value >= RULES["relative_weakening_threshold_pp"] for value in relative_changes)
    breadth_contracting = breadth_change is not None and breadth_change <= -RULES["breadth_contraction_sectors"]
    broadening_breadth = breadth_change is not None and breadth_change >= RULES["breadth_contraction_sectors"]
    leadership_weakening = ((defensive_direction == -1 and defensive_lead_weakening) or
        (defensive_direction == 1 and basket_momentum_change is not None and
         basket_momentum_change <= -RULES["relative_weakening_threshold_pp"]) or
        (defensive_direction in (None, 0) and (weakening_count >= 2 or breadth_contracting)))
    cyclical_crossover = any(item.get("to") == "outperforming" for item in relative_crossovers.values()) or basket_momentum_change is not None and basket_momentum_change >= RULES["opposing_crossover_threshold_pp"]
    defensive_crossover = any(item.get("to") == "outperforming" for item in relative_crossovers.values()) and basket_momentum_change is not None and basket_momentum_change <= -RULES["opposing_crossover_threshold_pp"]

    current_state = current_regime
    confirmed = _confirmed_transition_state(local_histories, local_vix, actual_spy_days, window, requested_asof, risk, baskets)
    supporting, contradictory = [], []
    if pair_features.get("RSP", {}).get("status") == "available":
        supporting.append(f"RSP/SPY {pair_features['RSP']['relative_return_pp']:+.2f} pp; 5-session momentum change {pair_features['RSP']['relative_momentum_change_pp']:+.2f} pp")
    if pair_features.get("IWM", {}).get("status") == "available":
        supporting.append(f"IWM/SPY {pair_features['IWM']['relative_return_pp']:+.2f} pp; momentum change {pair_features['IWM']['relative_momentum_change_pp']:+.2f} pp")
    if breadth_value:
        supporting.append(f"Sector breadth {breadth_value.get('sectors_outperforming_spy')}/11 outperforming SPY; change {breadth_change:+d} over {momentum_window} sessions" if breadth_change is not None else f"Sector breadth {breadth_value.get('sectors_outperforming_spy')}/11 outperforming SPY")
    if cyc_minus_def is not None and basket_momentum_change is not None:
        supporting.append(f"Cyclical minus defensive {cyc_minus_def:+.2f} pp; relative momentum change {basket_momentum_change:+.2f} pp")
    if credit_direction is not None:
        supporting.append(f"Credit ETF proxy group: {risk_components['credit_risk_appetite']['status']}")
    if volatility_direction is not None:
        supporting.append(f"Volatility group: {risk_components['volatility_pressure']['status']}")
    if equity_direction == 1 and credit_direction == -1:
        contradictory.append("Equity participation is broadening while HYG relative proxies weaken.")
    if equity_direction == 1 and volatility_direction == -1:
        contradictory.append("Breadth is constructive while VIX pressure is increasing.")
    if defensive_direction == -1 and equity_direction == 1:
        contradictory.append("Defensive sectors lead even though broad participation remains constructive.")
    if weakening_count:
        contradictory.append(f"Relative momentum weakened by at least {RULES['relative_weakening_threshold_pp']:.2f} pp in {weakening_count} broad/technology pairs.")
    if stale_symbols:
        contradictory.append("Some core equity symbols are stale or date-misaligned: " + ", ".join(stale_symbols[:5]) + ".")

    # Resolve transition only after the current state and independent group evidence exist.
    required_history = max(RULES["minimum_history_floor_sessions"], window + momentum_window + 1)
    enough = current_regime != "Insufficient Evidence" and not stale_symbols and len(actual_spy_days) >= required_history
    if not enough:
        transition_status = "Insufficient data"
    elif confirmed and confirmed["status"] == "confirmed":
        transition_status = "Transition confirmed by predefined rules"
        supporting.insert(0, confirmed["explanation"])
    elif missing_confirmation_groups:
        transition_status = "Mixed / inconclusive"
    else:
        has_opposition = (cyclical_crossover if defensive_direction == -1 else
                          defensive_crossover if defensive_direction == 1 else
                          cyclical_crossover or defensive_crossover)
        independent_early = credit_direction in (-1, 1) or volatility_direction in (-1, 1) or breadth_contracting or broadening_breadth
        if leadership_weakening:
            if has_opposition and independent_early:
                transition_status = "Possible reversal"
            else:
                transition_status = "Weakening"
        elif any(value is not None and value > 0 for value in
                 (equity_direction, defensive_direction, credit_direction, volatility_direction)) and any(
                 value is not None and value < 0 for value in
                 (equity_direction, defensive_direction, credit_direction, volatility_direction)):
            transition_status = "Mixed / inconclusive"
        elif strengthening_count >= 2 or (broadening_breadth and equity_direction == 1):
            transition_status = "Continuing"
        elif (equity_direction is not None and defensive_direction is not None and
              credit_direction is not None and volatility_direction is not None and
              (contradictory or equity_direction == 0 or defensive_direction == 0)):
            transition_status = "Mixed / inconclusive"
        else:
            transition_status = "Continuing"

    confirm_conditions = [
        f"A relative-strength crossover must persist for {RULES['crossover_persistence_sessions']} sessions at ≥{RULES['opposing_crossover_threshold_pp']:.2f} pp.",
        "Independent breadth, credit-proxy, or VIX evidence must agree; watch matched-date RSP/IWM and sector participation.",
        "The current Risk Appetite state must recompute consistently for three completed sessions before a rule-confirmed transition.",
    ]
    invalidate_conditions = [
        f"The apparent crossover falls back inside the ±{RULES['opposing_crossover_threshold_pp']:.2f} pp neutral band before persistence is met.",
        "Breadth, HYG relative returns, or VIX diverge from the proposed transition.",
        "Input observations become stale, gapped, or use incompatible dates/bases.",
    ]
    if current_leadership == "Defensive leadership":
        confirm_conditions[0] = ("Cyclical/technology relative strength must cross above defensives and persist for "
            f"{RULES['crossover_persistence_sessions']} sessions; monitor RSP/SPY and IWM/SPY.")
    elif current_leadership == "Technology leadership":
        confirm_conditions[0] = "A sustained technology-relative crossover plus broader sector participation is needed to confirm leadership beyond technology."

    rates = _rates_context(local_histories, treasury_rows, requested_asof, fred_rows)
    vix_ohlc = build_vix_risk_monitor(local_vix, completed_session=requested_asof)
    vix_cycle = analyze_volatility_cycle(local_vix, window=10)
    features = {
        "window_sessions": window, "momentum_window_sessions": momentum_window,
        "current_regime": current_state, "current_leadership": current_leadership,
        "pair_relative_performance": pair_features, "sector_relative_performance": sector_features,
        "short_horizon_pair_relative_performance": short_pair_features,
        "defensive_rotation_short_horizon_pairs": defensive_rotation_pairs,
        "sector_rank_top3": top_leaders, "sector_rank_bottom3": weak_laggards,
        "sector_rank_changes": rank_changes, "sector_relative_strength_crossovers": sector_crossovers,
        "positive_to_negative_sector_crossovers": positive_to_negative_sector_crossovers,
        "relative_strength_crossovers": relative_crossovers,
        "current_cyclical_minus_defensive_pp": cyc_minus_def,
        "five_session_cyclical_minus_defensive_pp": cyclical_defensive_5d_pp,
        "cyclical_defensive_as_of": (current_cyc.get("as_of") if current_cyc.get("as_of") == current_def.get("as_of") else None),
        "cyclical_defensive_momentum_change_pp": basket_momentum_change,
        "sector_breadth_current": current_breadth, "sector_breadth_change": breadth_change,
        "sector_relative_outperformance_count_change": relative_breadth_change,
        "equity_confirmation": risk_components.get("equity_participation"),
        "sector_leadership_confirmation": risk_components.get("defensive_rotation"),
        "credit_confirmation": risk_components.get("credit_risk_appetite"),
        "volatility_confirmation": risk_components.get("volatility_pressure"),
        "vix_recent_cycle": vix_cycle,
        "vix_five_session_ohlc_stress": vix_ohlc.get("five_session_ohlc", {}),
        "rates_and_macro_proxies": rates,
        "basket_definition": baskets,
        "technology_5d_absolute_return_pct": tech_return_5d,
        "technology_5d_relative_vs_spy_pp": sum(tech_relative_5d) / len(tech_relative_5d) if tech_relative_5d else None,
        "defensive_5d_absolute_return_pct": def_value,
        "cyclical_5d_absolute_return_pct": cyc_value_5d,
        "scenario_fixtures": scenarios,
        "turning_point_rules": dict(TURNING_POINT_RULES),
        "data_coverage": {"as_of": as_of, "expected_session": requested_asof,
                          "required_equity_symbols": list(required_symbols), "stale_or_misaligned_symbols": stale_symbols,
                          "credit_available": credit_direction is not None,
                          "vix_available": volatility_direction is not None,
                          "credit_as_of": (risk_components.get("credit_risk_appetite") or {}).get("as_of"),
                          "vix_as_of": (risk_components.get("volatility_pressure") or {}).get("as_of"),
                          "missing_confirmation_groups": missing_confirmation_groups,
                          "relative_pairs_available": [key for key, value in pair_features.items() if value.get("status") == "available"],
                          "minimum_observations": required_history},
        "rules": dict(RULES),
    }
    features["turning_point_watch"] = build_turning_point_watch(features, risk, vix_cycle,
        local_vix, window=window)
    result = {"methodology_version": TRANSITION_METHODOLOGY_VERSION, "as_of": as_of,
              "analysis_window_sessions": window, "transition_momentum_sessions": momentum_window,
              "current_regime": current_regime, "current_leadership": current_leadership,
              "transition_status": transition_status,
              "supporting_evidence": supporting[:5], "contradictory_evidence": contradictory[:5],
              "confirm_conditions": confirm_conditions, "invalidate_conditions": invalidate_conditions,
              "summary": _transition_explanation(transition_status, current_leadership,
                  supporting, contradictory, confirm_conditions, invalidate_conditions)["summary"],
              "features": features, "data_coverage": features["data_coverage"],
              "source_fingerprint": None}
    result["source_fingerprint"] = _fingerprint(result)
    result["feature_export"] = export_transition_features(result)
    result["signal_strip"] = build_transition_signal_strip(result)
    return result


def _confirmed_transition_state(histories, vix_rows, session_days, window, expected, current_risk, basket_config):
    persistence = RULES["confirmed_regime_persistence_sessions"]
    if len(session_days) < window + persistence + 1:
        return None
    recent_days = session_days[-persistence:]
    prior_day = session_days[-persistence - 1]
    if not _valid_sessions([*session_days[-persistence - 1:]]):
        return None
    states = []
    for day in recent_days:
        truncated = _truncate_histories(histories, day)
        vix = [row for row in vix_rows if str(row.get("observation_date", "")) <= day]
        vix_current = bool(vix and vix[-1].get("observation_date") == day)
        states.append(calculate_risk_appetite(truncated, vix, timeframe=window,
            expected_session=day, vix_current_available=vix_current,
            basket_config=basket_config).get("classification"))
    previous = calculate_risk_appetite(_truncate_histories(histories, prior_day),
        [row for row in vix_rows if str(row.get("observation_date", "")) <= prior_day],
        timeframe=window, expected_session=prior_day,
        vix_current_available=any(row.get("observation_date") == prior_day for row in vix_rows),
        basket_config=basket_config).get("classification")
    current = current_risk.get("classification")
    if (current in {"Risk-on", "Risk-off"} and states == [current] * persistence and previous != current
            and previous != "Insufficient Evidence"):
        return {"status": "confirmed", "from": previous, "to": current,
                "persistence_sessions": persistence,
                "explanation": f"Risk Appetite changed from {previous} to {current} and persisted across {persistence} completed sessions with the rule's breadth/credit/VIX confirmation."}
    return None


def _fingerprint(record):
    payload = {key: record.get(key) for key in ("methodology_version", "as_of", "current_regime",
        "transition_status", "features")}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def export_transition_features(record):
    """Versioned feature-only contract for later evaluation; contains no outcomes."""
    features = record.get("features") or {}
    return {"schema_version": TRANSITION_EXPORT_SCHEMA,
            "methodology_version": record.get("methodology_version"),
            "source_data_fingerprint": record.get("source_fingerprint"),
            "as_of_session": record.get("as_of"),
            "feature_timestamps": {"observed_through": record.get("as_of"),
                                   "expected_session": features.get("data_coverage", {}).get("expected_session")},
            "current_regime": record.get("current_regime"),
            "transition_state": record.get("transition_status"),
            "feature_values": features,
            "data_coverage": record.get("data_coverage")}


def _empty_transition(window, reason):
    result = {"methodology_version": TRANSITION_METHODOLOGY_VERSION, "as_of": None,
              "analysis_window_sessions": window, "transition_momentum_sessions": RULES["transition_momentum_sessions"],
              "current_regime": "Insufficient Evidence", "current_leadership": "Unavailable",
              "transition_status": "Insufficient data", "supporting_evidence": [],
              "contradictory_evidence": [], "confirm_conditions": [], "invalidate_conditions": [reason],
              "summary": reason, "features": {"data_coverage": {"status": "unavailable", "reason": reason}},
              "data_coverage": {"status": "unavailable", "reason": reason}}
    result["features"]["turning_point_watch"] = build_turning_point_watch(
        result["features"], {}, {}, [], window=window)
    result["source_fingerprint"] = _fingerprint(result)
    result["feature_export"] = export_transition_features(result)
    result["signal_strip"] = build_transition_signal_strip(result)
    return result


def build_transition_signal_strip(record: dict) -> dict:
    """Create the compact, rule-only UI summary without an AI interpretation."""
    record = record or {}
    features = record.get("features") or {}
    coverage = record.get("data_coverage") or features.get("data_coverage") or {}
    status = record.get("transition_status", "Insufficient data")
    current = record.get("current_regime", "Insufficient Evidence")
    leadership = record.get("current_leadership", "Unavailable")
    pairs = features.get("pair_relative_performance") or {}
    rsp = pairs.get("RSP") or {}
    iwm = pairs.get("IWM") or {}
    rsp_momentum = rsp.get("relative_momentum_change_pp")
    iwm_momentum = iwm.get("relative_momentum_change_pp")
    breadth_change = features.get("sector_breadth_change")
    threshold = float((features.get("rules") or RULES).get("relative_weakening_threshold_pp", RULES["relative_weakening_threshold_pp"]))
    broad_strengthening = (rsp_momentum is not None and iwm_momentum is not None
        and rsp_momentum >= threshold and iwm_momentum >= threshold
        and breadth_change is not None and breadth_change >= 0)
    basket_momentum = features.get("cyclical_defensive_momentum_change_pp")
    defensive_weakening = leadership == "Defensive leadership" and basket_momentum is not None and basket_momentum >= threshold
    defensive_strengthening = leadership == "Defensive leadership" and basket_momentum is not None and basket_momentum <= -threshold
    if status == "Insufficient data" or current == "Insufficient Evidence":
        direction = "No clear emerging direction"
    elif broad_strengthening:
        direction = "Broader equity participation strengthening"
    elif defensive_weakening:
        direction = "Defensive leadership weakening"
    elif defensive_strengthening and status in {"Continuing", "Mixed / inconclusive"}:
        direction = "Defensive leadership strengthening"
    elif status == "Possible reversal":
        direction = "Potential leadership reversal"
    elif status == "Weakening":
        direction = f"{leadership} weakening" if leadership != "Unavailable" else "Existing leadership weakening"
    elif status == "Transition confirmed by predefined rules":
        direction = f"Rule-defined transition toward {current}"
    elif status == "Mixed / inconclusive":
        direction = "Conflicting signals"
    elif status == "Continuing":
        direction = f"{leadership} continuing" if leadership != "Unavailable" else "No clear emerging direction"
    else:
        direction = "No clear emerging direction"

    components = (features.get("equity_confirmation"), features.get("sector_leadership_confirmation"),
                  features.get("credit_confirmation"), features.get("volatility_confirmation"))
    disagreements = []
    for item, label in zip(components, ("breadth", "sector leadership", "credit", "volatility")):
        if not isinstance(item, dict) or item.get("direction") is None:
            disagreements.append(label + " unavailable")
    directions = [item.get("direction") for item in components if isinstance(item, dict) and item.get("direction") is not None]
    if status == "Insufficient data":
        gaps = coverage.get("stale_or_misaligned_symbols") or coverage.get("missing_confirmation_groups") or []
        confirmation = "Insufficient — " + (", ".join(gaps[:2]) if gaps else "aligned history is incomplete")
    elif status == "Transition confirmed by predefined rules":
        confirmation = "Rule confirmation met — the observed state persisted; no future move is implied"
    elif status == "Possible reversal":
        confirmation = "Incomplete — early opposition needs persistence and independent confirmation"
    elif status == "Weakening":
        confirmation = "Incomplete — leadership has softened, but an opposing state is not established"
    elif status == "Mixed / inconclusive":
        directions_by_group = {label: (item.get("direction") if isinstance(item, dict) else None)
            for item, label in zip(components, ("breadth", "sector leadership", "credit", "volatility"))}
        if directions_by_group["breadth"] == 1 and any(directions_by_group[key] == -1 for key in ("credit", "volatility")):
            confirmation = "Incomplete — credit and/or volatility disagree with broad participation"
        elif directions_by_group["credit"] is not None and directions_by_group["volatility"] is not None and directions_by_group["credit"] * directions_by_group["volatility"] < 0:
            confirmation = "Incomplete — credit and volatility disagree"
        elif len(directions) >= 2 and min(directions) < 0 < max(directions):
            confirmation = "Incomplete — breadth, leadership, credit, and volatility signals disagree"
        elif disagreements:
            confirmation = "Incomplete — " + ", ".join(disagreements)
        else:
            confirmation = "Incomplete — indicators do not meet a persistent transition rule"
    else:
        confirmation = "No transition confirmed — current leadership has not met reversal rules"
    return {"current_regime": current, "emerging_direction": direction,
            "confirmation_status": confirmation, "transition_status": status,
            "as_of": record.get("as_of"), "analysis_window_sessions": record.get("analysis_window_sessions"),
            "momentum_window_sessions": record.get("transition_momentum_sessions", RULES["transition_momentum_sessions"]),
            "methodology_version": record.get("methodology_version", TRANSITION_METHODOLOGY_VERSION)}

"""Explainable, deterministic market risk-appetite classification.

All ETF returns use common adjusted-close sessions. The output is descriptive,
not a forecast, probability, or recommendation. ETF relationships are not
statistically independent and do not establish fund flows.
"""
from __future__ import annotations

from datetime import date
from math import isfinite

from dashboard.cross_market import price_map, sector_participation_metrics
from dashboard.market_history import SECTOR_SYMBOLS
from dashboard.market_explorer import DEFAULT_BASKETS, normalize_basket_config

METHODOLOGY_VERSION = "risk-appetite-v5"
SUPPORTED_WINDOWS = (5, 20, 60)
RULES = {
    "broad_participation_min_sectors": 6,
    "weak_participation_max_sectors": 4,
    "defensive_cyclical_spread_threshold_pp": 0.50,
    "pair_agreement_count": 2,
    "vix_subdued_below": 15.0,
    "vix_elevated_at": 20.0,
    "vix_extreme_at": 30.0,
    "vix_easing_5d_pct": -5.0,
    "vix_pressure_5d_pct": 10.0,
    "prior_stress_vix_points": 25.0,
    "minimum_signal_groups": 3,
}


def _return(histories, symbols, window, weights=None):
    """Weighted average simple return on exact common observed sessions."""
    maps = {symbol: price_map(histories.get(symbol, [])) for symbol in symbols}
    if not maps or any(not values for values in maps.values()):
        return None, None, 0
    dates = sorted(set.intersection(*(set(values) for values in maps.values())))
    if len(dates) < window + 1:
        return None, dates[-1] if dates else None, len(dates)
    dates = dates[-(window + 1):]
    weights = weights or {symbol: 1.0 for symbol in symbols}
    total_weight = sum(float(weights.get(symbol, 0)) for symbol in symbols)
    if total_weight <= 0:
        return None, dates[-1], len(dates)
    returns = [(values[dates[-1]] / values[dates[0]] - 1) * float(weights.get(symbol, 0)) / total_weight
               for symbol, values in maps.items()]
    if not all(isfinite(value) for value in returns):
        return None, dates[-1], len(dates)
    return sum(returns) / len(returns), dates[-1], len(dates)


def _relative_excess(histories, asset, benchmark, window):
    a, b = price_map(histories.get(asset, [])), price_map(histories.get(benchmark, []))
    dates = sorted(set(a) & set(b))
    if len(dates) < window + 1:
        return None, dates[-1] if dates else None, len(dates)
    dates = dates[-(window + 1):]
    excess_pp = ((a[dates[-1]] / a[dates[0]] - 1) - (b[dates[-1]] / b[dates[0]] - 1)) * 100
    return excess_pp, dates[-1], len(dates)


def _absolute_return(histories, symbol, window):
    values = price_map(histories.get(symbol, []))
    dates = sorted(values)
    if len(dates) < window + 1:
        return None, dates[-1] if dates else None, len(dates)
    dates = dates[-(window + 1):]
    return values[dates[-1]] / values[dates[0]] - 1, dates[-1], len(dates)


def _component(key, status, direction, value=None, unit=None, as_of=None, evidence=(), explanation=""):
    return {"id": key, "status": status, "direction": direction, "value": value,
            "unit": unit, "as_of": as_of, "evidence": list(evidence),
            "explanation": explanation}


def calculate_risk_appetite(histories, vix_rows, *, timeframe=20, expected_session=None,
                            vix_current_available=None, basket_config=None):
    """Summarize participation, cyclical rotation, credit, and VIX behavior.

    A component direction is +1 for constructive risk appetite, -1 for
    defensive/stress evidence, and 0 for mixed/contained evidence. Overall
    labels require multiple independent *groups*; no single signal determines
    the classification.
    """
    window = timeframe if timeframe in SUPPORTED_WINDOWS else 20
    expected = str(expected_session) if expected_session else None
    baskets = normalize_basket_config(basket_config or DEFAULT_BASKETS)
    cyclical_sectors = tuple(baskets["cyclical"])
    defensive_sectors = tuple(baskets["defensive"])
    if expected:
        histories = {symbol: [row for row in rows or [] if str(row.get("trading_date", "")) <= expected]
                    for symbol, rows in (histories or {}).items()}
        vix_rows = [row for row in vix_rows or [] if str(row.get("observation_date", "")) <= expected]
    participation = sector_participation_metrics(histories, timeframe=window)
    as_of = participation.get("as_of")
    broad_date = as_of
    stale_equity = False
    equity = _component("equity_participation", "unavailable", None,
                         as_of=broad_date, explanation="RSP/IWM relative returns and standard-sector participation on matched sessions.")
    relative_count = participation.get("outperformers", {}).get(window, {}).get("count")
    sector_available = participation.get("outperformers", {}).get(window, {}).get("available", 0)
    absolute = (participation.get("absolute_breadth") or {}).get(window, {})
    sector_count = absolute.get("positive_count")
    above50 = (participation.get("above_sma50") or {}).get("count")
    sma_available = (participation.get("above_sma50") or {}).get("available", 0)
    above200 = (participation.get("above_sma200") or {}).get("count")
    aligned_pairs = participation.get("broad_pair_returns") or {}
    rsp_pair = (aligned_pairs.get("RSP/SPY") or {}).get(window) or {}
    iwm_pair = (aligned_pairs.get("IWM/SPY") or {}).get(window) or {}
    rsp, iwm = rsp_pair.get("excess_pp"), iwm_pair.get("excess_pp")
    rsp_spy = (participation.get("rsp_spy_returns") or {}).get(window, {})
    spy_return = rsp_spy.get("spy_return")
    rsp_return = rsp_spy.get("rsp_return")
    iwm_return = iwm_pair.get("asset_return")
    equity_dates = (rsp_pair.get("as_of"), iwm_pair.get("as_of"), as_of)
    equity_date = equity_dates[0] if all(equity_dates) and len(set(equity_dates)) == 1 else None
    stale_equity = bool(expected and any(day != expected for day in equity_dates))
    if (not stale_equity and equity_date and sector_count is not None and rsp is not None and iwm is not None
            and sector_available >= 8):
        broad_min = RULES["broad_participation_min_sectors"]
        weak_max = RULES["weak_participation_max_sectors"]
        breadth_broad = sector_count >= broad_min
        breadth_weak = sector_count <= weak_max
        # Absolute returns determine whether participation is constructive.
        # RSP/IWM excess returns remain useful relative context, but cannot
        # turn a falling market into "broadening participation" by themselves.
        bullish = spy_return is not None and rsp_return is not None and iwm_return is not None and min(spy_return, rsp_return, iwm_return) > 0 and breadth_broad
        bearish = spy_return is not None and rsp_return is not None and iwm_return is not None and max(spy_return, rsp_return, iwm_return) < 0 and breadth_weak
        relative_resilience = (spy_return is not None and rsp_return is not None and spy_return < 0 and rsp_return < 0
                               and rsp is not None and rsp > 0 and relative_count is not None and relative_count >= broad_min)
        direction = 1 if bullish else -1 if bearish and not relative_resilience else 0
        status = ("Broadening participation" if bullish else "Relative resilience during selloff" if relative_resilience else
                  "Broad deterioration" if bearish else "Mixed participation")
        equity = _component("equity_participation", status, direction,
            value={"spy_return_pct": spy_return * 100 if spy_return is not None else None,
                    "rsp_return_pct": rsp_return * 100 if rsp_return is not None else None,
                    "iwm_return_pct": iwm_return * 100 if iwm_return is not None else None,
                    "rsp_excess_pp": rsp, "iwm_excess_pp": iwm,
                    "sectors_positive_absolute": sector_count,
                    "sectors_outperforming_spy": relative_count, "sectors_above_sma50": above50,
                    "sectors_above_sma200": above200},
            unit={"absolute_returns": "%", "relative_returns": "percentage points", "breadth": "of 11 sector ETFs"},
            as_of=equity_date,
            evidence=[f"SPY {spy_return * 100:+.2f}% · RSP {rsp_return * 100:+.2f}%" if spy_return is not None and rsp_return is not None else "SPY/RSP absolute return unavailable",
                      f"RSP − SPY: {rsp:+.2f} pp", f"IWM − SPY: {iwm:+.2f} pp",
                      f"Positive sector ETF returns: {sector_count}/11",
                      f"Sector ETFs outperforming SPY: {relative_count}/11",
                      f"Sectors above SMA50: {above50}/{sma_available}" if above50 is not None else "SMA50 breadth unavailable",
                      f"Sectors above SMA200: {above200}/11" if above200 is not None else "SMA200 breadth unavailable"],
            explanation="Absolute index/sector ETF returns determine participation direction; matched excess returns are reported separately. ETF proxies are not constituent breadth or fund-flow evidence.")
    elif stale_equity:
        equity["status"] = "Stale observations"
        equity["evidence"] = [f"Latest common sector date {broad_date}; expected completed session {expected}."]

    # Defensive and cyclical baskets are equal-weight simple-return proxies on
    # one common date set. Pair comparisons are separately matched by dates.
    defensive_ret, basket_date, basket_n = _return(histories, defensive_sectors, window, baskets["defensive"])
    cyclical_ret, cyclical_date, cyclical_n = _return(histories, cyclical_sectors, window, baskets["cyclical"])
    xlp_xly, d1, _ = _relative_excess(histories, "XLP", "XLY", window)
    xlu_xli, d2, _ = _relative_excess(histories, "XLU", "XLI", window)
    xlv_xlk, d3, _ = _relative_excess(histories, "XLV", "XLK", window)
    defensive_date = basket_date if basket_date == cyclical_date == d1 == d2 == d3 else None
    defensive = _component("defensive_rotation", "unavailable", None, as_of=defensive_date,
        explanation="Defensive versus cyclical ETF price leadership; not a recession signal or capital-flow measure.")
    if defensive_date and defensive_ret is not None and cyclical_ret is not None:
        basket_spread_pp = (cyclical_ret - defensive_ret) * 100
        pairs = [xlp_xly, xlu_xli, xlv_xlk]
        cyclical_pairs = sum(value is not None and value < 0 for value in pairs)
        defensive_pairs = sum(value is not None and value > 0 for value in pairs)
        spread_threshold = RULES["defensive_cyclical_spread_threshold_pp"]
        pair_agreement = RULES["pair_agreement_count"]
        direction = (1 if basket_spread_pp >= spread_threshold and cyclical_pairs >= pair_agreement else
                     -1 if basket_spread_pp <= -spread_threshold and defensive_pairs >= pair_agreement else 0)
        status = "Cyclical leadership" if direction > 0 else "Defensive leadership" if direction < 0 else "Mixed sector rotation"
        defensive = _component("defensive_rotation", status, direction,
            value={"cyclical_minus_defensive_pp": basket_spread_pp,
                   "xlp_minus_xly_pp": xlp_xly, "xlu_minus_xli_pp": xlu_xli,
                   "xlv_minus_xlk_pp": xlv_xlk},
            unit="percentage points", as_of=defensive_date,
            evidence=[f"Cyclical minus defensive basket: {basket_spread_pp:+.2f} pp",
                      f"XLP/XLY {xlp_xly:+.2f} pp · XLU/XLI {xlu_xli:+.2f} pp · XLV/XLK {xlv_xlk:+.2f} pp"],
            explanation="User-configured weighted sector ETF returns and matched pair spreads. Defensive leadership alone does not establish broad risk-off conditions.")
    elif expected and defensive_date and defensive_date != expected:
        defensive["status"] = "Stale observations"
        defensive["evidence"] = [f"Latest matched basket date {defensive_date}; expected {expected}."]

    hyg_lqd, credit_date_a, credit_n_a = _relative_excess(histories, "HYG", "LQD", window)
    hyg_ief, credit_date_b, credit_n_b = _relative_excess(histories, "HYG", "IEF", window)
    credit_date = credit_date_a if credit_date_a == credit_date_b else None
    credit = _component("credit_risk_appetite", "unavailable", None, as_of=credit_date,
        explanation="HYG relative ETF price behavior, not a directly observed credit spread.")
    if credit_date and hyg_lqd is not None and hyg_ief is not None and not (expected and credit_date != expected):
        direction = 1 if hyg_lqd > 0 and hyg_ief > 0 else -1 if hyg_lqd < 0 and hyg_ief < 0 else 0
        credit = _component("credit_risk_appetite", "Credit-sensitive proxies improving" if direction > 0 else
            "Credit-sensitive proxies weakening" if direction < 0 else "Mixed credit proxies", direction,
            value={"hyg_minus_lqd_pp": hyg_lqd, "hyg_minus_ief_pp": hyg_ief},
            unit="percentage points", as_of=credit_date,
            evidence=[f"HYG − LQD: {hyg_lqd:+.2f} pp", f"HYG − IEF: {hyg_ief:+.2f} pp"],
            explanation="Matched adjusted ETF returns; these ratios are credit-risk appetite proxies, not credit-spread levels.")
    elif credit_date and expected and credit_date != expected:
        credit["status"] = "Stale observations"
        credit["evidence"] = [f"Latest matched credit-proxy date {credit_date}; expected {expected}."]

    vix = _component("volatility_pressure", "unavailable", None,
                     explanation="VIX is implied volatility, not market direction.")
    rows = sorted((row for row in (vix_rows or []) if row.get("observation_date") and row.get("close") is not None),
                  key=lambda row: str(row["observation_date"]))
    if rows:
        vix_date = str(rows[-1]["observation_date"])
        closes = [float(row["close"]) for row in rows]
        stale = (vix_current_available is False) or bool(expected and vix_date != expected)
        if stale:
            vix["status"] = "Stale observations"
            vix["as_of"] = vix_date
            vix["evidence"] = [f"Latest VIX close {vix_date}; expected completed session {expected}."]
        elif len(closes) >= 6:
            daily = (closes[-1] / closes[-2] - 1) * 100
            five = (closes[-1] / closes[-6] - 1) * 100
            regime = ("Extreme" if closes[-1] >= RULES["vix_extreme_at"] else
                      "Elevated" if closes[-1] >= RULES["vix_elevated_at"] else
                      "Normal" if closes[-1] >= RULES["vix_subdued_below"] else "Subdued")
            direction = (1 if five <= RULES["vix_easing_5d_pct"] and daily < 5 and closes[-1] < RULES["vix_extreme_at"] else
                         -1 if five >= RULES["vix_pressure_5d_pct"] or
                         (closes[-1] >= RULES["vix_extreme_at"] and five > 0) else 0)
            vix = _component("volatility_pressure", "Stress easing" if direction > 0 else
                "Pressure increasing" if direction < 0 else "Contained / mixed", direction,
                value={"close": closes[-1], "regime": regime, "daily_change_percent": daily,
                       "five_session_change_percent": five}, unit={"level": "VIX points", "changes": "%"},
                as_of=vix_date, evidence=[f"VIX {closes[-1]:.2f} · {regime}", f"Daily close change {daily:+.1f}%",
                    f"5-session close change {five:+.1f}%"],
                explanation="Absolute VIX regime and close-to-close changes; VIX measures implied volatility, not equity direction.")
        else:
            vix["status"] = "Insufficient history"
            vix["as_of"] = vix_date

    components = {item["id"]: item for item in (equity, defensive, credit, vix)}
    available = [item for item in components.values() if item["direction"] is not None]
    supporting = sum(item["direction"] > 0 for item in available)
    opposing = sum(item["direction"] < 0 for item in available)
    prior_stress = False
    if rows:
        # Prior-stress requirement: a recorded VIX close >= 25 before the
        # selected current return window. A calm present alone cannot qualify.
        prior_rows = rows[-(2 * window + 1):-window] if len(rows) > window else []
        prior_stress = any(float(row["close"]) >= RULES["prior_stress_vix_points"] for row in prior_rows)
    stress_easing = {
        "vix_pressure_easing": vix["direction"] == 1 if vix["direction"] is not None else None,
        "credit_improving": credit["direction"] == 1 if credit["direction"] is not None else None,
        "breadth_recovering": equity["direction"] == 1 if equity["direction"] is not None else None,
        "cyclical_technology_improving": defensive["direction"] == 1 if defensive["direction"] is not None else None,
    }
    observed_easing = sum(value is True for value in stress_easing.values())
    available_easing = sum(value is not None for value in stress_easing.values())
    stress_easing["state"] = ("Stress easing" if observed_easing >= 2 else
        "Stress pressure persists" if available_easing and observed_easing == 0 else
        "Mixed / incomplete" if available_easing else "Insufficient Evidence")
    recovery_confirmed = (prior_stress and equity["direction"] == 1 and
                          credit["direction"] in (0, 1) and vix["direction"] == 1)
    if equity["direction"] is None or len(available) < RULES["minimum_signal_groups"]:
        classification = "Insufficient Evidence"
        explanation = "At least three current, aligned signal groups including equity participation are needed for a regime label."
    elif equity["direction"] == 1 and credit["direction"] == 1 and vix["direction"] in (0, 1) and defensive["direction"] != -1:
        classification = "Risk-on"
        explanation = ("Broad participation and credit-sensitive proxies improve while volatility is not rising sharply. "
                       "Stress easing is tracked separately and is not a fourth regime.")
    elif (equity["direction"] == -1 and defensive["direction"] == -1
          and credit["direction"] == -1 and vix["direction"] == -1):
        classification = "Risk-off"
        explanation = "Broad participation deteriorates alongside weakening credit proxies and increasing volatility pressure."
    else:
        classification = "Rotation / Mixed"
        explanation = "Sector leadership or cross-market confirmation is mixed; defensive leadership alone is not enough to call broad risk-off."

    dates = [item.get("as_of") for item in available if item.get("as_of")]
    return {"methodology_version": METHODOLOGY_VERSION, "classification": classification,
            "timeframe_sessions": window, "as_of": min(dates) if dates else None,
            "components": components, "supporting_groups": supporting,
            "contradicting_groups": opposing, "prior_stress_observed": prior_stress,
            "transition_characteristics": {**stress_easing,
                "recovery_from_stress_confirmed": recovery_confirmed,
                "recovery_confirmation_requires_prior_stress": True},
            "explanation": explanation,
            "conditions_for_change": [
                "Broader deterioration requires participation weakness plus credit-proxy weakness and rising VIX pressure.",
                "Stress easing describes current improving signals; confirmed recovery from stress additionally requires recorded prior stress, improving breadth, stabilizing credit proxies, and sustained VIX easing.",
                "ETF relative returns do not establish fund flows; sector ETFs overlap broad indexes and are not independent signals."],
            "missing_or_stale": [key for key, item in components.items() if item["direction"] is None],
            "signal_groups_available": len(available), "rules": dict(RULES),
            "basket_config": baskets}

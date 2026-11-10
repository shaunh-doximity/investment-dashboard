"""Deterministic regime features built only from locally cached daily observations."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt

from dashboard.analytics import annualized_realized_volatility, momentum, moving_average
from dashboard.market_history import BROAD_MARKET_SYMBOLS, SECTOR_SYMBOLS
from dashboard.market_visuals import sector_rotation_pulse
from dashboard.market_calendar import latest_completed_us_equity_session


def _clean(bars):
    rows = []
    for row in sorted(bars or [], key=lambda x: x.get("trading_date", "")):
        try:
            value = float(row["close"])
            if value > 0:
                rows.append({"trading_date": row["trading_date"], "close": value})
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def percentile_rank(value: float | None, history: list[float], minimum: int = 20) -> float | None:
    """Empirical percentile, with current excluded from reference observations by caller."""
    values = [float(x) for x in history if x is not None]
    if value is None or len(values) < minimum:
        return None
    return sum(item <= value for item in values) / len(values) * 100


def _drawdown_series(bars, lookback=252):
    rows = _clean(bars)
    output = []
    for index, row in enumerate(rows):
        start = max(0, index - lookback + 1)
        window = rows[start:index + 1]
        if len(window) < lookback:
            continue
        high = max(item["close"] for item in window)
        output.append({"trading_date": row["trading_date"], "drawdown": row["close"] / high - 1})
    return output


def _ratio_momentum(numerator, denominator, periods=20):
    left = {row["trading_date"]: row["close"] for row in _clean(numerator)}
    right = {row["trading_date"]: row["close"] for row in _clean(denominator)}
    dates = sorted(left.keys() & right.keys())
    if len(dates) < periods + 1:
        return None
    ratios = [left[day] / right[day] for day in dates[-periods - 1:]]
    return ratios[-1] / ratios[0] - 1


def _relative_strength_trend(numerator, denominator, periods=20):
    left = {row["trading_date"]: row["close"] for row in _clean(numerator)}
    right = {row["trading_date"]: row["close"] for row in _clean(denominator)}
    dates = sorted(left.keys() & right.keys())
    if len(dates) < 2 * periods + 1:
        return {"recent_20d": None, "prior_20d": None, "change_vs_prior_pp": None}
    ratios = [left[day] / right[day] for day in dates[-(2 * periods + 1):]]
    prior = ratios[periods] / ratios[0] - 1
    recent = ratios[-1] / ratios[periods] - 1
    return {"recent_20d": recent, "prior_20d": prior,
            "change_vs_prior_pp": (recent - prior) * 100}


def _sector_dispersion(histories, symbols=SECTOR_SYMBOLS, periods=20):
    returns = {symbol: momentum(histories.get(symbol, []), periods) for symbol in symbols}
    values = [value for value in returns.values() if value is not None]
    if len(values) < 2:
        return {"status": "insufficient", "available": len(values), "universe": len(symbols), "stddev_pp": None, "range_pp": None, "returns": returns}
    average = sum(values) / len(values)
    stddev = sqrt(sum((value-average)**2 for value in values) / len(values))
    return {"status": "available" if len(values) == len(symbols) else "partial",
            "available": len(values), "universe": len(symbols),
            "stddev_pp": stddev * 100, "range_pp": (max(values)-min(values))*100,
            "returns": returns}


def _historical_sector_dispersion(histories, symbols=SECTOR_SYMBOLS, periods=20):
    """Build dispersion observations only on common dates across the fixed sector set."""
    maps = {symbol: {row["trading_date"]: row["close"] for row in _clean(histories.get(symbol, []))} for symbol in symbols}
    if not maps or any(not values for values in maps.values()):
        return []
    dates = sorted(set.intersection(*(set(values) for values in maps.values())))
    output = []
    for end in range(periods, len(dates)):
        start_day, end_day = dates[end - periods], dates[end]
        returns = [maps[symbol][end_day] / maps[symbol][start_day] - 1 for symbol in symbols]
        mean = sum(returns) / len(returns)
        output.append(sqrt(sum((value - mean) ** 2 for value in returns) / len(returns)) * 100)
    return output


def classify_regime(features: dict) -> str:
    """Transparent descriptive rule set; thresholds are conventions, not trained labels."""
    spy = features.get("spy", {})
    vol = features.get("volatility", {})
    if spy.get("above_sma50") is None or spy.get("above_sma200") is None or vol.get("close") is None:
        return "Insufficient data"
    if spy.get("status") in {"stale", "provider_lag"} or vol.get("status") in {"stale", "provider_lag"}:
        return "Stale input data"
    drawdown = spy.get("drawdown_252")
    if (not spy["above_sma50"] and not spy["above_sma200"]) or (drawdown is not None and drawdown <= -0.15) or vol["close"] >= 30:
        return "Risk-off conditions"
    if drawdown is None:
        return "Insufficient data"
    if spy["above_sma50"] and spy["above_sma200"] and drawdown > -0.10 and vol["close"] < 25:
        return "Risk-on conditions"
    return "Mixed conditions"


def build_regime_features(histories: dict[str, list[dict]], volatility: dict | None = None, *,
                          today: date | None = None, expected_session: date | str | None = None) -> dict:
    """Create an auditable indicator snapshot. No values are imputed."""
    volatility = volatility or {"status": "unavailable"}
    spy_bars = _clean(histories.get("SPY", []))
    spy_close = spy_bars[-1]["close"] if spy_bars else None
    spy_as_of = spy_bars[-1]["trading_date"] if spy_bars else None
    try:
        expected = (date.fromisoformat(expected_session) if isinstance(expected_session, str)
                    else expected_session or today or date.fromisoformat(
                        latest_completed_us_equity_session(datetime.now(ZoneInfo("America/New_York")))))
        observed = date.fromisoformat(spy_as_of)
        spy_age = (expected - observed).days
        spy_freshness = "current" if observed == expected else "provider_lag" if observed < expected else "future_observation"
    except (TypeError, ValueError):
        spy_age, spy_freshness = None, "unavailable"
    sma50, sma200 = moving_average(spy_bars, 50), moving_average(spy_bars, 200)
    dd = _drawdown_series(spy_bars)
    current_dd = dd[-1]["drawdown"] if dd else None
    dd_pct = percentile_rank(current_dd, [row["drawdown"] for row in dd[:-1]])
    rel = {symbol: _relative_strength_trend(histories.get(symbol, []), histories.get("SPY", []), 20) for symbol in ("QQQ", "RSP")}
    dispersion = _sector_dispersion(histories)
    dispersion_history = _historical_sector_dispersion(histories)
    dispersion["stddev_percentile_252"] = percentile_rank(
        dispersion.get("stddev_pp"), dispersion_history[-252:-1],
    )
    dispersion["percentile_observations"] = len(dispersion_history[-252:-1])
    pulse = sector_rotation_pulse(histories, SECTOR_SYMBOLS, 20)
    broad = {symbol: momentum(histories.get(symbol, []), 20) for symbol in BROAD_MARKET_SYMBOLS}
    broad_available = [value for value in broad.values() if value is not None]
    vix = {key: volatility.get(key) for key in ("close", "change_5d_points", "change_20d_points", "sma20", "percentile_252", "percentile_observations", "vix9d_ratio", "vix9d_as_of", "vix9d_status", "vix3m_ratio", "as_of", "status", "condition")}
    rv = {window: annualized_realized_volatility(spy_bars, window) for window in (20, 60, 252)}
    features = {
        "as_of": max((row["trading_date"] for row in spy_bars), default=None),
        "spy": {"close": spy_close, "sma50": sma50, "sma200": sma200,
                "as_of": spy_as_of, "age_calendar_days": spy_age, "status": spy_freshness,
                "above_sma50": spy_close > sma50 if spy_close is not None and sma50 is not None else None,
                "above_sma200": spy_close > sma200 if spy_close is not None and sma200 is not None else None,
                "drawdown_252": current_dd, "drawdown_252_percentile": dd_pct,
                "drawdown_observations": len(dd)},
        "relative_strength_trends": rel,
        "sector_dispersion_20d": dispersion,
        "defensive_vs_cyclical_20d_pp": pulse.get("defensive_vs_cyclical_pp"),
        "defensive_leadership": pulse.get("defensive_interpretation"),
        "broad_etf_20d_proxy": {"positive": sum(value > 0 for value in broad_available),
                                "available": len(broad_available), "universe": len(BROAD_MARKET_SYMBOLS),
                                "returns": broad},
        "volatility": vix,
        "spy_realized_volatility_annualized": {
            f"{window}d": value for window, value in rv.items()
        },
        "methodology": {
            "equity_prices": "Tiingo split-and-dividend-adjusted daily closes; close-to-close sessions; no missing-value fill.",
            "regime_thresholds": "Descriptive convention: risk-off if SPY is below both SMA50/SMA200, 252-session drawdown <= -15%, or VIX >= 30; risk-on if above both averages, drawdown > -10% (or unavailable), and VIX < 25; otherwise mixed.",
            "breadth": "Count of positive 20-session returns among six broad-market ETFs; a proxy, not constituent advance/decline breadth.",
            "sector_dispersion": "Population standard deviation and max-min range of available standard 11 sector ETF 20-session returns.",
            "relative_strength": "20-session return of the same-date adjusted-close ratio QQQ/SPY or RSP/SPY.",
            "historical_context_caveat": "Features are computed using observations dated no later than each historical feature date and forward outcomes are fully observed by the current as-of date. The provider supplies current adjusted history rather than point-in-time data vintages, so later corporate-action adjustments may restate prior values; strict vintage-level point-in-time validation is unavailable.",
        },
    }
    features["regime_classification"] = classify_regime(features)
    return features


def historical_analog_distribution(vix_history: list[dict], spy_history: list[dict], horizon: int = 20, tolerance_percentile_points: float = 10) -> dict:
    """Descriptive forward-return distribution for past similar VIX percentile readings.

    Each candidate's percentile uses only VIX observations available on that date. The
    candidate is excluded unless its forward outcome is fully observed by current SPY as-of.
    This is an unvalidated historical association, not a forecast.
    """
    vix = sorted(vix_history, key=lambda r: r["observation_date"])
    spy = sorted(_clean(spy_history), key=lambda r: r["trading_date"])
    spy_map = {row["trading_date"]: row["close"] for row in spy}
    if len(vix) < 252 or len(spy) <= horizon:
        return {"status": "insufficient_history", "horizon_sessions": horizon, "sample_size": 0}
    current_values = [float(row["close"]) for row in vix[-252:]]
    current_percentile = percentile_rank(current_values[-1], current_values[:-1], minimum=100)
    if current_percentile is None:
        return {"status": "insufficient_history", "horizon_sessions": horizon, "sample_size": 0}
    spy_index = {row["trading_date"]: i for i, row in enumerate(spy)}
    outcomes = []
    for i in range(251, len(vix)):
        day = vix[i]["observation_date"]
        if day not in spy_index or i >= len(vix) - 1:
            continue
        hist = [float(row["close"]) for row in vix[max(0, i-251):i+1]]
        rank = percentile_rank(hist[-1], hist[:-1], minimum=100)
        index = spy_index[day]
        end = index + horizon
        if rank is None or abs(rank-current_percentile) > tolerance_percentile_points or end >= len(spy):
            continue
        if spy[end]["trading_date"] > spy[-1]["trading_date"]:
            continue
        outcomes.append(spy[end]["close"] / spy[index]["close"] - 1)
    if not outcomes:
        return {"status": "no_matches", "horizon_sessions": horizon, "sample_size": 0,
                "current_vix_percentile": current_percentile, "tolerance_pp": tolerance_percentile_points}
    ordered = sorted(outcomes)
    return {"status": "available", "horizon_sessions": horizon, "sample_size": len(ordered),
            "current_vix_percentile": current_percentile, "tolerance_pp": tolerance_percentile_points,
            "mean_return": sum(ordered)/len(ordered), "median_return": ordered[len(ordered)//2],
            "range_return": [ordered[0], ordered[-1]],
            "caveat": "Descriptive historical association only; unvalidated, non-predictive, and sample-dependent."}

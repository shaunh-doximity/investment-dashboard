"""Deterministic cross-market, rates, and sector participation analytics.

All ETF comparisons assume the caller supplies one provider and one adjustment
basis (the dashboard uses Tiingo split-and-dividend-adjusted closes). FRED
macro observations are mapped to the first cached SPY session strictly after
their observation date, conservatively avoiding same-day post-close look-ahead.
"""

from __future__ import annotations

from datetime import date
from math import sqrt

from dashboard.analytics import moving_average
from dashboard.market_history import SECTOR_SYMBOLS


DEFAULT_WINDOWS = (20, 60, 120, 252)
DEFENSIVE_SECTORS = ("XLV", "XLP", "XLU")
CYCLICAL_SECTORS = ("XLY", "XLI", "XLB", "XLE", "XLF", "XLK", "XLC", "XLRE")
BREADTH_CLASSIFICATION_RULES = {
    "minimum_available_sector_etfs": 9,
    "healthy_broadening_min_positive_sector_etfs": 7,
    "relative_resilience_min_outperforming_sector_etfs": 6,
    "selling_pressure_max_positive_sector_etfs": 4,
    "selling_pressure_participation_decline_etfs": 2,
    "narrow_recovery_max_positive_sector_etfs": 4,
}


def price_map(bars):
    result = {}
    for row in bars or []:
        try:
            day, close = str(row["trading_date"]), float(row["close"])
            if close > 0:
                result[day] = close
        except (KeyError, TypeError, ValueError):
            continue
    return result


def session_calendar(histories, calendar_symbol="SPY"):
    return sorted(price_map(histories.get(calendar_symbol, [])).keys())


def _valid_gap(before, after):
    try:
        gap = (date.fromisoformat(after) - date.fromisoformat(before)).days
    except ValueError:
        return False
    # A normal weekend/long weekend is allowed; longer holes are not treated as sessions.
    return 0 < gap <= 4


def normalized_performance(histories, symbols, periods=20, *, benchmark=None):
    """Normalize selected daily ETF series on complete SPY sessions; no ffill."""
    symbols = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    if periods < 1:
        raise ValueError("periods must be positive")
    calendar = session_calendar(histories)
    if len(calendar) < periods + 1:
        return {"status": "insufficient_history", "required_sessions": periods + 1,
                "available_sessions": len(calendar), "missing_symbols": symbols}
    dates = calendar[-(periods + 1):]
    if any(not _valid_gap(a, b) for a, b in zip(dates, dates[1:])):
        return {"status": "calendar_gap", "required_sessions": periods + 1,
                "available_sessions": len(dates), "missing_symbols": []}
    maps = {symbol: price_map(histories.get(symbol, [])) for symbol in symbols}
    missing = [symbol for symbol, values in maps.items() if any(day not in values for day in dates)]
    if benchmark and benchmark.upper() not in maps:
        maps[benchmark.upper()] = price_map(histories.get(benchmark.upper(), []))
        if any(day not in maps[benchmark.upper()] for day in dates):
            missing.append(benchmark.upper())
    if missing:
        return {"status": "missing_observations", "required_sessions": periods + 1,
                "available_sessions": len(dates), "missing_symbols": list(dict.fromkeys(missing)),
                "dates": dates}
    base = maps[benchmark.upper()][dates[0]] if benchmark else None
    output = {}
    for symbol in symbols:
        vals = [maps[symbol][day] for day in dates]
        if benchmark:
            values = [100.0 * (price / maps[benchmark.upper()][day]) /
                      (vals[0] / maps[benchmark.upper()][dates[0]]) for day, price in zip(dates, vals)]
        else:
            values = [100.0 * value / vals[0] for value in vals]
        output[symbol] = values
    return {"status": "available", "dates": dates, "latest_date": dates[-1],
            "series": output, "base": 100.0,
            "normalization": "relative ratio rebased to 100" if benchmark else "price rebased to 100",
            "benchmark": benchmark.upper() if benchmark else None}


def daily_returns_by_session(bars, sessions):
    """Close-to-close returns keyed to SPY sessions, requiring both exact closes."""
    values = price_map(bars)
    result = {}
    for previous, current in zip(sessions, sessions[1:]):
        if not _valid_gap(previous, current):
            continue
        if previous in values and current in values:
            result[current] = values[current] / values[previous] - 1.0
    return result


def _latest_before(observations, session_date):
    candidates = []
    for row in observations or []:
        try:
            day = str(row["observation_date"])
            value = float(row.get("value", row.get("close")))
            if day < session_date and value == value:
                candidates.append((day, value))
        except (KeyError, TypeError, ValueError):
            continue
    return max(candidates, default=None, key=lambda item: item[0])


def macro_levels_as_known(observations, sessions):
    """Map last known macro level to sessions; also retain its actual observation date."""
    output = {}
    for session in sessions:
        latest = _latest_before(observations, session)
        if latest:
            output[session] = {"value": latest[1], "observation_date": latest[0],
                               "availability_rule": "observation date strictly before equity session"}
    return output


def macro_changes_by_session(observations, sessions, *, units="percent"):
    """Release-aware macro changes assigned to the next cached SPY session.

    The change is present only when a newer observation became available since
    the prior equity session; missing observations are never converted to zero.
    ``percent`` inputs are translated to basis points.
    """
    output = {}
    for previous_session, session in zip(sessions, sessions[1:]):
        prior = _latest_before(observations, previous_session)
        current = _latest_before(observations, session)
        if not prior or not current or current[0] <= prior[0]:
            continue
        difference = current[1] - prior[1]
        if units == "percent":
            difference *= 100.0
        output[session] = {"change": difference, "observation_date": current[0],
                           "previous_observation_date": prior[0],
                           "units": "basis_points" if units == "percent" else units}
    return output


def pearson_correlation(x, y):
    if len(x) != len(y) or len(x) < 2:
        return None
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    dx = [value - mean_x for value in x]
    dy = [value - mean_y for value in y]
    sum_x = sum(value * value for value in dx)
    sum_y = sum(value * value for value in dy)
    if sum_x <= 0 or sum_y <= 0:
        return None
    covariance = sum(a * b for a, b in zip(dx, dy))
    return covariance / sqrt(sum_x * sum_y)


def rolling_correlations(series_a, series_b, sessions, window):
    """Rolling correlations; any missing point in a window leaves that window absent."""
    if window < 2:
        raise ValueError("window must be at least 2")
    output = []
    for end in range(window - 1, len(sessions)):
        dates = sessions[end - window + 1:end + 1]
        if any(day not in series_a or day not in series_b for day in dates):
            continue
        corr = pearson_correlation([series_a[d] for d in dates], [series_b[d] for d in dates])
        if corr is not None:
            output.append({"date": dates[-1], "correlation": corr,
                           "observations": window, "window": window})
    return output


def pair_observations(series_a, series_b, sessions):
    return [{"date": day, "x": series_a[day], "y": series_b[day]}
            for day in sessions if day in series_a and day in series_b]


def lead_lag_correlations(leading_series, following_series, sessions, *, max_lag=5, minimum_observations=20):
    """Explore whether one dated series leads another; positive lag means X leads Y."""
    output = []
    for lag in range(-max_lag, max_lag + 1):
        x, y = [], []
        for index in range(len(sessions)):
            following_index = index + lag
            if not 0 <= following_index < len(sessions):
                continue
            x_day, y_day = sessions[index], sessions[following_index]
            if x_day in leading_series and y_day in following_series:
                x.append(leading_series[x_day])
                y.append(following_series[y_day])
        output.append({"lag_sessions": lag, "correlation": pearson_correlation(x, y),
                       "observations": len(x), "status": "available" if len(x) >= minimum_observations else "insufficient"})
    return output


def lead_lag_sample_diagnostics(leading_series, following_series, sessions, *, max_lag=5,
                                minimum_observations=20) -> dict:
    """Explain matched-date availability for an exploratory lead/lag request."""
    matched = pair_observations(leading_series, following_series, sessions)
    per_lag = lead_lag_correlations(leading_series, following_series, sessions,
                                    max_lag=max_lag, minimum_observations=minimum_observations)
    available = [item for item in per_lag if item["status"] == "available"]
    dates = [item["date"] for item in matched]
    if not sessions:
        reason = "No market session dates are available."
    elif not matched:
        reason = "The selected series have no matched observations after date alignment and transformations."
    elif not available:
        max_count = max((item["observations"] for item in per_lag), default=0)
        if len(sessions) <= 2 * max_lag:
            reason = "The requested lag range removes too many edge sessions from the selected observation window."
        else:
            reason = f"Only {max_count} valid matched pairs remain at every requested lag; at least {minimum_observations} are required. Missing dates and lag edge loss are not filled."
    else:
        reason = "Analysis has at least one lag with the minimum matched sample; each lag's sample count is shown separately."
    return {"status": "available" if available else "unavailable",
            "selected_start": sessions[0] if sessions else None,
            "selected_end": sessions[-1] if sessions else None,
            "overlap_start": min(dates) if dates else None,
            "overlap_end": max(dates) if dates else None,
            "matched_observations_zero_lag": len(matched),
            "requested_lags": (-int(max_lag), int(max_lag)),
            "minimum_required": int(minimum_observations),
            "per_lag": per_lag, "reason": reason}


def correlation_by_spy_200d_regime(asset_returns, benchmark_returns, histories, sessions, *, minimum_observations=10):
    """Compare pair correlation when SPY closes above/below its trailing SMA200."""
    spy = price_map(histories.get("SPY", []))
    regimes = {"above_200d": ([], []), "below_200d": ([], [])}
    for index, day in enumerate(sessions):
        if day not in asset_returns or day not in benchmark_returns or day not in spy:
            continue
        start = max(0, index - 199)
        close_window = [spy.get(item) for item in sessions[start:index + 1]]
        if len(close_window) < 200 or any(value is None for value in close_window):
            continue
        key = "above_200d" if spy[day] > sum(close_window) / 200 else "below_200d"
        regimes[key][0].append(asset_returns[day])
        regimes[key][1].append(benchmark_returns[day])
    result = {}
    for name, (left, right) in regimes.items():
        result[name] = {"correlation": pearson_correlation(left, right), "observations": len(left),
                        "status": "available" if len(left) >= minimum_observations else "insufficient"}
    return result


def sector_participation_metrics(histories, *, timeframe=20, sector_symbols=SECTOR_SYMBOLS):
    """Fixed-universe sector participation, relative performance, and concentration proxies."""
    symbols = tuple(sector_symbols)
    required = ("SPY", *symbols)
    ratio_symbols = ("RSP", "QQQ", "IWM", "SMH", "XLY", "XLP")
    all_symbols = tuple(dict.fromkeys((*required, *ratio_symbols)))
    maps = {symbol: price_map(histories.get(symbol, [])) for symbol in all_symbols}
    if any(not maps[symbol] for symbol in required):
        latest = None
        dates = []
    else:
        dates = sorted(set.intersection(*(set(maps[symbol]) for symbol in required)))
        latest = dates[-1] if dates else None
    if latest is None:
        return {"status": "insufficient_history", "as_of": None, "universe": len(symbols),
                "above_sma50": None, "above_sma200": None, "outperformers": {},
                "absolute_breadth": {}, "participation_change": {}, "rsp_spy_returns": {},
                "dispersion_pp": None, "leadership_concentration": None,
                "ratio_returns": {}, "defensive_vs_cyclical_pp": None}
    calendar = session_calendar(histories)
    common_index = calendar.index(latest) if latest in calendar else -1
    # Use the exact common observation dates for SPY and all 11 sector ETFs.
    # Calendar dates alone can otherwise hide a missing bar in one instrument.
    end_dates = [day for day in dates if common_index < 0 or day in set(calendar[:common_index + 1])]
    participation = {}
    for lookback in (50, 200):
        above, valid = 0, 0
        for symbol in symbols:
            bars = [row for row in histories.get(symbol, []) if row.get("trading_date") <= latest]
            avg = moving_average(bars, lookback)
            close = maps[symbol].get(latest)
            if avg is not None and close is not None:
                valid += 1
                above += close > avg
        participation[f"above_sma{lookback}"] = {"count": above if valid else None,
                                                   "available": valid, "universe": len(symbols)}

    sector_returns = {}
    windows = (5, 10, 20, 60, 120)
    for window in windows:
        returned = {}
        if len(end_dates) >= window + 1:
            days = end_dates[-(window + 1):]
            for symbol in symbols:
                if all(day in maps[symbol] for day in days):
                    returned[symbol] = maps[symbol][days[-1]] / maps[symbol][days[0]] - 1
        sector_returns[window] = returned
    spy_returns = {}
    for window in windows:
        if len(end_dates) >= window + 1:
            days = end_dates[-(window + 1):]
            if all(day in maps["SPY"] for day in days):
                spy_returns[window] = maps["SPY"][days[-1]] / maps["SPY"][days[0]] - 1
            else:
                spy_returns[window] = None
        else:
            spy_returns[window] = None
    outperformers = {}
    for window in (5, 10, 20, 60):
        baseline = spy_returns.get(window)
        values = sector_returns[window]
        outperformers[window] = {"count": sum(value > baseline for value in values.values()) if baseline is not None else None,
                                 "available": len(values), "universe": len(symbols),
                                 "excess_pp": {symbol: (value - baseline) * 100 for symbol, value in values.items()} if baseline is not None else {}}
    # Absolute participation and relative outperformance are deliberately
    # separate: a sector can beat SPY while still posting a negative return.
    absolute_breadth = {}
    for window in (5, 10, 20, 60):
        values = sector_returns.get(window, {})
        benchmark = spy_returns.get(window)
        absolute_breadth[window] = {
            "positive_count": sum(value > 0 for value in values.values()) if values else None,
            "negative_count": sum(value < 0 for value in values.values()) if values else None,
            "available": len(values), "universe": len(symbols),
            "outperforming_spy_count": (outperformers.get(window) or {}).get("count"),
            "spy_return": benchmark,
            "as_of": end_dates[-1] if len(end_dates) >= window + 1 else None,
            "start_date": end_dates[-(window + 1)] if len(end_dates) >= window + 1 else None,
        }
    participation_change = {}
    for window in (5, 10, 20):
        item = {"status": "insufficient_history", "window_sessions": window,
                "required_aligned_closes": 2 * window + 1,
                "current_positive_count": None, "prior_positive_count": None,
                "positive_count_change": None, "current_outperforming_spy_count": None,
                "prior_outperforming_spy_count": None, "relative_count_change": None,
                "current_start_date": None, "current_end_date": None,
                "prior_start_date": None, "prior_end_date": None,
                "available_sectors": 0, "universe": len(symbols)}
        if len(end_dates) >= 2 * window + 1:
            prior_days = end_dates[-(2 * window + 1):-window]
            current_days = end_dates[-(window + 1):]
            prior_positive, current_positive, prior_relative, current_relative = [], [], [], []
            for symbol in symbols:
                if all(day in maps[symbol] for day in prior_days + current_days) and all(day in maps["SPY"] for day in prior_days + current_days):
                    prior_ret = maps[symbol][prior_days[-1]] / maps[symbol][prior_days[0]] - 1
                    current_ret = maps[symbol][current_days[-1]] / maps[symbol][current_days[0]] - 1
                    prior_spy = maps["SPY"][prior_days[-1]] / maps["SPY"][prior_days[0]] - 1
                    current_spy = maps["SPY"][current_days[-1]] / maps["SPY"][current_days[0]] - 1
                    prior_positive.append(prior_ret > 0)
                    current_positive.append(current_ret > 0)
                    prior_relative.append(prior_ret > prior_spy)
                    current_relative.append(current_ret > current_spy)
            needed = len(symbols)
            if len(prior_positive) == needed:
                item.update({"status": "available", "current_positive_count": sum(current_positive),
                    "prior_positive_count": sum(prior_positive),
                    "positive_count_change": sum(current_positive) - sum(prior_positive),
                    "current_outperforming_spy_count": sum(current_relative),
                    "prior_outperforming_spy_count": sum(prior_relative),
                    "relative_count_change": sum(current_relative) - sum(prior_relative),
                    "current_start_date": current_days[0], "current_end_date": current_days[-1],
                    "prior_start_date": prior_days[0], "prior_end_date": prior_days[-1],
                    "available_sectors": needed})
        participation_change[window] = item
    current_returns = list(sector_returns[timeframe].values()) if timeframe in sector_returns else []
    dispersion = (sqrt(sum((value - sum(current_returns) / len(current_returns)) ** 2
                           for value in current_returns) / len(current_returns)) * 100
                  if len(current_returns) > 1 else None)
    excess = sorted((value for value in outperformers.get(timeframe, {}).get("excess_pp", {}).values() if value > 0), reverse=True)
    leadership_concentration = sum(excess[:3]) / sum(excess) if excess and sum(excess) else None

    ratio_returns = {}
    pairs = {"RSP/SPY": ("RSP", "SPY"), "IWM/SPY": ("IWM", "SPY"),
             "SMH/SPY": ("SMH", "SPY"), "SMH/QQQ": ("SMH", "QQQ"),
             "XLY/XLP": ("XLY", "XLP")}
    for label, (left, right) in pairs.items():
        if left not in maps or right not in maps:
            continue
        if len(end_dates) >= timeframe + 1:
            days = end_dates[-(timeframe + 1):]
            if all(day in maps[left] and day in maps[right] for day in days):
                values = {day: maps[left][day] / maps[right][day] for day in days}
                ratio_returns[label] = values[days[-1]] / values[days[0]] - 1
            else:
                ratio_returns[label] = None
        else:
            ratio_returns[label] = None

    def basket_return(basket):
        vals = [sector_returns.get(timeframe, {}).get(symbol) for symbol in basket]
        if any(value is None for value in vals) or len(vals) != len(basket):
            return None
        return sum(vals) / len(vals)
    defensive = basket_return(DEFENSIVE_SECTORS)
    cyclical = basket_return(CYCLICAL_SECTORS)
    rsp_spy_returns = {}
    broad_pair_returns = {"RSP/SPY": {}, "IWM/SPY": {}}
    for window in windows:
        days = end_dates[-(window + 1):] if len(end_dates) >= window + 1 else []
        valid = bool(days and all(day in maps["RSP"] and day in maps["SPY"] for day in days))
        rsp_value = maps["RSP"][days[-1]] / maps["RSP"][days[0]] - 1 if valid else None
        spy_value = spy_returns.get(window)
        rsp_spy_returns[window] = {"rsp_return": rsp_value, "spy_return": spy_value,
            "excess_pp": (rsp_value - spy_value) * 100 if rsp_value is not None and spy_value is not None else None,
            "start_date": days[0] if days else None, "as_of": days[-1] if days else None,
            "status": "available" if rsp_value is not None and spy_value is not None else "insufficient_history"}
        for pair, asset in (("RSP/SPY", "RSP"), ("IWM/SPY", "IWM")):
            pair_valid = bool(days and all(day in maps[asset] and day in maps["SPY"] for day in days))
            asset_return = maps[asset][days[-1]] / maps[asset][days[0]] - 1 if pair_valid else None
            broad_pair_returns[pair][window] = {"asset_return": asset_return, "spy_return": spy_value,
                "excess_pp": (asset_return - spy_value) * 100 if asset_return is not None and spy_value is not None else None,
                "start_date": days[0] if pair_valid else None, "as_of": days[-1] if pair_valid else None,
                "status": "available" if pair_valid and spy_value is not None else "insufficient_history"}
    return {"status": "available", "as_of": latest, "timeframe": timeframe,
            "universe": len(symbols), "above_sma50": participation["above_sma50"],
            "above_sma200": participation["above_sma200"],
            "sector_returns": sector_returns, "outperformers": outperformers,
            "absolute_breadth": absolute_breadth, "participation_change": participation_change,
            "rsp_spy_returns": rsp_spy_returns, "broad_pair_returns": broad_pair_returns,
            "dispersion_pp": dispersion, "leadership_concentration": leadership_concentration,
            "ratio_returns": ratio_returns,
            "defensive_return": defensive, "cyclical_return": cyclical,
            "defensive_vs_cyclical_pp": (defensive - cyclical) * 100 if defensive is not None and cyclical is not None else None,
            "methodology": {"breadth": "Fixed 11 standard sector ETFs; percent of available sector ETFs above same-date 50D/200D SMA; not constituent breadth.",
                            "leadership_concentration": "Top-three positive sector excess returns divided by sum of all positive sector excess returns; return-magnitude proxy, not market-cap concentration or fund flows.",
                            "baskets": "Equal-weight arithmetic mean of returns: defensive XLV/XLP/XLU; cyclical XLY/XLI/XLB/XLE/XLF/XLK/XLC/XLRE."}}


def classify_breadth_condition(metrics, *, window=20):
    """Classify absolute and relative sector-ETF participation separately.

    Healthy broadening requires positive SPY and RSP returns, at least 7/11
    positive sector ETF returns, and an increase in positive-sector count
    versus the prior equal-length window. Relative resilience in a selloff is
    never called healthy broadening. This is a descriptive ETF proxy, not
    constituent breadth, a forecast, or a fund-flow measure.
    """
    metrics = metrics or {}
    absolute = (metrics.get("absolute_breadth") or {}).get(window) or {}
    pair = (metrics.get("rsp_spy_returns") or {}).get(window) or {}
    change = (metrics.get("participation_change") or {}).get(window) or {}
    required = (absolute.get("positive_count"), absolute.get("spy_return"),
                pair.get("rsp_return"), pair.get("excess_pp"))
    rules = BREADTH_CLASSIFICATION_RULES
    if any(value is None for value in required) or absolute.get("available", 0) < rules["minimum_available_sector_etfs"]:
        return {"status": "Insufficient data", "window": window,
                "explanation": "Aligned absolute returns for SPY, RSP and most standard sector ETFs are required.",
                "as_of": absolute.get("as_of"), "positive_sectors": absolute.get("positive_count"),
                "outperforming_sectors": absolute.get("outperforming_spy_count"),
                "positive_count_change": change.get("positive_count_change")}
    spy, rsp, excess = pair["spy_return"], pair["rsp_return"], pair["excess_pp"]
    positive = absolute["positive_count"]
    delta = change.get("positive_count_change") if change.get("status") == "available" else None
    if spy < 0 and rsp < 0 and excess > 0 and (absolute.get("outperforming_spy_count") or 0) >= rules["relative_resilience_min_outperforming_sector_etfs"]:
        status, explanation = "Relative resilience during a selloff", "RSP and SPY both declined; RSP fell less and sector ETFs relatively outperformed, which is not positive absolute breadth."
    elif spy < 0 and positive <= rules["selling_pressure_max_positive_sector_etfs"] and delta is not None and delta <= -rules["selling_pressure_participation_decline_etfs"]:
        status, explanation = "Broadening selling pressure", "SPY declined, few sectors posted positive returns, and positive-sector participation contracted versus the prior window."
    elif spy > 0 and positive <= rules["narrow_recovery_max_positive_sector_etfs"] and excess < 0:
        status, explanation = "Narrow leadership recovery", "SPY rose while few sectors were positive and RSP lagged, indicating narrow rather than broad participation."
    elif spy > 0 and rsp > 0 and positive >= rules["healthy_broadening_min_positive_sector_etfs"] and delta is not None and delta >= 1:
        status, explanation = "Healthy broadening", "SPY and RSP rose, at least 7 sector ETFs gained, and positive-sector participation expanded versus the prior window."
    else:
        status, explanation = "Mixed", "Absolute returns, relative performance, or participation changes do not meet a directional rule."
    return {"status": status, "window": window, "explanation": explanation,
            "as_of": absolute.get("as_of"), "positive_sectors": positive,
            "outperforming_sectors": absolute.get("outperforming_spy_count"),
            "positive_count_change": delta, "spy_return_pct": spy * 100,
            "rsp_return_pct": rsp * 100, "rsp_excess_pp": excess,
            "start_date": absolute.get("start_date"),
            "participation_change_status": change.get("status", "unavailable"),
            "methodology_version": "sector-etf-breadth-v2", "rules": dict(rules)}


def observation_freshness(observation_date, *, today=None, stale_after_calendar_days=5):
    """Simple freshness label for daily observations; no data is modified."""
    try:
        age = ((today or date.today()) - date.fromisoformat(str(observation_date))).days
    except (TypeError, ValueError):
        return {"status": "unavailable", "age_days": None}
    if age < 0:
        return {"status": "future_date", "age_days": age}
    return {"status": "stale" if age > stale_after_calendar_days else "current", "age_days": age}


def relative_ratio_return(histories, numerator, denominator, periods=20):
    """Exact return of numerator/denominator price ratio over N SPY sessions."""
    sessions = session_calendar(histories)
    if len(sessions) < periods + 1:
        return {"status": "insufficient_history", "return": None, "available_sessions": len(sessions)}
    dates = sessions[-(periods + 1):]
    left, right = price_map(histories.get(numerator, [])), price_map(histories.get(denominator, []))
    missing = [symbol for symbol, values in ((numerator, left), (denominator, right))
               if any(day not in values for day in dates)]
    if missing:
        return {"status": "missing_observations", "return": None, "missing_symbols": missing,
                "dates": dates}
    beginning = left[dates[0]] / right[dates[0]]
    ending = left[dates[-1]] / right[dates[-1]]
    return {"status": "available", "return": ending / beginning - 1,
            "dates": dates, "as_of": dates[-1], "periods": periods}


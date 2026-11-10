"""Point-in-time descriptive VIX statistics; no prediction probabilities."""

from __future__ import annotations

from datetime import date
from math import isfinite, log, sqrt
from statistics import NormalDist


def _clean(rows):
    result = []
    for row in rows or []:
        try:
            day = date.fromisoformat(str(row["observation_date"])[:10])
            close = float(row["close"])
            if close <= 0 or not isfinite(close):
                continue
            item = {**row, "observation_date": day.isoformat(), "close": close}
            for field in ("open", "high", "low"):
                value = row.get(field)
                item[field] = float(value) if value is not None and isfinite(float(value)) else None
            result.append(item)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return sorted({row["observation_date"]: row for row in result}.values(), key=lambda row: row["observation_date"])


def _continuous(rows):
    return all((date.fromisoformat(b["observation_date"]) - date.fromisoformat(a["observation_date"])).days <= 4
               for a, b in zip(rows, rows[1:]))


def _mean_std(values):
    mean = sum(values) / len(values)
    std = sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    return mean, std


def vix_z_score(rows, *, window: int = 60) -> dict:
    """Current close standardized and ranked against the same trailing closes.

    The sample includes the as-of close and ends there (no look-ahead). Empirical
    rank uses midranks: (count below + half the ties) / window. The normal
    equivalent is an educational Gaussian mapping, not a VIX probability.
    """
    if window < 2:
        raise ValueError("window must be at least two sessions")
    clean = _clean(rows)
    if len(clean) < window:
        return {"status": "insufficient_history", "window": window, "observations": len(clean)}
    sample = clean[-window:]
    if not _continuous(sample):
        return {"status": "incomplete_dates", "window": window, "observations": len(sample)}
    values = [row["close"] for row in sample]
    mean, std = _mean_std(values)
    current = values[-1]
    below = sum(value < current for value in values)
    equal = sum(value == current for value in values)
    empirical_percentile = (below + 0.5 * equal) / window * 100
    basis = "trailing closes including the as-of close; population standard deviation; empirical percentile uses midrank"
    if std <= max(1e-12, abs(mean) * 1e-12):
        return {"status": "zero_standard_deviation", "window": window, "observations": window,
                "as_of": sample[-1]["observation_date"], "mean": mean, "standard_deviation": 0,
                "empirical_percentile": empirical_percentile, "normal_equivalent_percentile": None,
                "ranking_method": "midrank inclusive trailing sample", "basis": basis}
    z = (current - mean) / std
    return {"status": "available", "window": window, "observations": window,
            "as_of": sample[-1]["observation_date"], "value": z,
            "empirical_percentile": empirical_percentile,
            "normal_equivalent_percentile": NormalDist().cdf(z) * 100,
            "ranking_method": "midrank inclusive trailing sample",
            "mean": mean, "standard_deviation": std, "basis": basis}


def vix_return_variability(rows, *, return_window: int = 20, history_window: int = 252) -> dict:
    """Std. dev. of daily log returns, ranked against preceding rolling windows."""
    if return_window < 2 or history_window < 1:
        raise ValueError("return_window must be >=2 and history_window must be positive")
    clean = _clean(rows)
    values = []
    # Window N consists of N close-to-close returns and thus N+1 closes.
    for end in range(return_window, len(clean)):
        segment = clean[end - return_window:end + 1]
        if not _continuous(segment):
            values.append(None)
            continue
        returns = [log(segment[i]["close"] / segment[i - 1]["close"]) for i in range(1, len(segment))]
        values.append(_mean_std(returns)[1])
    if not values or values[-1] is None:
        return {"status": "insufficient_or_incomplete_history", "window": return_window,
                "observations": sum(value is not None for value in values)}
    current = values[-1]
    prior = [value for value in values[:-1] if value is not None][-history_window:]
    # A single or tiny comparison sample is not a useful historical ranking.
    # Keep variability itself available, but require 60 prior windows for rank.
    percentile = (sum(value <= current for value in prior) / len(prior) * 100
                  if len(prior) >= 60 else None)
    return {"status": "available", "window": return_window, "daily_log_return_std": current,
            "daily_log_return_std_percent": current * 100, "prior_windows": len(prior),
            "historical_percentile": percentile,
            "unusually_turbulent": percentile is not None and percentile >= 90,
            "percentile_status": "available" if percentile is not None else "insufficient_comparison_history",
            "as_of": clean[-1]["observation_date"],
            "basis": "population standard deviation of trailing daily log returns; percentile versus prior rolling windows only"}


def vix_reversal_frequency(rows, *, sessions: int = 20, minimum_spike_percent: float = 10.0,
                           unwind_threshold_percent: float = 50.0, large_range_percent: float = 10.0) -> dict:
    """Count broad intraday ranges separately from reversals of prior-close spikes."""
    if sessions < 1 or minimum_spike_percent <= 0 or not 0 < unwind_threshold_percent <= 100 or large_range_percent <= 0:
        raise ValueError("invalid reversal thresholds or session count")
    clean = _clean(rows)
    if len(clean) < sessions + 1:
        return {"status": "insufficient_history", "requested_sessions": sessions}
    sample = clean[-(sessions + 1):]
    if not _continuous(sample):
        return {"status": "incomplete_dates", "requested_sessions": sessions}
    records = sample[1:]
    if any(any(row.get(field) is None for field in ("open", "high", "low")) for row in records):
        return {"status": "incomplete_ohlc", "requested_sessions": sessions}
    if any(not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"] for row in records):
        return {"status": "invalid_ohlc", "requested_sessions": sessions}
    eligible = reversals = large_ranges = 0
    for previous, row in zip(sample, records):
        prior = previous["close"]
        if (row["high"] - row["low"]) / prior * 100 >= large_range_percent:
            large_ranges += 1
        surge = row["high"] / prior - 1
        if surge * 100 >= minimum_spike_percent:
            eligible += 1
            unwound = (row["high"] - row["close"]) / (row["high"] - prior) * 100
            if unwound >= unwind_threshold_percent:
                reversals += 1
    return {"status": "available", "as_of": records[-1]["observation_date"], "sessions": sessions,
            "eligible_spike_sessions": eligible, "significant_reversals": reversals,
            "reversal_fraction": reversals / eligible if eligible else None,
            "large_intraday_ranges": large_ranges, "minimum_spike_percent": minimum_spike_percent,
            "unwind_threshold_percent": unwind_threshold_percent, "large_range_percent": large_range_percent,
            "basis": "A reversal is counted only among sessions whose high exceeds prior close by the minimum spike; large ranges are counted independently."}


def _price_rows(rows):
    out = {}
    for row in rows or []:
        try:
            day = str(row.get("trading_date", row.get("observation_date")))[:10]
            close = float(row["close"])
            if close > 0 and isfinite(close):
                out[day] = close
        except (TypeError, ValueError, KeyError):
            pass
    return out


def equity_confirmation(histories: dict, *, as_of: str, sectors: tuple[str, ...] = ()) -> dict:
    """Calculate SPY trend and sector breadth on the latest shared date <= VIX date."""
    symbols = ("SPY", *sectors)
    mapped = {symbol: _price_rows((histories or {}).get(symbol, [])) for symbol in symbols}
    eligible = sorted(day for day in mapped.get("SPY", {}) if day <= str(as_of)[:10])
    if not eligible:
        return {"status": "unavailable", "reason": "No shared SPY/sector close date on or before the VIX observation."}
    day = eligible[-1]
    spy_rows = sorted((row for row in (histories or {}).get("SPY", [])
                       if str(row.get("trading_date", row.get("observation_date", "")))[:10] <= day),
                      key=lambda row: str(row.get("trading_date", row.get("observation_date", ""))))
    spy_values = []
    seen = set()
    for row in spy_rows:
        parsed = _price_rows([row])
        if parsed:
            session = next(iter(parsed))
            if session not in seen:
                seen.add(session)
                spy_values.append(parsed[session])
    close = mapped["SPY"][day]
    sma50 = sum(spy_values[-50:]) / 50 if len(spy_values) >= 50 else None
    sma200 = sum(spy_values[-200:]) / 200 if len(spy_values) >= 200 else None
    above50 = above200 = 0
    valid50 = valid200 = 0
    available_on_date = [symbol for symbol in sectors if day in mapped.get(symbol, {})]
    for symbol in available_on_date:
        series = sorted((row for row in (histories or {}).get(symbol, [])
                         if str(row.get("trading_date", row.get("observation_date", "")))[:10] <= day),
                        key=lambda row: str(row.get("trading_date", row.get("observation_date", ""))))
        values = []
        seen_dates = set()
        for row in series:
            parsed = _price_rows([row])
            if parsed:
                d = next(iter(parsed))
                if d not in seen_dates:
                    seen_dates.add(d)
                    values.append(parsed[d])
        if len(values) >= 50:
            above50 += values[-1] > sum(values[-50:]) / 50
            valid50 += 1
        if len(values) >= 200:
            above200 += values[-1] > sum(values[-200:]) / 200
            valid200 += 1
    spy_series = sorted((day_, value) for day_, value in mapped["SPY"].items() if day_ <= day)
    spy_return = (spy_series[-1][1] / spy_series[-6][1] - 1) * 100 if len(spy_series) >= 6 and _continuous([{"observation_date": d} for d, _ in spy_series[-6:]]) else None
    sector_relative = {}
    for symbol in available_on_date:
        sector_series = _price_rows((histories or {}).get(symbol, []))
        sector_days = sorted(d for d in sector_series if d <= day and d in mapped["SPY"])
        if len(sector_days) >= 6:
            dates = sector_days[-6:]
            segment = [{"observation_date": d} for d in dates]
            if _continuous(segment):
                sector_relative[symbol] = (sector_series[dates[-1]] / sector_series[dates[0]] -
                                           (mapped["SPY"][dates[-1]] / mapped["SPY"][dates[0]])) * 100
    return {"status": "available", "as_of": day, "spy_close": close,
            "spy_above_sma50": close > sma50 if sma50 is not None else None,
            "spy_above_sma200": close > sma200 if sma200 is not None else None,
            "spy_return_5d_percent": spy_return, "sector_above_sma50": above50,
            "sector_sma50_coverage": valid50, "sector_above_sma200": above200,
            "sector_sma200_coverage": valid200, "sector_count": len(sectors),
            "sector_data_on_as_of": len(available_on_date), "sector_relative_vs_spy_pp": sector_relative,
            "as_of_match": day == str(as_of)[:10]}


def build_vix_descriptive_signals(rows, histories=None, sectors=(), *, z_window: int = 60,
                                  variability_window: int = 20, reversal_window: int = 20) -> dict:
    clean = _clean(rows)
    if not clean:
        return {"status": "unavailable", "as_of": None}
    as_of = clean[-1]["observation_date"]
    return {"status": "available", "as_of": as_of,
            "z_score": vix_z_score(clean, window=z_window),
            "return_variability": vix_return_variability(clean, return_window=variability_window),
            "reversal_frequency": vix_reversal_frequency(clean, sessions=reversal_window),
            "equity_confirmation": equity_confirmation(histories or {}, as_of=as_of, sectors=sectors) if histories else {"status": "unavailable"},
            "momentum": "Requires a current matching VIX observation"}


def summarize_vix_statistical_signals(signals: dict, monitor: dict, cycle: dict | None = None) -> dict:
    """Concise descriptive interpretation of existing statistics; no probability claims.

    Momentum bands are descriptive: > +5% net over five sessions means increasing,
    < -5% means easing; otherwise the net direction is muted. A high variability
    percentile or frequent intraday reversals can make the state mixed.
    """
    if not signals or signals.get("status") != "available":
        return {"status": "unavailable", "headline": "Statistical context unavailable",
                "historical": "Insufficient validated VIX observations.",
                "implication": "No statistical interpretation is made.",
                "confirmation": "Wait for a complete dated VIX OHLC window and matched SPY observations."}
    z = signals.get("z_score") or {}
    variability = signals.get("return_variability") or {}
    reversal = signals.get("reversal_frequency") or {}
    equity = signals.get("equity_confirmation") or {}
    percentile = monitor.get("percentile_1y")
    z_value = z.get("value")
    if z_value is not None:
        window = z.get("window", 60)
        empirical = z.get("empirical_percentile")
        normal = z.get("normal_equivalent_percentile")
        rank_text = f"the empirical midrank is {empirical:.1f}th" if empirical is not None else "its empirical rank is unavailable"
        normal_text = f"the normal-equivalent rank is {normal:.1f}th" if normal is not None else "a normal-equivalent rank is unavailable"
        adjective = "unusually elevated" if z_value >= 1.5 else "unusually low" if z_value <= -1.5 else "within the usual range"
        historical = (f"VIX is {adjective} in its trailing {window}-session sample (z={z_value:+.2f}; "
                      f"{rank_text}; {normal_text}). The normal mapping assumes a Gaussian distribution; "
                      "the empirical rank reflects the observed, often skewed VIX sample.")
    elif z.get("status") == "zero_standard_deviation":
        empirical = z.get("empirical_percentile")
        historical = (f"The trailing {z.get('window', 60)}-session sample has zero variation, so its z-score and normal-equivalent rank are undefined; "
                      f"the empirical midrank is {empirical:.1f}th." if empirical is not None else
                      "The trailing sample has zero variation; standardized ranking is unavailable.")
    elif percentile is not None:
        historical = f"VIX ranks at the {percentile:.0f}th percentile of its one-year history; this is a historical rank, not a forecast."
    else:
        historical = "A reliable trailing VIX historical ranking is unavailable."
    variability_rank = variability.get("historical_percentile")
    turbulent = variability.get("unusually_turbulent") is True
    five_day = (monitor.get("change_5_sessions") or {}).get("percent")
    if five_day is None:
        pressure = "mixed"
    elif five_day > 5 and not turbulent:
        pressure = "increasing"
    elif five_day < -5:
        pressure = "easing"
    else:
        pressure = "mixed"
    if turbulent and pressure != "easing":
        pressure = "erratic / mixed"
    reversal_count = reversal.get("significant_reversals")
    reversal_eligible = reversal.get("eligible_spike_sessions")
    intraday = (f"{reversal_count} significant reversals among {reversal_eligible} qualifying spikes in the last {reversal.get('sessions')} sessions."
                if reversal.get("status") == "available" else "Intraday reversal frequency is unavailable because the OHLC window is incomplete.")
    if equity.get("status") == "available":
        breadth = equity.get("sector_above_sma50")
        breadth_n = equity.get("sector_sma50_coverage")
        spy_weak = equity.get("spy_above_sma50") is False
        if spy_weak and breadth_n and breadth is not None and breadth < breadth_n / 2:
            confirmation = (f"Equity data through {equity['as_of']} broadly confirm caution: SPY is below its 50-session average and fewer than half of covered sector ETFs are above theirs.")
        elif spy_weak or (equity.get("spy_above_sma50") is True and breadth_n and breadth is not None and breadth >= breadth_n / 2):
            confirmation = (f"Equity evidence through {equity['as_of']} is mixed or less broad than VIX alone: check SPY's 50/200-session trend and sector participation for confirmation.")
        else:
            confirmation = f"Matched equity confirmation is incomplete through {equity.get('as_of')}; use SPY trend and sector participation before interpreting VIX stress."
    else:
        confirmation = "Matched SPY trend and sector-breadth observations are unavailable."
    variability_text = (f"Recent 20-return variability ranks at the {variability_rank:.0f}th percentile of prior windows."
                        if variability_rank is not None else "Recent variability has insufficient prior windows for a historical rank.")
    if pressure == "increasing":
        headline = "Volatility pressure is increasing"
    elif pressure == "easing":
        headline = "Volatility pressure is easing"
    elif turbulent:
        headline = "Volatility is erratic; net pressure is mixed"
    else:
        headline = "Volatility direction is mixed"
    return {"status": "available", "as_of": signals.get("as_of"), "headline": headline,
            "historical": historical, "variability": variability_text, "intraday": intraday,
            "pressure": pressure, "implication": ("Recent VIX changes describe stress in options-implied volatility; they do not establish equity direction or a market bottom."),
            "confirmation": confirmation,
            "methodology": "Descriptive rule: 5-session net VIX change above +5% is increasing, below −5% easing; otherwise mixed. Variability is flagged unusually turbulent at a ≥90th percentile of prior 20-return rolling standard deviations. Neither is a probability or forecast."}

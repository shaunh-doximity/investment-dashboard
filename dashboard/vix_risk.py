"""Transparent recent VIX risk-monitor calculations; VIX is not a tradable asset."""

from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from zoneinfo import ZoneInfo
from dashboard.market_calendar import latest_completed_us_equity_session


def latest_completed_weekday(now: datetime | None = None) -> str:
    """Backward-compatible name; uses scheduled XNYS closes, holidays and DST."""
    return latest_completed_us_equity_session(now)


def _valid(row):
    try:
        close = float(row["close"])
        return isfinite(close) and close > 0
    except (KeyError, TypeError, ValueError):
        return False


def _change(rows, sessions):
    if len(rows) <= sessions:
        return None
    segment = rows[-sessions - 1:]
    try:
        dates = [date.fromisoformat(row["observation_date"]) for row in segment]
        closes = [float(row["close"]) for row in segment]
    except (ValueError, KeyError, TypeError):
        return None
    if any((right - left).days > 4 for left, right in zip(dates, dates[1:])):
        return None
    points = closes[-1] - closes[0]
    return {"points": points, "percent": (closes[-1] / closes[0] - 1) * 100,
            "reference_date": segment[0]["observation_date"], "reference_close": closes[0],
            "latest_date": segment[-1]["observation_date"], "latest_close": closes[-1],
            "sessions": sessions}


def vix_five_session_summary(rows: list[dict]) -> dict:
    """Summarize exactly five complete, consecutive-enough VIX OHLC sessions.

    No missing OHLC row is filled.  A calendar gap over four days makes the
    requested window incomplete (weekends and a single holiday are allowed).
    Large close-to-close rises are defined as >=6%; a significant reversal is
    a high-to-close retreat of at least max(3 VIX points, 15% of range), with
    the close in the lower 55% of that session's range.
    """
    clean = sorted((dict(row) for row in rows or [] if _valid(row)), key=lambda row: str(row.get("observation_date", "")))
    clean = list({str(row["observation_date"]): row for row in clean}.values())
    if len(clean) < 5:
        return {"status": "incomplete", "observations": len(clean)}
    window = clean[-5:]
    candles = []
    try:
        dates = [date.fromisoformat(str(row["observation_date"])) for row in window]
        for row in window:
            candle = {key: float(row[key]) for key in ("open", "high", "low", "close")}
            if (not all(isfinite(value) and value > 0 for value in candle.values())
                    or not candle["low"] <= min(candle["open"], candle["close"])
                    <= max(candle["open"], candle["close"]) <= candle["high"]):
                raise ValueError
            candles.append(candle)
    except (KeyError, TypeError, ValueError):
        return {"status": "incomplete_ohlc", "observations": len(window),
                "dates": [str(row.get("observation_date", "")) for row in window]}
    if any((right - left).days > 4 for left, right in zip(dates, dates[1:])):
        return {"status": "incomplete_dates", "observations": len(window),
                "dates": [str(row.get("observation_date", "")) for row in window]}

    highs = [row["high"] for row in candles]
    lows = [row["low"] for row in candles]
    low, high = min(lows), max(highs)
    reversals = 0
    for candle in candles:
        width = candle["high"] - candle["low"]
        if width > 0:
            close_location = (candle["close"] - candle["low"]) / width
            if candle["high"] - candle["close"] >= max(3.0, 0.15 * width) and close_location <= 0.55:
                reversals += 1

    large_increases = None
    if len(clean) >= 6:
        six = clean[-6:]
        try:
            closes = [float(row["close"]) for row in six]
            six_dates = [date.fromisoformat(str(row["observation_date"])) for row in six]
            if not any((right - left).days > 4 for left, right in zip(six_dates, six_dates[1:])):
                large_increases = sum((closes[index] / closes[index - 1] - 1) * 100 >= 6.0
                                      for index in range(1, len(closes)))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            large_increases = None
    peak_unwound = {"status": "incomplete_history"}
    if len(clean) >= 6:
        reference, current = clean[-6], clean[-1]
        try:
            six_dates = [date.fromisoformat(str(row["observation_date"])) for row in clean[-6:]]
            reference_close = float(reference["close"])
            peak_high = high
            latest_close = float(current["close"])
            if any((right - left).days > 4 for left, right in zip(six_dates, six_dates[1:])):
                peak_unwound = {"status": "incomplete_dates"}
            elif peak_high <= reference_close:
                peak_unwound = {"status": "no_upward_spike", "reference_date": reference["observation_date"],
                                "reference_close": reference_close, "peak_high": peak_high,
                                "peak_date": window[highs.index(peak_high)]["observation_date"],
                                "latest_close": latest_close, "latest_date": current["observation_date"]}
            else:
                surge = peak_high - reference_close
                unwind = (peak_high - latest_close) / surge * 100
                peak_unwound = {"status": "fully_reversed_below_reference" if latest_close < reference_close else
                                "fully_reversed_to_reference" if latest_close == reference_close else
                                "partially_reversed" if latest_close < peak_high else "peak_retained",
                                "reference_date": reference["observation_date"], "reference_close": reference_close,
                                "peak_high": peak_high, "peak_date": window[highs.index(peak_high)]["observation_date"],
                                "latest_close": latest_close, "latest_date": current["observation_date"],
                                "peak_surge_percent": (peak_high / reference_close - 1) * 100,
                                "peak_surge_unwound_percent": unwind,
                                "peak_surge_retained_percent": (latest_close - reference_close) / surge * 100}
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            peak_unwound = {"status": "incomplete_history"}
    from dashboard.vix_statistics import vix_reversal_frequency
    reversal = vix_reversal_frequency(clean, sessions=5)
    return {
        "status": "complete", "observations": 5,
        "start_date": window[0]["observation_date"], "end_date": window[-1]["observation_date"],
        "lowest_vix": low, "highest_vix": high,
        "range_expansion_percent": (high / low - 1) * 100 if low > 0 else None,
        "close_change": _change(clean, 5),
        "large_daily_increases": large_increases,
        "significant_reversals": reversals,
        "qualifying_upward_spikes": reversal.get("eligible_spike_sessions") if reversal.get("status") == "available" else None,
        "qualifying_spike_reversals": reversal.get("significant_reversals") if reversal.get("status") == "available" else None,
        "spike_qualification": {"minimum_high_vs_prior_close_percent": 10, "minimum_unwound_percent": 50},
        "peak_surge_unwound": peak_unwound,
    }


def vix_selected_window_expansion(rows: list[dict], *, sessions: int) -> dict:
    """Measure OHLC high/low expansion across the exact selected session window.

    A complete window requires every selected observation to contain valid
    OHLC and calendar gaps no longer than four days (weekends/one holiday).
    No partial window is reported as a full-period statistic.
    """
    if sessions not in (5, 10, 15, 20):
        raise ValueError("sessions must be 5, 10, 15, or 20")
    clean = sorted((dict(row) for row in rows or [] if _valid(row)),
                   key=lambda row: str(row.get("observation_date", "")))
    clean = list({str(row["observation_date"]): row for row in clean}.values())
    if len(clean) < sessions:
        return {"status": "insufficient_history", "sessions": sessions,
                "available_observations": len(clean)}
    window = clean[-sessions:]
    dates, candles = [], []
    try:
        for row in window:
            day = date.fromisoformat(str(row["observation_date"]))
            candle = {key: float(row[key]) for key in ("open", "high", "low", "close")}
            if (not all(isfinite(value) and value > 0 for value in candle.values())
                    or not candle["low"] <= min(candle["open"], candle["close"])
                    <= max(candle["open"], candle["close"]) <= candle["high"]):
                raise ValueError
            dates.append(day)
            candles.append(candle)
    except (KeyError, TypeError, ValueError):
        return {"status": "incomplete_ohlc", "sessions": sessions,
                "start_date": window[0].get("observation_date"),
                "end_date": window[-1].get("observation_date"),
                "available_observations": len(window)}
    if any((right - left).days > 4 for left, right in zip(dates, dates[1:])):
        return {"status": "incomplete_dates", "sessions": sessions,
                "start_date": window[0]["observation_date"],
                "end_date": window[-1]["observation_date"],
                "available_observations": len(window)}
    highest = max(candle["high"] for candle in candles)
    lowest = min(candle["low"] for candle in candles)
    high_index = next(index for index, candle in enumerate(candles) if candle["high"] == highest)
    low_index = next(index for index, candle in enumerate(candles) if candle["low"] == lowest)
    return {"status": "available", "sessions": sessions,
            "start_date": window[0]["observation_date"], "end_date": window[-1]["observation_date"],
            "highest_high": highest, "highest_high_date": window[high_index]["observation_date"],
            "lowest_low": lowest, "lowest_low_date": window[low_index]["observation_date"],
            "expansion_percent": (highest / lowest - 1) * 100,
            "observations": sessions}


def vix_candlestick_data(rows: list[dict], *, sessions: int = 5, show_average: bool = False) -> dict:
    """Prepare recent VIX candles without deriving OHLC from closes.

    The window is the last `sessions` observed dates. Rows missing any OHLC
    field are omitted from candle traces and reported in `missing_ohlc_dates`.
    The optional SMA is calculated only from observed closes.
    """
    if sessions not in (5, 10, 15, 20):
        raise ValueError("sessions must be 5, 10, 15, or 20")
    full_history = sorted((dict(row) for row in rows or [] if _valid(row)), key=lambda row: row["observation_date"])
    full_history = list({str(row["observation_date"]): row for row in full_history}.values())
    clean = full_history[-sessions:]
    candles = []
    missing = []
    for row in clean:
        try:
            values = {field: float(row[field]) for field in ("open", "high", "low", "close")}
            if not all(isfinite(value) and value > 0 for value in values.values()):
                raise ValueError
            if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
                raise ValueError
            candles.append({"date": str(row["observation_date"]), **values})
        except (KeyError, TypeError, ValueError):
            missing.append(str(row.get("observation_date", "unknown")))
    closes = [float(row["close"]) for row in clean]
    average = []
    if show_average:
        all_closes = [float(row["close"]) for row in full_history]
        selected_dates = {str(row["observation_date"]) for row in clean}
        for index, row in enumerate(full_history):
            if index >= 4 and str(row["observation_date"]) in selected_dates:
                average.append({"date": str(row["observation_date"]), "value": sum(all_closes[index-4:index+1]) / 5})
    return {"sessions": sessions, "dates": [str(row["observation_date"]) for row in clean],
            "candles": candles, "missing_ohlc_dates": missing, "five_session_average": average,
            "latest_close": closes[-1] if closes else None,
            "previous_close": closes[-2] if len(closes) > 1 else None}


def build_vix_risk_monitor(rows: list[dict], *, completed_session: str | None,
                           today: date | None = None) -> dict:
    """Return current-session metrics only when the close is fresh and session-matched."""
    clean = sorted((dict(row) for row in rows if _valid(row)), key=lambda row: row["observation_date"])
    clean = list({str(row["observation_date"]): row for row in clean}.values())
    current_day = today or datetime.now(ZoneInfo("America/New_York")).date()
    if not clean:
        return {"status": "unavailable", "history": []}
    latest = clean[-1]
    latest_date = date.fromisoformat(latest["observation_date"])
    age = (current_day - latest_date).days
    current_valid = bool(completed_session and latest["observation_date"] == completed_session and 0 <= age <= 4)
    history_1y = [float(row["close"]) for row in clean[-252:]]
    history_5y = [float(row["close"]) for row in clean[-1260:]]

    def percentile(values):
        if len(values) < len_required:
            return None
        current = values[-1]
        return sum(value <= current for value in values) / len(values) * 100

    len_required = 252
    percentile_1y = percentile(history_1y)
    len_required = 1260
    percentile_5y = percentile(history_5y)
    valid_ohlc = []
    for row in clean:
        try:
            opn, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if all(isfinite(value) and value > 0 for value in (opn, high, low, close)) and low <= min(opn, close) <= max(opn, close) <= high:
            valid_ohlc.append({**row, "open": opn, "high": high, "low": low, "close": close})

    current_ohlc = valid_ohlc[-1] if current_valid and valid_ohlc and valid_ohlc[-1]["observation_date"] == latest["observation_date"] else None
    def safe_percent(numerator, denominator):
        if denominator is None or denominator == 0:
            return None
        return (numerator / denominator - 1) * 100
    prior_close = None
    daily_points = daily_percent = None
    prior_date = None
    if current_valid and len(clean) >= 2:
        segment = clean[-2:]
        try:
            delta_days = (date.fromisoformat(segment[-1]["observation_date"]) - date.fromisoformat(segment[0]["observation_date"])).days
            prior_close = float(segment[0]["close"]) if delta_days <= 4 else None
        except (ValueError, TypeError, KeyError):
            prior_close = None
        if prior_close and prior_close > 0:
            prior_date = segment[0]["observation_date"]
            daily_points = float(latest["close"]) - prior_close
            daily_percent = daily_points / prior_close * 100

    peak_surge_percent = peak_unwound_percent = peak_retained_percent = None
    spike_status = "unavailable"
    if current_ohlc and prior_close and current_ohlc["high"] > prior_close:
        surge_span = current_ohlc["high"] - prior_close
        peak_surge_percent = (current_ohlc["high"] / prior_close - 1) * 100
        peak_unwound_percent = (current_ohlc["high"] - current_ohlc["close"]) / surge_span * 100
        peak_retained_percent = (current_ohlc["close"] - prior_close) / surge_span * 100
        if current_ohlc["close"] < prior_close:
            spike_status = "fully_reversed_below_prior"
        elif current_ohlc["close"] == prior_close:
            spike_status = "fully_reversed_to_prior"
        elif current_ohlc["close"] < current_ohlc["high"]:
            spike_status = "partially_reversed"
        else:
            spike_status = "spike_persisted"
    elif current_ohlc and prior_close:
        spike_status = "no_upward_spike"
    five_session = vix_five_session_summary(clean) if current_valid else {"status": "unavailable"}

    change_3 = _change(clean, 3) if current_valid else None
    change_5 = _change(clean, 5) if current_valid else None
    labels = []
    if current_valid:
        pctl = percentile_1y
        level = float(latest["close"])
        if pctl is not None and (pctl >= 95 or level >= 40):
            labels.append("Volatility extreme")
        elif pctl is not None and (pctl >= 80 or level >= 25):
            labels.append("Volatility elevated")
        elif pctl is not None and pctl <= 20 and level < 20:
            labels.append("Volatility subdued")
        else:
            labels.append("Volatility normal")
        if change_3 and change_3["percent"] >= 15:
            labels.append("Volatility accelerating")
        if change_3 and change_3["percent"] <= -15:
            labels.append("Volatility easing")
        if current_ohlc:
            intraday_range = current_ohlc["high"] - current_ohlc["low"]
            if intraday_range > 0:
                close_location = (current_ohlc["close"] - current_ohlc["low"]) / intraday_range
                high_to_close = current_ohlc["high"] - current_ohlc["close"]
                if high_to_close >= max(3.0, 0.15 * intraday_range) and close_location <= 0.55:
                    labels.append("Intraday spike partially reversed")
                elif close_location >= 0.75 and change_3 and change_3["points"] > 0:
                    if "Volatility accelerating" not in labels:
                        labels.append("Volatility accelerating")
    return {
        "status": "current" if current_valid else "stale_or_session_mismatch",
        "current_available": current_valid,
        "observation_date": latest["observation_date"],
        "source": latest.get("provider", "unknown"),
        "retrieved_at": latest.get("retrieved_at"),
        "age_calendar_days": age,
        "close": float(latest["close"]) if current_valid else None,
        "prior_close": prior_close,
        "prior_close_date": prior_date,
        "daily_change_points": daily_points,
        "daily_change_percent": daily_percent,
        "change_3_sessions": change_3,
        "change_5_sessions": change_5,
        "open": current_ohlc["open"] if current_ohlc else None,
        "high": current_ohlc["high"] if current_ohlc else None,
        "low": current_ohlc["low"] if current_ohlc else None,
        "high_to_close_points": current_ohlc["high"] - current_ohlc["close"] if current_ohlc else None,
        "peak_surge_percent": peak_surge_percent,
        "peak_unwound_percent": peak_unwound_percent,
        "peak_retained_percent": peak_retained_percent,
        "spike_status": spike_status,
        "open_to_high_percent": safe_percent(current_ohlc["high"], current_ohlc["open"]) if current_ohlc else None,
        "high_to_close_percent": safe_percent(current_ohlc["close"], current_ohlc["high"]) if current_ohlc else None,
        "open_to_close_percent": safe_percent(current_ohlc["close"], current_ohlc["open"]) if current_ohlc else None,
        "high_to_close_range_percent": ((current_ohlc["high"] - current_ohlc["close"]) / (current_ohlc["high"] - current_ohlc["low"]) * 100) if current_ohlc and current_ohlc["high"] > current_ohlc["low"] else None,
        "closing_location_percent": ((current_ohlc["close"] - current_ohlc["low"]) / (current_ohlc["high"] - current_ohlc["low"]) * 100) if current_ohlc and current_ohlc["high"] > current_ohlc["low"] else None,
        "ohlc_available": current_ohlc is not None,
        "percentile_1y": percentile_1y,
        "percentile_1y_observations": len(history_1y),
        "percentile_5y": percentile_5y,
        "percentile_5y_observations": len(history_5y),
        "five_session_ohlc": five_session,
        "classifications": labels,
        "history_5_closes": [{"date": row["observation_date"], "close": float(row["close"])} for row in clean[-6:]],
        "methodology": {
            "freshness": "Current requires date match to latest cached completed SPY session and age <=4 calendar days.",
            "percentiles": "Empirical rank of current close within last 252 or 1,260 available valid closes; unavailable until full window exists.",
            "accelerating": "3-session close change >= +15%; separately, a close in top 25% of same-day OHLC range with positive 3-session point change.",
            "easing": "3-session close change <= -15%.",
            "reversal": "High-close >= max(3 VIX points, 15% of daily range), and close location <=55% of range.",
            "peak_surge": "(session high / prior close - 1) × 100, only when session high exceeds prior close.",
            "peak_unwound": "(session high - close) / (session high - prior close) × 100. May exceed 100% when the close falls below prior close.",
            "peak_retained": "(close - prior close) / (session high - prior close) × 100; not clamped.",
            "five_session_range": "Highest daily high / lowest daily low - 1 over exactly five complete OHLC observations. No partial windows are presented as complete.",
            "level": "Extreme: 1Y percentile >=95 or VIX >=40; elevated: percentile >=80 or VIX >=25; subdued: percentile <=20 and VIX <20; otherwise normal. Descriptive conventions only.",
        },
        "history": clean,
    }

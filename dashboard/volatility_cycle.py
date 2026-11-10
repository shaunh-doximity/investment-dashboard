"""Rule-based VIX cycle descriptors; these are monitoring labels, not forecasts."""
from __future__ import annotations

from datetime import date
from math import isfinite


DEFAULT_REGIME_BOUNDS = {"subdued_max": 15.0, "normal_max": 20.0, "elevated_max": 30.0}


def absolute_vix_regime(level, *, subdued_max=15.0, normal_max=20.0, elevated_max=30.0):
    if level is None:
        return "Unavailable"
    level = float(level)
    if not all(isfinite(value) for value in (level, subdued_max, normal_max, elevated_max)):
        return "Unavailable"
    if not 0 < subdued_max < normal_max < elevated_max:
        raise ValueError("VIX boundaries must be positive and strictly increasing.")
    if level < subdued_max:
        return "Subdued"
    if level < normal_max:
        return "Normal"
    if level < elevated_max:
        return "Elevated"
    return "Extreme"


def daily_vix_momentum(change_percent, *, stress_threshold=6.0):
    if change_percent is None:
        return "Unavailable"
    if change_percent >= abs(stress_threshold):
        return "Stress increasing"
    if change_percent <= -abs(stress_threshold):
        return "Stress easing"
    return "Neutral daily move"


def _pct_change(first, last):
    return (last / first - 1) * 100 if first else None


def analyze_volatility_cycle(rows, *, window=20, high_vol_level=20.0):
    """Summarize 5–20 recent sessions and assign a state only on clear rules.

    Acceleration is the latest five-session percent return minus the preceding
    five-session return, in percentage points. A failed high means a session
    trades above the previous five-session intraday high but closes below the
    previous close. Intraday range is compared with the median of prior ranges.
    """
    if window not in (5, 10, 15, 20):
        raise ValueError("window must be 5, 10, 15, or 20 sessions")
    clean = []
    for raw in rows or []:
        try:
            day = date.fromisoformat(str(raw["observation_date"])[:10])
            close = float(raw["close"])
            if not isfinite(close) or close <= 0:
                continue
            row = {**raw, "observation_date": day.isoformat(), "close": close}
            for field in ("open", "high", "low"):
                if raw.get(field) is not None:
                    row[field] = float(raw[field])
            clean.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    clean = sorted({row["observation_date"]: row for row in clean}.values(), key=lambda row: row["observation_date"])
    # N return sessions require N+1 close observations; the returned count is
    # the number of observed returns available, capped at the requested window.
    sample = clean[-(window + 1):]
    if not sample:
        return {"status": "unavailable", "state": "Insufficient data", "observations": 0}
    closes = [row["close"] for row in sample]
    dates = [date.fromisoformat(row["observation_date"]) for row in sample]
    def contiguous(start, end):
        return all((right - left).days <= 4 for left, right in zip(dates[start:end-1], dates[start+1:end]))
    five_return = _pct_change(closes[-6], closes[-1]) if len(closes) >= 6 and contiguous(len(closes)-6, len(closes)) else None
    three_return = _pct_change(closes[-4], closes[-1]) if len(closes) >= 4 and contiguous(len(closes)-4, len(closes)) else None
    prior_five = _pct_change(closes[-11], closes[-6]) if len(closes) >= 11 and contiguous(len(closes)-11, len(closes)-5) else None
    acceleration = five_return - prior_five if five_return is not None and prior_five is not None else None
    full_ohlc = all(row.get("high") is not None and row.get("low") is not None for row in sample)
    highs = [row["high"] for row in sample] if full_ohlc else closes
    lows = [row["low"] for row in sample] if full_ohlc else closes
    ranges = [row["high"] - row["low"] for row in sample] if full_ohlc else []
    last = sample[-1]
    last_range = last.get("high") - last.get("low") if last.get("high") is not None and last.get("low") is not None else None
    prior_ranges = ranges[:-1]
    range_multiple = last_range / (sorted(prior_ranges)[len(prior_ranges)//2]) if last_range is not None and prior_ranges and sorted(prior_ranges)[len(prior_ranges)//2] > 0 else None
    large_up = large_down = reversals = new_highs = failed_highs = 0
    for index, row in enumerate(sample):
        if index:
            gap_days = (date.fromisoformat(row["observation_date"]) - date.fromisoformat(sample[index-1]["observation_date"])).days
            change = _pct_change(sample[index-1]["close"], row["close"]) if gap_days <= 4 else None
            large_up += int(change is not None and change >= 6)
            large_down += int(change is not None and change <= -6)
        if row.get("high") is not None and index >= 5:
            previous = sample[index-5:index]
            prior_highs = [item.get("high", item["close"]) for item in previous]
            if prior_highs and row["high"] > max(prior_highs):
                new_highs += 1
                if row["close"] < previous[-1]["close"]:
                    failed_highs += 1
        if row.get("high") is not None and row.get("low") is not None and row["high"] > row["low"]:
            retrace = (row["high"] - row["close"]) / (row["high"] - row["low"])
            if row["close"] <= row["high"] and retrace >= .5:
                reversals += 1
    consecutive_high = 0
    for row in reversed(sample):
        if row["close"] >= high_vol_level:
            consecutive_high += 1
        else:
            break
    momentum = "insufficient history"
    if five_return is not None and three_return is not None:
        momentum = "persisting" if five_return > 0 and three_return >= five_return * .4 else "weakening" if five_return > 0 else "easing" if five_return < 0 else "flat"
    level = last["close"]
    state = "Mixed signals"
    if len(sample) >= 6 and level >= 30 and ((five_return is not None and five_return > 0) or consecutive_high >= 2):
        state = "Panic/disorder"
    elif five_return is not None and five_return >= 15 and (acceleration is None or acceleration > 0 or new_highs > 0):
        state = "Stress building"
    elif five_return is not None and five_return >= 10 and (reversals > 0 or failed_highs > 0) and momentum == "weakening":
        state = "Turbulent stabilization"
    elif five_return is not None and five_return <= -10 and level < 30 and consecutive_high == 0:
        state = "Normalizing"
    elif level < 15 and five_return is not None and abs(five_return) < 10:
        state = "Calm"
    latest_high_close = last.get("high") - last["close"] if last.get("high") is not None else None
    latest_range = last.get("high") - last.get("low") if last.get("high") is not None and last.get("low") is not None else None
    return {
        "status": "available" if len(sample) >= 6 else "limited",
        "state": state, "observations": max(0, len(sample) - 1), "window": window,
        "as_of": last["observation_date"], "level": level,
        "return_5_sessions_pct": five_return, "return_3_sessions_pct": three_return,
        "acceleration_pp": acceleration, "five_session_high": max(highs) if highs else None,
        "five_session_low": min(lows) if lows else None,
        "five_session_extrema_basis": "intraday OHLC" if full_ohlc else "daily closes (OHLC unavailable/incomplete)",
        "new_highs": new_highs,
        "failed_highs": failed_highs, "daily_high_close_reversals": reversals,
        "latest_high_to_close_points": latest_high_close,
        "latest_high_to_close_range_pct": latest_high_close / latest_range * 100 if latest_high_close is not None and latest_range and latest_range > 0 else None,
        "latest_intraday_range": latest_range,
        "latest_range_vs_prior_median": range_multiple, "large_up_moves": large_up,
        "large_down_moves": large_down, "consecutive_high_vol_sessions": consecutive_high,
        "upward_momentum": momentum,
        "methodology": "Calm: close <15 and abs 5-session return <10%; stress building: 5-session return >=15% and acceleration/new-high evidence; panic: close >=30 with persistence; turbulent stabilization: +10% 5-session move with weakening momentum and failed-high/reversal evidence; normalizing: <=-10% and below 30. Otherwise mixed. These analyst rules are descriptive, not validated forecasts.",
    }

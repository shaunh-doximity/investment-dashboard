"""Deterministic cached-price comparison and basket calculations."""

from __future__ import annotations

from math import isfinite

from dashboard.cross_market import price_map
from dashboard.market_history import BROAD_MARKET_SYMBOLS, SECTOR_SYMBOLS


COMPARISON_UNIVERSE = tuple(dict.fromkeys((*BROAD_MARKET_SYMBOLS, *SECTOR_SYMBOLS.keys())))
COMPARISON_WINDOWS = (5, 20, 60, 120, 252)
BROAD_EQUITY_INDEX_SYMBOLS = ("SPY", "RSP", "QQQ", "DIA", "IWM")
BROAD_EQUITY_WINDOWS = (5, 20, 60, 120)
DEFAULT_PRESETS = (("XLY", "XLP"), ("XLY", "SPY"), ("RSP", "SPY"), ("SMH", "SPY"), ("SMH", "QQQ"))
STANDARD_COMPARISONS = tuple(dict.fromkeys(
    (*((symbol, "SPY") for symbol in SECTOR_SYMBOLS),
     ("RSP", "SPY"), ("IWM", "SPY"), ("SMH", "SPY"), ("SMH", "QQQ"), ("XLY", "XLP"))
))
DEFAULT_BASKETS = {
    "cyclical": {"XLY": 1, "XLI": 1, "XLB": 1, "XLE": 1, "XLF": 1, "XLK": 1, "XLC": 1, "XLRE": 1},
    "defensive": {"XLV": 1, "XLP": 1, "XLU": 1},
}


def compare_etfs(histories, first: str, second: str, window: int, *, mode="excess") -> dict:
    """Compare two ETFs on N+1 exact common adjusted closes; no forward fills."""
    first, second = first.strip().upper(), second.strip().upper()
    if first not in COMPARISON_UNIVERSE or second not in COMPARISON_UNIVERSE:
        raise ValueError("Select ETFs from the supported comparison universe.")
    if first == second:
        raise ValueError("Choose two different ETFs.")
    if window not in COMPARISON_WINDOWS:
        raise ValueError("Unsupported comparison window.")
    if mode not in {"excess", "relative_ratio", "two_line"}:
        raise ValueError("Unsupported comparison mode.")
    first_prices, second_prices = price_map(histories.get(first, [])), price_map(histories.get(second, []))
    dates = sorted(set(first_prices) & set(second_prices))
    if len(dates) < window + 1:
        return {"status": "insufficient_history", "required_observations": window + 1,
                "available_observations": len(dates), "symbols": (first, second), "window": window}
    dates = dates[-(window + 1):]
    p0a, pna = first_prices[dates[0]], first_prices[dates[-1]]
    p0b, pnb = second_prices[dates[0]], second_prices[dates[-1]]
    ret_a, ret_b = pna / p0a - 1, pnb / p0b - 1
    a_norm = [first_prices[day] / p0a * 100 for day in dates]
    b_norm = [second_prices[day] / p0b * 100 for day in dates]
    excess_series = [(first_prices[day] / p0a - second_prices[day] / p0b) * 100 for day in dates]
    ratio_norm = [(first_prices[day] / second_prices[day]) / (p0a / p0b) * 100 for day in dates]
    return {"status": "available", "symbols": (first, second), "window": window,
            "dates": dates, "as_of": dates[-1], "return_first": ret_a, "return_second": ret_b,
            "excess_pp": (ret_a - ret_b) * 100, "normalized_first": a_norm,
            "normalized_second": b_norm, "relative_ratio_index": ratio_norm,
            "cumulative_excess_pp": excess_series,
            "mode": mode, "price_basis": "Tiingo split-and-dividend-adjusted close"}


def broad_index_comparison_library(histories, windows=BROAD_EQUITY_WINDOWS) -> dict[str, dict]:
    """Return matched-date broad-index cumulative and SPY-excess returns.

    Each index is aligned independently to SPY; the benchmark remains in the
    returned matrix as a zero-excess reference. Momentum is the difference
    between current and immediately preceding equal-length excess-return windows.
    """
    output = {}
    for symbol in BROAD_EQUITY_INDEX_SYMBOLS:
        for window in windows:
            comparison_id = f"{symbol}_SPY_{int(window)}D"
            if symbol == "SPY":
                dates = sorted(price_map(histories.get("SPY", [])))
                if len(dates) < int(window) + 1:
                    output[comparison_id] = {"comparison_id": comparison_id, "status": "insufficient_history",
                        "symbol": symbol, "benchmark": "SPY", "window": int(window),
                        "observations": len(dates), "required_observations": int(window) + 1}
                    continue
                dates = dates[-(int(window) + 1):]
                spy_prices = price_map(histories.get("SPY", []))
                first_return = second_return = (spy_prices[dates[-1]] / spy_prices[dates[0]] - 1) * 100
                excess = 0.0
                previous_excess = 0.0 if len(price_map(histories.get("SPY", []))) >= 2 * int(window) + 1 else None
            else:
                result = compare_etfs(histories, symbol, "SPY", int(window))
                if result.get("status") != "available":
                    output[comparison_id] = {**result, "comparison_id": comparison_id}
                    continue
                first_return = result["return_first"] * 100
                second_return = result["return_second"] * 100
                excess = result["excess_pp"]
                dates = result["dates"]
                previous_excess = compare_etfs_for_prior_window(histories, symbol, "SPY", int(window))
            momentum = excess - previous_excess if previous_excess is not None else None
            output[comparison_id] = {
                "comparison_id": comparison_id, "status": "available", "symbol": symbol,
                "benchmark": "SPY", "window": int(window), "start_date": dates[0],
                "as_of": dates[-1], "cumulative_return_pct": first_return,
                "benchmark_return_pct": second_return, "excess_return_pp": excess,
                "previous_window_excess_pp": previous_excess,
                "momentum_change_pp": momentum,
                "direction": "outperforming" if excess > .01 else "underperforming" if excess < -.01 else "in line",
                "recent_change": "improving" if momentum is not None and momentum > .05 else
                    "deteriorating" if momentum is not None and momentum < -.05 else
                    "steady" if momentum is not None else "insufficient_history",
                "observations": len(dates), "required_observations": int(window) + 1,
                "price_basis": "Tiingo split-and-dividend-adjusted close",
            }
    return output


def broad_index_chart_data(histories, window: int, mode: str, visible_symbols=None) -> dict:
    """Chart-ready cached data for broad indexes on matched adjusted-close dates."""
    if window not in BROAD_EQUITY_WINDOWS:
        raise ValueError("Unsupported broad-index window.")
    if mode not in {"Normalized cumulative performance", "Relative cumulative performance vs SPY"}:
        raise ValueError("Unsupported broad-index chart mode.")
    requested_visible = BROAD_EQUITY_INDEX_SYMBOLS if visible_symbols is None else visible_symbols
    visible_set = set(requested_visible)
    visible = tuple(symbol for symbol in BROAD_EQUITY_INDEX_SYMBOLS if symbol in visible_set)
    if not visible:
        return {"status": "no_visible_indexes", "series": {}, "window": window, "mode": mode}
    if mode == "Normalized cumulative performance":
        from dashboard.cross_market import normalized_performance
        result = normalized_performance(histories, visible, window)
        if result.get("status") != "available":
            return {**result, "window": window, "mode": mode}
        return {"status": "available", "window": window, "mode": mode,
                "dates": result["dates"], "as_of": result["latest_date"],
                "series": {symbol: [value - 100.0 for value in values]
                           for symbol, values in result["series"].items()},
                "start_date": result["dates"][0], "symbols": list(visible),
                "price_basis": "Tiingo split-and-dividend-adjusted close"}
    series, start_dates, as_of_dates = {}, [], []
    for symbol in visible:
        if symbol == "SPY":
            # The SPY benchmark is zero excess by definition and need not be
            # visible for its data to remain in every other comparison.
            dates = sorted(price_map(histories.get("SPY", [])))
            if len(dates) < window + 1:
                continue
            dates = dates[-(window + 1):]
            series[symbol] = (dates, [0.0] * len(dates))
        else:
            result = compare_etfs(histories, symbol, "SPY", window)
            if result.get("status") != "available":
                continue
            series[symbol] = (result["dates"], result["cumulative_excess_pp"])
        start_dates.append(series[symbol][0][0])
        as_of_dates.append(series[symbol][0][-1])
    if not series:
        return {"status": "insufficient_history", "window": window, "mode": mode,
                "symbols": list(visible), "required_observations": window + 1}
    return {"status": "available", "window": window, "mode": mode,
            "series": series, "start_date": min(start_dates), "as_of": max(as_of_dates),
            "symbols": list(visible), "benchmark": "SPY", "price_basis": "Tiingo split-and-dividend-adjusted close"}


def compare_etfs_for_prior_window(histories, first: str, second: str, window: int) -> float | None:
    """Prior equal-length relative return, returned in percentage points."""
    a, b = price_map(histories.get(first, [])), price_map(histories.get(second, []))
    dates = sorted(set(a) & set(b))
    if len(dates) < 2 * window + 1:
        return None
    prior = dates[-(2 * window + 1):-window]
    return ((a[prior[-1]] / a[prior[0]] - 1) - (b[prior[-1]] / b[prior[0]] - 1)) * 100


def standard_comparison_library(histories, windows=COMPARISON_WINDOWS) -> dict[str, dict]:
    """Compact point-in-time comparisons for standard pairs and horizons.

    Momentum compares the latest window's excess return with the immediately
    preceding equal-length window on exact common dates. No dates are filled.
    """
    output = {}
    for first, second in STANDARD_COMPARISONS:
        first_prices, second_prices = price_map(histories.get(first, [])), price_map(histories.get(second, []))
        common_dates = sorted(set(first_prices) & set(second_prices))
        for window in windows:
            comparison_id = f"{first}_{second}_{int(window)}D"
            if len(common_dates) < int(window) + 1:
                output[comparison_id] = {"status": "insufficient_history", "symbols": [first, second],
                                         "window": int(window), "observations": len(common_dates),
                                         "required_observations": int(window) + 1}
                continue
            current_dates = common_dates[-(int(window) + 1):]
            p_first, p_second = first_prices[current_dates[0]], second_prices[current_dates[0]]
            ret_first = first_prices[current_dates[-1]] / p_first - 1
            ret_second = second_prices[current_dates[-1]] / p_second - 1
            excess = (ret_first - ret_second) * 100
            previous_excess = None
            if len(common_dates) >= 2 * int(window) + 1:
                prior_dates = common_dates[-(2 * int(window) + 1):-int(window)]
                prior_first, prior_second = first_prices[prior_dates[0]], second_prices[prior_dates[0]]
                previous_excess = ((first_prices[prior_dates[-1]] / prior_first - 1) -
                                   (second_prices[prior_dates[-1]] / prior_second - 1)) * 100
            momentum = excess - previous_excess if previous_excess is not None else None
            trend = "improving" if momentum is not None and momentum > .05 else (
                "deteriorating" if momentum is not None and momentum < -.05 else
                "steady" if momentum is not None else "insufficient_history")
            output[comparison_id] = {
                "status": "available", "symbols": [first, second], "window": int(window),
                "start_date": current_dates[0], "as_of": current_dates[-1],
                "first_return_pct": ret_first * 100, "second_return_pct": ret_second * 100,
                "excess_pp": excess, "previous_window_excess_pp": previous_excess,
                "relative_momentum_change_pp": momentum, "trend": trend,
                "price_basis": "Tiingo split-and-dividend-adjusted close",
                "method": "matched-date cumulative returns; momentum is current minus prior equal-length excess return",
            }
    return output


def normalize_basket_config(config=None) -> dict[str, dict[str, float]]:
    """Validate two separately weighted sector baskets; normalize weights to 1."""
    config = config or DEFAULT_BASKETS
    output = {}
    for name in ("cyclical", "defensive"):
        raw = config.get(name, {})
        weights = {}
        for symbol, raw_weight in raw.items():
            symbol = str(symbol).strip().upper()
            if symbol not in SECTOR_SYMBOLS:
                raise ValueError(f"{symbol} is not a standard sector ETF.")
            weight = float(raw_weight)
            if not isfinite(weight) or weight <= 0:
                raise ValueError("Basket weights must be positive finite numbers.")
            weights[symbol] = weight
        if not weights:
            raise ValueError(f"The {name} basket needs at least one sector ETF.")
        total = sum(weights.values())
        output[name] = {symbol: weight / total for symbol, weight in weights.items()}
    return output


def basket_performance(histories, config=None, periods=20) -> dict:
    config = normalize_basket_config(config)
    symbols = tuple(dict.fromkeys(symbol for basket in config.values() for symbol in basket))
    maps = {symbol: price_map(histories.get(symbol, [])) for symbol in symbols}
    common_dates = sorted(set.intersection(*(set(values) for values in maps.values()))) if maps else []
    if len(common_dates) < periods + 1:
        return {"status": "insufficient_history", "required_observations": periods + 1,
                "available_observations": len(common_dates)}
    dates = common_dates[-(periods + 1):]
    series = {}
    returns = {}
    for name, weights in config.items():
        values = [sum(weight * (maps[symbol][day] / maps[symbol][dates[0]] - 1) for symbol, weight in weights.items()) * 100 for day in dates]
        series[name] = values
        returns[name] = values[-1]
    return {"status": "available", "dates": dates, "as_of": dates[-1], "series": series,
            "returns_pct": returns, "defensive_minus_cyclical_pp": returns["defensive"] - returns["cyclical"],
            "weights": config}

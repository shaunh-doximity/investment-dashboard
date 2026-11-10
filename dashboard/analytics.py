"""Small, dependency-free analytics over one consistent adjusted daily-price series.

Returns are close-to-close. Volatility uses 20 recent daily returns by default,
252 trading days per year, and sample standard deviation. RSI uses Wilder smoothing.
Drawdown is based on adjusted daily closes (not intraday lows). Callers must pass
one provider and adjustment basis; the persistence layer stores those separately.
"""

from __future__ import annotations

from datetime import date
from math import isfinite, sqrt


def _ordered(bars: list[dict]) -> list[dict]:
    return sorted(bars, key=lambda row: row["trading_date"])


def _close(row: dict) -> float | None:
    try:
        value = float(row["close"])
        return value if isfinite(value) and value > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def daily_returns(bars: list[dict]) -> list[dict]:
    """Return date/value pairs; gaps longer than four calendar days are omitted."""
    ordered = _ordered(bars)
    output = []
    for previous, current in zip(ordered, ordered[1:]):
        old, new = _close(previous), _close(current)
        if old is None or new is None:
            continue
        try:
            gap = (date.fromisoformat(current["trading_date"]) - date.fromisoformat(previous["trading_date"])).days
        except (KeyError, TypeError, ValueError):
            continue
        if gap <= 0 or gap > 4:
            continue
        output.append({"trading_date": current["trading_date"], "return": new / old - 1.0})
    return output


def cumulative_returns(bars: list[dict]) -> list[dict]:
    """Cumulative close-to-close return from the first available valid close."""
    ordered = _ordered(bars)
    usable = [(row["trading_date"], _close(row)) for row in ordered]
    usable = [(day, close) for day, close in usable if close is not None]
    if len(usable) < 2:
        return []
    base = usable[0][1]
    return [{"trading_date": day, "return": close / base - 1.0} for day, close in usable]


def momentum(bars: list[dict], periods: int) -> float | None:
    """N-observation close-to-close return, requiring N+1 contiguous closes."""
    if periods <= 0:
        raise ValueError("periods must be positive")
    ordered = _ordered(bars)
    if len(ordered) < periods + 1:
        return None
    window = ordered[-(periods + 1):]
    if not _contiguous(window):
        return None
    first, last = _close(window[0]), _close(window[-1])
    return last / first - 1.0 if first is not None and last is not None else None


def moving_average(bars: list[dict], periods: int) -> float | None:
    if periods <= 0:
        raise ValueError("periods must be positive")
    ordered = _ordered(bars)
    if len(ordered) < periods:
        return None
    window = ordered[-periods:]
    values = [_close(row) for row in window]
    if not _contiguous(window) or any(value is None for value in values):
        return None
    return sum(values) / periods


def sma20(bars: list[dict]) -> float | None:
    return moving_average(bars, 20)


def sma50(bars: list[dict]) -> float | None:
    return moving_average(bars, 50)


def sma200(bars: list[dict]) -> float | None:
    return moving_average(bars, 200)


def rsi14(bars: list[dict]) -> float | None:
    ordered = _ordered(bars)
    # Use the most recent uninterrupted segment, then seed Wilder's smoothing
    # with 14 changes and carry it forward through later available observations.
    segment_start = 0
    for index, (before, after) in enumerate(zip(ordered, ordered[1:]), 1):
        gap = (date.fromisoformat(after["trading_date"]) - date.fromisoformat(before["trading_date"])).days
        if gap <= 0 or gap > 4:
            segment_start = index
    segment = ordered[segment_start:]
    if len(segment) < 15:
        return None
    closes = [_close(row) for row in segment]
    if any(value is None for value in closes):
        return None
    changes = [new - old for old, new in zip(closes, closes[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain, average_loss = sum(gains[:14]) / 14, sum(losses[:14]) / 14
    for gain, loss in zip(gains[14:], losses[14:]):
        average_gain = (average_gain * 13 + gain) / 14
        average_loss = (average_loss * 13 + loss) / 14
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


def annualized_realized_volatility(bars: list[dict], periods: int = 20) -> float | None:
    if periods <= 1:
        raise ValueError("periods must be greater than one")
    returns = daily_returns(bars)[-periods:]
    if len(returns) < periods or not _contiguous_returns(returns):
        return None
    values = [item["return"] for item in returns]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return sqrt(variance * 252)


def maximum_drawdown(bars: list[dict]) -> float | None:
    """Largest peak-to-trough percentage drawdown using daily closes."""
    ordered = _ordered(bars)
    values = [_close(row) for row in ordered]
    if len(values) < 2 or any(value is None for value in values) or not _contiguous(ordered):
        return None
    peak = values[0]
    worst = 0.0
    for value in values[1:]:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def beta_and_correlation(asset_bars: list[dict], benchmark_bars: list[dict]) -> dict:
    asset = {item["trading_date"]: item["return"] for item in daily_returns(asset_bars)}
    benchmark = {item["trading_date"]: item["return"] for item in daily_returns(benchmark_bars)}
    dates = sorted(asset.keys() & benchmark.keys())
    if len(dates) < 2:
        return {"beta": None, "correlation": None, "observations": len(dates)}
    x = [benchmark[day] for day in dates]
    y = [asset[day] for day in dates]
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    var_x = sum((value - mean_x) ** 2 for value in x)
    var_y = sum((value - mean_y) ** 2 for value in y)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    beta = covariance / var_x if var_x else None
    correlation = covariance / sqrt(var_x * var_y) if var_x and var_y else None
    return {"beta": beta, "correlation": correlation, "observations": len(dates)}


def relative_performance(
    histories: dict[str, list[dict]],
    symbols: tuple[str, ...] = ("SPY", "RSP", "QQQ", "IWM", "SMH"),
    benchmark: str = "SPY",
    periods: tuple[int, ...] = (5, 20),
) -> list[dict]:
    output = []
    for symbol in symbols:
        bars = histories.get(symbol, [])
        row = {"symbol": symbol}
        for period in periods:
            value = momentum(bars, period)
            reference = momentum(histories.get(benchmark, []), period)
            row[f"return_{period}d"] = value
            row[f"excess_{period}d"] = value - reference if value is not None and reference is not None else None
        output.append(row)
    return output


def sector_performance_rankings(
    histories: dict[str, list[dict]], sectors: dict[str, str], periods: tuple[int, ...] = (5, 20)
) -> dict[int, list[dict]]:
    results = {}
    for period in periods:
        rows = []
        for symbol, name in sectors.items():
            value = momentum(histories.get(symbol, []), period)
            rows.append({"symbol": symbol, "name": name, "return": value})
        results[period] = sorted(rows, key=lambda item: (item["return"] is not None, item["return"] or 0), reverse=True)
    return results


def _contiguous(rows: list[dict]) -> bool:
    for before, after in zip(rows, rows[1:]):
        try:
            gap = (date.fromisoformat(after["trading_date"]) - date.fromisoformat(before["trading_date"])).days
        except (KeyError, TypeError, ValueError):
            return False
        if gap <= 0 or gap > 4:
            return False
    return True


def _contiguous_returns(rows: list[dict]) -> bool:
    for before, after in zip(rows, rows[1:]):
        gap = (date.fromisoformat(after["trading_date"]) - date.fromisoformat(before["trading_date"])).days
        if gap > 4:
            return False
    return True

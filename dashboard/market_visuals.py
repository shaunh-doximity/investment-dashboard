"""Pure transformations for Market Home charts; never requests market data."""

from __future__ import annotations

from datetime import date

from dashboard.analytics import momentum, sector_performance_rankings


DEFENSIVE_SECTORS = ("XLV", "XLP", "XLU")
CYCLICAL_SECTORS = ("XLY", "XLI", "XLB", "XLE", "XLF", "XLK", "XLC", "XLRE")


def sector_return_rows(histories: dict[str, list[dict]], sectors: dict[str, str], window: int) -> list[dict]:
    """Return sorted sector rows using only the supplied, consistently adjusted cache."""
    if window not in (5, 20, 60):
        raise ValueError("window must be 5, 20, or 60 trading observations")
    ranked = sector_performance_rankings(histories, sectors, periods=(window,))[window]
    return [
        {**row, "name": sectors.get(row["symbol"], row["symbol"]),
         "as_of": max((bar.get("trading_date") for bar in histories.get(row["symbol"], [])), default=None)}
        for row in ranked if row.get("return") is not None
    ]


def sector_rotation_points(
    histories: dict[str, list[dict]], sectors: dict[str, str], benchmark: str = "SPY",
    strength_window: int = 20, momentum_window: int = 5,
) -> list[dict]:
    """Return relative-strength and relative-momentum points (not a standard RRG).

    Dates are intersected before calculation. Relative strength is the percent change in
    the sector/benchmark adjusted-close ratio over ``strength_window`` observations.
    Relative momentum is the change in that rolling relative-strength value over
    ``momentum_window`` observations. Every input should share provider and adjustment
    basis. The chart is not a standard RRG.
    """
    benchmark_rows = {row["trading_date"]: row.get("close") for row in histories.get(benchmark, [])}
    output = []
    for symbol, name in sectors.items():
        sector_rows = {row["trading_date"]: row.get("close") for row in histories.get(symbol, [])}
        dates = sorted(set(benchmark_rows) & set(sector_rows))
        ratios = []
        for day in dates:
            try:
                sector_close, benchmark_close = float(sector_rows[day]), float(benchmark_rows[day])
                if sector_close > 0 and benchmark_close > 0:
                    if ratios and (date.fromisoformat(day) - date.fromisoformat(ratios[-1][0])).days > 4:
                        ratios = []
                    ratios.append((day, sector_close / benchmark_close))
            except (TypeError, ValueError):
                continue
        required = strength_window + momentum_window + 1
        if len(ratios) < required:
            continue
        latest_index = len(ratios) - 1
        x = ratios[-1][1] / ratios[-1 - strength_window][1] - 1
        prior_strength = ratios[-1 - momentum_window][1] / ratios[-1 - momentum_window - strength_window][1] - 1
        y = x - prior_strength
        quadrant = rotation_quadrant(x, y)
        output.append({"symbol": symbol, "name": name, "trading_date": ratios[-1][0],
                       "relative_strength": x, "relative_momentum": y,
                       "quadrant": quadrant,
                       "trail": _rotation_trail(ratios, latest_index, strength_window, momentum_window)})
    return output


def _rotation_trail(ratios, latest_index, strength_window, momentum_window):
    points = []
    first = max(strength_window + momentum_window, latest_index - 20)
    for index in range(first, latest_index + 1):
        current_strength = ratios[index][1] / ratios[index - strength_window][1] - 1
        prior_strength = ratios[index - momentum_window][1] / ratios[index - momentum_window - strength_window][1] - 1
        points.append({
            "trading_date": ratios[index][0],
            "relative_strength": current_strength,
            "relative_momentum": current_strength - prior_strength,
        })
    return points


def breadth_rows(histories: dict[str, list[dict]], symbols: tuple[str, ...], benchmark: str = "SPY") -> list[dict]:
    """Compact 5D/20D leadership values and excess returns vs benchmark."""
    spy5, spy20 = momentum(histories.get(benchmark, []), 5), momentum(histories.get(benchmark, []), 20)
    rows = []
    for symbol in symbols:
        bars = histories.get(symbol, [])
        r5, r20 = momentum(bars, 5), momentum(bars, 20)
        rows.append({"symbol": symbol, "return_5d": r5, "excess_5d": r5 - spy5 if r5 is not None and spy5 is not None else None,
                     "return_20d": r20, "excess_20d": r20 - spy20 if r20 is not None and spy20 is not None else None,
                     "as_of": bars[-1]["trading_date"] if bars else None})
    return rows


def signed_return_color(value: float | None) -> str:
    """Theme-compatible Streamlit color name for absolute return values."""
    if value is None:
        return "gray"
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "gray"


def relative_strength_label(row: dict, benchmark: str = "SPY") -> str:
    """Format a return difference in percentage points, with an explicit benchmark label."""
    if row.get("symbol") == benchmark:
        return "Benchmark"
    excess = row.get("excess_5d")
    return f"{excess * 100:+.2f} pp vs {benchmark}" if excess is not None else "Unavailable vs benchmark"


def breadth_proxy_summary(
    rows: list[dict], window: int = 5, today: date | None = None, stale_after_days: int = 4,
) -> dict:
    """Describe RSP relative to SPY as an equal-weight participation proxy.

    This is an ETF return comparison, not constituent-level breadth or fund-flow data.
    The difference is RSP's selected-period return minus SPY's, in percentage points.
    """
    if window not in (5, 20, 60):
        raise ValueError("window must be 5, 20, or 60 trading observations")
    rsp = next((row for row in rows if row.get("symbol") == "RSP"), None)
    spy = next((row for row in rows if row.get("symbol") == "SPY"), None)
    field = f"return_{window}d"
    unavailable = {"status": "unavailable", "window": window, "difference_pp": None,
                   "as_of": None, "interpretation": "Unavailable: aligned RSP and SPY returns are not available."}
    if not rsp or not spy or rsp.get(field) is None or spy.get(field) is None:
        return unavailable
    rsp_date, spy_date = rsp.get("as_of"), spy.get("as_of")
    if not rsp_date or rsp_date != spy_date:
        return unavailable
    try:
        observed = date.fromisoformat(rsp_date)
        age = ((today or date.today()) - observed).days
    except (TypeError, ValueError):
        return unavailable
    difference = (rsp[field] - spy[field]) * 100
    rsp_return, spy_return = rsp.get(field), spy.get(field)
    if age > stale_after_days:
        interpretation = f"Stale cache through {rsp_date}: RSP relative return {difference:+.2f} pp over {window}D. Refresh local history to update."
        status = "stale"
    elif rsp_return < 0 and spy_return < 0 and difference > 0:
        interpretation = "Relative resilience during a selloff; RSP and SPY both declined, with RSP falling less. This is not positive absolute breadth."
        status = "available"
    elif difference > 0 and rsp_return > 0 and spy_return > 0:
        interpretation = "RSP outperformed SPY and both gained; check positive sector counts and participation changes before calling this healthy broadening."
        status = "available"
    elif difference > 0:
        interpretation = "RSP outperformed SPY, but absolute returns are mixed or negative; relative resilience alone does not establish healthy broadening."
        status = "available"
    elif difference < 0:
        interpretation = "RSP underperformed SPY; this relative comparison alone does not establish narrowing absolute breadth."
        status = "available"
    else:
        interpretation = f"RSP matched SPY over {window}D; no relative difference in this proxy."
        status = "available"
    return {"status": status, "window": window, "difference_pp": difference,
            "rsp_return_pct": rsp_return * 100, "spy_return_pct": spy_return * 100,
            "as_of": rsp_date, "interpretation": interpretation}


def relative_leadership_rows(
    histories: dict[str, list[dict]], symbols: tuple[str, ...], window: int,
    benchmark: str = "SPY",
) -> list[dict]:
    """Chart rows for ETF return less benchmark return, sorted strongest first."""
    if window not in (5, 20, 60):
        raise ValueError("window must be 5, 20, or 60 trading observations")
    benchmark_return = momentum(histories.get(benchmark, []), window)
    output = []
    for symbol in symbols:
        bars = histories.get(symbol, [])
        if symbol == benchmark:
            aligned = bars
            reference = benchmark_return
        else:
            benchmark_by_date = {row["trading_date"]: row for row in histories.get(benchmark, [])}
            asset_dates = {row["trading_date"] for row in bars}
            common_dates = sorted(asset_dates & benchmark_by_date.keys())
            aligned = [row for row in bars if row["trading_date"] in benchmark_by_date]
            aligned.sort(key=lambda row: row["trading_date"])
            aligned_benchmark = [benchmark_by_date[day] for day in common_dates]
            reference = momentum(aligned_benchmark, window)
        value = momentum(aligned, window)
        excess = (value - reference) if value is not None and reference is not None else None
        if symbol == benchmark and value is not None:
            excess = 0.0
        as_of = aligned[-1].get("trading_date") if aligned else None
        output.append({
            "symbol": symbol,
            "return": value,
            "benchmark_return": reference,
            "excess": excess,
            f"return_{window}d": value,
            f"excess_{window}d": excess,
            "as_of": as_of,
            "is_benchmark": symbol == benchmark,
        })
    return sorted(output, key=lambda row: (
        row["excess"] is not None,
        row["excess"] if row["excess"] is not None else float("-inf"),
    ), reverse=True)


def sector_heatmap_grid(rows: list[dict], columns: int = 3) -> dict:
    """Lay strongest-to-weakest sector rows into a responsive multi-row tile grid."""
    if columns < 1:
        raise ValueError("columns must be positive")
    ordered = sorted((row for row in rows if row.get("return") is not None),
                     key=lambda row: row["return"], reverse=True)
    height = (len(ordered) + columns - 1) // columns
    values = [[None for _ in range(columns)] for _ in range(height)]
    labels = [["" for _ in range(columns)] for _ in range(height)]
    hover = [[None for _ in range(columns)] for _ in range(height)]
    for index, row in enumerate(ordered):
        y, x = divmod(index, columns)
        pct = row["return"] * 100
        values[y][x] = pct
        labels[y][x] = f"{row['symbol']}<br>{row.get('name', row['symbol'])}<br>{pct:+.2f}%"
        hover[y][x] = [row["symbol"], row.get("name", row["symbol"]), pct, row.get("as_of")]
    max_abs = max((abs(row["return"] * 100) for row in ordered), default=1.0)
    bound = max(max_abs, 0.01)
    return {"rows": ordered, "values": values, "labels": labels, "hover": hover,
            "columns": columns, "height": height, "color_bound": bound}


def rotation_quadrant(relative_strength: float, relative_momentum: float) -> str:
    """Classify coordinates around zero using the chart's four relative quadrants."""
    if relative_strength >= 0 and relative_momentum >= 0:
        return "Leading"
    if relative_strength >= 0 and relative_momentum < 0:
        return "Weakening"
    if relative_strength < 0 and relative_momentum < 0:
        return "Lagging"
    return "Improving"


def sector_rotation_pulse(
    histories: dict[str, list[dict]], sectors: dict[str, str], window: int,
) -> dict:
    """Calculate selected-period sector participation, leadership, and equal-weight baskets.

    Defensive = XLV, XLP, XLU. Cyclical = XLY, XLI, XLB, XLE, XLF, XLK, XLC,
    XLRE. Each basket is the arithmetic mean of its sector ETF returns; every member
    must have a valid return on the same as-of date. Participation change compares the
    current window with the immediately preceding equal-length window.
    """
    if window not in (5, 20, 60):
        raise ValueError("window must be 5, 20, or 60 trading observations")
    rows = sector_return_rows(histories, sectors, window)
    returns = {row["symbol"]: row["return"] for row in rows}
    by_symbol = {row["symbol"]: row for row in rows}
    count = len(sectors)
    available = len(rows)
    positive = sum(value > 0 for value in returns.values())
    strongest = max(rows, key=lambda row: row["return"]) if rows else None
    weakest = min(rows, key=lambda row: row["return"]) if rows else None
    spread_pp = (strongest["return"] - weakest["return"]) * 100 if rows else None

    def equal_weight_basket(members):
        member_rows = [by_symbol.get(symbol) for symbol in members]
        if any(row is None for row in member_rows):
            return None
        as_of_dates = {row.get("as_of") for row in member_rows}
        if len(as_of_dates) != 1 or None in as_of_dates:
            return None
        return sum(row["return"] for row in member_rows) / len(member_rows)

    defensive = equal_weight_basket(DEFENSIVE_SECTORS)
    cyclical = equal_weight_basket(CYCLICAL_SECTORS)
    basket_difference_pp = (defensive - cyclical) * 100 if defensive is not None and cyclical is not None else None

    paired_periods = {}
    for symbol in sectors:
        bars = sorted(histories.get(symbol, []), key=lambda row: row.get("trading_date", ""))
        required = 2 * window + 1
        if len(bars) < required:
            continue
        segment = bars[-required:]
        if momentum(segment, 2 * window) is None:
            continue
        prior_return = momentum(segment[:window + 1], window)
        current_return = momentum(segment[window:], window)
        if prior_return is not None and current_return is not None:
            paired_periods[symbol] = (prior_return, current_return, segment[-1].get("trading_date"))
    trend_dates = {values[2] for values in paired_periods.values()}
    if paired_periods and len(trend_dates) == 1:
        previous_positive = sum(values[0] > 0 for values in paired_periods.values())
        current_positive = sum(values[1] > 0 for values in paired_periods.values())
        participation_delta = current_positive - previous_positive
        participation = "broader" if participation_delta > 0 else "narrower" if participation_delta < 0 else "unchanged"
    else:
        previous_positive = current_positive = participation_delta = None
        participation = "unavailable"

    return {
        "window": window, "sector_count": count, "available_sector_count": available,
        "positive_count": positive, "strongest": strongest, "weakest": weakest,
        "leadership_spread_pp": spread_pp,
        "defensive_return": defensive, "cyclical_return": cyclical,
        "defensive_vs_cyclical_pp": basket_difference_pp,
        "defensive_interpretation": (
            "Defensive sectors are outperforming cyclical sectors."
            if basket_difference_pp is not None and basket_difference_pp > 0 else
            "Cyclical sectors are outperforming defensive sectors."
            if basket_difference_pp is not None and basket_difference_pp < 0 else
            "Defensive and cyclical baskets are matching."
            if basket_difference_pp is not None else
            "Defensive/cyclical comparison unavailable: basket data is incomplete or dates do not align."
        ),
        "previous_positive_count": previous_positive,
        "current_positive_count": current_positive,
        "participation_sector_count": len(paired_periods) if participation != "unavailable" else 0,
        "participation_delta": participation_delta,
        "participation": participation,
        "rows": rows,
    }


def market_leadership_series(
    histories: dict[str, list[dict]], symbols: tuple[str, ...], window: int,
    benchmark: str = "SPY",
) -> dict:
    """Build normalized absolute and benchmark-relative series on shared cached dates.

    The selected period uses N+1 common adjusted closes for N trading-session returns.
    Missing or invalid closes are omitted from the common calendar; no prices are filled
    or interpolated. Return values are percentages, while relative values are pp.
    """
    if not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive number of trading observations")
    if benchmark not in symbols:
        raise ValueError("benchmark must be included in symbols")

    close_maps = {}
    for symbol in symbols:
        values = {}
        for row in histories.get(symbol, []):
            try:
                value = float(row["close"])
                day = row["trading_date"]
                if value > 0 and value < float("inf") and value == value:
                    values[day] = value
            except (KeyError, TypeError, ValueError):
                continue
        close_maps[symbol] = values

    common_dates = set.intersection(*(set(close_maps[symbol]) for symbol in symbols)) if symbols else set()
    dates = sorted(common_dates)
    # A long break means the latest cached segment is not a continuous daily series.
    if dates:
        segment_start = 0
        for index in range(1, len(dates)):
            try:
                gap = (date.fromisoformat(dates[index]) - date.fromisoformat(dates[index - 1])).days
            except ValueError:
                return {"status": "unavailable", "window": window, "available_observations": 0,
                        "required_observations": window + 1, "dates": [], "absolute_returns": {},
                        "relative_returns": {}, "latest_date": None, "summary_rows": []}
            if gap <= 0 or gap > 4:
                segment_start = index
        dates = dates[segment_start:]

    required = window + 1
    if len(dates) < required:
        return {"status": "insufficient", "window": window,
                "available_observations": len(dates), "required_observations": required,
                "dates": dates, "absolute_returns": {}, "relative_returns": {},
                "latest_date": dates[-1] if dates else None, "summary_rows": []}
    dates = dates[-required:]
    bases = {symbol: close_maps[symbol][dates[0]] for symbol in symbols}
    absolute = {
        symbol: [(close_maps[symbol][day] / bases[symbol] - 1.0) * 100 for day in dates]
        for symbol in symbols
    }
    benchmark_values = absolute[benchmark]
    relative = {
        symbol: ([0.0] * required if symbol == benchmark else
                 [value - baseline for value, baseline in zip(absolute[symbol], benchmark_values)])
        for symbol in symbols
    }
    summary_rows = [{
        "symbol": symbol,
        f"return_{window}d": absolute[symbol][-1] / 100,
        "as_of": dates[-1],
    } for symbol in symbols]
    return {"status": "available", "window": window, "available_observations": required,
            "required_observations": required, "dates": dates,
            "absolute_returns": absolute, "relative_returns": relative,
            "latest_date": dates[-1], "summary_rows": summary_rows}


def leadership_calculation_symbols(visible_symbols, benchmark: str = "SPY") -> tuple[str, ...]:
    """Include SPY for calculations even when the user hides it from the chart."""
    normalized = list(dict.fromkeys(str(symbol).strip().upper() for symbol in visible_symbols if str(symbol).strip()))
    return tuple(dict.fromkeys((benchmark.upper(), *normalized)))


def market_leadership_observation(series: dict, benchmark: str = "SPY") -> str:
    """Summarize the strongest and weakest ETF using the final cached relative values."""
    if series.get("status") != "available":
        return "Leadership observation unavailable: insufficient aligned cached history."
    values = series["relative_returns"]
    candidates = [(symbol, rows[-1]) for symbol, rows in values.items() if symbol != benchmark and rows]
    if not candidates:
        return "Leadership observation unavailable: no non-benchmark series are available."
    strongest = max(candidates, key=lambda item: item[1])
    weakest = min(candidates, key=lambda item: item[1])
    window = series["window"]
    if strongest[1] > 0:
        first = f"{strongest[0]} is outperforming SPY by {strongest[1]:+.2f} percentage points"
    elif strongest[1] < 0:
        first = f"{strongest[0]} is holding up best versus SPY at {strongest[1]:+.2f} percentage points"
    else:
        first = f"{strongest[0]} is matching SPY"
    if weakest[1] < 0:
        second = f"{weakest[0]} is underperforming SPY by {weakest[1]:+.2f} percentage points"
    elif weakest[1] > 0:
        second = f"{weakest[0]} lags the group but is outperforming SPY by {weakest[1]:+.2f} percentage points"
    else:
        second = f"{weakest[0]} is matching SPY"
    return f"{first}, while {second} over the selected {window}D period."

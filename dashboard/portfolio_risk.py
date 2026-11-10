"""Local, deterministic portfolio risk and reward calculations.

Historical risk is a current-holdings simulation: current market-value weights are
held constant over aligned Tiingo adjusted-close returns. Cash is modeled as a 0%
daily return. This is not a time-weighted or realized account-performance series.
"""

from __future__ import annotations

from datetime import date
from math import isfinite, sqrt

from dashboard.calculations import holding_metrics
from dashboard.market_history import SECTOR_SYMBOLS
from dashboard.setup_calculations import calculate_long_setup


def aligned_daily_return_matrix(
    histories: dict[str, list[dict]], symbols: list[str] | tuple[str, ...], lookback: int,
) -> dict:
    """Align cached daily returns by date; never fill, interpolate, or mix symbols."""
    if lookback < 2:
        raise ValueError("lookback must be at least two trading sessions")
    close_maps: dict[str, dict[str, float]] = {}
    for symbol in dict.fromkeys(symbol.upper() for symbol in symbols):
        bars = sorted(histories.get(symbol, []), key=lambda row: row.get("trading_date", ""))
        values = {}
        for current in bars:
            try:
                close = float(current["close"])
                if close > 0 and isfinite(close):
                    values[current["trading_date"]] = close
            except (KeyError, TypeError, ValueError):
                continue
        close_maps[symbol] = values
    if not close_maps or any(not values for values in close_maps.values()):
        return {"dates": [], "returns": {symbol: [] for symbol in close_maps},
                "available_observations": 0, "required_observations": lookback}
    common_closes = sorted(set.intersection(*(set(values) for values in close_maps.values())))
    return_dates = []
    aligned_returns = {symbol: [] for symbol in close_maps}
    for previous_day, current_day in zip(common_closes, common_closes[1:]):
        try:
            gap = (date.fromisoformat(current_day) - date.fromisoformat(previous_day)).days
        except ValueError:
            continue
        if gap <= 0 or gap > 4:
            continue
        return_dates.append(current_day)
        for symbol, prices in close_maps.items():
            aligned_returns[symbol].append(prices[current_day] / prices[previous_day] - 1.0)
    if len(return_dates) > lookback:
        return_dates = return_dates[-lookback:]
        aligned_returns = {symbol: values[-lookback:] for symbol, values in aligned_returns.items()}
    return {
        "dates": return_dates,
        "returns": aligned_returns,
        "available_observations": len(return_dates),
        "required_observations": lookback,
    }


def correlation_matrix(
    histories: dict[str, list[dict]], symbols: list[str] | tuple[str, ...], lookback: int = 252,
) -> dict:
    """Compute Pearson correlations on common daily return dates."""
    matrix = aligned_daily_return_matrix(histories, symbols, lookback)
    returns = matrix["returns"]
    output = {}
    names = list(returns)
    for left in names:
        output[left] = {}
        for right in names:
            x, y = returns[left], returns[right]
            if len(x) < 2:
                value = None
            else:
                vx, vy = _sample_variance(x), _sample_variance(y)
                value = _sample_covariance(x, y) / sqrt(vx * vy) if vx > 0 and vy > 0 else None
            output[left][right] = value
    return {"dates": matrix["dates"], "observations": matrix["available_observations"], "correlations": output}


def _sample_variance(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _sample_covariance(left: list[float], right: list[float]) -> float:
    mean_left, mean_right = sum(left) / len(left), sum(right) / len(right)
    return sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right)) / (len(left) - 1)


def portfolio_risk_analysis(
    holdings: list[dict], quotes: dict[str, dict], cash: float,
    histories: dict[str, list[dict]], *, lookback: int = 252, benchmark: str = "SPY",
) -> dict:
    """Calculate risk from aligned adjusted closes and report incomplete coverage.

    Position weights use current cached quotes where available, with the latest
    common adjusted close as a real-price fallback. Missing history is excluded
    from a clearly marked subset estimate and never filled with zero returns.
    Cash contributes zero daily return.
    """
    active = [row for row in holdings if float(row.get("shares", 0) or 0) > 0]
    symbols = [row["symbol"].upper() for row in active]
    benchmark = benchmark.upper()
    usable_symbols = [symbol for symbol in symbols if _valid_close_map(histories.get(symbol))]
    matrix = aligned_daily_return_matrix(histories, [*usable_symbols, benchmark], lookback)
    result = {
        "status": "available", "lookback": lookback,
        "observations": matrix["available_observations"],
        "required_observations": lookback, "covered_positions": 0,
        "position_count": len(active), "portfolio_beta": None,
        "invested_equity_beta": None, "annualized_volatility": None,
        "correlation_to_spy": None, "risk_contributions": {},
        "capital_weights": {}, "cash_weight": None,
        "missing_history_symbols": [symbol for symbol in symbols if symbol not in usable_symbols],
        "quote_covered_positions": sum(bool((quotes.get(symbol) or {}).get("price")) for symbol in symbols),
        "included_symbols": [], "aligned_dates": matrix["dates"], "weight_price_sources": {},
    }
    if not active:
        result.update(status="cash_only" if cash > 0 else "empty",
                      portfolio_beta=0.0 if cash > 0 else None,
                      annualized_volatility=0.0 if cash > 0 else None,
                      cash_weight=1.0 if cash > 0 else None)
        return result
    if matrix["available_observations"] < 2 or benchmark not in matrix["returns"]:
        result["status"] = "insufficient_history"
        return result
    included = [symbol for symbol in usable_symbols if symbol in matrix["returns"]]
    as_of = matrix["dates"][-1]
    close_maps = {symbol: _valid_close_map(histories.get(symbol)) for symbol in included}
    shares = {row["symbol"].upper(): float(row["shares"]) for row in active}
    weighted_prices = {}
    for symbol in included:
        try:
            quote_price = float((quotes.get(symbol) or {}).get("price"))
        except (TypeError, ValueError):
            quote_price = 0.0
        if isfinite(quote_price) and quote_price > 0:
            weighted_prices[symbol] = (quote_price, "cached quote")
        elif close_maps[symbol].get(as_of, 0.0) > 0:
            weighted_prices[symbol] = (close_maps[symbol][as_of], f"Tiingo adjusted close ({as_of})")
    values = {symbol: shares[symbol] * weighted_prices[symbol][0]
              for symbol in included if symbol in weighted_prices}
    included = list(values)
    values = {symbol: value for symbol, value in values.items() if value > 0}
    included = list(values)
    result["covered_positions"] = len(included)
    result["included_symbols"] = included
    if not included:
        result["status"] = "insufficient_history"
        return result
    invested_value = sum(values.values())
    total_value = invested_value + max(float(cash), 0.0)
    if invested_value <= 0 or total_value <= 0:
        result["status"] = "invalid_portfolio_value"
        return result
    equity_weights = {symbol: value / invested_value for symbol, value in values.items()}
    total_weights = {symbol: value / total_value for symbol, value in values.items()}
    cash_weight = max(float(cash), 0.0) / total_value
    spy = matrix["returns"][benchmark.upper()]
    equity_series = [
        sum(equity_weights[symbol] * matrix["returns"][symbol][index] for symbol in included)
        for index in range(matrix["available_observations"])
    ]
    portfolio_series = [
        sum(total_weights[symbol] * matrix["returns"][symbol][index] for symbol in included)
        for index in range(matrix["available_observations"])
    ]  # The remaining cash weight contributes zero each day.
    spy_variance = _sample_variance(spy)
    portfolio_variance = _sample_variance(portfolio_series)
    equity_variance = _sample_variance(equity_series)
    portfolio_covariance = _sample_covariance(portfolio_series, spy)
    equity_covariance = _sample_covariance(equity_series, spy)
    result.update(
        status=("available" if len(included) == len(active) and matrix["available_observations"] >= lookback else "partial"),
        capital_weights=total_weights, cash_weight=cash_weight,
        portfolio_beta=portfolio_covariance / spy_variance if spy_variance else None,
        invested_equity_beta=equity_covariance / spy_variance if spy_variance else None,
        annualized_volatility=sqrt(portfolio_variance * 252),
        invested_equity_annualized_volatility=sqrt(equity_variance * 252),
        correlation_to_spy=(portfolio_covariance / sqrt(portfolio_variance * spy_variance)
                             if portfolio_variance > 0 and spy_variance > 0 else None),
    )
    if portfolio_variance > 0:
        contributions = {}
        for symbol in included:
            covariance = _sample_covariance(matrix["returns"][symbol], portfolio_series)
            contribution = total_weights[symbol] * covariance / portfolio_variance
            contributions[symbol] = {
                "fraction_of_portfolio_volatility": contribution,
                "annualized_volatility_contribution": contribution * result["annualized_volatility"],
                "standalone_annualized_volatility": sqrt(_sample_variance(matrix["returns"][symbol]) * 252),
            }
        result["risk_contributions"] = contributions
    result["aligned_dates"] = matrix["dates"]
    result["as_of"] = as_of
    result["modeled_invested_value"] = invested_value
    result["modeled_total_value"] = total_value
    result["weight_price_sources"] = {symbol: weighted_prices[symbol][1] for symbol in included}
    result["missing_valuation_symbols"] = [symbol for symbol in usable_symbols if symbol not in included]
    return result


def _valid_close_map(bars) -> dict[str, float]:
    """Map valid, positive closes by date without filling missing observations."""
    values = {}
    for row in bars or []:
        try:
            close = float(row["close"])
            day = str(row["trading_date"])
            if close > 0 and isfinite(close):
                values[day] = close
        except (KeyError, TypeError, ValueError):
            continue
    return values


def sector_exposure(
    holdings: list[dict], quotes: dict[str, dict], cash: float,
    classifications: dict[str, str | dict],
) -> dict:
    """Aggregate priced positions by saved classification, keeping funds explicit."""
    categories: dict[str, dict] = {}
    missing_symbols = []
    known_invested = 0.0
    verified_value = 0.0
    verified_symbols = []
    for holding in holdings:
        symbol = holding["symbol"].upper()
        metrics = holding_metrics(holding, quotes.get(symbol))
        value = metrics["market_value"]
        if value is None:
            missing_symbols.append(symbol)
            continue
        known_invested += value
        classification = classifications.get(symbol, {})
        category = classification.get("sector") if isinstance(classification, dict) else classification
        category = category or "Unclassified / diversified"
        if (isinstance(classification, dict)
                and classification.get("verification_status") == "Verified"
                and classification.get("sector_benchmark") in SECTOR_SYMBOLS):
            verified_value += value
            verified_symbols.append(symbol)
        item = categories.setdefault(category, {"category": category, "value": 0.0, "symbols": []})
        item["value"] += value
        item["symbols"].append(symbol)
    total_value = None if missing_symbols else known_invested + float(cash)
    for category in categories.values():
        category["pct_invested_equity"] = category["value"] / known_invested if known_invested > 0 else None
        category["pct_total_portfolio"] = category["value"] / total_value if total_value and total_value > 0 else None
    cash_item = {"category": "Cash", "value": float(cash), "symbols": [],
                 "pct_invested_equity": None,
                 "pct_total_portfolio": float(cash) / total_value if total_value and total_value > 0 else None}
    rows = [*categories.values(), cash_item]
    rows.sort(key=lambda row: row["value"], reverse=True)
    return {
        "rows": rows, "known_invested_value": known_invested,
        "total_value": total_value, "missing_symbols": missing_symbols,
        "priced_position_count": sum(len(row["symbols"]) for row in categories.values()),
        "position_count": len(holdings),
        "verified_classification_value": verified_value,
        "verified_classification_symbols": verified_symbols,
        "verified_classification_coverage": verified_value / known_invested if known_invested > 0 else None,
        "verified_sector_classification_coverage": verified_value / known_invested if known_invested > 0 else None,
    }


def position_reward_risk(
    *, shares: float, entry_price, current_price, target_price, stop_price,
) -> dict:
    """Return entry/current RRR plus position-dollar upside/downside scenarios."""
    setup = calculate_long_setup(
        entry_price=entry_price, current_price=current_price,
        target_price=target_price, stop_price=stop_price,
    )
    try:
        quantity = float(shares)
        current = float(current_price)
        target = float(target_price)
        stop = float(stop_price)
        valid_inputs = (isfinite(quantity) and quantity >= 0
                        and all(isfinite(value) and value > 0 for value in (current, target, stop)))
    except (TypeError, ValueError):
        valid_inputs = False
    upside = downside = None
    if valid_inputs and stop < current < target:
        upside = quantity * (target - current)
        downside = quantity * (current - stop)
    elif valid_inputs and current >= target and stop < current:
        upside, downside = 0.0, quantity * (current - stop)
    return {
        "entry_rrr": setup.get("planned_rrr") if setup.get("status") == "valid" else None,
        "remaining_rrr": setup.get("current_rrr"),
        "status": setup.get("current_status"),
        "message": setup.get("current_message"),
        "potential_upside_dollars": upside,
        "potential_downside_to_stop_dollars": downside,
        "setup": setup,
    }


def aggregate_downside_to_stops(holdings: list[dict], quotes: dict[str, dict], strategies: dict[str, dict]) -> dict:
    """Sum current-price-to-stop scenario loss only for valid, unbreached stops."""
    total = 0.0
    covered = 0
    crossed = []
    missing = []
    for holding in holdings:
        symbol = holding["symbol"].upper()
        quote = quotes.get(symbol) or {}
        strategy = strategies.get(symbol) or {}
        current = quote.get("price")
        stop = strategy.get("invalidation_price")
        if current is None or stop is None:
            missing.append(symbol)
            continue
        try:
            current, stop = float(current), float(stop)
            if not isfinite(current) or not isfinite(stop) or current <= 0 or stop <= 0:
                missing.append(symbol)
            elif current <= stop:
                crossed.append(symbol)
            else:
                total += float(holding["shares"]) * (current - stop)
                covered += 1
        except (TypeError, ValueError, KeyError):
            missing.append(symbol)
    return {"dollars": total if covered else None, "covered": covered,
            "position_count": len(holdings), "crossed_symbols": crossed,
            "missing_symbols": missing}

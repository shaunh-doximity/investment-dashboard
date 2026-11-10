"""Deterministic risk/reward calculations for long equity setups."""

from math import floor, isfinite


def format_return_percent(value) -> str:
    """Format a fractional return consistently (0.1453 -> +14.53%)."""
    if value is None:
        return "Unavailable"
    number = float(value)
    sign = "+" if number >= 0 else "−"
    return f"{sign}{abs(number):.2%}"


def format_return_range(bounds) -> str:
    """Format a pair of fractional returns without changing their units."""
    if bounds is None or len(bounds) != 2 or bounds[0] is None or bounds[1] is None:
        return "Unavailable"
    low, high = bounds
    if abs(float(low) - float(high)) < 1e-9:
        return format_return_percent(low)
    return f"{format_return_percent(low)} to {format_return_percent(high)}"


def _positive_number(value, label: str) -> tuple[float | None, str | None]:
    if value is None or value == "":
        return None, f"{label} is missing"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{label} must be a number"
    if not isfinite(number) or number <= 0:
        return None, f"{label} must be a finite number greater than zero"
    return number, None


def _at_entry(entry: float, stop: float, target: float) -> dict:
    if stop > entry:
        return {"entry": entry, "status": "invalid_setup", "message": "Stop/invalidation must be below entry."}
    if stop == entry:
        return {"entry": entry, "status": "zero_risk", "message": "Stop/invalidation equals entry, so planned risk is zero."}
    if target <= entry:
        return {"entry": entry, "status": "invalid_setup", "message": "Target must be above entry."}
    risk = entry - stop
    reward = target - entry
    if risk <= 0:
        return {"entry": entry, "status": "zero_risk", "message": "Risk per share is zero or negative."}
    return {
        "entry": entry,
        "status": "valid",
        "risk_per_share": risk,
        "reward_per_share": reward,
        "planned_rrr": reward / risk,
        # Percentage fields use fractional returns throughout Python (0.1453 = 14.53%).
        "upside_percent": reward / entry,
        "downside_percent": -risk / entry,
        "message": "Valid long setup.",
    }


def calculate_long_setup(
    *,
    target_price=None,
    stop_price=None,
    entry_price=None,
    entry_low=None,
    entry_high=None,
    current_price=None,
) -> dict:
    """Calculate planned RRR and, independently, current-price RRR.

    A range is evaluated at both boundaries; its midpoint is never assumed.
    Current RRR uses current price as reference: (target-current)/(current-stop).
    Stop prices describe a planned scenario, not a guaranteed execution price.
    """
    target, target_error = _positive_number(target_price, "Target price")
    stop, stop_error = _positive_number(stop_price, "Stop/invalidation price")
    result = {
        "status": "missing_data",
        "message": "",
        "planned_scenarios": [],
        "planned_rrr": None,
        "current_status": "missing_data",
        "current_message": "Current quote is unavailable.",
        "current_rrr": None,
        "upside_percent": None,
        "downside_percent": None,
    }

    if target_error or stop_error:
        errors = [error for error in (target_error, stop_error) if error]
        result.update(status="missing_data" if any("missing" in error for error in errors) else "invalid_input", message=" ".join(errors))
        return result

    exact_entry, exact_error = _positive_number(entry_price, "Entry price") if entry_price not in (None, "") else (None, None)
    low, low_error = _positive_number(entry_low, "Entry range lower bound") if entry_low not in (None, "") else (None, None)
    high, high_error = _positive_number(entry_high, "Entry range upper bound") if entry_high not in (None, "") else (None, None)
    errors = [error for error in (exact_error, low_error, high_error) if error]
    if errors:
        result.update(status="invalid_input", message=" ".join(errors))
        return result
    if exact_entry is not None and (low is not None or high is not None):
        result.update(status="invalid_input", message="Provide one entry price or an entry range, not both.")
        return result
    if exact_entry is None and (low is None) != (high is None):
        result.update(status="invalid_input", message="Provide both bounds for an entry range.")
        return result
    if exact_entry is None and low is None:
        result.update(status="missing_data", message="Entry price or complete entry range is missing.")
        return result
    if low is not None and low > high:
        result.update(status="invalid_input", message="Entry range lower bound must not exceed upper bound.")
        return result

    entries = [exact_entry] if exact_entry is not None else ([low] if low == high else [low, high])
    scenarios = [_at_entry(entry, stop, target) for entry in entries]
    result["planned_scenarios"] = scenarios
    if any(scenario["status"] != "valid" for scenario in scenarios):
        nonvalid = [s for s in scenarios if s["status"] != "valid"]
        result.update(status="zero_risk" if all(s["status"] == "zero_risk" for s in nonvalid) else "invalid_setup", message=" ".join(s["message"] for s in nonvalid))
    else:
        result.update(status="valid", message="Valid long setup.")
        if len(scenarios) == 1:
            result["planned_rrr"] = scenarios[0]["planned_rrr"]
            result["upside_percent"] = scenarios[0]["upside_percent"]
            result["downside_percent"] = scenarios[0]["downside_percent"]

    current, current_error = _positive_number(current_price, "Current quote") if current_price not in (None, "") else (None, "Current quote is unavailable")
    if current_error:
        result["current_message"] = current_error
        return result
    if current <= stop:
        result.update(current_status="stop_crossed", current_message="Current quote is at or below the planned stop; setup is invalidated.")
    elif current >= target:
        result.update(current_status="target_crossed", current_message="Current quote is at or above the target; planned reward is already reached.")
    else:
        risk = current - stop
        reward = target - current
        if risk <= 0:
            result.update(current_status="zero_risk", current_message="Current reference leaves zero or negative risk to the stop.")
        else:
            result.update(current_status="valid", current_message="Current quote is between stop and target.", current_rrr=reward / risk)
    return result


def calculate_position_sizing(
    *, entry_price, stop_price, target_price=None, risk_budget=None,
    shares=None, current_portfolio_value=None, current_position_value=0.0,
) -> dict:
    """Calculate a hypothetical long-equity size and dollar scenarios.

    This function never persists a holding or submits an order. Risk budget
    sizing floors to whole shares and excludes slippage, gaps, taxes and fees.
    """
    entry, entry_error = _positive_number(entry_price, "Entry price")
    stop, stop_error = _positive_number(stop_price, "Stop/invalidation price")
    target, target_error = _positive_number(target_price, "Target price") if target_price not in (None, "") else (None, None)
    if entry_error or stop_error:
        return {"status": "invalid_input", "message": " ".join(e for e in (entry_error, stop_error) if e)}
    if stop >= entry:
        return {"status": "invalid_setup", "message": "Stop/invalidation must be below entry for a long setup."}
    risk_per_share = entry - stop
    reward_per_share = target - entry if target is not None else None
    if target is not None and target <= entry:
        return {"status": "invalid_setup", "message": "Target must be above entry for a long setup."}
    budget = None
    if risk_budget not in (None, ""):
        budget, error = _positive_number(risk_budget, "Risk budget")
        if error:
            return {"status": "invalid_input", "message": error}
    actual_shares = None
    if shares not in (None, ""):
        try:
            actual_shares = float(shares)
        except (TypeError, ValueError):
            return {"status": "invalid_input", "message": "Shares must be a non-negative number."}
        if not isfinite(actual_shares) or actual_shares < 0:
            return {"status": "invalid_input", "message": "Shares must be a non-negative number."}
    max_shares = floor(budget / risk_per_share) if budget is not None else None
    sizing_shares = max_shares if max_shares is not None else actual_shares
    exposure = entry * sizing_shares if sizing_shares is not None else None
    portfolio_value = None
    if current_portfolio_value not in (None, ""):
        try:
            portfolio_value = float(current_portfolio_value)
        except (TypeError, ValueError):
            portfolio_value = None
    try:
        old_position_value = max(0.0, float(current_position_value or 0.0))
    except (TypeError, ValueError):
        old_position_value = 0.0
    simulated_total = portfolio_value + exposure - old_position_value if exposure is not None and portfolio_value is not None else None
    return {
        "status": "valid", "message": "Hypothetical long setup; stop execution is not guaranteed.",
        "risk_per_share": risk_per_share, "reward_per_share": reward_per_share,
        "entry_rrr": reward_per_share / risk_per_share if reward_per_share is not None else None,
        "upside_percent": reward_per_share / entry if reward_per_share is not None else None,
        "downside_percent": -risk_per_share / entry,
        "max_shares": max_shares, "shares": sizing_shares, "position_exposure": exposure,
        "planned_dollar_risk": risk_per_share * sizing_shares if sizing_shares is not None else None,
        "potential_dollar_upside": reward_per_share * sizing_shares if reward_per_share is not None and sizing_shares is not None else None,
        "potential_dollar_downside": risk_per_share * sizing_shares if sizing_shares is not None else None,
        "portfolio_allocation_after_change": (exposure / simulated_total
                                              if exposure is not None and simulated_total is not None and simulated_total > 0 else None),
    }

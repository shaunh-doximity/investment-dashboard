"""Deterministic, offline position planning for long stocks and ETFs.

Percentages are fractions internally (0.10 means 10%). A plan is capacity
analysis, never an order or a recommendation to trade.
"""

from __future__ import annotations

from math import floor, isfinite


DEFAULT_POLICY = {
    "max_position_allocation": 0.15,
    "target_position_allocation": 0.08,
    "max_loss_per_position": 0.01,
    "minimum_cash_reserve": 0.0,
    "max_sector_allocation": 0.30,
    "rrr_monitoring_threshold": 1.0,
}


def resolve_planner_reference(assessment, quote=None, *, quote_basis_confirmed=False,
                              manual_price=None, manual_date=None,
                              manual_basis_confirmed=False):
    """Select a sizing reference without silently upgrading stale/incompatible data."""
    assessment = assessment or {}
    if manual_basis_confirmed and manual_price is not None and manual_date:
        try:
            price = float(manual_price)
        except (TypeError, ValueError):
            price = None
        if price is not None and isfinite(price) and price > 0:
            return {"available": True, "price": price, "date": str(manual_date),
                    "source": "User-confirmed manual reference", "is_manual": True,
                    "basis_confirmed": True}
    if assessment.get("status") == "compatible" and assessment.get("price") is not None:
        return {"available": True, "price": float(assessment["price"]),
                "date": (quote or {}).get("quote_date"), "source": "Alpha Vantage cached quote",
                "is_manual": False, "basis_confirmed": True}
    if (assessment.get("status") not in {"stale", "missing"} and quote_basis_confirmed
            and quote and quote.get("price") is not None):
        try:
            price = float(quote["price"])
        except (TypeError, ValueError):
            price = None
        if price is not None and isfinite(price) and price > 0:
            return {"available": True, "price": price, "date": quote.get("quote_date"),
                    "source": "User-confirmed Alpha Vantage quote basis", "is_manual": False,
                    "basis_confirmed": True}
    return {"available": False, "price": None, "date": None,
            "source": None, "is_manual": False, "basis_confirmed": False,
            "status": assessment.get("status", "missing"),
            "message": assessment.get("message") or "A current price reference has not been confirmed."}


def _number(value, name, *, optional=False):
    if value is None and optional:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number" + (" or null." if optional else "."))
    if not isfinite(result):
        raise ValueError(f"{name} must be finite.")
    return result


def calculate_position_capacity(*, portfolio_equity, current_market_value,
                                current_shares, average_cost, cash,
                                entry_price, stop_price=None, target_price=None,
                                current_price=None, quote_stale=False,
                                price_basis_compatible=True,
                                price_compatibility_status=None,
                                price_compatibility_note=None,
                                current_price_is_manual=False,
                                target_allocation=None, max_allocation=None,
                                max_risk_fraction=None, minimum_cash_reserve=0.0,
                                sector_value=0.0, sector_ceiling=None,
                                fractional_shares=False, position_type="long_equity"):
    """Return capacity constrained by allocation, stop risk, cash and sector.

    No entry-range midpoint is assumed: caller must pass a specific planned
    entry price. Existing over-limit positions get zero added capacity and an
    excess warning; no liquidation quantity is proposed.
    """
    if position_type not in {"long_equity", "long_etf"}:
        return {"status": "unsupported", "message": "Only long stock and ETF positions are supported; no sizing was calculated."}
    if quote_stale and not current_price_is_manual:
        return {"status": "stale_reference", "message": price_compatibility_note or "The cached quote is stale. Enter and explicitly confirm a manual reference price before using current-price-dependent sizing."}
    if not price_basis_compatible:
        status = price_compatibility_status if price_compatibility_status in {"missing", "different_observation_date", "incompatible_basis", "unconfirmed_basis"} else "incompatible_price_basis"
        return {"status": status, "message": price_compatibility_note or "Current quote and strategy price basis are not confirmed compatible; stop-risk and tranche capacity are unavailable."}
    values = {
        "portfolio_equity": _number(portfolio_equity, "portfolio_equity"),
        "current_market_value": _number(current_market_value, "current_market_value"),
        "current_shares": _number(current_shares, "current_shares"),
        "average_cost": _number(average_cost, "average_cost"),
        "cash": _number(cash, "cash"),
        "entry_price": _number(entry_price, "entry_price"),
    }
    names = tuple(values)
    equity, current_value, shares, cost, cash_value, entry = (values[name] for name in names)
    if equity <= 0 or entry <= 0 or min(current_value, shares, cost, cash_value) < 0:
        return {"status": "invalid", "message": "Portfolio equity and entry price must be positive; position and cash inputs cannot be negative."}
    stop = _number(stop_price, "stop_price", optional=True)
    target = _number(target_price, "target_price", optional=True)
    current = _number(current_price, "current_price", optional=True)
    target_alloc = _number(target_allocation, "target_allocation", optional=True)
    max_alloc = _number(max_allocation, "max_allocation", optional=True)
    max_risk = _number(max_risk_fraction, "max_risk_fraction", optional=True)
    reserve = _number(minimum_cash_reserve, "minimum_cash_reserve")
    sector = _number(sector_value, "sector_value")
    sector_cap = _number(sector_ceiling, "sector_ceiling", optional=True)
    for name, value in (("target_allocation", target_alloc), ("max_allocation", max_alloc),
                        ("max_risk_fraction", max_risk), ("sector_ceiling", sector_cap)):
        if value is not None and not 0 <= value <= 1:
            raise ValueError(f"{name} must be a fraction between 0 and 1.")
    if reserve < 0 or sector < 0:
        raise ValueError("Cash reserve and sector value cannot be negative.")
    warnings = []
    if current is None and shares > 0:
        current = current_value / shares
    if current_price_is_manual:
        warnings.append("Sizing uses a manually entered, explicitly confirmed reference price; it is not a provider quote.")
    if stop is not None and stop >= entry:
        return {"status": "invalid", "message": "For a long position, stop/invalidation must be below the selected entry price."}
    if target is not None and target <= entry:
        warnings.append("Target is at or below entry; upside scenario is unavailable.")
    if current is not None and target is not None and current >= target:
        warnings.append("Target has already been reached at the supplied current reference; review the plan before adding exposure.")
    if current is not None and stop is not None and current <= stop:
        warnings.append("Current reference is at or below the planned stop; the setup needs review and does not imply an execution.")
    if stop is None:
        warnings.append("No numeric stop: risk-budget sizing is not included.")
    current_alloc = current_value / equity
    allocation_headroom = max(0.0, (max_alloc * equity - current_value) if max_alloc is not None else float("inf"))
    allocation_excess = max(0.0, current_value - max_alloc * equity) if max_alloc is not None else 0.0
    if allocation_excess:
        warnings.append("Existing position is above its maximum allocation; review required. No automatic sale is proposed.")
    if target_alloc is not None:
        target_gap = max(0.0, target_alloc * equity - current_value)
        target_excess = max(0.0, current_value - target_alloc * equity)
    else:
        target_gap, target_excess = None, None
    deployable_cash = max(0.0, cash_value - reserve)
    constraints = {
        "allocation": allocation_headroom,
        "cash": deployable_cash,
    }
    if current is not None and stop is not None and current <= stop:
        constraints["invalidation review"] = 0.0
    if current is not None and target is not None and current >= target:
        constraints["target review"] = 0.0
    risk_budget_dollars = max_risk * equity if max_risk is not None else None
    current_reference = current_value / shares if shares > 0 else entry
    existing_stop_risk = max(0.0, shares * (current_reference - stop)) if stop is not None else None
    if stop is not None and max_risk is not None:
        remaining_risk_budget = max(0.0, risk_budget_dollars - existing_stop_risk)
        constraints["stop risk"] = (remaining_risk_budget / (entry - stop)) * entry
    if sector_cap is not None:
        sector_room = max(0.0, sector_cap * equity - sector)
        constraints["sector"] = sector_room
    permitted_capital = max(0.0, min(constraints.values()))
    additional = permitted_capital / entry
    additional_shares = additional if fractional_shares else floor(additional + 1e-12)
    capital = additional_shares * entry
    resulting_value = current_value + capital
    resulting_equity = equity  # buying with cash converts cash to equity; portfolio equity is unchanged.
    additional_stop_risk = additional_shares * (entry - stop) if stop is not None else None
    total_stop_risk = existing_stop_risk + additional_stop_risk if stop is not None else None
    remaining_buying_capacity = max(0.0, deployable_cash - capital)
    if target_gap is not None and target_gap > 0 and capital + 1e-8 < min(target_gap, permitted_capital):
        warnings.append("Permitted capacity is below the target allocation because another limit is tighter.")
    return {
        "status": "valid", "message": "Capacity estimate only; it is not a recommendation or order.",
        "portfolio_equity": equity, "current_market_value": current_value,
        "current_price": current, "quote_stale": bool(quote_stale),
        "current_price_is_manual": bool(current_price_is_manual),
        "current_shares": shares, "average_cost": cost, "cash": cash_value,
        "current_total_allocation": current_alloc,
        "current_invested_weight": current_value / (equity - cash_value) if equity > cash_value else None,
        "target_allocation": target_alloc, "target_dollar_allocation": target_alloc * equity if target_alloc is not None else None,
        "target_gap_dollars": target_gap, "target_excess_dollars": target_excess,
        "maximum_allocation": max_alloc, "maximum_dollar_allocation": max_alloc * equity if max_alloc is not None else None,
        "allocation_excess_dollars": allocation_excess,
        "maximum_total_shares": shares + additional_shares,
        "existing_shares": shares, "additional_permitted_shares": additional_shares,
        "additional_capital_required": capital, "allocation_after_purchase": resulting_value / resulting_equity,
        "risk_budget_dollars": risk_budget_dollars, "existing_stop_risk_dollars": existing_stop_risk,
        "additional_stop_risk_dollars": additional_stop_risk, "total_stop_risk_dollars": total_stop_risk,
        "stop_risk_portfolio_fraction": total_stop_risk / equity if total_stop_risk is not None else None,
        "remaining_buying_capacity": remaining_buying_capacity,
        "constraint_capital": constraints, "binding_constraint": min(constraints, key=constraints.get),
        "warnings": warnings,
    }


def plan_entry_tranches(tranches, *, capacity_result, entry_price, stop_price=None,
                        fractional_shares=False):
    """Allocate capacity once across ordered tranches; never reuse buying power."""
    if capacity_result.get("status") != "valid":
        return {"status": "invalid", "tranches": [], "message": capacity_result.get("message")}
    price = _number(entry_price, "entry_price")
    if price <= 0:
        raise ValueError("Entry price must be positive.")
    rows = sorted((dict(row) for row in (tranches or [])), key=lambda row: (int(row.get("sequence", 0)), str(row.get("id", ""))))
    weights = [_number(row.get("fraction"), "tranche fraction") for row in rows]
    if any(value < 0 or value > 1 for value in weights):
        raise ValueError("Tranche fractions must be between zero and one.")
    total = sum(weights)
    if total > 1 + 1e-9:
        raise ValueError("Entry tranche fractions cannot exceed 100% of the planned position.")
    permitted = int(capacity_result["additional_permitted_shares"])
    remaining = permitted
    available_capital = float(capacity_result["constraint_capital"][capacity_result["binding_constraint"]])
    total_planned_capital = available_capital
    available_risk = None
    if stop_price is not None and capacity_result.get("risk_budget_dollars") is not None:
        available_risk = max(0.0, capacity_result["risk_budget_dollars"] - (capacity_result.get("existing_stop_risk_dollars") or 0.0))
    output = []
    cumulative_shares = 0
    cumulative_capital = 0.0
    cumulative_stop_risk = capacity_result.get("existing_stop_risk_dollars") or 0.0
    equity = capacity_result.get("portfolio_equity")
    existing_value = capacity_result.get("current_market_value", 0.0)
    active_indexes = [index for index, row in enumerate(rows) if row.get("status", "planned") not in {"canceled", "skipped", "filled"}]
    last_active = active_indexes[-1] if active_indexes else -1
    active_fraction_total = sum(weights[index] for index in active_indexes)
    for index, (row, fraction) in enumerate(zip(rows, weights)):
        if row.get("status", "planned") in {"canceled", "skipped", "filled"}:
            shares = 0
        else:
            trigger = _number(row.get("trigger_price"), "trigger_price")
            if trigger <= 0 or (stop_price is not None and trigger <= stop_price):
                row["status"] = "blocked"
                shares = 0
            else:
                stage_budget = available_capital if index == last_active and active_fraction_total >= 1 - 1e-9 else total_planned_capital * fraction
                shares = floor(stage_budget / trigger + 1e-12)
                if available_risk is not None:
                    shares = min(shares, floor(available_risk / (trigger - stop_price) + 1e-12))
                shares = max(0, shares)
                actual_commitment = shares * trigger
                available_capital = max(0.0, available_capital - actual_commitment)
                if available_risk is not None:
                    available_risk = max(0.0, available_risk - shares * (trigger - stop_price))
        remaining = max(0, remaining - shares)
        trigger_value = row.get("trigger_price")
        trigger = price if trigger_value is None and row.get("status", "planned") in {"canceled", "skipped", "filled"} else _number(trigger_value, "trigger_price")
        capital = shares * trigger
        risk = shares * (trigger - stop_price) if stop_price is not None and stop_price < trigger else None
        cumulative_shares += shares
        cumulative_capital += capital
        if risk is not None:
            cumulative_stop_risk += risk
        output.append({**row, "action": "buy", "planned_shares": shares,
                       "maximum_capital_commitment": capital,
                       "stop_risk_dollars": risk,
                       "cumulative_projected_shares": capacity_result.get("current_shares", 0) + cumulative_shares,
                       "cumulative_capital_committed": cumulative_capital,
                       "projected_allocation_after_stage": ((existing_value + cumulative_capital) / equity if equity else None),
                       "cumulative_stop_risk_dollars": cumulative_stop_risk if stop_price is not None else None,
                       "cumulative_stop_risk_fraction": (cumulative_stop_risk / equity if equity and stop_price is not None else None),
                       "status": row.get("status", "planned")})
    return {"status": "valid", "planned_shares": sum(row["planned_shares"] for row in output),
            "unallocated_base_entry_shares": remaining,
            "remaining_capacity_dollars": available_capital,
            "remaining_stop_risk_dollars": available_risk,
            "tranches": output,
            "message": "All tranche quantities share the same total approved capacity."}


def simulate_exit_tranches(tranches, *, current_shares, current_price=None,
                           stop_price=None, portfolio_equity=None):
    """Simulate sequential reductions. Percent basis is original or remaining."""
    original = _number(current_shares, "current_shares")
    if original < 0:
        raise ValueError("Current shares cannot be negative.")
    remaining = original
    output = []
    rows = sorted((dict(row) for row in (tranches or [])), key=lambda row: (int(row.get("sequence", 0)), str(row.get("id", ""))))
    for row in rows:
        action = row.get("action", "sell")
        if action != "sell":
            raise ValueError("Exit tranche action must be sell.")
        price = _number(row.get("trigger_price"), "trigger_price", optional=True)
        quantity = _number(row.get("shares"), "shares", optional=True)
        percent = _number(row.get("percent"), "percent", optional=True)
        sell_all = bool(row.get("sell_all", False))
        status = row.get("status", "planned")
        basis = row.get("percent_basis", "remaining")
        if basis not in {"original", "remaining"}:
            raise ValueError("percent_basis must be 'original' or 'remaining'.")
        specified = sum((quantity is not None, percent is not None, sell_all))
        actual_fill = _number(row.get("filled_shares"), "filled_shares", optional=True)
        if specified != 1 and status not in {"canceled", "skipped"} and not (status in {"filled", "partial_fill"} and actual_fill is not None):
            raise ValueError("Specify exactly one of an exit share quantity, percentage, or sell_all=true.")
        if status in {"canceled", "skipped"}:
            desired = 0.0
        elif status in {"filled", "partial_fill"}:
            desired = actual_fill or 0.0
        else:
            desired = quantity if quantity is not None else (max(0.0, original if basis == "original" else remaining) * percent if percent is not None else (remaining if sell_all else 0.0))
        sell_shares = min(remaining, max(0.0, desired))
        remaining = max(0.0, remaining - sell_shares)
        proceeds = sell_shares * price if price is not None else None
        value = remaining * price if price is not None else None
        stop_risk = remaining * max(0.0, price - stop_price) if price is not None and stop_price is not None else None
        output.append({**row, "sell_shares": sell_shares, "estimated_proceeds": proceeds,
                       "remaining_shares": remaining, "post_sale_value": value,
                       "post_sale_allocation": value / portfolio_equity if value is not None and portfolio_equity else None,
                       "remaining_stop_risk": stop_risk,
                       "stop_breached_at_trigger": bool(price is not None and stop_price is not None and price <= stop_price),
                       "gap_risk_warning": bool(price is not None and row.get("trigger_type", "price") == "price")})
    return {"status": "valid", "original_shares": original, "remaining_shares": remaining, "tranches": output}


def build_plan_view(plan, *, portfolio_equity, current_market_value, current_shares,
                    average_cost, cash, entry_price, stop_price=None, target_price=None,
                    policy=None, sector_value=0.0, position_type="long_equity"):
    policy = {**DEFAULT_POLICY, **(policy or {})}
    plan = plan or {}
    values = {**policy, **plan}
    return calculate_position_capacity(
        portfolio_equity=portfolio_equity, current_market_value=current_market_value,
        current_shares=current_shares, average_cost=average_cost, cash=cash,
        entry_price=entry_price, stop_price=stop_price, target_price=target_price,
        target_allocation=values.get("target_allocation"),
        max_allocation=values.get("max_allocation"),
        max_risk_fraction=values.get("max_loss_per_position"),
        minimum_cash_reserve=values.get("minimum_cash_reserve", 0),
        sector_value=sector_value, sector_ceiling=values.get("max_sector_allocation"),
        fractional_shares=bool(plan.get("fractional_shares", False)), position_type=position_type,
    )

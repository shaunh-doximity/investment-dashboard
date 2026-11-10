"""Optional, transparent binary-payoff Kelly illustration for long positions.

The module does not source or infer probabilities. All returns are fractions
of invested capital; the Kelly allocation is not a fraction of risk budget.
The stop is modeled as an execution price only for the stated scenario and is
not a guaranteed maximum loss.
"""

from __future__ import annotations

from math import floor, isfinite


PROBABILITY_SOURCES = {
    "User-provided hypothetical assumption",
    "ChatGPT research estimate (unverified)",
    "Historical or validated probability model",
}


def _finite(value, name, *, optional=False):
    if value is None and optional:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number" + (" or null." if optional else "."))
    if not isfinite(result):
        raise ValueError(f"{name} must be finite.")
    return result


def _kelly_allocation(probability, gain_fraction, loss_fraction):
    """Unconstrained log-growth optimum as a fraction of invested capital."""
    if probability is None:
        return None
    if gain_fraction <= 0 or loss_fraction <= 0:
        return None
    value = (probability * gain_fraction - (1 - probability) * loss_fraction) / (gain_fraction * loss_fraction)
    return max(0.0, value)


def calculate_binary_kelly(*, entry_price, target_price, stop_price,
                           probability=None, probability_source=None,
                           probability_evidence="", probability_low=None,
                           probability_high=None, fractional_kelly=0.25,
                           portfolio_equity=None, gap_slippage_fraction=0.0):
    """Calculate full/fractional Kelly and a scenario allocation.

    ``probability`` is P(target reached before stop), explicitly supplied by
    the user. The binary payoff uses target and stop as hypothetical fills.
    ``gap_slippage_fraction`` increases the loss as a fraction of entry capital
    (e.g. .02 models a loss 2 percentage points worse than the planned stop).
    This is a sensitivity assumption, not a prediction.
    """
    entry = _finite(entry_price, "entry_price")
    target = _finite(target_price, "target_price")
    stop = _finite(stop_price, "stop_price")
    equity = _finite(portfolio_equity, "portfolio_equity", optional=True)
    multiplier = _finite(fractional_kelly, "fractional_kelly")
    gap = _finite(gap_slippage_fraction, "gap_slippage_fraction")
    if min(entry, target, stop) <= 0 or not stop < entry < target:
        return {"status": "invalid", "message": "Kelly requires a long setup with 0 < stop < entry < target."}
    if not 0 <= multiplier <= 1:
        raise ValueError("fractional_kelly must be between 0 and 1.")
    if gap < 0 or gap >= 1:
        raise ValueError("gap_slippage_fraction must be at least 0 and below 1.")
    if equity is not None and equity <= 0:
        raise ValueError("portfolio_equity must be positive.")

    gain = (target - entry) / entry
    planned_loss = (entry - stop) / entry
    adjusted_loss = planned_loss + gap
    planned_break_even = planned_loss / (gain + planned_loss)
    break_even = adjusted_loss / (gain + adjusted_loss)
    reward_risk = (target - entry) / (entry - stop)
    result = {
        "status": "unavailable", "gain_per_invested_dollar": gain,
        "planned_loss_per_invested_dollar": planned_loss,
        "gap_adjusted_loss_per_invested_dollar": adjusted_loss,
        "reward_risk": reward_risk, "break_even_probability": break_even,
        "planned_stop_break_even_probability": planned_break_even,
        "probability": None, "probability_source": probability_source,
        "probability_evidence": probability_evidence,
        "full_kelly_allocation_fraction": None,
        "half_kelly_allocation_fraction": None,
        "quarter_kelly_allocation_fraction": None,
        "full_kelly_risk_capital_fraction": None,
        "half_kelly_risk_capital_fraction": None,
        "quarter_kelly_risk_capital_fraction": None,
        "selected_fractional_kelly": multiplier,
        "selected_allocation_before_no_leverage_cap": None,
        "no_leverage_cap_applied": False,
        "selected_allocation_fraction": None,
        "selected_risk_capital_fraction": None,
        "selected_dollar_allocation": None,
        "selected_whole_shares": None,
        "probability_sensitivity": None,
        "model_assumption": "Binary target/stop fill model; stop gaps, slippage, interim paths and correlated positions can produce larger losses.",
    }
    if probability is None:
        result["message"] = "Kelly unavailable: no explicit probability was supplied. No probability is inferred."
        return result
    p = _finite(probability, "probability")
    if not 0 <= p <= 1:
        raise ValueError("probability must be between 0 and 1.")
    if probability_source not in PROBABILITY_SOURCES:
        raise ValueError("Select an explicit supported probability source.")
    if probability_source != "User-provided hypothetical assumption" and not str(probability_evidence or "").strip():
        raise ValueError("Provide the method or evidence for a non-hypothetical probability.")

    full = _kelly_allocation(p, gain, adjusted_loss)
    # If the expected edge is non-positive, do not show a positive suggestion.
    if full is None or full <= 0:
        result.update(status="no_positive_edge", probability=p,
                      message="The supplied probability does not imply positive Kelly sizing under the stated payoff assumptions.")
        return result
    half, quarter = full * .5, full * .25
    selected_unconstrained = full * multiplier
    # This planner only supports unlevered long equity. Portfolio policy will
    # usually impose a much lower cap through calculate_position_capacity.
    selected = min(1.0, selected_unconstrained)
    result.update({
        "status": "valid", "probability": p,
        "full_kelly_allocation_fraction": full,
        "half_kelly_allocation_fraction": half,
        "quarter_kelly_allocation_fraction": quarter,
        "full_kelly_risk_capital_fraction": full * adjusted_loss,
        "half_kelly_risk_capital_fraction": half * adjusted_loss,
        "quarter_kelly_risk_capital_fraction": quarter * adjusted_loss,
        "selected_allocation_fraction": selected,
        "selected_allocation_before_no_leverage_cap": selected_unconstrained,
        "no_leverage_cap_applied": selected_unconstrained > 1.0,
        "selected_risk_capital_fraction": selected * adjusted_loss,
        "selected_dollar_allocation": selected * equity if equity is not None else None,
        "selected_whole_shares": floor(selected * equity / entry) if equity is not None else None,
        "message": "Illustrative Kelly output only; apply portfolio, cash, sector and risk caps separately.",
    })
    low = _finite(probability_low, "probability_low", optional=True)
    high = _finite(probability_high, "probability_high", optional=True)
    if low is not None or high is not None:
        if low is None or high is None or not (0 <= low <= p <= high <= 1):
            raise ValueError("Probability range must satisfy 0 <= low <= probability <= high <= 1.")
        result["probability_sensitivity"] = {
            "low": low,
            "low_allocation_fraction": _kelly_allocation(low, gain, adjusted_loss) * multiplier,
            "high": high,
            "high_allocation_fraction": _kelly_allocation(high, gain, adjusted_loss) * multiplier,
            "label": "Hypothetical sensitivity only; not a validated allocation range.",
        }
    return result

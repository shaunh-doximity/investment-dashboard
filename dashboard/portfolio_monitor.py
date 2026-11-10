"""Deterministic approved-strategy mapping and position-monitor flags."""
from __future__ import annotations

import json

from dashboard.calculations import holding_metrics
from dashboard.strategy_presentation import HORIZONS, analyze_scenario, strategy_scenarios

LABELS = {key: label for key, label in HORIZONS}
KEYS = {label: key for key, label in HORIZONS}
PRIMARY = {"short": "short_term", "medium": "medium_term", "long": "long_term"}


def _num(value):
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (ValueError, TypeError):
        return None


def scenario_levels(scenario):
    scenario = scenario or {}
    target = scenario.get("target_price")
    return {
        "entry_low": _num(scenario.get("entry_price_low")),
        "entry_high": _num(scenario.get("entry_price_high")),
        "target_low": _num(scenario.get("target_price_low", target)),
        "target_high": _num(scenario.get("target_price_high", target)),
        "stop": _num(scenario.get("stop_price", scenario.get("invalidation_price"))),
    }


def _has_content(scenario):
    levels = scenario_levels(scenario)
    if any(value is not None for value in levels.values()):
        return True
    return any(str(scenario.get(key) or "").strip() for key in (
        "thesis", "entry_conditions", "holding_horizon", "invalidation_conditions",
        "fundamental_invalidation", "catalysts", "risks"))


def resolve_approved_strategy(record, rows=None, *, active_horizon=None,
                              current_price=None, current_quote_stale=False):
    """Resolve approved rows only; legacy strategy_records remain supported."""
    record = record or {}
    scenarios = strategy_scenarios(record, rows or [])
    available = [key for key, _ in HORIZONS if _has_content(scenarios[key])]
    if not available:
        return {"available": False, "available_horizons": [], "scenario": None,
                "analysis": None, "thesis_status": record.get("thesis_status")}
    selected = KEYS.get(active_horizon or "")
    if active_horizon and active_horizon.lower() in PRIMARY:
        selected = PRIMARY[active_horizon.lower()]
    primary = PRIMARY.get(str(record.get("investment_horizon") or "").lower())
    if selected not in available:
        selected = None
    if selected is None:
        if len(available) == 1:
            selected = available[0]
        elif primary in available:
            selected = primary
        else:
            return {"available": True, "requires_selection": True,
                    "available_horizons": [LABELS[key] for key in available],
                    "scenario": None, "analysis": None,
                    "thesis_status": record.get("thesis_status")}
    scenario = dict(scenarios[selected])
    levels = scenario_levels(scenario)
    if levels["target_low"] is not None:
        scenario.setdefault("target_price_low", levels["target_low"])
        scenario.setdefault("target_price_high", levels["target_high"])
    if levels["stop"] is not None:
        scenario.setdefault("stop_price", levels["stop"])
    incompatible_basis = False
    company_name = scenario.get("company_name")
    try:
        detail = json.loads(record.get("structured_analysis_json") or "{}")
        reference = detail.get("market_price_reference") or {}
        incompatible_basis = any(word in str(reference.get("basis") or "").lower()
                                 for word in ("adjusted", "dividend"))
        company_name = company_name or detail.get("company_name")
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    analysis = analyze_scenario(scenario, current_price,
                                quote_is_stale=current_quote_stale or incompatible_basis)
    return {"available": True, "requires_selection": False,
            "available_horizons": [LABELS[key] for key in available],
            "scenario": scenario, "horizon_key": selected,
            "horizon": LABELS[selected], "analysis": analysis,
            "thesis_status": record.get("thesis_status"),
            "investment_thesis": record.get("investment_thesis") or scenario.get("thesis", ""),
            "strategy_type": record.get("strategy_type") or scenario.get("strategy_type"),
            "company_name": company_name,
            "quote_basis_compatible": not incompatible_basis,
            "quote_basis_note": "Alpha Vantage raw quote is not compared with an adjusted-price strategy reference." if incompatible_basis else None}


def target_range_text(scenario):
    low, high = scenario_levels(scenario)["target_low"], scenario_levels(scenario)["target_high"]
    if low is None or high is None:
        return "Not specified"
    return f"${low:,.2f}" if low == high else f"${low:,.2f}–${high:,.2f}"


def price_position_status(current, scenario, *, stale=False, stop_near_pct=3, target_near_pct=3):
    if stale:
        return ["Stale quote"]
    current = _num(current)
    if current is None:
        return ["Quote unavailable"]
    levels = scenario_levels(scenario)
    stop, low, high = levels["stop"], levels["target_low"], levels["target_high"]
    result = []
    if stop is not None and current <= stop:
        result.append("Stop breached")
    if high is not None and current >= high:
        result.append("Target reached")
    if stop is not None and current > stop and 100 * (current - stop) / current <= stop_near_pct:
        result.append("Near stop")
    if low is not None and current < low and 100 * (low - current) / current <= target_near_pct:
        result.append("Near target")
    return result


def monitor_alerts(holding, approved, current, *, stale, stop_near_pct=3,
                   target_near_pct=3, remaining_rrr_limit=1, levels_changed=False):
    alerts = []
    thesis = str(approved.get("thesis_status") or holding.get("thesis_status") or "").lower()
    if thesis in {"review needed", "invalidated"}:
        alerts.append(f"Thesis {thesis}")
    if levels_changed:
        alerts.append("Approved levels changed")
    if stale or not approved.get("quote_basis_compatible", True):
        return alerts
    alerts += price_position_status(current, approved.get("scenario"),
                                    stop_near_pct=stop_near_pct, target_near_pct=target_near_pct)
    remaining = (approved.get("analysis") or {}).get("remaining_range")
    if remaining and min(remaining) < remaining_rrr_limit:
        alerts.append(f"Remaining RRR below {remaining_rrr_limit:.1f}×")
    return list(dict.fromkeys(alerts))


def order_holdings(holdings, mode, *, quotes=None, custom_order=None, classifications=None):
    """Return a display-only ordering; never mutate holdings or saved custom order."""
    quotes, classifications = quotes or {}, classifications or {}
    by_symbol = {row["symbol"].upper(): row for row in holdings}
    if mode == "Custom Order":
        desired = [str(symbol).upper() for symbol in (custom_order or [])]
        return [by_symbol[symbol] for symbol in desired if symbol in by_symbol] + [
            by_symbol[symbol] for symbol in sorted(set(by_symbol) - set(desired))
        ]
    if mode == "Ticker":
        return [by_symbol[symbol] for symbol in sorted(by_symbol)]
    if mode != "Position Size":
        raise ValueError("Choose Custom Order, Position Size, or Ticker.")

    def key(symbol):
        classification = classifications.get(symbol, {})
        diversified = (classification.get("asset_type") == "ETF"
                       and "diversified" in str(classification.get("sector", "")).lower())
        value = holding_metrics(by_symbol[symbol], quotes.get(symbol)).get("market_value")
        return (diversified, -(float(value) if value is not None else float("-inf")), symbol)

    return [by_symbol[symbol] for symbol in sorted(by_symbol, key=key)]

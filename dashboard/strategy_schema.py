"""Validation helpers for AI strategy drafts.

This deliberately uses the standard library so the schema has no new runtime
dependency. Nullable price levels are preserved as ``None`` when unsupported.
"""

from __future__ import annotations

from math import isfinite


HORIZONS = {"Swing / short-term", "Medium-term", "Long-term"}
TOP_LEVEL_TEXT = (
    "investment_thesis", "strategy_type", "expected_holding_window",
    "bullish_scenario", "bearish_scenario", "catalysts", "risks",
    "fundamental_analysis_notes", "valuation_analysis_notes", "technical_analysis_notes",
    "thesis_invalidation_conditions", "position_sizing_considerations",
    "supporting_evidence", "fields_requiring_verification",
)
PRICE_FIELDS = (
    "entry_price_low", "entry_price_high", "target_price", "invalidation_price",
)


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value if isinstance(item, (str, int, float)))
    return ""


def _optional_price(value, field: str):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a positive number or null.")
    if not isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{field} must be a positive number or null.")
    return float(value)


def validate_strategy_draft(payload: dict, ticker: str, *, company_name: str | None = None) -> dict:
    """Return a normalized, typed-by-contract strategy draft or raise ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("Strategy draft must be a JSON object.")
    horizon = payload.get("investment_horizon")
    if horizon not in ("Long", "Medium", "Short"):
        raise ValueError("investment_horizon must be Long, Medium, or Short.")
    result = {
        "ticker": ticker.strip().upper(),
        # Only use the locally supplied label; never let the model assert identity.
        "company_name": company_name,
        "investment_horizon": horizon,
    }
    for field in TOP_LEVEL_TEXT:
        result[field] = _text(payload.get(field))
    # Compatibility with the earlier draft fields.
    result["fundamental_analysis_notes"] = result["fundamental_analysis_notes"] or _text(payload.get("fundamental_notes"))
    result["valuation_analysis_notes"] = result["valuation_analysis_notes"] or _text(payload.get("valuation_notes"))
    result["technical_analysis_notes"] = result["technical_analysis_notes"] or _text(payload.get("technical_notes"))
    result["thesis_invalidation_conditions"] = result["thesis_invalidation_conditions"] or _text(payload.get("stop_invalidation"))
    result["position_sizing_considerations"] = result["position_sizing_considerations"] or _text(payload.get("position_sizing_notes"))
    result["supporting_evidence"] = result["supporting_evidence"] or _text(payload.get("evidence"))
    result["fields_requiring_verification"] = result["fields_requiring_verification"] or _text(payload.get("uncertainties"))
    result["assumptions"] = _text(payload.get("assumptions"))
    result["uncertainties"] = _text(payload.get("uncertainties"))
    result["bullish_arguments"] = _text(payload.get("bullish_arguments")) or result["bullish_scenario"]
    result["bearish_arguments"] = _text(payload.get("bearish_arguments")) or result["bearish_scenario"]
    result["catalysts"] = result["catalysts"] or ""
    result["risks"] = result["risks"] or ""
    for field in PRICE_FIELDS:
        result[field] = _optional_price(payload.get(field), field)
    if (result["entry_price_low"] is None) != (result["entry_price_high"] is None):
        raise ValueError("Both entry range bounds are required, or leave both null.")
    if (result["entry_price_low"] is not None and result["entry_price_high"] is not None
            and result["entry_price_low"] > result["entry_price_high"]):
        raise ValueError("Entry range lower bound must not exceed upper bound.")
    evidence_refs = payload.get("supporting_evidence_references", payload.get("evidence_references", []))
    result["supporting_evidence_references"] = evidence_refs if isinstance(evidence_refs, list) else []
    result["horizon_scenarios"] = []
    scenarios = payload.get("horizon_scenarios", payload.get("scenarios", []))
    if not isinstance(scenarios, list):
        raise ValueError("horizon_scenarios must be a list.")
    for item in scenarios[:3]:
        if not isinstance(item, dict) or item.get("horizon") not in HORIZONS:
            raise ValueError("Each horizon scenario must use a supported horizon label.")
        scenario = dict(item)
        for field in ("entry_price_low", "entry_price_high", "target_price", "invalidation_price"):
            scenario[field] = _optional_price(scenario.get(field), f"scenario {field}")
        for field in ("thesis", "holding_window", "entry_conditions", "bullish_catalysts",
                      "bearish_catalysts", "invalidation_conditions", "fundamental_invalidation_conditions",
                      "evidence_for_levels", "assumptions", "confidence_uncertainty"):
            scenario[field] = _text(scenario.get(field))
        if (scenario["entry_price_low"] is None) != (scenario["entry_price_high"] is None):
            raise ValueError("Scenario entry range needs both bounds or neither.")
        result["horizon_scenarios"].append(scenario)
    result["provider"] = str(payload.get("provider") or "")
    result["model"] = str(payload.get("model") or "")
    result["generated_at"] = str(payload.get("generated_at") or "")
    return result

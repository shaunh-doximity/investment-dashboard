"""Compact, deterministic view models for approved research strategies."""

from __future__ import annotations

import json
import re

from dashboard.setup_calculations import calculate_long_setup

HORIZONS = (
    ("short_term", "Swing / short-term"),
    ("medium_term", "Medium-term"),
    ("long_term", "Long-term"),
)
LOGIC_GROUPS = (
    ("bullish_drivers", "Bullish drivers"),
    ("bearish_risks", "Bearish risks"),
    ("catalysts", "Catalysts"),
    ("invalidation_signals", "Thesis invalidation"),
)


def _as_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def strategy_scenarios(strategy: dict, saved_rows: list[dict] | None = None) -> dict[str, dict]:
    """Load per-horizon approved scenarios, preferring their dedicated table.

    Older records have one legacy setup. That setup appears only under its
    saved horizon; it is never copied into the other horizons.
    """
    result = {key: {"horizon": label} for key, label in HORIZONS}
    rows = saved_rows or []
    if rows:
        by_label = {row.get("horizon"): row.get("scenario", {}) for row in rows}
        labels = {label: key for key, label in HORIZONS}
        for label, scenario in by_label.items():
            if label in labels and isinstance(scenario, dict):
                result[labels[label]] = {**result[labels[label]], **scenario}
        return result

    try:
        detail = json.loads(strategy.get("structured_analysis_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        detail = {}
    imported = detail.get("scenarios") if isinstance(detail, dict) else None
    if isinstance(imported, dict):
        for key, _label in HORIZONS:
            scenario = imported.get(key)
            if isinstance(scenario, dict):
                result[key] = {**result[key], **scenario}
        if any(isinstance(imported.get(key), dict) for key, _ in HORIZONS):
            return result
    legacy_scenarios = detail.get("horizon_scenarios") if isinstance(detail, dict) else None
    if isinstance(legacy_scenarios, list) and legacy_scenarios:
        aliases = {
            "short_term": {"short", "short-term", "swing / short-term"},
            "medium_term": {"medium", "medium-term"},
            "long_term": {"long", "long-term"},
        }
        for raw in legacy_scenarios:
            if not isinstance(raw, dict):
                continue
            horizon = str(raw.get("horizon") or "").strip().lower()
            key = next((name for name, options in aliases.items() if horizon in options), None)
            if key is None:
                continue
            target = raw.get("target_price")
            result[key].update({
                "thesis": raw.get("thesis", ""),
                "holding_horizon": raw.get("holding_window") or raw.get("holding_horizon", ""),
                "entry_price_low": raw.get("entry_price_low"),
                "entry_price_high": raw.get("entry_price_high"),
                "target_price_low": raw.get("target_price_low", target),
                "target_price_high": raw.get("target_price_high", target),
                "stop_price": raw.get("stop_price", raw.get("invalidation_price")),
                "entry_conditions": raw.get("entry_conditions", ""),
                "invalidation_conditions": raw.get("invalidation_conditions", raw.get("thesis_invalidation_conditions", "")),
            })
        return result

    horizon = str(strategy.get("investment_horizon") or "Medium").lower()
    key = "short_term" if "short" in horizon else "long_term" if "long" in horizon else "medium_term"
    result[key].update({
        "entry_price_low": strategy.get("entry_price_low"),
        "entry_price_high": strategy.get("entry_price_high"),
        "target_price_low": strategy.get("target_price"),
        "target_price_high": strategy.get("target_price"),
        "stop_price": strategy.get("invalidation_price"),
        "holding_horizon": strategy.get("expected_holding_window"),
        "entry_conditions": strategy.get("legacy_entry_zone"),
        "invalidation_conditions": strategy.get("stop_invalidation"),
        "thesis": strategy.get("investment_thesis"),
    })
    return result


def analyze_scenario(scenario: dict, current_price=None, *, quote_is_stale: bool = False) -> dict:
    """Calculate price-range RRR and a transparent mechanical setup label."""
    entry_low = _as_float(scenario.get("entry_price_low"))
    entry_high = _as_float(scenario.get("entry_price_high"))
    target_low = _as_float(scenario.get("target_price_low", scenario.get("target_price")))
    target_high = _as_float(scenario.get("target_price_high", scenario.get("target_price")))
    stop = _as_float(scenario.get("stop_price", scenario.get("invalidation_price")))
    missing = []
    if entry_low is None or entry_high is None:
        missing.append("entry price or complete entry range")
    if target_low is None or target_high is None:
        missing.append("target price or complete target range")
    if stop is None:
        missing.append("stop/invalidation price")
    if missing:
        return {
            "status": "Incomplete", "missing": missing, "planned_rrr": None,
            "remaining_rrr": None, "upside_percent": None, "downside_percent": None,
            "message": "Missing: " + ", ".join(missing), "rrr_range": None,
            "remaining_range": None,
        }

    entries = sorted(set((entry_low, entry_high)))
    targets = sorted(set((target_low, target_high)))
    cases = [calculate_long_setup(entry_price=entry, target_price=target, stop_price=stop)
             for entry in entries for target in targets]
    invalid = [case for case in cases if case.get("status") != "valid"]
    if invalid:
        return {
            "status": "Invalidated", "missing": [], "planned_rrr": None,
            "remaining_rrr": None, "upside_percent": None, "downside_percent": None,
            "message": "; ".join(dict.fromkeys(case.get("message", "Invalid long setup") for case in invalid)),
            "rrr_range": None, "remaining_range": None,
        }
    planned = [case["planned_rrr"] for case in cases]
    upsides = [case["upside_percent"] for case in cases]
    downsides = [case["downside_percent"] for case in cases]

    remaining = []
    current_statuses = []
    if current_price is not None and not quote_is_stale:
        for target in targets:
            calculated = calculate_long_setup(target_price=target, stop_price=stop, entry_price=entry_low,
                                              current_price=current_price)
            current_statuses.append(calculated.get("current_status"))
            if calculated.get("current_rrr") is not None:
                remaining.append(calculated["current_rrr"])
    if "stop_crossed" in current_statuses:
        status, message = "Invalidated", "Cached quote is at or below the planned stop."
    elif current_statuses and "target_crossed" in current_statuses:
        status, message = "Conditional", "Cached quote has reached a target; review the setup."
    elif current_price is None or quote_is_stale:
        status, message = "Conditional", "Current quote is missing, incompatible, or stale."
    elif entry_low <= float(current_price) <= entry_high:
        status, message = "Actionable", "Cached quote is within the planned entry range and above the stop."
    else:
        status, message = "Conditional", "Cached quote is outside the planned entry range."
    return {
        "status": status, "missing": [], "planned_rrr": planned[0] if len(set(planned)) == 1 else None,
        "remaining_rrr": remaining[0] if len(remaining) == 1 else None,
        "upside_percent": (min(upsides), max(upsides)),
        "downside_percent": (min(downsides), max(downsides)), "message": message,
        "rrr_range": (min(planned), max(planned)),
        "remaining_range": (min(remaining), max(remaining)) if remaining else None,
    }


def summarize_thesis(text: str, max_sentences: int = 2, max_chars: int = 360) -> str:
    """Create a readable preview without modifying the stored full thesis."""
    value = " ".join(str(text or "").split())
    if not value:
        return "No approved thesis yet."
    sentences = re.split(r"(?<=[.!?])\s+", value)
    preview = " ".join(sentences[:max_sentences])
    if len(preview) > max_chars:
        preview = preview[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return preview


def _split_items(text: str, limit: int = 3) -> list[str]:
    parts = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if line:
            parts.extend(piece.strip() for piece in re.split(r"(?<=[.!?])\s+", line) if piece.strip())
    return parts[:limit]


def investment_logic(strategy: dict, structured_detail: dict | None = None) -> dict[str, list[dict]]:
    """Return at most three concise items per investment-logic category."""
    structured_detail = structured_detail or {}
    supplied = structured_detail.get("investment_logic") if isinstance(structured_detail, dict) else None
    result = {}
    fields = {
        "bullish_drivers": strategy.get("bullish_scenario") or structured_detail.get("bull_case", ""),
        "bearish_risks": strategy.get("bearish_scenario") or structured_detail.get("bear_case", ""),
        "catalysts": strategy.get("catalysts") or structured_detail.get("catalysts", ""),
        "invalidation_signals": strategy.get("stop_invalidation", ""),
    }
    for key, _label in LOGIC_GROUPS:
        values = supplied.get(key, []) if isinstance(supplied, dict) else []
        items = []
        if isinstance(values, list):
            for item in values[:3]:
                if isinstance(item, dict) and (item.get("label") or item.get("explanation")):
                    status = str(item.get("evidence_status") or "Imported / unverified")
                    # Import metadata alone cannot establish that a claim is verified.
                    if status == "Verified":
                        status = "Imported / unverified"
                    items.append({
                        "label": str(item.get("label") or "Research point"),
                        "explanation": str(item.get("explanation") or ""),
                        "evidence_status": status,
                        "details": str(item.get("details") or ""),
                    })
        if not items:
            items = [{"label": " ".join(part.split()[:5]).rstrip(".,;:").title() or "Imported research", "explanation": part,
                      "evidence_status": "Imported / unverified", "details": ""}
                     for part in _split_items(fields[key])]
        result[key] = items[:3]
    return result


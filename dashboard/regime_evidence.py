"""Deterministic, descriptive evidence support across three market regimes.

Scores are normalized compatibility support, not calibrated probabilities.
Correlated observations are summarized inside five evidence groups; only the
first four groups vote because rates and macro proxies are directionally
ambiguous without a validated economic interpretation.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite


METHODOLOGY_VERSION = "regime-evidence-v2"
REGIMES = (
    "Risk-on Expansion",
    "Rotation / Mixed",
    "Risk-off Contraction",
)
GROUP_WEIGHTS = {
    "equity_participation": 0.30,
    "sector_leadership": 0.25,
    "credit": 0.25,
    "volatility": 0.20,
    # Descriptive context only: rates/proxies can have competing explanations.
    "rates_macro": 0.00,
}
MINIMUM_VOTING_GROUPS = 3
DIRECTION_DEADBAND_PP = 0.15
TECH_DIRECTION_SCALE_PP = 2.5
OAS_CHANGE_DEADBAND_BP = 5.0
OAS_CHANGE_FULL_SCALE_BP = 20.0

GROUP_LABELS = {
    "equity_participation": "Equity participation",
    "sector_leadership": "Sector leadership",
    "credit": "Credit Confirmation",
    "volatility": "Volatility",
    "rates_macro": "Rates & macro context",
}

# Future-only import contract for a separately validated forecasting system.
# This dashboard does not create, import, or display these forecasts.
FUTURE_PREDICTION_IMPORT_SCHEMA = {
    "schema_version": "market-regime-prediction-import-v1",
    "required": ["model_version", "forecast_date", "horizon_trading_sessions",
                 "outcome_definition", "probabilities", "calibration", "data_provenance"],
    "probability_fields": list(REGIMES),
    "probabilities_sum_to": 1.0,
    "calibration_required": ["evaluation_period", "chronological_out_of_sample", "calibration_method",
                             "sample_count", "baseline_comparison"],
    "confidence_intervals": "optional per-regime lower/upper bounds when methodologically supported",
    "warning": "Future interface only; probabilities must be validated elsewhere before import or display.",
}

TOOLTIPS = {
    "regime": "Summarizes how current, matched-date evidence aligns with Risk-on Expansion, Rotation / Mixed, or Risk-off Contraction. Stress easing is shown separately as a transition characteristic. It is not a forecast or probability.",
    "relative_evidence": "Scores normalize support from the available weighted evidence groups to 100%. This share is not an empirically calibrated likelihood.",
    "equity_participation": "Combines matched RSP/SPY and IWM/SPY relative returns with sector breadth. It describes participation, not future direction or fund flows.",
    "sector_leadership": "Aggregates technology-versus-SPY relative performance with the cyclical-minus-defensive basket. It summarizes leadership; it does not establish rotation flows.",
    "credit": "Uses HYG relative price behavior versus LQD and IEF as one credit-sensitive proxy group. It is not a direct credit spread.",
    "volatility": "Uses the current VIX regime and recent close changes as one volatility-pressure group. VIX measures implied volatility, not equity direction.",
    "rates_macro": "Shows Treasury and TLT/GLD/USO observations as context only. Their moves have competing explanations and are not scored directionally.",
    "scenario": "A hypothetical assumption changes only this local evidence calculation. It does not change observed data, saved records, portfolio state, or a forecast.",
    "stress_easing": "Describes current improvement in VIX pressure, credit, breadth, or cyclical/technology leadership. Stress easing does not require a prior-stress episode; a confirmed recovery from stress does.",
}


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _direction(value):
    value = _finite_number(value)
    if value is None:
        return None
    return max(-1.0, min(1.0, value))


def _tech_direction(transition):
    features = (transition or {}).get("features") or {}
    pairs = features.get("pair_relative_performance") or {}
    values = []
    dates = set()
    for symbol in ("XLK", "SMH", "MAGS"):
        item = pairs.get(symbol) or {}
        relative = _finite_number(item.get("relative_return_pp"))
        if item.get("status") == "available" and relative is not None:
            values.append(max(-1.0, min(1.0, relative / TECH_DIRECTION_SCALE_PP)))
            if item.get("as_of"):
                dates.add(item["as_of"])
    if not values or len(dates) > 1:
        return None, None, {"available_pairs": len(values), "relative_returns_pp": {}}
    evidence = {symbol: (pairs.get(symbol) or {}).get("relative_return_pp")
                for symbol in ("XLK", "SMH", "MAGS")
                if _finite_number((pairs.get(symbol) or {}).get("relative_return_pp")) is not None}
    return sum(values) / len(values), next(iter(dates), None), {
        "available_pairs": len(values), "relative_returns_pp": evidence,
    }


def _component_direction(component):
    if not isinstance(component, dict) or component.get("status") in {
        "unavailable", "Stale observations", "Insufficient history", "Insufficient data",
        "insufficient_history", "inconclusive_gap",
    }:
        return None
    return _direction(component.get("direction"))


def build_regime_evidence(risk_appetite: dict | None, transition: dict | None = None,
                          rates_macro: dict | None = None, *, overrides: dict | None = None) -> dict:
    """Calculate relative support shares from existing deterministic signals.

    ``overrides`` only affects the returned hypothetical. Supported keys are
    equity_participation, technology_leadership, defensive_leadership, credit,
    and volatility, each with a direction in [-1, 1].
    """
    risk = risk_appetite or {}
    components = risk.get("components") or {}
    transition = transition or {}
    features = transition.get("features") or {}
    overrides = overrides or {}

    equity = _component_direction(components.get("equity_participation"))
    credit = _component_direction(components.get("credit_risk_appetite"))
    volatility = _component_direction(components.get("volatility_pressure"))
    defensive = _component_direction(components.get("defensive_rotation"))
    tech, tech_date, tech_details = _tech_direction(transition)
    defensive_date = (components.get("defensive_rotation") or {}).get("as_of")
    if tech is not None and defensive is not None and tech_date and defensive_date and tech_date != defensive_date:
        tech = None
        tech_details = {**tech_details, "alignment_status": "technology and basket dates differ"}

    if "equity_participation" in overrides:
        equity = _direction(overrides["equity_participation"])
    if "credit" in overrides:
        credit = _direction(overrides["credit"])
    if "volatility" in overrides:
        volatility = _direction(overrides["volatility"])
    if "technology_leadership" in overrides:
        tech = _direction(overrides["technology_leadership"])
    if "defensive_leadership" in overrides:
        # This variable measures defensive leadership; invert it into the
        # common direction where positive means constructive/cyclical.
        defensive = -_direction(overrides["defensive_leadership"])

    sector_parts = [value for value in (defensive, tech) if value is not None]
    sector = sum(sector_parts) / len(sector_parts) if sector_parts else None

    equity_value = (components.get("equity_participation") or {}).get("value") or {}
    sector_value = (components.get("defensive_rotation") or {}).get("value") or {}
    credit_value = (components.get("credit_risk_appetite") or {}).get("value") or {}
    volatility_value = (components.get("volatility_pressure") or {}).get("value") or {}
    credit_date = (components.get("credit_risk_appetite") or {}).get("as_of")
    rates_features = ((transition.get("features") or {}).get("rates_and_macro_proxies") or {})
    verified_spreads = rates_features.get("verified_credit_spreads") or {}
    spread_changes = {}
    spread_directions = []
    for series_id, spread in verified_spreads.items():
        spread = spread if isinstance(spread, dict) else {}
        change = _finite_number((spread or {}).get("change_20_observations_bp"))
        spread_as_of = (spread or {}).get("as_of")
        if (spread.get("status") == "available" and change is not None and
                spread_as_of and (not credit_date or spread_as_of == credit_date)):
            spread_changes[series_id] = {"change_20_observations_bp": change, "as_of": spread_as_of}
            magnitude = max(-1.0, min(1.0, change / OAS_CHANGE_FULL_SCALE_BP))
            spread_directions.append(0.0 if abs(change) < OAS_CHANGE_DEADBAND_BP else -magnitude)
    # Treat the high-yield and investment-grade spread series as one OAS
    # sub-signal, so multiple highly related series do not outvote the ETF pair.
    spread_group_direction = (sum(spread_directions) / len(spread_directions)
                              if spread_directions else None)
    available_credit_evidence = ([credit] if credit is not None else [])
    if spread_group_direction is not None:
        available_credit_evidence.append(spread_group_direction)
    credit = (sum(available_credit_evidence) / len(available_credit_evidence)
              if available_credit_evidence else None)
    if credit is not None and not credit_date and spread_changes:
        dates = {item["as_of"] for item in spread_changes.values()}
        credit_date = next(iter(dates)) if len(dates) == 1 else None

    groups = {
        "equity_participation": {
            "direction": equity,
            "as_of": (components.get("equity_participation") or {}).get("as_of"),
            "metrics": {key: equity_value.get(key) for key in (
                "rsp_excess_pp", "iwm_excess_pp", "sectors_outperforming_spy",
                "sectors_above_sma50", "sectors_above_sma200")},
            "factors": list((components.get("equity_participation") or {}).get("evidence") or []),
        },
        "sector_leadership": {
            "direction": sector,
            "as_of": tech_date or defensive_date,
            "subsignals": {"technology_relative": tech, "cyclical_vs_defensive": defensive},
            "metrics": {"technology_relative_returns_pp": tech_details["relative_returns_pp"],
                        "cyclical_minus_defensive_pp": sector_value.get("cyclical_minus_defensive_pp")},
            "factors": list((components.get("defensive_rotation") or {}).get("evidence") or []),
        },
        "credit": {
            "direction": credit,
            "as_of": credit_date,
            "metrics": {**{key: credit_value.get(key) for key in ("hyg_minus_lqd_pp", "hyg_minus_ief_pp")},
                        "verified_spread_changes": spread_changes},
            "factors": list((components.get("credit_risk_appetite") or {}).get("evidence") or []) +
                       ([f"Verified OAS changes: {spread_changes}"] if spread_changes else []),
        },
        "volatility": {
            "direction": volatility,
            "as_of": (components.get("volatility_pressure") or {}).get("as_of"),
            "metrics": {key: volatility_value.get(key) for key in (
                "close", "regime", "daily_change_percent", "five_session_change_percent")},
            "factors": list((components.get("volatility_pressure") or {}).get("evidence") or []),
        },
        "rates_macro": {
            "direction": None,
            "as_of": (rates_macro or {}).get("as_of"),
            "metrics": {key: item.get("value") for key, item in ((rates_macro or {}).get("metrics") or {}).items()
                        if isinstance(item, dict)
                        and key.startswith(("two_year_change_bp_", "ten_year_change_bp_", "spread_change_bp_",
                                           "tlt_absolute_return_pct_", "gld_absolute_return_pct_", "uso_absolute_return_pct_"))},
            "factors": ["Context only: yield and asset-proxy moves have competing explanations; not a regime vote."],
        },
    }
    as_of_dates = [group["as_of"] for key, group in groups.items()
                   if key in GROUP_WEIGHTS and GROUP_WEIGHTS[key] > 0 and group.get("direction") is not None and group.get("as_of")]
    aligned_date = max(as_of_dates) if as_of_dates else None
    stale_groups = [key for key, group in groups.items()
                    if GROUP_WEIGHTS.get(key, 0) > 0 and group.get("direction") is not None
                    and aligned_date and group.get("as_of") != aligned_date]
    voting = {key: group["direction"] for key, group in groups.items()
              if GROUP_WEIGHTS.get(key, 0) > 0 and group.get("direction") is not None
              and key not in stale_groups}
    total_weight = sum(GROUP_WEIGHTS[key] for key in voting)
    required_equity = "equity_participation" in voting
    enough_evidence = required_equity and len(voting) >= MINIMUM_VOTING_GROUPS
    prior_stress = bool(risk.get("prior_stress_observed"))
    transition_characteristics = {
        "vix_pressure_easing": voting.get("volatility", 0.0) > 0 if "volatility" in voting else None,
        "credit_improving": voting.get("credit", 0.0) > 0 if "credit" in voting else None,
        "breadth_recovering": voting.get("equity_participation", 0.0) > 0 if "equity_participation" in voting else None,
        "cyclical_technology_improving": voting.get("sector_leadership", 0.0) > 0 if "sector_leadership" in voting else None,
    }
    easing_values = [value for value in transition_characteristics.values() if value is not None]
    easing_count = sum(easing_values)
    transition_characteristics["stress_easing_state"] = (
        "Stress easing" if easing_count >= 2 else
        "Stress pressure persists" if easing_values and easing_count == 0 else
        "Mixed / incomplete" if easing_values else "Insufficient Evidence")
    transition_characteristics["recovery_from_stress_confirmed"] = bool(
        prior_stress and transition_characteristics["breadth_recovering"] is True
        and transition_characteristics["credit_improving"] is True
        and transition_characteristics["vix_pressure_easing"] is True)
    transition_characteristics["recovery_confirmation_requires_prior_stress"] = True
    if not enough_evidence or total_weight <= 0:
        return {
            "methodology_version": METHODOLOGY_VERSION,
            "status": "insufficient_data", "classification": "Insufficient Evidence", "as_of": aligned_date,
            "relative_evidence_support": None, "groups": groups,
            "missing_groups": [key for key in GROUP_WEIGHTS if GROUP_WEIGHTS[key] > 0 and key not in voting],
            "stale_groups": stale_groups, "weights": dict(GROUP_WEIGHTS),
            "minimum_voting_groups": MINIMUM_VOTING_GROUPS,
            "explanation": "Aligned equity participation and at least two other current evidence groups are required.",
            "prior_stress_observed": prior_stress,
            "transition_characteristics": transition_characteristics,
        }

    normalized_weights = {key: GROUP_WEIGHTS[key] / total_weight for key in voting}
    positive = sum(normalized_weights[key] * max(voting[key], 0.0) for key in voting)
    negative = sum(normalized_weights[key] * max(-voting[key], 0.0) for key in voting)
    neutral = sum(normalized_weights[key] * (1.0 - abs(voting[key])) for key in voting)
    disagreement = 2.0 * min(positive, negative)
    # Transition characteristics remain a separate descriptive field; they do
    # not compete with the three contemporaneous regimes.
    raw = {
        "Risk-on Expansion": positive,
        "Rotation / Mixed": neutral + disagreement,
        "Risk-off Contraction": negative,
    }
    total = sum(raw.values())
    if total <= 1e-12:
        shares = None
        status = "insufficient_data"
    else:
        shares = {name: raw[name] / total * 100.0 for name in REGIMES}
        status = "available"
    ranked = sorted((shares or {}).items(), key=lambda pair: pair[1], reverse=True)
    classification = ranked[0][0] if ranked else None
    support = {}
    for name in REGIMES:
        values = []
        contradictions = []
        for key, direction in voting.items():
            label = GROUP_LABELS[key]
            if name == "Risk-on Expansion":
                if direction > 0:
                    values.append(label)
                elif direction < 0:
                    contradictions.append(label)
            elif name == "Risk-off Contraction":
                if direction < 0:
                    values.append(label)
                elif direction > 0:
                    contradictions.append(label)
            else:
                if abs(direction) < 0.5:
                    values.append(label)
                elif (positive > 0 and negative > 0):
                    values.append(label + " contributes to cross-group disagreement")
        support[name] = {"supporting_factors": list(dict.fromkeys(values))[:3],
                         "contradictory_factors": list(dict.fromkeys(contradictions))[:3]}
    return {
        "methodology_version": METHODOLOGY_VERSION,
        "status": status,
        "as_of": aligned_date,
        "relative_evidence_support": shares,
        "raw_support": raw,
        "classification": classification,
        "groups": groups,
        "voting_groups": list(voting),
        "missing_groups": [key for key in GROUP_WEIGHTS if GROUP_WEIGHTS[key] > 0 and key not in voting],
        "stale_groups": stale_groups,
        "weights": dict(GROUP_WEIGHTS),
        "normalized_weights_used": normalized_weights,
        "minimum_voting_groups": MINIMUM_VOTING_GROUPS,
        "prior_stress_observed": prior_stress,
        "transition_characteristics": transition_characteristics,
        "support_factors": support,
        "explanation": "Relative evidence support is normalized across available evidence groups; it is not a calibrated likelihood.",
    }


def compare_scenario_support(baseline: dict, hypothetical: dict) -> dict:
    """Return compact group-level direction deltas; no persistence side effects."""
    base_groups = baseline.get("groups") or {}
    hyp_groups = hypothetical.get("groups") or {}
    changes = []
    for key in GROUP_WEIGHTS:
        if GROUP_WEIGHTS[key] <= 0:
            continue
        before = (base_groups.get(key) or {}).get("direction")
        after = (hyp_groups.get(key) or {}).get("direction")
        if before != after:
            changes.append({"group": key, "before": before, "after": after,
                            "label": GROUP_LABELS[key]})
    return {"changed_groups": changes,
            "baseline": deepcopy(baseline.get("relative_evidence_support")),
            "hypothetical": deepcopy(hypothetical.get("relative_evidence_support")),
            "hypothetical_only": True}

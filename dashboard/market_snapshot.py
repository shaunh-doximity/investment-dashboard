"""Canonical, deterministic Market Intelligence snapshot and comparisons."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
import math


SNAPSHOT_VERSION = "market-intelligence-snapshot-v3"
LEGACY_SNAPSHOT_VERSION = "market-intelligence-snapshot-v1"
PREVIOUS_SNAPSHOT_VERSION = "market-intelligence-snapshot-v2"
LEGACY_SECTION_ORDER = (
    "catalysts", "regime", "sector_rotation", "cyclical_defensive",
    "relative_strength", "treasury", "vix", "technology_capital_cycle", "cross_market",
)
SECTION_ORDER = ("catalysts", "regime", "sector_rotation", "relative_strength", "rates_macro", "vix")
SECTION_LABELS = {
    "catalysts": "Upcoming Market Catalysts",
    "regime": "Market Regime Summary",
    "sector_rotation": "Sector Rotation and Breadth",
    "relative_strength": "Relative Strength and Defensive Rotation",
    "rates_macro": "Rates and Macro Proxies",
    "vix": "VIX Volatility Risk Monitor",
}
PROVIDER_ACCENTS = {"deepseek": "#68a9ff", "claude": "#d8b46a", "openai": "#65c695"}
logger = logging.getLogger(__name__)


def provider_accent(provider: str | None) -> str:
    """Subtle report-provider accent; colors indicate provider only."""
    return PROVIDER_ACCENTS.get(str(provider or "").lower(), "#8f9aa8")


def select_saved_market_report(reports, current_fingerprint: str | None = None):
    """Choose one whole saved report, preferring an exact snapshot match.

    It never merges highlights or briefing text across separate report runs.
    """
    eligible = [report for report in reports or []
                if report.get("research_status") in {"complete", "complete_cost_warning"}
                and report.get("report_text")
                and (report.get("snapshot") or {}).get("schema_version") in {SNAPSHOT_VERSION, PREVIOUS_SNAPSHOT_VERSION, LEGACY_SNAPSHOT_VERSION}]
    if current_fingerprint:
        exact = next((report for report in eligible
                      if (report.get("snapshot") or {}).get("fingerprint") == current_fingerprint), None)
        if exact:
            return exact
    return eligible[0] if eligible else None


def saved_report_is_stale(report: dict | None, current_fingerprint: str | None,
                          current_prompt_version: str | None = None) -> bool:
    if not report:
        return False
    saved_fingerprint = (report.get("snapshot") or {}).get("fingerprint")
    saved_prompt = (report.get("usage") or {}).get("prompt_version")
    prompt_stale = bool(current_prompt_version and saved_prompt != current_prompt_version)
    return prompt_stale or bool(current_fingerprint and saved_fingerprint != current_fingerprint)


def _finite(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    return True


def _canonical(value):
    if isinstance(value, dict):
        return {str(key): _canonical(val) for key, val in sorted(value.items()) if val is not None and _finite(val)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value if item is not None and _finite(item)]
    if isinstance(value, float):
        return round(value, 10)
    return value


def build_market_snapshot(sections: dict, *, configurations: dict | None = None,
                          view_state: dict | None = None) -> dict:
    """Build a versioned ordered snapshot; unavailable sections remain explicit."""
    source = sections or {}
    normalized = {}
    for section_id in SECTION_ORDER:
        item = source.get(section_id)
        if not isinstance(item, dict):
            normalized[section_id] = {"status": "unavailable", "metrics": {}, "gaps": ["Section data unavailable"]}
            continue
        normalized[section_id] = _canonical(item)
        normalized[section_id].setdefault("status", "available" if normalized[section_id].get("metrics") else "unavailable")
        normalized[section_id].setdefault("metrics", {})
        normalized[section_id].setdefault("gaps", [])
    result = {
        "schema_version": SNAPSHOT_VERSION,
        "as_of": max((str(section.get("as_of")) for section in normalized.values() if section.get("as_of")), default=None),
        "sections_in_reading_order": list(SECTION_ORDER),
        "section_labels": SECTION_LABELS,
        "sections": normalized,
        "configurations": _canonical(configurations or {}),
    }
    # View controls (selected chart pair/window, visible layout choices) do not
    # alter the underlying research snapshot. Stable analysis definitions and
    # measured section values do.
    result["fingerprint"] = market_snapshot_fingerprint(result)
    view = _canonical(view_state or {})
    result["view_state"] = view
    result["view_fingerprint"] = hashlib.sha256(
        json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return result


def market_snapshot_fingerprint(snapshot: dict) -> str:
    """Hash only the canonical data/configuration fields, excluding view metadata."""
    keys = ("schema_version", "as_of", "sections_in_reading_order", "section_labels", "sections", "configurations")
    stable = {key: snapshot.get(key) for key in keys if key in snapshot}
    fingerprint_payload = json.dumps(stable, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str)
    return hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()


def snapshot_with_recent_catalysts(snapshot: dict, catalyst_cache: dict) -> dict:
    """Bind an explicitly refreshed catalyst cache to the research snapshot."""
    updated = deepcopy(snapshot or {})
    sections = updated.setdefault("sections", {})
    catalysts = sections.setdefault("catalysts", {"status": "available", "metrics": {}, "gaps": []})
    catalysts["recent_developments"] = list(catalyst_cache.get("records") or [])
    catalysts["recent_developments_status"] = catalyst_cache.get("status", "unavailable")
    catalysts["recent_developments_refreshed_at"] = catalyst_cache.get("refreshed_at")
    updated["fingerprint"] = market_snapshot_fingerprint(updated)
    return updated


def metric_references(snapshot: dict) -> set[str]:
    """Return references the model is permitted to cite from local metrics."""
    refs = set()
    for section_id, section in (snapshot.get("sections") or {}).items():
        for metric_id, metric in (section.get("metrics") or {}).items():
            if isinstance(metric, dict) and metric.get("value") is not None:
                refs.add(f"{section_id}.{metric_id}")
    return refs


def compare_market_snapshots(current: dict, previous: dict | None) -> dict:
    """Compare same metric paths only when units, windows, and universes match."""
    if not previous:
        return {"status": "no_previous_snapshot", "changes": [], "incompatible": []}
    if current.get("schema_version") != previous.get("schema_version"):
        return {"status": "incompatible_schema", "changes": [], "incompatible": []}
    changes, incompatible = [], []
    for section_id in SECTION_ORDER:
        now_section = current.get("sections", {}).get(section_id, {})
        old_section = previous.get("sections", {}).get(section_id, {})
        now_metrics, old_metrics = now_section.get("metrics", {}), old_section.get("metrics", {})
        for key in sorted(set(now_metrics) & set(old_metrics)):
            now, old = now_metrics[key], old_metrics[key]
            if not isinstance(now, dict) or not isinstance(old, dict):
                continue
            comparable_fields = ("unit", "window", "universe", "definition")
            if any(now.get(field) != old.get(field) for field in comparable_fields):
                incompatible.append(f"{section_id}.{key}")
                continue
            if isinstance(now.get("value"), bool) and isinstance(old.get("value"), bool) and now["value"] != old["value"]:
                changes.append({"section_id": section_id, "metric_id": key,
                                "earlier": old["value"], "latest": now["value"],
                                "change": f"{old['value']} → {now['value']}",
                                "unit": now.get("unit"), "earlier_date": old.get("as_of"),
                                "latest_date": now.get("as_of")})
            elif isinstance(now.get("value"), (int, float)) and not isinstance(now.get("value"), bool) and isinstance(old.get("value"), (int, float)) and not isinstance(old.get("value"), bool):
                changes.append({"section_id": section_id, "metric_id": key,
                                "earlier": old["value"], "latest": now["value"],
                                "change": now["value"] - old["value"],
                                "unit": now.get("unit"), "earlier_date": old.get("as_of"),
                                "latest_date": now.get("as_of")})
            elif isinstance(now.get("value"), str) and isinstance(old.get("value"), str) and now["value"] != old["value"]:
                changes.append({"section_id": section_id, "metric_id": key,
                                "earlier": old["value"], "latest": now["value"],
                                "change": f"{old['value']} → {now['value']}",
                                "unit": now.get("unit"), "earlier_date": old.get("as_of"),
                                "latest_date": now.get("as_of")})
    return {"status": "comparable" if changes else "no_compatible_metrics",
            "previous_fingerprint": previous.get("fingerprint"),
            "changes": changes, "incompatible": incompatible}


def _reconstruct_briefing_metric_refs(payload: dict, briefing: dict, required_briefing: set[str]) -> dict:
    """Rebuild a missing briefing citation index from refs already in the response."""
    def refs(items):
        found = []
        for item in items or []:
            values = item.get("metric_refs", []) if isinstance(item, dict) else []
            for value in values if isinstance(values, list) else []:
                if isinstance(value, str) and value not in found:
                    found.append(value)
        return found

    highlights = payload.get("highlights") or []
    by_section = {item.get("section_id"): item for item in highlights if isinstance(item, dict)}
    transition = briefing.get("transition_outlook") or {}
    raw_transition_refs = transition.get("metric_refs", [])
    transition_refs = [value for value in raw_transition_refs if isinstance(value, str)] if isinstance(raw_transition_refs, list) else []
    hypothesis_refs = refs(briefing.get("hypotheses"))
    all_refs = refs(highlights) + hypothesis_refs + transition_refs
    all_refs = list(dict.fromkeys(all_refs))

    def highlight_refs(*section_ids, categories=()):
        selected = []
        for section_id in section_ids:
            item = by_section.get(section_id)
            if item and (not categories or item.get("category") in categories):
                selected.extend(refs([item]))
        return list(dict.fromkeys(selected))

    category_refs = {
        "executive_takeaway": highlight_refs("regime", "sector_rotation") + transition_refs,
        "risks": highlight_refs("regime", "sector_rotation", "relative_strength", "rates_macro", "vix",
                                 categories=("caution",)) + hypothesis_refs,
        "supportive": highlight_refs("regime", "sector_rotation", "relative_strength", "rates_macro", "vix",
                                     categories=("constructive",)) + hypothesis_refs,
        "sector_implications": highlight_refs("sector_rotation", "relative_strength", "rates_macro"),
        "conflicting_evidence": highlight_refs("regime", "sector_rotation", "relative_strength", "rates_macro", "vix",
                                                categories=("mixed", "caution")) + transition_refs,
        "catalysts": highlight_refs("catalysts"),
        "conditions": transition_refs + refs(highlights),
        "hypotheses": hypothesis_refs,
        "transition_outlook": transition_refs,
    }
    output = {}
    for key in required_briefing - {"metric_refs"}:
        section_value = briefing.get(key)
        has_claim = bool(section_value.strip()) if isinstance(section_value, str) else bool(section_value)
        chosen = category_refs.get(key) or all_refs if has_claim else []
        output[key] = list(dict.fromkeys(chosen))
    return output


def validate_market_analysis(payload, snapshot: dict, sources=()) -> dict:
    """Validate shared AI highlights + final briefing and local metric citations."""
    response_version = (payload or {}).get("schema_version") if isinstance(payload, dict) else None
    # Some completed DeepSeek generations express one schema string as a short
    # list of sector names. Canonicalize that representation without inventing
    # content so downstream renderers and persisted reports stay consistent.
    payload = deepcopy(payload) if isinstance(payload, dict) else payload
    briefing_data = payload.get("briefing") if isinstance(payload, dict) else None
    hypotheses_data = briefing_data.get("hypotheses") if isinstance(briefing_data, dict) else None
    if isinstance(hypotheses_data, list):
        for hypothesis in hypotheses_data:
            if not isinstance(hypothesis, dict):
                continue
            sector_implications = hypothesis.get("sector_implications")
            if (isinstance(sector_implications, list) and sector_implications
                    and all(isinstance(item, str) and item.strip() for item in sector_implications)):
                hypothesis["sector_implications"] = "; ".join(item.strip() for item in sector_implications)
    legacy = response_version == "market-analyst-response-v1"
    v4 = response_version in {"market-analyst-response-v4", "market-analyst-response-v5", "market-analyst-response-v6"}
    v5 = response_version in {"market-analyst-response-v5", "market-analyst-response-v6"}
    v6 = response_version == "market-analyst-response-v6"
    if not isinstance(payload, dict) or response_version not in {"market-analyst-response-v1", "market-analyst-response-v2", "market-analyst-response-v3", "market-analyst-response-v4", "market-analyst-response-v5", "market-analyst-response-v6"}:
        raise ValueError("Expected a supported market analyst response schema version.")
    required_briefing = {"executive_takeaway", "risks", "supportive", "sector_implications",
                         "conflicting_evidence", "catalysts", "conditions", "metric_refs"}
    if not legacy:
        required_briefing.add("hypotheses")
    if v4:
        required_briefing.add("transition_outlook")
    expected_sections = tuple((snapshot or {}).get("sections_in_reading_order") or
                              (LEGACY_SECTION_ORDER if legacy else SECTION_ORDER))
    highlights = payload.get("highlights")
    if not isinstance(highlights, list):
        raise ValueError("highlights must be an array.")
    section_ids = set()
    allowed_categories = {"constructive", "caution", "mixed", "insufficient evidence"}
    allowed_refs = metric_references(snapshot)
    # Early v5 generations sometimes emitted a bare metric key for these
    # transition features even though the snapshot registry uses the full
    # section.metric_id path. Canonicalize only an unambiguous section match;
    # arbitrary or ambiguous IDs remain validation errors. The saved raw JSON
    # is not rewritten in storage; this is an in-memory compatibility repair.
    canonicalized = []
    section_metrics = {section_id: set((section or {}).get("metrics", {}))
                       for section_id, section in ((snapshot or {}).get("sections") or {}).items()}

    def canonical_comparison_alias(reference):
        """Repair the older AI naming pattern for broad-index vs SPY metrics."""
        if not isinstance(reference, str) or not reference.startswith("relative_strength."):
            return None
        import re
        match = re.fullmatch(r"relative_strength\.([a-z]+)_spy_(5|20|60|120)d_excess_pp", reference)
        if not match:
            return None
        symbol, window = match.groups()
        if symbol not in {"rsp", "qqq", "dia", "iwm"}:
            return None
        target = f"regime.broad_{symbol}_{window}d_excess_vs_spy"
        return target if target in allowed_refs else None

    def repair_metric_refs(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key == "metric_refs" and isinstance(child, list):
                    repaired = []
                    for ref in child:
                        broad_alias = canonical_comparison_alias(ref)
                        if broad_alias:
                            canonicalized.append((child_path, ref, broad_alias))
                            repaired.append(broad_alias)
                            continue
                        if isinstance(ref, str) and "." not in ref:
                            matches = [f"{section}.{ref}" for section, ids in section_metrics.items() if ref in ids]
                            if len(matches) == 1 and matches[0] in allowed_refs:
                                canonicalized.append((child_path, ref, matches[0]))
                                repaired.append(matches[0])
                                continue
                        repaired.append(ref)
                    value[key] = repaired
                else:
                    repair_metric_refs(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                repair_metric_refs(child, f"{path}[{index}]")
    repair_metric_refs(payload)
    if canonicalized:
        logger.info("Canonicalized known market metric aliases count=%s paths=%s",
                    len(canonicalized), [item[0] for item in canonicalized[:12]])
    for index, item in enumerate(highlights):
        if not isinstance(item, dict):
            raise ValueError(f"highlights[{index}] must be an object.")
        section_id = item.get("section_id")
        if section_id not in expected_sections or section_id in section_ids:
            raise ValueError(f"highlights[{index}].section_id is unknown or duplicated.")
        section_ids.add(section_id)
        if item.get("category") not in allowed_categories:
            raise ValueError(f"highlights[{index}].category is invalid.")
        for field in ("headline", "insight", "why_it_matters", "monitor_next"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"highlights[{index}].{field} must be nonempty text.")
        refs = item.get("metric_refs")
        if not isinstance(refs, list):
            raise ValueError(f"highlights[{index}].metric_refs: expected an array of exact local metric IDs.")
        invalid_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in allowed_refs]
        if invalid_refs:
            raise ValueError(f"highlights[{index}].metric_refs: invalid metric ID(s) {invalid_refs!r}; IDs must exactly match the finite available snapshot registry.")
        if item["category"] != "insufficient evidence" and not refs:
            raise ValueError(f"highlights[{index}].metric_refs is required for a non-empty signal.")
    missing_sections = set(expected_sections) - section_ids
    if missing_sections:
        raise ValueError("Missing highlights for: " + ", ".join(sorted(missing_sections)))
    briefing = payload.get("briefing")
    if not isinstance(briefing, dict):
        raise ValueError("briefing must be an object containing the integrated-briefing sections.")
    missing_briefing = required_briefing - set(briefing)
    # DeepSeek occasionally returns the complete briefing but omits only the
    # redundant top-level citation index. Rebuild that index exclusively from
    # exact metric IDs it already cited in validated-output fields; never
    # invent a metric ID or value. The normal reference validation below still
    # rejects any unknown IDs and requires evidence for each nonempty section.
    if missing_briefing == {"metric_refs"}:
        briefing = dict(briefing)
        briefing["metric_refs"] = _reconstruct_briefing_metric_refs(payload, briefing, required_briefing)
        payload = dict(payload)
        payload["briefing"] = briefing
    elif missing_briefing:
        raise ValueError("briefing is missing required section(s): " + ", ".join(sorted(missing_briefing)) + ".")
    for field in required_briefing:
        value = briefing[field]
        if field == "executive_takeaway":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("briefing.executive_takeaway must be nonempty text.")
        elif field == "metric_refs":
            if not isinstance(value, dict) or set(value) != required_briefing - {"metric_refs"}:
                raise ValueError("briefing.metric_refs must map every briefing section to an array of exact metric references.")
            for section, refs in value.items():
                if not isinstance(refs, list):
                    raise ValueError(f"briefing.metric_refs.{section}: expected an array of exact local metric IDs.")
                invalid_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in allowed_refs]
                if invalid_refs:
                    raise ValueError(f"briefing.metric_refs.{section}: invalid metric ID(s) {invalid_refs!r}; IDs must exactly match the finite available snapshot registry.")
        elif field == "transition_outlook":
            if not isinstance(value, dict):
                raise ValueError("briefing.transition_outlook must be a structured object.")
            required_transition_fields = {"current_regime_interpretation", "direction_classification",
                "emerging_direction", "scenarios", "confirmation_invalidation", "main_uncertainty",
                "python_alignment", "disagreement_explanation", "metric_refs"}
            if v5:
                required_transition_fields.add("turning_point_interpretations")
            if not required_transition_fields.issubset(value):
                raise ValueError("briefing.transition_outlook is missing required structured fields.")
            for key in ("current_regime_interpretation", "emerging_direction", "confirmation_invalidation", "main_uncertainty"):
                if not isinstance(value.get(key), str) or not value[key].strip():
                    raise ValueError(f"briefing.transition_outlook.{key} must be nonempty text.")
            if value.get("direction_classification") not in {"strengthening", "weakening", "potential reversal", "conflicting", "no clear emerging direction"}:
                raise ValueError("briefing.transition_outlook.direction_classification is invalid.")
            if value.get("python_alignment") not in {"agrees", "disagrees", "inconclusive"}:
                raise ValueError("briefing.transition_outlook.python_alignment must be agrees, disagrees, or inconclusive.")
            disagreement = value.get("disagreement_explanation")
            if not isinstance(disagreement, str):
                raise ValueError("briefing.transition_outlook.disagreement_explanation must be text.")
            if value["python_alignment"] == "disagrees" and not disagreement.strip():
                raise ValueError("briefing.transition_outlook.disagreement_explanation is required when AI differs from the Python-derived state.")
            scenarios = value.get("scenarios")
            allowed_scenarios = {"current_leadership_continues", "broader_risk_on_participation",
                "leadership_handoff_without_broad_improvement", "stress_spreading_across_sectors"}
            if not isinstance(scenarios, list) or len(scenarios) > 4:
                raise ValueError("briefing.transition_outlook.scenarios must be an array of at most four evidence-supported scenarios.")
            seen_scenarios = set()
            for index, scenario in enumerate(scenarios):
                if not isinstance(scenario, dict) or scenario.get("scenario_id") not in allowed_scenarios or scenario.get("scenario_id") in seen_scenarios:
                    raise ValueError(f"briefing.transition_outlook.scenarios[{index}].scenario_id is unknown or duplicated.")
                seen_scenarios.add(scenario["scenario_id"])
                if scenario.get("assessment") not in {"supported", "plausible but unconfirmed"}:
                    raise ValueError(f"briefing.transition_outlook.scenarios[{index}].assessment must identify evidence-supported or plausible-but-unconfirmed scenarios only.")
                for key in ("interpretation", "monitor_next"):
                    if not isinstance(scenario.get(key), str) or not scenario[key].strip():
                        raise ValueError(f"briefing.transition_outlook.scenarios[{index}].{key} must be nonempty text.")
                refs = scenario.get("metric_refs")
                if not isinstance(refs, list) or not refs:
                    raise ValueError(f"briefing.transition_outlook.scenarios[{index}].metric_refs must cite local evidence.")
                invalid_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in allowed_refs]
                if invalid_refs:
                    raise ValueError(f"briefing.transition_outlook.scenarios[{index}].metric_refs has invalid metric IDs {invalid_refs!r}.")
            refs = value.get("metric_refs")
            if not isinstance(refs, list):
                raise ValueError("briefing.transition_outlook.metric_refs must be an array of exact local metric IDs.")
            invalid_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in allowed_refs]
            if invalid_refs:
                raise ValueError(f"briefing.transition_outlook.metric_refs has invalid metric IDs {invalid_refs!r}.")
            if v5:
                turning_items = value.get("turning_point_interpretations")
                required_signal_ids = {"volatility_stabilization", "technology_mag7_recovery",
                    "broad_market_participation", "defensive_leadership"}
                if v6:
                    required_signal_ids.add("credit_confirmation")
                if not isinstance(turning_items, list):
                    raise ValueError("briefing.transition_outlook.turning_point_interpretations must be an array.")
                found_signal_ids = set()
                for index, item in enumerate(turning_items):
                    path = f"briefing.transition_outlook.turning_point_interpretations[{index}]"
                    if not isinstance(item, dict):
                        raise ValueError(f"{path} must be an object.")
                    signal_id = item.get("signal_id")
                    # Some DeepSeek responses omit the machine ID while still
                    # citing the signal's unique, canonical metric namespace.
                    # Recover only when the local metric references identify
                    # exactly one supported signal; never infer from prose or
                    # array position.
                    if not signal_id:
                        item_refs = item.get("metric_refs")
                        inferred_ids = {
                            candidate for candidate in required_signal_ids
                            if isinstance(item_refs, list) and any(
                                isinstance(ref, str) and
                                ref.startswith(f"sector_rotation.turning_point_{candidate}_")
                                for ref in item_refs
                            )
                        }
                        if len(inferred_ids) == 1:
                            signal_id = next(iter(inferred_ids))
                            item["signal_id"] = signal_id
                        elif len(inferred_ids) > 1:
                            raise ValueError(f"{path}.signal_id is missing and metric_refs point to multiple signals: {sorted(inferred_ids)!r}.")
                        else:
                            raise ValueError(f"{path}.signal_id is missing and cannot be inferred from signal-specific local metric references.")
                    if signal_id not in required_signal_ids:
                        raise ValueError(f"{path}.signal_id has unknown value {signal_id!r}.")
                    if signal_id in found_signal_ids:
                        raise ValueError(f"{path}.signal_id duplicates {signal_id!r}.")
                    found_signal_ids.add(signal_id)
                    for key in ("interpretation", "monitor_next"):
                        if not isinstance(item.get(key), str) or not item[key].strip():
                            raise ValueError(f"briefing.transition_outlook.turning_point_interpretations[{index}].{key} must be nonempty text.")
                    item_refs = item.get("metric_refs")
                    if not isinstance(item_refs, list) or not item_refs:
                        raise ValueError(f"briefing.transition_outlook.turning_point_interpretations[{index}].metric_refs must cite local metrics.")
                    bad_refs = [ref for ref in item_refs if not isinstance(ref, str) or ref not in allowed_refs]
                    if bad_refs:
                        raise ValueError(f"briefing.transition_outlook.turning_point_interpretations[{index}].metric_refs has invalid metric IDs {bad_refs!r}.")
                if found_signal_ids != required_signal_ids:
                    raise ValueError("briefing.transition_outlook.turning_point_interpretations must cover all deterministic signal IDs for this response version.")
        elif field == "hypotheses":
            if not isinstance(value, list) or len(value) > 3:
                raise ValueError("briefing.hypotheses must be an array of at most three evidence-led objects.")
            required_hypothesis_fields = {"title", "supporting_evidence", "competing_explanation",
                                          "confirm_or_invalidate", "sector_implications", "metric_refs"}
            for index, hypothesis in enumerate(value):
                if not isinstance(hypothesis, dict) or not required_hypothesis_fields.issubset(hypothesis):
                    raise ValueError(f"briefing.hypotheses[{index}] is missing required evidence fields.")
                for field in required_hypothesis_fields - {"metric_refs"}:
                    if not isinstance(hypothesis[field], str) or not hypothesis[field].strip():
                        raise ValueError(f"briefing.hypotheses[{index}].{field} must be nonempty text.")
                refs = hypothesis["metric_refs"]
                if not isinstance(refs, list) or not refs:
                    raise ValueError(f"briefing.hypotheses[{index}].metric_refs: expected one or more exact local metric IDs.")
                invalid_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in allowed_refs]
                if invalid_refs:
                    raise ValueError(f"briefing.hypotheses[{index}].metric_refs: invalid metric ID(s) {invalid_refs!r}.")
        elif not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"briefing.{field} must be an array of strings.")
    if briefing["executive_takeaway"].strip() and not briefing["metric_refs"].get("executive_takeaway"):
        raise ValueError("briefing.metric_refs.executive_takeaway must cite local metrics for the takeaway.")
    for key in required_briefing - {"executive_takeaway", "metric_refs"}:
        if briefing[key] and not briefing["metric_refs"].get(key):
            raise ValueError(f"briefing.metric_refs.{key} must cite local metrics when that section contains claims.")
    comparison_highlights = payload.get("comparison_highlights")
    if not legacy:
        relative_section = (snapshot.get("sections") or {}).get("relative_strength") or {}
        expected_comparisons = set(relative_section.get("ai_comparison_ids") or relative_section.get("comparisons", {}))
        if not isinstance(comparison_highlights, list):
            raise ValueError("comparison_highlights must be an array.")
        found = set()
        for index, item in enumerate(comparison_highlights):
            if not isinstance(item, dict) or item.get("comparison_id") not in expected_comparisons or item.get("comparison_id") in found:
                raise ValueError(f"comparison_highlights[{index}].comparison_id is unknown or duplicated.")
            found.add(item["comparison_id"])
            if item.get("category") not in {"constructive", "caution", "mixed", "insufficient evidence"}:
                raise ValueError(f"comparison_highlights[{index}].category is invalid.")
            for field in ("headline", "insight", "monitor_next"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ValueError(f"comparison_highlights[{index}].{field} must be nonempty text.")
            refs = item.get("metric_refs")
            if not isinstance(refs, list):
                raise ValueError(f"comparison_highlights[{index}].metric_refs: expected an array of exact local metric IDs.")
            invalid_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in allowed_refs]
            if invalid_refs:
                raise ValueError(f"comparison_highlights[{index}].metric_refs: invalid metric ID(s) {invalid_refs!r}.")
        if found != expected_comparisons:
            raise ValueError("comparison_highlights must cover every standard comparison ID in the snapshot.")
    source_ids = {f"S{i}" for i, source in enumerate(sources, 1) if isinstance(source, dict) and source.get("url")}
    import re
    text_fields = []
    for item in highlights:
        text_fields.extend(item[field] for field in ("headline", "insight", "why_it_matters", "monitor_next"))
    text_fields.append(briefing["executive_takeaway"])
    if v4:
        transition = briefing["transition_outlook"]
        text_fields.extend(transition[key] for key in ("current_regime_interpretation", "emerging_direction",
            "confirmation_invalidation", "main_uncertainty", "disagreement_explanation"))
        for scenario in transition["scenarios"]:
            text_fields.extend(scenario[key] for key in ("interpretation", "monitor_next"))
        for item in transition.get("turning_point_interpretations", []):
            text_fields.extend(item[key] for key in ("interpretation", "monitor_next"))
    for key in required_briefing - {"executive_takeaway", "metric_refs"}:
        if key == "hypotheses":
            text_fields.extend(value for hypothesis in briefing[key] for value in hypothesis.values() if isinstance(value, str))
        elif key == "transition_outlook":
            continue
        else:
            text_fields.extend(briefing[key])
    for item in comparison_highlights or []:
        text_fields.extend(item[field] for field in ("headline", "insight", "monitor_next"))
    for text in text_fields:
        for source_ref in re.findall(r"\[(S\d+)\]", text):
            if source_ref not in source_ids:
                raise ValueError(f"Unknown external source citation [{source_ref}].")
    return payload


def parse_market_analysis_json(text: str, snapshot: dict, sources=()) -> dict:
    import json
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("The analyst response was not valid JSON.") from exc
    return validate_market_analysis(payload, snapshot, sources)


def analysis_to_markdown(payload: dict) -> str:
    """Stable display/persistence representation of validated structured output."""
    briefing = payload["briefing"]
    headings = {
        "executive_takeaway": "Market regime / key takeaway", "risks": "Top 3 accumulating risks",
        "supportive": "Top 3 supportive developments", "sector_implications": "Sector rotation implications",
        "catalysts": "Key market catalysts", "conflicting_evidence": "Conflicting evidence",
        "conditions": "Conditions that would change the assessment", "hypotheses": "Main hypotheses · support, alternatives, and tests",
    }
    lines = []
    for key, title in headings.items():
        if key not in briefing:
            continue  # v1 saved assessments predate the structured hypothesis field.
        lines.append(f"## {title}")
        value = briefing[key]
        if isinstance(value, str):
            lines.append(f"- {value}")
        elif key == "hypotheses":
            lines.extend(f"- **{item['title']}** · Supporting: {item['supporting_evidence']} · Competing: {item['competing_explanation']} · Test: {item['confirm_or_invalidate']} · Implications: {item.get('sector_implications') or 'Not specified in the saved response.'}" for item in value)
        else:
            lines.extend(f"- {item}" for item in value)
        lines.append("")
    if "transition_outlook" in briefing:
        transition = briefing["transition_outlook"]
        lines.extend(("## Market Regime Transition Outlook",
            f"- Python/AI alignment: {transition.get('python_alignment', 'inconclusive')}",
            f"- Current regime: {transition.get('current_regime_interpretation', '')}",
            f"- Emerging direction: {transition.get('emerging_direction', '')}"))
        lines.extend(f"- Scenario ({item['assessment']}): {item['interpretation']} · Monitor: {item['monitor_next']}"
                     for item in transition.get("scenarios", []))
        lines.extend((f"- Confirmation / invalidation: {transition.get('confirmation_invalidation', '')}",
                      f"- Main uncertainty: {transition.get('main_uncertainty', '')}", ""))
        for item in transition.get("turning_point_interpretations", []):
            lines.append(f"- Turning-point interpretation ({item['signal_id']}): {item['interpretation']} · Monitor: {item['monitor_next']}")
        if transition.get("turning_point_interpretations"):
            lines.append("")
    return "\n".join(lines).strip()


def highlights_from_analysis(payload: dict) -> dict[str, dict]:
    return {item["section_id"]: deepcopy(item) for item in payload["highlights"]}

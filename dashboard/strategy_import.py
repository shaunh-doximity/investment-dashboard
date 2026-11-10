"""Local validation and approval helpers for ChatGPT strategy exports.

Imported JSON is treated as untrusted text. It is parsed as data only; URLs are
retained as references and are never fetched by this module.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from math import isfinite
import re
from urllib.parse import urlparse

from dashboard.data_access import (
    get_strategy_record, save_research_strategy_scenarios, save_strategy_record,
)
from dashboard.setup_calculations import calculate_long_setup

SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3}
HORIZONS = ("short_term", "medium_term", "long_term")
INVESTMENT_LOGIC_STATUSES = {
    "Verified", "User assumption", "Analyst inference", "Missing evidence",
    "Imported / unverified",
}
HORIZON_LABELS = {
    "short_term": "Swing / short-term",
    "medium_term": "Medium-term",
    "long_term": "Long-term",
}
TOP_LEVEL_TEXT_FIELDS = (
    "company_name", "strategy_type", "fundamental_analysis", "valuation_analysis",
    "technical_analysis", "bull_case", "base_case", "bear_case", "catalysts",
    "risks", "uncertainties", "research_gaps",
)
SCENARIO_TEXT_FIELDS = (
    "thesis", "holding_horizon", "entry_conditions", "fundamental_invalidation",
    "invalidation_conditions", "bull_case", "base_case", "bear_case",
    "catalysts", "risks", "evidence", "assumptions", "uncertainty",
)
SCENARIO_PRICE_FIELDS = (
    "entry_price_low", "entry_price_high", "target_price_low", "target_price_high", "stop_price",
)
STRATEGY_IMPORT_SCHEMA = {
    "schema_version": "integer, required; currently 3 (versions 1 and 2 remain supported)",
    "ticker": "string, required; must match the open workspace",
    "company_name": "string or null, optional; user supplied unless independently verified elsewhere",
    "research_date": "YYYY-MM-DD string or null",
    "strategy_type": "string or null",
    "investment_thesis": "non-empty string (structured summary/reasoning is normalized when unambiguous)",
    "fundamental_analysis": "string or null",
    "valuation_analysis": "string or null",
    "technical_analysis": "string or null",
    "bull_case": "string or null", "base_case": "string or null", "bear_case": "string or null",
    "catalysts": "string or null", "risks": "string or null",
    "uncertainties": "string or null", "research_gaps": "string or null",
    "market_price_reference": "object or null: price (positive number/null), date (YYYY-MM-DD/null), basis (string), source (string)",
    "evidence": "array of objects: title (string/null), url (absolute http/https string), source (string/null), as_of (YYYY-MM-DD/null)",
    "scenarios": "object, required keys: short_term, medium_term, long_term; each value is a scenario object",
    "investment_logic": "optional object or null with bullish_drivers, bearish_risks, catalysts, invalidation_signals arrays (max 3 each); each item has label, explanation, evidence_status, and details",
    "position_sizing": "optional object or null with allocation policy proposals and optional probability_estimate {probability (0–1/null), range ([low,high]/null), source, method, evidence}; probabilities remain unverified proposals and are never auto-promoted as sizing inputs; no imported quantities/calculations are authoritative",
}


def _received(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _problem(path: str, expected: str, value, suggestion: str) -> ValueError:
    return ValueError(
        f"{path}: expected {expected}; received {_received(value)}. {suggestion}"
    )


def _url_string(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalization_record() -> dict:
    return {"changes": [], "review_flags": [], "preserved_unrecognized": {}}


def _note(meta: dict, path: str, action: str) -> None:
    meta["changes"].append({"path": path, "action": action})


def _review(meta: dict, path: str, reason: str, original) -> None:
    meta["review_flags"].append({"path": path, "reason": reason})
    meta["preserved_unrecognized"][path] = original


def _alias_fields(source: dict, aliases: dict[str, tuple[str, ...]], meta: dict, prefix: str = "") -> dict:
    result = dict(source)
    for canonical, alternatives in aliases.items():
        present = [name for name in alternatives if name in result]
        if canonical not in result and present:
            chosen = present[0]
            result[canonical] = result.pop(chosen)
            _note(meta, f"{prefix}{chosen}", f"mapped to {prefix}{canonical}")
            present = present[1:]
        for alias in present:
            value = result.pop(alias)
            if value != result.get(canonical):
                _review(meta, f"{prefix}{alias}", f"conflicts with canonical field {prefix}{canonical}; canonical value kept", value)
            else:
                _note(meta, f"{prefix}{alias}", f"duplicate of {prefix}{canonical}; canonical value kept")
    return result


def _normalize_numeric_strings(value, path: str, meta: dict):
    if value is None or value == "":
        if value == "":
            _note(meta, path, "empty optional numeric value converted to null")
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if re.fullmatch(r"\$?\s*[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", candidate):
            number = float(candidate.replace("$", "").replace(",", "").strip())
            _note(meta, path, "numeric string converted to JSON number")
            return number
    return value


def _normalize_thesis(value, meta: dict):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        known = ("summary", "thesis", "conclusion", "reasoning", "rationale")
        parts = []
        unknown = {}
        for key in known:
            item = value.get(key)
            if item is not None:
                if isinstance(item, str):
                    if item.strip():
                        parts.append(f"{key.title()}: {item.strip()}")
                else:
                    unknown[key] = item
        unknown.update({key: item for key, item in value.items() if key not in known})
        if parts:
            _note(meta, "investment_thesis", "structured summary/reasoning combined into canonical text")
            if unknown:
                _review(meta, "investment_thesis.unrecognized", "unrecognized nested thesis fields preserved for review", unknown)
            return "\n\n".join(parts)
    return value


def _normalize_evidence_item(item, path: str, meta: dict, title_hint: str = ""):
    if isinstance(item, str):
        if _url_string(item):
            _note(meta, path, "URL string converted to canonical evidence object")
            return {"title": title_hint, "url": item.strip(), "source": "", "as_of": None}
        _review(meta, path, "text is not a recognizable URL; preserved for review", item)
        return None
    if not isinstance(item, dict):
        _review(meta, path, "evidence entry is not a recognized URL or object; preserved for review", item)
        return None

    entry = dict(item)
    url_keys = ("url", "source_url", "citation_url", "link", "href", "uri")
    # A URL-shaped canonical field is not an invitation to drop an invalid
    # source. Keep it in the canonical slot so strict validation can report
    # the precise field path and reject unsafe schemes such as javascript:.
    for key in url_keys:
        candidate = entry.get(key)
        if isinstance(candidate, str) and candidate.strip() and not _url_string(candidate):
            if key != "url" and "url" not in entry:
                entry["url"] = candidate.strip()
                _note(meta, f"{path}.{key}", "mapped to canonical url field for strict validation")
            result = {
                "title": entry.get("title") or entry.get("name") or title_hint,
                "url": entry.get("url", candidate.strip()),
                "source": entry.get("source") or entry.get("publisher") or "",
                "as_of": entry.get("as_of", entry.get("date")),
            }
            return result
    url_candidates = []
    for key in url_keys:
        candidate = entry.get(key)
        if isinstance(candidate, str) and _url_string(candidate):
            url_candidates.append((key, candidate.strip()))
        elif isinstance(candidate, dict):
            nested = [candidate.get(name) for name in ("url", "href", "link", "value")]
            nested_urls = [part.strip() for part in nested if isinstance(part, str) and _url_string(part)]
            if len(set(nested_urls)) == 1:
                url_candidates.append((key, nested_urls[0]))
                _note(meta, f"{path}.{key}", "nested URL object reduced to its identifiable URL string")
                nested_unknown = {name: part for name, part in candidate.items() if name not in {"url", "href", "link", "value"}}
                if nested_unknown:
                    _review(meta, f"{path}.{key}.unrecognized", "unrecognized nested URL fields preserved for review", nested_unknown)
            elif len(set(nested_urls)) > 1:
                _review(meta, f"{path}.{key}", "multiple URL values found; no URL selected", candidate)
    if isinstance(entry.get("source"), str) and _url_string(entry["source"]):
        url_candidates.append(("source", entry["source"].strip()))
    if not url_candidates:
        nested_values = []

        def collect_urls(value, current_path):
            if isinstance(value, str) and _url_string(value):
                nested_values.append((current_path, value.strip()))
            elif isinstance(value, dict):
                for child_key, child in value.items():
                    collect_urls(child, f"{current_path}.{child_key}")
            elif isinstance(value, list):
                for child_index, child in enumerate(value):
                    collect_urls(child, f"{current_path}[{child_index}]")

        collect_urls(entry, path)
        distinct_nested = list(dict.fromkeys(value for _, value in nested_values))
        if len(distinct_nested) == 1:
            url_candidates.append((nested_values[0][0], distinct_nested[0]))
            _note(meta, path, "one unambiguous nested URL string extracted")
        elif len(distinct_nested) > 1:
            _review(meta, path, "multiple nested URL strings found; no URL selected", item)
            return None
    unique_urls = list(dict.fromkeys(url for _, url in url_candidates))
    if len(unique_urls) > 1:
        _review(meta, path, "multiple distinct URLs found in one evidence entry; no URL selected", item)
        return None
    if not unique_urls:
        _review(meta, path, "no recognizable http/https URL found; full evidence object preserved", item)
        return None
    url = unique_urls[0]
    title = entry.get("title") or entry.get("name") or title_hint
    source = entry.get("source") or entry.get("publisher") or ""
    as_of = entry.get("as_of", entry.get("date"))
    result = {"title": title, "url": url, "source": source, "as_of": as_of}
    consumed = set(url_keys) | {"title", "name", "source", "publisher", "as_of", "date"}
    unknown = {key: value for key, value in entry.items() if key not in consumed}
    if unknown:
        _review(meta, f"{path}.unrecognized", "unrecognized evidence fields preserved for review", unknown)
    if url_candidates and url_candidates[0][0] != "url":
        _note(meta, f"{path}.{url_candidates[0][0]}", "mapped to canonical url field")
    if "name" in entry or "publisher" in entry or "date" in entry:
        _note(meta, path, "compatible evidence aliases mapped to canonical fields")
    return result


def _normalize_evidence(value, meta: dict):
    if value is None or value == "":
        if value is not None:
            _note(meta, "evidence", "empty evidence converted to an empty list")
        return []
    if isinstance(value, str):
        normalized = _normalize_evidence_item(value, "evidence", meta)
        return [normalized] if normalized else []
    if isinstance(value, dict):
        # A single reference object is distinct from a keyed collection of references.
        if any(key in value for key in ("url", "source_url", "citation_url", "link", "href")):
            _note(meta, "evidence", "single evidence object wrapped in canonical array")
            normalized = _normalize_evidence_item(value, "evidence[0]", meta)
            return [normalized] if normalized else []
        refs = []
        unknown = {}
        for key, item in value.items():
            if key in {"sources", "references", "citations"} and isinstance(item, (list, dict)):
                candidates = list(enumerate(item)) if isinstance(item, list) else list(item.items())
            else:
                candidates = [(key, item)]
            for label, candidate in candidates:
                ref = _normalize_evidence_item(candidate, f"evidence.{label}", meta, str(label))
                if ref:
                    refs.append(ref)
                else:
                    unknown[f"{key}.{label}"] = candidate
        if unknown:
            meta["preserved_unrecognized"]["evidence"] = unknown
            if not any(flag["path"] == "evidence" for flag in meta["review_flags"]):
                meta["review_flags"].append({"path": "evidence", "reason": "some evidence content could not be mapped to source references; preserved for review"})
        _note(meta, "evidence", "keyed evidence collection normalized to canonical array")
        return refs
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            ref = _normalize_evidence_item(item, f"evidence[{index}]", meta)
            if ref:
                result.append(ref)
        return result
    _review(meta, "evidence", "expected a reference, object, or array; original content preserved", value)
    return []


def normalize_strategy_import(raw_json: str) -> dict:
    """Loss-aware adapter from common ChatGPT JSON shapes to canonical schema v1."""
    def reject_non_json_constant(value):
        raise ValueError(f"Invalid JSON number {value}.")

    try:
        payload = json.loads(raw_json, parse_constant=reject_non_json_constant)
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("$: expected a valid JSON object; received malformed JSON. Check quotes, commas, and null syntax.") from exc
    if not isinstance(payload, dict):
        raise _problem("$", "JSON object", payload, "Paste one strategy object enclosed in braces.")

    meta = _normalization_record()
    aliases = {
        "research_date": ("research_as_of", "analysis_date"),
        "scenarios": ("horizon_scenarios", "strategy_scenarios"),
        "strategy_type": ("strategy", "setup_type"),
        "investment_thesis": ("thesis", "investment_summary"),
        "fundamental_analysis": ("fundamental_summary", "fundamental_notes"),
        "valuation_analysis": ("valuation_summary", "valuation_notes"),
        "technical_analysis": ("technical_summary", "technical_notes"),
        "bull_case": ("bullish_case", "bullish_scenario"),
        "base_case": ("base_scenario",),
        "bear_case": ("bearish_case", "bearish_scenario"),
        "catalysts": ("key_catalysts",),
        "risks": ("key_risks",),
        "uncertainties": ("unknowns", "research_uncertainties"),
        "investment_logic": ("logic_map",),
    }
    normalized = _alias_fields(payload, aliases, meta)
    if "investment_thesis" in normalized:
        normalized["investment_thesis"] = _normalize_thesis(normalized["investment_thesis"], meta)

    evidence_value = normalized.pop("evidence", [])
    normalized["evidence"] = _normalize_evidence(evidence_value, meta)

    scenario_value = normalized.get("scenarios")
    if isinstance(scenario_value, list):
        expanded_scenarios = {}
        aliases_by_horizon = {
            "short_term": {"short_term", "short", "swing", "swing / short-term", "short-term"},
            "medium_term": {"medium_term", "medium", "medium-term"},
            "long_term": {"long_term", "long", "long-term"},
        }
        ambiguous_items = []
        for index, item in enumerate(scenario_value):
            if not isinstance(item, dict):
                ambiguous_items.append(item)
                continue
            horizon_value = item.get("horizon", item.get("time_horizon", item.get("term")))
            normalized_horizon = str(horizon_value or "").strip().lower()
            matched = [key for key, aliases in aliases_by_horizon.items() if normalized_horizon in aliases]
            if len(matched) != 1 or matched[0] in expanded_scenarios:
                ambiguous_items.append(item)
                continue
            expanded_scenarios[matched[0]] = {key: value for key, value in item.items() if key not in {"horizon", "time_horizon", "term"}}
            _note(meta, f"scenarios[{index}]", f"mapped to scenarios.{matched[0]}")
        if ambiguous_items:
            _review(meta, "scenarios.unmapped_array_items", "scenario array items have missing, duplicate, or unclear horizon labels; preserved for review", ambiguous_items)
        scenario_value = expanded_scenarios
        normalized["scenarios"] = scenario_value
    if isinstance(scenario_value, dict):
        horizon_aliases = {
            "short_term": ("short", "swing", "swing_short_term", "short-term"),
            "medium_term": ("medium", "medium-term"),
            "long_term": ("long", "long-term"),
        }
        scenarios = dict(scenario_value)
        for canonical, alternatives in horizon_aliases.items():
            found = [key for key in alternatives if key in scenarios]
            if canonical not in scenarios and found:
                source_key = found.pop(0)
                scenarios[canonical] = scenarios.pop(source_key)
                _note(meta, f"scenarios.{source_key}", f"mapped to scenarios.{canonical}")
            for alias in found:
                extra = scenarios.pop(alias)
                if extra != scenarios.get(canonical):
                    _review(meta, f"scenarios.{alias}", f"conflicts with scenarios.{canonical}; canonical value kept", extra)
        for horizon in HORIZONS:
            scenario = scenarios.get(horizon)
            if isinstance(scenario, dict):
                scenario_aliases = {
                    "holding_horizon": ("holding_period", "expected_holding_period"),
                    "entry_price_low": ("entry_low",), "entry_price_high": ("entry_high",),
                    "target_price_low": ("target_low",), "target_price_high": ("target_high",),
                    "stop_price": ("stop_loss", "invalidation_price", "stop"),
                    "invalidation_conditions": ("invalidation", "thesis_invalidation"),
                    "bull_case": ("bullish_case",), "bear_case": ("bearish_case",),
                    "evidence": ("supporting_evidence",), "uncertainty": ("uncertainties",),
                    "entry_price": ("entry",), "target_price": ("target",),
                }
                scenario = _alias_fields(scenario, scenario_aliases, meta, f"scenarios.{horizon}.")
                for range_name, low_name, high_name in (
                    ("entry_range", "entry_price_low", "entry_price_high"),
                    ("target_range", "target_price_low", "target_price_high"),
                ):
                    if range_name in scenario:
                        pair = scenario.pop(range_name)
                        if isinstance(pair, (list, tuple)) and len(pair) == 2 and low_name not in scenario and high_name not in scenario:
                            scenario[low_name], scenario[high_name] = pair
                            _note(meta, f"scenarios.{horizon}.{range_name}", f"two-value range mapped to {low_name}/{high_name}")
                        else:
                            _review(meta, f"scenarios.{horizon}.{range_name}", "range could not be mapped without guessing; original value preserved", pair)
                if "entry_price" in scenario:
                    price = scenario.pop("entry_price")
                    if "entry_price_low" not in scenario and "entry_price_high" not in scenario:
                        scenario["entry_price_low"] = price
                        scenario["entry_price_high"] = price
                        _note(meta, f"scenarios.{horizon}.entry_price", "single price expanded to equal entry bounds")
                    else:
                        _review(meta, f"scenarios.{horizon}.entry_price", "conflicts with explicit entry bounds; preserved for review", price)
                if "target_price" in scenario:
                    price = scenario.pop("target_price")
                    if "target_price_low" not in scenario and "target_price_high" not in scenario:
                        scenario["target_price_low"] = price
                        scenario["target_price_high"] = price
                        _note(meta, f"scenarios.{horizon}.target_price", "single price expanded to equal target bounds")
                    else:
                        _review(meta, f"scenarios.{horizon}.target_price", "conflicts with explicit target bounds; preserved for review", price)
                for field in SCENARIO_PRICE_FIELDS:
                    if field in scenario:
                        scenario[field] = _normalize_numeric_strings(scenario[field], f"scenarios.{horizon}.{field}", meta)
                recognized = set(SCENARIO_TEXT_FIELDS) | set(SCENARIO_PRICE_FIELDS) | {"horizon"}
                extra = {key: value for key, value in scenario.items() if key not in recognized}
                if extra:
                    _review(meta, f"scenarios.{horizon}.unrecognized", "unrecognized scenario fields preserved for review", extra)
                scenarios[horizon] = scenario
            elif scenario is not None:
                _review(meta, f"scenarios.{horizon}", "scenario is not an object; original content preserved", scenario)
        normalized["scenarios"] = scenarios
    elif scenario_value is not None:
        _review(meta, "scenarios", "scenario container is not an object; original content preserved", scenario_value)

    if isinstance(normalized.get("market_price_reference"), dict):
        ref = dict(normalized["market_price_reference"])
        if "price" in ref:
            ref["price"] = _normalize_numeric_strings(ref["price"], "market_price_reference.price", meta)
        normalized["market_price_reference"] = ref

    known = {
        "schema_version", "ticker", "company_name", "research_date", "strategy_type",
        "investment_thesis", "fundamental_analysis", "valuation_analysis", "technical_analysis",
        "bull_case", "base_case", "bear_case", "catalysts", "risks", "uncertainties",
        "research_gaps", "market_price_reference", "evidence", "scenarios",
        "investment_logic", "position_sizing",
    }
    extras = {key: value for key, value in normalized.items() if key not in known}
    if extras:
        _review(meta, "root.unrecognized", "unrecognized root fields preserved for review", extras)
    normalized = {key: value for key, value in normalized.items() if key in known}
    normalized["_normalization"] = meta
    return normalized


def _text(value, field: str, *, optional: bool = True) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        expected = "text string or null" if optional else "text string"
        correction = "Provide readable text in quotes, or use null for an optional field." if optional else "Provide the thesis as a non-empty string in quotes."
        raise _problem(field, expected, value, correction)
    return value.strip()


def _price(value, field: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise _problem(field, "positive JSON number or null", value, "Use a number such as 125.50, or null when unknown.")
    if not isfinite(float(value)) or value <= 0:
        raise ValueError(f"{field}: expected a finite positive price or null; received {_received(value)}. Use a price greater than zero, or null when unknown.")
    return float(value)


def _validate_urls(value, field: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _problem(field, "array of evidence objects", value, "Use [{\"title\": \"...\", \"url\": \"https://...\"}].")
    clean = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _problem(f"{field}[{index}]", "evidence object", item, "Use an object with a URL string, for example {\"url\": \"https://...\"}.")
        path = f"{field}[{index}].url"
        url = item.get("url")
        if not isinstance(url, str):
            raise _problem(path, "URL string", url, "Please use the source URL as text, for example \"https://example.org/report\".")
        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{path}: expected an absolute http/https URL; received string. Please use the source URL as text.")
        clean.append({
            "title": _text(item.get("title"), f"{field}[{index}].title"),
            "url": url,
            "source": _text(item.get("source"), f"{field}[{index}].source"),
            "as_of": _date_or_empty(item.get("as_of"), f"{field}[{index}].as_of"),
        })
    return clean


def _validate_investment_logic(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _problem("investment_logic", "object or null", value,
                       "Use an object containing the four named arrays, or null if not supplied.")
    groups = ("bullish_drivers", "bearish_risks", "catalysts", "invalidation_signals")
    extra = set(value) - set(groups)
    if extra:
        raise _problem(f"investment_logic.{sorted(extra)[0]}", "one of the four named arrays", value[sorted(extra)[0]],
                       "Use bullish_drivers, bearish_risks, catalysts, or invalidation_signals.")
    result = {}
    for group in groups:
        raw_items = value.get(group, [])
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, list):
            raise _problem(f"investment_logic.{group}", "array of logic items", raw_items,
                           "Use an array of objects with label, explanation, evidence_status, and details.")
        if len(raw_items) > 3:
            raise ValueError(f"investment_logic.{group}: expected at most 3 items; received {len(raw_items)}. Keep the strongest three points.")
        items = []
        for index, item in enumerate(raw_items):
            path = f"investment_logic.{group}[{index}]"
            if not isinstance(item, dict):
                raise _problem(path, "logic item object", item,
                               "Use {\"label\": \"...\", \"explanation\": \"...\", \"evidence_status\": \"Imported / unverified\", \"details\": null}.")
            unknown = set(item) - {"label", "explanation", "evidence_status", "details"}
            if unknown:
                field = sorted(unknown)[0]
                raise _problem(f"{path}.{field}", "label, explanation, evidence_status, or details field", item[field],
                               "Move extra research text to details or a scenario field so it remains visible.")
            label = _text(item.get("label"), f"{path}.label", optional=False)
            explanation = _text(item.get("explanation"), f"{path}.explanation", optional=False)
            if not label or not explanation:
                raise ValueError(f"{path}: expected non-empty label and explanation; received empty text. Add a short label and its causal explanation.")
            status = _text(item.get("evidence_status", "Imported / unverified"), f"{path}.evidence_status", optional=False)
            if status not in INVESTMENT_LOGIC_STATUSES:
                raise ValueError(f"{path}.evidence_status: expected one of {', '.join(sorted(INVESTMENT_LOGIC_STATUSES))}; received {status!r}. Use Imported / unverified unless a connected source independently verifies the claim.")
            items.append({
                "label": label,
                "explanation": explanation,
                "evidence_status": status,
                "details": _text(item.get("details"), f"{path}.details"),
            })
        result[group] = items
    return result


def _date_or_empty(value, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise _problem(field, "ISO date string (YYYY-MM-DD) or null", value, "Use a date like \"2026-10-25\", or null if unknown.")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field}: expected ISO date string (YYYY-MM-DD); received string. Use a date like \"2026-10-25\".")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field}: expected a real calendar date in YYYY-MM-DD format; received string. Correct the date and retry.") from exc


def _normalize_scenario(key: str, value) -> dict:
    if not isinstance(value, dict):
        raise _problem(f"scenarios.{key}", "scenario object", value, "Use an object with this horizon's thesis, price levels, and invalidation fields.")
    result = {"horizon": HORIZON_LABELS[key]}
    text_fields = (
        "thesis", "holding_horizon", "entry_conditions", "fundamental_invalidation",
        "invalidation_conditions", "bull_case", "base_case", "bear_case",
        "catalysts", "risks", "evidence", "assumptions", "uncertainty",
    )
    for field in text_fields:
        result[field] = _text(value.get(field), f"scenarios.{key}.{field}")
    for field in ("entry_price_low", "entry_price_high", "target_price_low", "target_price_high", "stop_price"):
        result[field] = _price(value.get(field), f"scenarios.{key}.{field}")
    if (result["entry_price_low"] is None) != (result["entry_price_high"] is None):
        raise ValueError(f"scenarios.{key}.entry_price_low/high: expected both prices or both null; received incomplete range. Set both bounds, or set both to null.")
    if (result["target_price_low"] is None) != (result["target_price_high"] is None):
        raise ValueError(f"scenarios.{key}.target_price_low/high: expected both prices or both null; received incomplete range. Set both bounds, or set both to null.")
    for low_key, high_key, label in (("entry_price_low", "entry_price_high", "entry"),
                                     ("target_price_low", "target_price_high", "target")):
        low, high = result[low_key], result[high_key]
        if low is not None and low > high:
            raise ValueError(f"scenarios.{key}.{label}_price_low/high: expected lower bound <= upper bound; received reversed range. Correct the two values.")
    return result


def validate_normalized_strategy(payload: dict, expected_ticker: str) -> dict:
    """Strictly validate normalized canonical fields and return storage-ready data."""
    if not isinstance(payload, dict):
        raise _problem("$", "normalized strategy object", payload, "Validate a JSON object after normalization.")
    version = payload.get("schema_version")
    if isinstance(version, bool) or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"schema_version: expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}; received {_received(version)}. Use {SCHEMA_VERSION} for new exports.")
    ticker = payload.get("ticker")
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,14}", ticker.strip()):
        raise _problem("ticker", "ticker string", ticker, "Use the stock or ETF symbol for this workspace, such as \"MCD\".")
    ticker = ticker.strip().upper()
    expected = expected_ticker.strip().upper()
    if ticker != expected:
        raise ValueError(f"ticker: expected {expected} to match this workspace; received {ticker}. Open the matching ticker workspace or correct the export.")
    research_date = _date_or_empty(payload.get("research_date"), "research_date")
    if research_date and date.fromisoformat(research_date) > date.today():
        raise ValueError("research_date: expected today or an earlier date; received a future date. Correct the research date.")
    strategy_type = _text(payload.get("strategy_type"), "strategy_type")
    raw_thesis = payload.get("investment_thesis")
    thesis = _text(raw_thesis, "investment_thesis", optional=False)
    if not thesis:
        raise ValueError("investment_thesis: expected non-empty text; received empty string. Add the strategy's central thesis.")
    scenarios_payload = payload.get("scenarios")
    if not isinstance(scenarios_payload, dict):
        raise _problem("scenarios", "object keyed by short_term, medium_term, and long_term", scenarios_payload, "Provide all three horizon objects; price fields may be null.")
    scenarios = {}
    for key in HORIZONS:
        if key not in scenarios_payload:
            raise ValueError(f"scenarios.{key}: expected scenario object; received missing field. Include this horizon and use null for unsupported prices.")
        raw_scenario = scenarios_payload[key]
        if isinstance(raw_scenario, dict):
            for field in SCENARIO_TEXT_FIELDS:
                if field in raw_scenario:
                    _text(raw_scenario[field], f"scenarios.{key}.{field}")
            for field in SCENARIO_PRICE_FIELDS:
                if field in raw_scenario:
                    _price(raw_scenario[field], f"scenarios.{key}.{field}")
        scenarios[key] = _normalize_scenario(key, raw_scenario)
    refs = _validate_urls(payload.get("evidence", []), "evidence")
    investment_logic = _validate_investment_logic(payload.get("investment_logic"))
    position_sizing = _validate_position_sizing(payload.get("position_sizing"))
    ref_market = payload.get("market_price_reference")
    if ref_market is not None:
        if not isinstance(ref_market, dict):
            raise _problem("market_price_reference", "object or null", ref_market, "Use {\"price\": null, \"date\": null, \"basis\": \"\", \"source\": \"\"}.")
        ref_market = {
            "price": _price(ref_market.get("price"), "market_price_reference.price"),
            "date": _date_or_empty(ref_market.get("date"), "market_price_reference.date"),
            "basis": _text(ref_market.get("basis"), "market_price_reference.basis"),
            "source": _text(ref_market.get("source"), "market_price_reference.source"),
        }
        if ref_market["date"] and date.fromisoformat(ref_market["date"]) > date.today():
            raise ValueError("market_price_reference.date: expected today or an earlier date; received a future date. Correct the reference date.")
    result = {
        "schema_version": SCHEMA_VERSION,
        "ticker": ticker,
        "company_name": _text(payload.get("company_name"), "company_name"),
        "research_date": research_date,
        "strategy_type": strategy_type,
        "investment_thesis": thesis,
        "fundamental_analysis": _text(payload.get("fundamental_analysis"), "fundamental_analysis"),
        "valuation_analysis": _text(payload.get("valuation_analysis"), "valuation_analysis"),
        "technical_analysis": _text(payload.get("technical_analysis"), "technical_analysis"),
        "bull_case": _text(payload.get("bull_case"), "bull_case"),
        "base_case": _text(payload.get("base_case"), "base_case"),
        "bear_case": _text(payload.get("bear_case"), "bear_case"),
        "catalysts": _text(payload.get("catalysts"), "catalysts"),
        "risks": _text(payload.get("risks"), "risks"),
        "uncertainties": _text(payload.get("uncertainties"), "uncertainties"),
        "research_gaps": _text(payload.get("research_gaps"), "research_gaps"),
        "evidence": refs,
        "investment_logic": investment_logic,
        "position_sizing": position_sizing,
        "market_price_reference": ref_market,
        "scenarios": scenarios,
    }
    result["normalization_metadata"] = deepcopy(payload.get("_normalization", _normalization_record()))
    return result


def _validate_position_sizing(value):
    """Validate policy proposals only; imported quantities/calculations are rejected."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _problem("position_sizing", "object or null", value,
                       "Use fractions (0.08 means 8%) and optional tranche arrays.")
    allowed = {"target_allocation", "target_allocation_range", "maximum_allocation", "maximum_loss_fraction",
               "entry_tranches", "exit_tranches", "allocation_rationale", "assumptions",
               "constraints", "evidence", "uncertainty", "probability_estimate"}
    forbidden = {"shares", "quantity", "dollar_amount", "risk_dollars", "rrr", "planned_rrr"}
    extra = set(value) - allowed
    if extra:
        name = sorted(extra)[0]
        if name in forbidden:
            raise ValueError(f"position_sizing.{name}: imported quantities and calculations are not authoritative; the application recalculates them from current portfolio data.")
        raise _problem(f"position_sizing.{name}", "a supported position-sizing field", value[name],
                       "Remove unsupported fields or preserve them in allocation_rationale text for review.")
    result = {}
    raw_probability = value.get("probability_estimate")
    if raw_probability is None:
        result["probability_estimate"] = None
    else:
        if not isinstance(raw_probability, dict):
            raise _problem("position_sizing.probability_estimate", "object or null", raw_probability,
                           "Use probability, range, source, method, and evidence fields.")
        allowed_probability = {"probability", "range", "source", "method", "evidence"}
        unknown_probability = set(raw_probability) - allowed_probability
        if unknown_probability:
            field = sorted(unknown_probability)[0]
            raise _problem(f"position_sizing.probability_estimate.{field}", "supported field", raw_probability[field],
                           "Remove unsupported fields; this estimate is not an authoritative calculation.")
        probability = raw_probability.get("probability")
        if probability is not None:
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not isfinite(float(probability)) or not 0 <= float(probability) <= 1:
                raise ValueError("position_sizing.probability_estimate.probability: expected a number from 0 to 1 or null.")
            probability = float(probability)
        raw_range = raw_probability.get("range")
        if raw_range is None:
            bounds = None
        else:
            if not isinstance(raw_range, list) or len(raw_range) != 2:
                raise ValueError("position_sizing.probability_estimate.range: expected [low, high] or null.")
            bounds = []
            for index, item in enumerate(raw_range):
                if isinstance(item, bool) or not isinstance(item, (int, float)) or not isfinite(float(item)) or not 0 <= float(item) <= 1:
                    raise ValueError(f"position_sizing.probability_estimate.range[{index}]: expected a number from 0 to 1.")
                bounds.append(float(item))
            if bounds[0] > bounds[1] or (probability is not None and not bounds[0] <= probability <= bounds[1]):
                raise ValueError("position_sizing.probability_estimate.range: bounds must be ordered and contain probability when supplied.")
        clean_probability = {}
        for field in ("source", "method", "evidence"):
            item = raw_probability.get(field)
            if item is not None and not isinstance(item, str):
                raise _problem(f"position_sizing.probability_estimate.{field}", "text or null", item, "Use text or null.")
            clean_probability[field] = (item or "").strip()
        result["probability_estimate"] = {
            "probability": probability, "range": bounds, **clean_probability,
            "approval_status": "unverified proposal; not an approved sizing input",
        }
    for field in ("target_allocation", "maximum_allocation", "maximum_loss_fraction"):
        number = value.get(field)
        if number is not None:
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not isfinite(float(number)) or not 0 <= float(number) <= 1:
                raise ValueError(f"position_sizing.{field}: expected a fraction from 0 to 1 or null; for example 0.08 means 8%.")
            result[field] = float(number)
        else:
            result[field] = None
    raw_range = value.get("target_allocation_range")
    if raw_range is not None and value.get("target_allocation") is not None:
        raise ValueError("position_sizing: provide target_allocation or target_allocation_range, not both.")
    if raw_range is None:
        result["target_allocation_range"] = None
    else:
        if not isinstance(raw_range, list) or len(raw_range) != 2:
            raise ValueError("position_sizing.target_allocation_range: expected a two-number array [low, high] or null.")
        pair = []
        for index, item in enumerate(raw_range):
            if isinstance(item, bool):
                raise ValueError(f"position_sizing.target_allocation_range[{index}]: expected a fraction from 0 to 1.")
            try:
                number = float(item)
            except (TypeError, ValueError):
                raise ValueError(f"position_sizing.target_allocation_range[{index}]: expected a fraction from 0 to 1.")
            if not isfinite(number) or not 0 <= number <= 1:
                raise ValueError(f"position_sizing.target_allocation_range[{index}]: expected a fraction from 0 to 1.")
            pair.append(number)
        if pair[0] > pair[1]:
            raise ValueError("position_sizing.target_allocation_range: low must not exceed high.")
        result["target_allocation_range"] = pair
    for field in ("allocation_rationale", "assumptions", "constraints", "uncertainty"):
        item = value.get(field)
        if item is None:
            result[field] = ""
        elif isinstance(item, str):
            result[field] = item.strip()
        elif field in {"assumptions", "constraints", "evidence"} and isinstance(item, list) and all(isinstance(part, str) for part in item):
            result[field] = "\n".join(item)
        else:
            raise _problem(f"position_sizing.{field}", "text or null", item, "Use concise text; price, quantity and risk calculations are generated by the application.")
    raw_evidence = value.get("evidence")
    if raw_evidence is None or isinstance(raw_evidence, str):
        result["evidence"] = (raw_evidence or "").strip() if isinstance(raw_evidence, str) else ""
    else:
        result["evidence"] = _validate_urls(raw_evidence, "position_sizing.evidence")
    for field in ("entry_tranches", "exit_tranches"):
        items = value.get(field, [])
        if items is None:
            items = []
        if not isinstance(items, list):
            raise _problem(f"position_sizing.{field}", "array of tranche objects", items, "Provide an array or an empty array.")
        result[field] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise _problem(f"position_sizing.{field}[{index}]", "tranche object", item, "Use a structured object with fraction, trigger and rationale.")
            forbidden_keys = set(item) & forbidden
            if forbidden_keys:
                key = sorted(forbidden_keys)[0]
                raise ValueError(f"position_sizing.{field}[{index}].{key}: quantities and calculated amounts are not trusted on import.")
            permitted_keys = {"id", "sequence", "priority", "fraction", "trigger_price_low", "trigger_price_high", "trigger_price", "confirmation_conditions", "applicable_stop", "status", "rationale", "evidence", "condition", "percent", "percent_basis", "trigger_type", "sell_all"}
            unknown = set(item) - permitted_keys
            if unknown:
                raise _problem(f"position_sizing.{field}[{index}].{sorted(unknown)[0]}", "supported tranche field", item[sorted(unknown)[0]], "Move extra reasoning to rationale or evidence.")
            clean = dict(item)
            for key in ("fraction", "percent"):
                if clean.get(key) is not None:
                    try:
                        number = float(clean[key])
                    except (TypeError, ValueError):
                        raise ValueError(f"position_sizing.{field}[{index}].{key}: expected a fraction from 0 to 1 or null.")
                    if not isfinite(number) or not 0 <= number <= 1:
                        raise ValueError(f"position_sizing.{field}[{index}].{key}: expected a fraction from 0 to 1 or null.")
                    clean[key] = number
            if field == "entry_tranches" and clean.get("fraction") is None:
                raise ValueError(f"position_sizing.entry_tranches[{index}].fraction: required fraction from 0 to 1; use null only by omitting this entire unsupported tranche.")
            if field == "exit_tranches":
                specified = int(clean.get("percent") is not None) + int(clean.get("shares") is not None) + int(bool(clean.get("sell_all", False)))
                if specified != 1:
                    raise ValueError(f"position_sizing.exit_tranches[{index}]: specify exactly one of percent or sell_all=true; imported share quantities are not authoritative.")
            sequence = clean.get("sequence")
            if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1):
                raise ValueError(f"position_sizing.{field}[{index}].sequence: expected a positive integer.")
            for text_key in ("confirmation_conditions", "rationale", "evidence", "condition"):
                if clean.get(text_key) is not None and not isinstance(clean[text_key], str):
                    raise _problem(f"position_sizing.{field}[{index}].{text_key}", "text or null", clean[text_key], "Use a concise text explanation.")
            if clean.get("status") is not None and clean["status"] not in {"planned", "eligible", "canceled", "skipped"}:
                raise ValueError(f"position_sizing.{field}[{index}].status: unsupported tranche status.")
            for key in ("trigger_price_low", "trigger_price_high", "trigger_price", "applicable_stop"):
                if clean.get(key) is not None:
                    clean[key] = _price(clean[key], f"position_sizing.{field}[{index}].{key}")
            result[field].append(clean)
    return result


def validate_import_json(raw: str, expected_ticker: str) -> dict:
    """Compatibility entry point: normalize variations, then enforce a supported schema version."""
    return validate_normalized_strategy(normalize_strategy_import(raw), expected_ticker)


def preview_import(strategy: dict, *, current_price=None, current_quote_date: str | None = None, today: date | None = None) -> dict:
    """Attach deterministic RRR and data-freshness warnings without persistence."""
    today = today or date.today()
    result = deepcopy(strategy)
    warnings = []
    normalization = result.get("normalization_metadata") or {}
    for flag in normalization.get("review_flags", []):
        warnings.append(f"Review needed at {flag['path']}: {flag['reason']}")
    research_date = result.get("research_date")
    if research_date:
        age = (today - date.fromisoformat(research_date)).days
        if age > 90:
            warnings.append(f"Research date is {age} days old.")
    else:
        warnings.append("Research date was not provided.")
    reference = result.get("market_price_reference")
    if reference and reference.get("date"):
        age = (today - date.fromisoformat(reference["date"])).days
        if age > 5:
            warnings.append(f"Market-price reference is {age} days old.")
        if current_quote_date and reference["date"] != current_quote_date:
            warnings.append(f"Imported market reference is dated {reference['date']}; cached quote is dated {current_quote_date}.")
    if current_price is None:
        warnings.append("Current RRR is unavailable unless the imported price basis/source/date can be matched to the cached quote.")
    computed = {}
    for key, scenario in result["scenarios"].items():
        low, high = scenario["entry_price_low"], scenario["entry_price_high"]
        entry_price = low if low is not None and low == high else None
        setup = calculate_long_setup(
            entry_price=entry_price,
            entry_low=low if entry_price is None else None,
            entry_high=high if entry_price is None else None,
            target_price=scenario["target_price_low"] if scenario["target_price_low"] == scenario["target_price_high"] else None,
            stop_price=scenario["stop_price"],
            current_price=current_price,
        )
        # Target ranges are calculated at both ends, never collapsed to one value.
        target_low, target_high = scenario["target_price_low"], scenario["target_price_high"]
        if target_low is not None and target_high is not None and target_low != target_high:
            target_setups = [calculate_long_setup(
                entry_price=entry_price,
                entry_low=low if entry_price is None else None,
                entry_high=high if entry_price is None else None,
                target_price=target,
                stop_price=scenario["stop_price"], current_price=current_price,
            ) for target in (target_low, target_high)]
            setup["target_range_scenarios"] = target_setups
            planned_values = [
                entry_result.get("planned_rrr")
                for target_setup in target_setups
                for entry_result in target_setup.get("planned_scenarios", [])
                if entry_result.get("planned_rrr") is not None
            ]
            setup["planned_rrr_range"] = [min(planned_values), max(planned_values)] if planned_values else [None, None]
            setup["current_rrr_range"] = [item.get("current_rrr") for item in target_setups]
            if current_price is not None:
                statuses = {item.get("current_status") for item in target_setups}
                if "stop_crossed" in statuses or "target_crossed" in statuses:
                    warnings.append(f"{scenario['horizon']}: current quote has crossed a scenario stop or target; remaining RRR is unavailable for that level.")
        else:
            planned_values = [item.get("planned_rrr") for item in setup.get("planned_scenarios", []) if item.get("planned_rrr") is not None]
            if len(planned_values) > 1:
                setup["planned_rrr_range"] = [min(planned_values), max(planned_values)]
            if setup.get("current_status") in {"stop_crossed", "target_crossed"}:
                warnings.append(f"{scenario['horizon']}: {setup['current_message']}")
        computed[key] = setup
    result["calculated_setups"] = computed
    result["validation_warnings"] = warnings
    return result


def compatible_current_quote(strategy: dict, quote: dict | None, *, is_stale: bool) -> tuple[float | None, str | None]:
    """Return a quote only when the imported reference explicitly matches it."""
    if not quote:
        return None, "No cached Alpha Vantage quote is available."
    if is_stale:
        return None, f"Cached Alpha Vantage quote dated {quote.get('quote_date') or 'unknown'} is stale."
    reference = strategy.get("market_price_reference") or {}
    if not reference.get("date") or reference.get("date") != quote.get("quote_date"):
        return None, "Imported reference date does not match the cached quote date."
    source = str(reference.get("source", "")).strip().lower()
    basis = str(reference.get("basis", "")).strip().lower()
    explicitly_raw = source == "alpha vantage" and basis in {"unadjusted", "raw"}
    try:
        same_value = (reference.get("price") is not None and quote.get("price") is not None
                      and abs(float(reference["price"]) - float(quote["price"])) < 0.000001)
    except (TypeError, ValueError):
        same_value = False
    explicitly_unadjusted = any(term in basis for term in ("unadjusted", "raw", "not adjusted")) and not (
        ("adjusted" in basis and "unadjusted" not in basis and "not adjusted" not in basis)
        or "dividend" in basis
    )
    same_observation = same_value and explicitly_unadjusted
    if not explicitly_raw and not same_observation:
        return None, "Imported reference and cached quote share a date but their price basis/value is not confirmed compatible."
    return quote.get("price"), None


def assess_current_quote(strategy: dict, quote: dict | None, *, is_stale: bool) -> dict:
    """Explain quote freshness and price-basis compatibility as separate facts."""
    if not quote:
        return {"status": "missing", "freshness_status": "missing", "basis_status": "unknown",
                "price": None, "message": "No cached Alpha Vantage quote is available."}
    quote_date = quote.get("quote_date") or "unknown date"
    reference = strategy.get("market_price_reference") or {}
    basis = str(reference.get("basis") or "").strip().lower()
    explicit_raw = any(term in basis for term in ("unadjusted", "raw", "not adjusted")) and not (
        ("adjusted" in basis and "unadjusted" not in basis and "not adjusted" not in basis)
        or "dividend" in basis
    )
    explicitly_adjusted = ("dividend" in basis) or ("adjusted" in basis and "unadjusted" not in basis and "not adjusted" not in basis)
    same_date = bool(reference.get("date") and reference.get("date") == quote.get("quote_date"))
    try:
        same_value = (reference.get("price") is not None and quote.get("price") is not None
                      and abs(float(reference["price"]) - float(quote["price"])) < 0.000001)
    except (TypeError, ValueError):
        same_value = False
    basis_status = ("adjusted_incompatible" if explicitly_adjusted else
                    "compatible" if explicit_raw and same_date and same_value else
                    "unconfirmed")
    basis_note = ("Imported reference explicitly uses adjusted closing prices, which cannot be reconciled to the raw quote without adjustment factors."
                  if explicitly_adjusted else
                  "Imported reference does not explicitly confirm an unadjusted/raw basis; equal-looking prices alone are not treated as proof."
                  if basis_status == "unconfirmed" else None)
    if is_stale:
        extra = f" {basis_note}" if basis_note else " Price basis is compatible from the saved matching raw reference."
        return {
            "status": "stale", "freshness_status": "stale", "basis_status": basis_status, "price": None,
            "message": f"Cached Alpha Vantage quote dated {quote_date} is stale and is not used for actionable sizing.{extra}",
        }
    if not reference.get("date"):
        return {"status": "unconfirmed_basis", "freshness_status": "fresh", "basis_status": "unknown", "price": None,
                "message": "The quote is fresh, but the strategy has no dated price reference, so adjustment compatibility cannot be checked."}
    if reference.get("date") != quote.get("quote_date"):
        return {
            "status": "different_observation_date", "freshness_status": "different_observation_date", "basis_status": "unconfirmed", "price": None,
            "message": f"Quote date {quote_date} differs from strategy-reference date {reference.get('date')}. This is a freshness difference, not evidence of a price discrepancy; {basis_note or 'cross-source price-basis compatibility is unconfirmed.'}",
        }
    price, message = compatible_current_quote(strategy, quote, is_stale=False)
    if price is None:
        return {"status": "incompatible_basis", "freshness_status": "fresh", "basis_status": basis_status, "price": None,
                "message": message or basis_note or "Price-adjustment basis is not confirmed compatible."}
    return {"status": "compatible", "freshness_status": "fresh", "basis_status": "compatible", "price": price,
            "message": f"Quote and strategy reference match on {quote_date}; basis is supported by the saved reference."}


def imported_strategy_record(preview: dict, existing: dict | None = None) -> dict:
    """Map an approved import into the legacy revisioned strategy record."""
    existing = existing or {}
    medium = preview["scenarios"]["medium_term"]
    entry_low = medium.get("entry_price_low")
    entry_high = medium.get("entry_price_high")
    target_low, target_high = medium.get("target_price_low"), medium.get("target_price_high")
    target = target_low if target_low is not None and target_low == target_high else None
    stop = medium.get("stop_price")
    detail = dict(preview)
    detail["imported_at"] = datetime.now(timezone.utc).isoformat()
    detail["import_source"] = "ChatGPT JSON import; references are user supplied and not independently verified"
    horizon = "Medium"
    holding_window = medium.get("holding_horizon") or ""
    record = dict(existing)
    record.update({
        "investment_thesis": preview["investment_thesis"],
        "investment_horizon": horizon,
        "thesis_status": existing.get("thesis_status") or "Active",
        "strategy_type": preview.get("strategy_type", ""),
        "expected_holding_window": holding_window,
        "catalysts": preview.get("catalysts", ""),
        "risks": preview.get("risks", ""),
        "entry_price_low": entry_low,
        "entry_price_high": entry_high,
        "target_price": target,
        "invalidation_price": stop,
        "stop_invalidation": medium.get("invalidation_conditions") or medium.get("fundamental_invalidation") or "",
        "position_sizing_notes": existing.get("position_sizing_notes", ""),
        "last_review_date": preview.get("research_date") or date.today().isoformat(),
        "valuation_notes": preview.get("valuation_analysis", ""),
        "legacy_entry_zone": medium.get("entry_conditions", ""),
        "bullish_scenario": preview.get("bull_case", ""),
        "bearish_scenario": preview.get("bear_case", ""),
        "assumptions": preview.get("base_case", ""),
        "uncertainties": "\n".join(filter(None, (preview.get("uncertainties"), preview.get("research_gaps")))),
        "ai_price_estimates": 0,
        "structured_analysis_json": json.dumps(detail, sort_keys=True),
    })
    return record


def approve_import(preview: dict, database_path=None) -> bool:
    """Explicit approval boundary: persist one new strategy revision."""
    existing = get_strategy_record(preview["ticker"], database_path) if database_path else get_strategy_record(preview["ticker"])
    record = imported_strategy_record(preview, existing)
    if database_path:
        changed = save_strategy_record(preview["ticker"], record, database_path)
    else:
        changed = save_strategy_record(preview["ticker"], record)
    approved_scenarios = []
    for key in HORIZONS:
        scenario = dict(preview["scenarios"][key])
        scenario["holding_window"] = scenario.pop("holding_horizon", "")
        scenario["calculated_setup"] = preview["calculated_setups"][key]
        approved_scenarios.append(scenario)
    save_research_strategy_scenarios(
        preview["ticker"], approved_scenarios,
        provider="ChatGPT import", model=f"schema-v{SCHEMA_VERSION}",
        **({"database_path": database_path} if database_path else {}),
    )
    return changed


def canonical_import_example(ticker: str = "MCD") -> dict:
    """A complete, schema-aligned, importable example with no financial claims."""
    placeholder = "Illustrative placeholder — replace with your own researched content."
    scenario = {
        "thesis": placeholder,
        "holding_horizon": "Set the intended holding window",
        "entry_conditions": "Describe conditions; leave numeric prices null if unsupported.",
        "entry_price_low": None, "entry_price_high": None,
        "target_price_low": None, "target_price_high": None, "stop_price": None,
        "fundamental_invalidation": "Describe thesis invalidation; no numerical stop supplied.",
        "invalidation_conditions": placeholder,
        "bull_case": placeholder, "base_case": placeholder, "bear_case": placeholder,
        "catalysts": "", "risks": "", "evidence": "",
        "assumptions": "", "uncertainty": "Replace this example with verified research.",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ticker": ticker,
        "company_name": None,
        "research_date": date.today().isoformat(),
        "strategy_type": "Example only",
        "investment_thesis": placeholder,
        "fundamental_analysis": placeholder,
        "valuation_analysis": placeholder,
        "technical_analysis": placeholder,
        "bull_case": placeholder,
        "base_case": placeholder,
        "bear_case": placeholder,
        "catalysts": "",
        "risks": "",
        "uncertainties": "Example placeholders only; no research has been verified.",
        "research_gaps": "Replace all example text with your own research.",
        "market_price_reference": {"price": None, "date": None, "basis": "", "source": ""},
        "evidence": [{
            "title": "Generic official starting point — not evidence for this ticker",
            "url": "https://www.sec.gov/",
            "source": "U.S. Securities and Exchange Commission website; generic reference only",
            "as_of": None,
        }],
        "investment_logic": {
            "bullish_drivers": [], "bearish_risks": [], "catalysts": [], "invalidation_signals": [],
        },
        "position_sizing": None,
        "scenarios": {key: deepcopy(scenario) for key in HORIZONS},
    }


def json_template(ticker: str = "MCD") -> str:
    """Serialize the canonical schema-aligned example as the import template."""
    return json.dumps(canonical_import_example(ticker), indent=2, ensure_ascii=False)


def chatgpt_export_instructions(ticker: str = "MCD") -> str:
    """Generate export directions and a complete example from the canonical schema."""
    root_fields = "\n".join(f"- `{name}`: {shape}" for name, shape in STRATEGY_IMPORT_SCHEMA.items())
    scenario_text = ", ".join(f"`{name}` (string or null)" for name in SCENARIO_TEXT_FIELDS)
    scenario_prices = ", ".join(f"`{name}` (positive number or null)" for name in SCENARIO_PRICE_FIELDS)
    return (
        f"Export exactly one JSON object using schema version {SCHEMA_VERSION}. Keep ticker `{ticker}` exactly so it matches the open workspace.\n\n"
        "Required root fields are `schema_version`, `ticker`, `investment_thesis`, and `scenarios`. Versions 1 and 2 remain accepted; version 3 adds optional probability-estimate metadata inside `position_sizing`. The canonical field shapes are:\n"
        f"{root_fields}\n\n"
        "Each `scenarios` value is an object. Include all three keys: `short_term`, `medium_term`, and `long_term`. "
        f"Each scenario may contain: {scenario_text}; and {scenario_prices}. "
        "For a price range, supply both low/high fields or set both to null. Use JSON numbers or null for prices; simple numeric strings are accepted and normalized, but numbers are preferred. "
        "In `investment_logic`, each of the four optional arrays may contain up to three objects with exactly `label` (string), `explanation` (string), `evidence_status` (one of Verified, User assumption, Analyst inference, Missing evidence, Imported / unverified), and `details` (string or null). Use Imported / unverified unless the cited evidence directly supports the item; citations themselves are not independently verified by this app. Empty arrays are valid. "
        "Never invent unsupported prices, probabilities, evidence, or URLs. Use null when unknown. The `evidence` field is an array of objects with `title`, `url`, `source`, and `as_of`; the URL must be an absolute http/https string and the date is YYYY-MM-DD or null. "
        "Do not include precomputed RRR, share counts, or dollar quantities; the dashboard calculates them from local portfolio data. Optional `position_sizing` may contain policy proposals: target allocation or target-allocation range, maximum allocation, maximum loss fraction, rationale, assumptions, constraints, evidence, and optional tranche proposals. Optionally include `probability_estimate` as an object with `probability` (number 0–1 or null), `range` (two-number [low, high] or null), `source`, `method`, and `evidence`, or use null. Never invent a win probability; specifically, do not reuse an SPY correction probability as a single-stock success probability. Imported probabilities are unverified proposals and are never automatically used for Kelly sizing. No imported share quantities, dollar sizing, or calculated risk is authoritative. If unsupported, set `position_sizing` to null. Citations are retained as references and are not automatically verified. The example below is schema-valid but contains illustrative placeholder prose and a generic SEC homepage reference, not ticker-specific research. Replace placeholders with sourced research before approving.\n\n"
        "Complete schema-aligned example:\n```json\n"
        f"{json_template(ticker)}\n```"
    )

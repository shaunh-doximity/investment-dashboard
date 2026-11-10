"""AI research-draft helpers; numeric RRR remains in setup_calculations.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from math import isfinite
import os
import re

from dashboard.ai_providers import AIProviderError, generate_text, load_ai_environment
from dashboard.market_data import quote_is_stale
from dashboard.setup_calculations import calculate_long_setup
from dashboard.strategy_schema import validate_strategy_draft


STRATEGIST_INSTRUCTIONS = """You are a rigorous, skeptical investment research assistant. Your job is to test an investment thesis, not affirm it. Understand the user's thesis and intended holding period; assess business quality, valuation, competitive position, catalysts, trends, momentum, volatility, moving averages and relative strength only from the provided context. Challenge unsupported assumptions, identify contradictory evidence, compare bullish/base/bearish cases, and say what new evidence would change your view. Explain when a strong business may still be an unattractive investment. Never invent current prices, financial results, analyst targets, sources, events or probabilities. The model has no automatic live internet access. Distinguish verified local observations, user-provided claims, your interpretation, unverified hypotheses, and unknowns. If fundamentals, valuation or news are absent, say exactly that and ask for a source or limit the conclusion. Do not claim a causal explanation for price moves without verified news evidence."""

STRATEGY_INSTRUCTIONS = STRATEGIST_INSTRUCTIONS + """\n\nConvert the saved conversation and local evidence into a structured, unverified investment strategy draft for explicit user review. Never save or replace approved records. Do not calculate reward-to-risk or other arithmetic; Python calculates these. Return exactly one complete JSON object and nothing else: no preamble, markdown fence, or text after the object. Use valid JSON syntax with double-quoted keys/strings, commas between fields, and JSON null for missing prices. Keep text concise: generally one or two sentences per field and brief scenario notes. The object must include: investment_thesis, strategy_type, investment_horizon (Long/Medium/Short), expected_holding_window, bullish_scenario, bearish_scenario, bullish_arguments, bearish_arguments, catalysts, risks, fundamental_analysis_notes, valuation_analysis_notes, technical_analysis_notes, thesis_invalidation_conditions, position_sizing_considerations, supporting_evidence (text), supporting_evidence_references (array of source-backed references only), fields_requiring_verification, assumptions, uncertainties, entry_price_low, entry_price_high, target_price, invalidation_price, stop_invalidation, and horizon_scenarios. Use null for unsupported price values and explain missing evidence; do not force targets/stops for every horizon. Horizon scenarios can include only evidence-supported horizons and use horizon labels Swing / short-term, Medium-term, or Long-term. Each scenario may include holding_window, thesis, entry_conditions, entry_price_low/high, bullish_catalysts, bearish_catalysts, target_price, invalidation_price, fundamental_invalidation_conditions, invalidation_conditions, evidence_for_levels, assumptions, confidence_uncertainty. AI prices are estimates, not market data. Do not invent company identity, fundamentals, news, citations, targets, or probabilities. Do not infer or include portfolio position sizes unless portfolio context is explicitly provided."""


def _configured_strategist_instructions(*, strategy_draft: bool = False) -> str:
    load_ai_environment()
    base = os.environ.get("RESEARCH_STRATEGIST_PROMPT", "").strip() or STRATEGIST_INSTRUCTIONS
    if not strategy_draft:
        return base + " Do not make trades or automatically save or replace strategy records."
    suffix = STRATEGY_INSTRUCTIONS[len(STRATEGIST_INSTRUCTIONS):]
    return base + suffix


def _text(value, default="") -> str:
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value if isinstance(item, (str, int, float)))
    return value.strip() if isinstance(value, str) else default


def _price(value, label):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AIProviderError(f"AI strategy returned a malformed {label}. No strategy was saved.")
    if not isfinite(value) or value <= 0:
        raise AIProviderError(f"AI strategy returned an invalid {label}. No strategy was saved.")
    return float(value)


def _parse_json(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise AIProviderError("The AI strategy response was empty. No strategy was saved; retry the draft request.")
    candidate = text.strip().lstrip("\ufeff")
    # Models sometimes wrap otherwise valid JSON in Markdown or add a short
    # preface/reasoning block. Parse candidate objects without evaluating text.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    def parse_candidate(value: str):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            # A trailing comma is a common model formatting slip. Remove it
            # only when outside JSON strings, then parse strictly again.
            repaired = []
            in_string = escaped = False
            for index, char in enumerate(value):
                if in_string:
                    repaired.append(char)
                    if escaped:
                        escaped = False
                    elif char == chr(92):
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                    repaired.append(char)
                elif char == ",":
                    lookahead = index + 1
                    while lookahead < len(value) and value[lookahead].isspace():
                        lookahead += 1
                    if lookahead >= len(value) or value[lookahead] not in "}]":
                        repaired.append(char)
                else:
                    repaired.append(char)
            return json.loads("".join(repaired))

    try:
        result = parse_candidate(candidate)
    except (TypeError, json.JSONDecodeError):
        expected_keys = {
            "ticker", "investment_thesis", "strategy_type", "investment_horizon",
            "expected_holding_window", "bullish_scenario", "bearish_scenario",
            "catalysts", "risks", "entry_price_low", "target_price", "invalidation_price",
            "horizon_scenarios", "fields_requiring_verification",
        }
        candidates: list[tuple[int, dict]] = []
        non_object_found = False
        for start, char in enumerate(candidate):
            if char != "{":
                continue
            depth = 0
            in_string = escaped = False
            end = None
            for index in range(start, len(candidate)):
                current = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif current == chr(92):
                        escaped = True
                    elif current == '"':
                        in_string = False
                    continue
                if current == '"':
                    in_string = True
                elif current == "{":
                    depth += 1
                elif current == "}":
                    depth -= 1
                    if depth == 0:
                        end = index + 1
                        break
            if end is None:
                continue
            try:
                parsed = parse_candidate(candidate[start:end])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                candidates.append((len(expected_keys.intersection(parsed)), parsed))
            else:
                non_object_found = True
        if candidates:
            result = max(candidates, key=lambda item: item[0])[1]
        elif non_object_found:
            raise AIProviderError("The AI strategy response had an unexpected format. No strategy was saved.")
        else:
            raise AIProviderError(
                "The AI strategy response was not valid JSON or was incomplete. No strategy was saved. "
                "Retry; if this repeats, increase Max Output Tokens in AI settings or choose a model with a larger output limit."
            )
    if not isinstance(result, dict):
        raise AIProviderError("The AI strategy response had an unexpected format. No strategy was saved.")
    return result


def _saved_strategy_context(strategy: dict | None, *, include_sizing: bool = False) -> dict | None:
    if not strategy:
        return None
    fields = (
        "investment_thesis", "investment_horizon", "thesis_status", "strategy_type",
        "catalysts", "risks", "entry_price_low", "entry_price_high", "target_price",
        "invalidation_price", "stop_invalidation", "valuation_notes", "bullish_scenario",
        "bearish_scenario", "assumptions", "uncertainties", "last_review_date",
    )
    context = {field: strategy.get(field) for field in fields}
    if include_sizing:
        context.update({field: strategy.get(field) for field in ("position_sizing_notes", "max_planned_dollar_risk")})
    return context


def generate_strategy_draft(
    symbol: str, *, provider: str, model: str, max_output_tokens: int,
    quote: dict | None = None, portfolio_context: list[dict] | None = None,
    research_context: dict | None = None, saved_strategy: dict | None = None,
    conversation_context: list[dict] | None = None,
) -> dict:
    context = {"ticker": symbol.strip().upper(), "current_quote": None}
    if quote:
        context["current_quote"] = {"price": quote.get("price"), "quote_date": quote.get("quote_date"),
                                    "provider": "Alpha Vantage", "source": "locally cached quote; may be delayed",
                                    "observation_freshness": "stale" if quote_is_stale(quote) else "fresh_by_local_policy",
                                    "price_adjustment_basis": "unverified quote basis; not compared with adjusted technical levels"}
    if portfolio_context:
        context["optional_portfolio_context"] = portfolio_context
    if research_context:
        context["local_research_context"] = research_context
    if saved_strategy:
        context["saved_user_strategy"] = _saved_strategy_context(saved_strategy, include_sizing=portfolio_context is not None)
    if conversation_context:
        context["saved_conversation"] = [
            {"role": item.get("role"), "content": str(item.get("content", ""))[-2500:]}
            for item in conversation_context[-24:]
            if item.get("role") in ("user", "assistant") and item.get("content")
        ]
    prompt = (
        "Prepare a cautious long-equity strategy draft for this ticker from the saved conversation and the supplied local context. "
        "Output exactly one valid json object matching the required keys; do not include analysis outside the object. "
        "Generate even when evidence is incomplete. Retain missing numerical values as JSON null and explain the missing evidence. "
        "The Alpha Vantage quote is separate from Tiingo dividend-adjusted closes; use Tiingo history for technical levels and do not compare unlike price bases. "
        "Do not use external tools, claim live access, or turn model recollection into verified facts.\n"
        "INPUT_JSON:\n" + json.dumps(context, sort_keys=True)
    )
    response = generate_text(prompt=prompt, provider=provider, model=model,
                             max_output_tokens=max_output_tokens,
                             instructions=_configured_strategist_instructions(strategy_draft=True), web_search=False,
                             json_mode=True)
    raw = _parse_json(response.text)
    try:
        research = research_context or {}
        verified_company_name = research.get("company_name") if research.get("company_name_provenance") == "verified" else None
        schema = validate_strategy_draft(raw, symbol, company_name=verified_company_name)
    except ValueError as error:
        raise AIProviderError(f"AI strategy did not match the expected structure: {error} No strategy was saved.") from error
    raw.update(schema)
    horizon = raw.get("investment_horizon")
    low = _price(raw.get("entry_price_low"), "entry range lower bound")
    high = _price(raw.get("entry_price_high"), "entry range upper bound")
    target = _price(raw.get("target_price"), "target price")
    stop = _price(raw.get("invalidation_price"), "invalidation price")
    if (low is None) != (high is None):
        raise AIProviderError("AI strategy must return both entry-range bounds or neither. No strategy was saved.")
    setup = calculate_long_setup(
        entry_price=low if low is not None and high == low else None,
        entry_low=low if low is not None and high != low else None,
        entry_high=high if low is not None and high != low else None,
        target_price=target, stop_price=stop,
        current_price=(quote or {}).get("price") if quote else None,
    )
    horizon_scenarios = raw.get("horizon_scenarios", [])
    if not isinstance(horizon_scenarios, list):
        raise AIProviderError("AI strategy returned malformed horizon scenarios. No strategy was saved.")
    normalized_scenarios = []
    allowed_horizons = {"Swing / short-term", "Medium-term", "Long-term"}
    for item in horizon_scenarios[:3]:
        if not isinstance(item, dict) or item.get("horizon") not in allowed_horizons:
            raise AIProviderError("AI strategy returned an unknown horizon scenario. No strategy was saved.")
        entry_low = _price(item.get("entry_price_low"), "scenario entry lower bound")
        entry_high = _price(item.get("entry_price_high"), "scenario entry upper bound")
        scenario_target = _price(item.get("target_price"), "scenario target")
        scenario_stop = _price(item.get("invalidation_price"), "scenario invalidation")
        scenario_setup = calculate_long_setup(
            entry_price=entry_low if entry_low is not None and entry_high == entry_low else None,
            entry_low=entry_low if entry_low is not None and entry_high != entry_low else None,
            entry_high=entry_high if entry_low is not None and entry_high != entry_low else None,
            target_price=scenario_target, stop_price=scenario_stop,
            current_price=(quote or {}).get("price") if quote else None,
        )
        if (entry_low is None) != (entry_high is None):
            scenario_setup["status"] = "missing_data"
            scenario_setup["message"] = "Both entry range bounds are required."
        normalized_scenarios.append({
            "horizon": item["horizon"],
            "holding_window": _text(item.get("holding_window")),
            "thesis": _text(item.get("thesis")),
            "entry_conditions": _text(item.get("entry_conditions")),
            "entry_price_low": entry_low, "entry_price_high": entry_high,
            "bullish_catalysts": _text(item.get("bullish_catalysts")),
            "bearish_catalysts": _text(item.get("bearish_catalysts")),
            "target_price": scenario_target, "invalidation_price": scenario_stop,
            "invalidation_conditions": _text(item.get("invalidation_conditions")),
            "fundamental_invalidation_conditions": _text(item.get("fundamental_invalidation_conditions")),
            "evidence_for_levels": _text(item.get("evidence_for_levels")),
            "assumptions": _text(item.get("assumptions")),
            "confidence_uncertainty": _text(item.get("confidence_uncertainty")),
            "ai_price_estimates": True, "calculated_setup": scenario_setup,
        })
    text_fields = {
        "investment_thesis": _text(raw.get("investment_thesis")),
        "strategy_type": _text(raw.get("strategy_type")),
        "expected_holding_window": _text(raw.get("expected_holding_window")),
        "bullish_scenario": _text(raw.get("bullish_scenario")),
        "bearish_scenario": _text(raw.get("bearish_scenario")),
        "catalysts": _text(raw.get("catalysts")), "risks": _text(raw.get("risks")),
        "stop_invalidation": _text(raw.get("stop_invalidation")),
        "assumptions": _text(raw.get("assumptions")), "uncertainties": _text(raw.get("uncertainties")),
    }
    missing = [name.replace("_", " ") for name, value in text_fields.items() if not value]
    if missing:
        raise AIProviderError("AI strategy omitted required sections: " + ", ".join(missing) + ". No strategy was saved.")
    raw.update({
        "investment_thesis": text_fields["investment_thesis"],
        "strategy_type": text_fields["strategy_type"],
        "investment_horizon": horizon,
        "expected_holding_window": text_fields["expected_holding_window"],
        "bullish_scenario": text_fields["bullish_scenario"],
        "bearish_scenario": text_fields["bearish_scenario"],
        "catalysts": text_fields["catalysts"],
        "risks": text_fields["risks"],
        "entry_price_low": low, "entry_price_high": high,
        "target_price": target, "invalidation_price": stop,
        "stop_invalidation": text_fields["stop_invalidation"],
        "assumptions": text_fields["assumptions"],
        "uncertainties": text_fields["uncertainties"],
        "ai_price_estimates": 1,
        "horizon_scenarios": normalized_scenarios,
        "calculated_setup": setup,
        "provider": response.provider, "model": response.model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_quote_date": (quote or {}).get("quote_date"),
        "current_quote_stale": quote_is_stale(quote) if quote else None,
    })
    # Only retain citations that already exist in the supplied local evidence.
    allowed_urls = {
        item.get("url") for item in (research_context or {}).get("verified_external_evidence", [])
        if isinstance(item, dict) and item.get("url")
    }
    allowed_urls.update(
        item.get("source_url") for item in (research_context or {}).get("upcoming_earnings_events", [])
        if isinstance(item, dict) and item.get("source_url")
    )
    verified_refs = [ref for ref in raw.get("supporting_evidence_references", [])
                     if isinstance(ref, dict) and ref.get("url") in allowed_urls]
    unverified_refs = [ref for ref in raw.get("supporting_evidence_references", [])
                       if isinstance(ref, dict) and ref.get("url") and ref.get("url") not in allowed_urls]
    raw["supporting_evidence_references"] = verified_refs
    if unverified_refs:
        labels = [str(ref.get("title") or ref.get("url")) for ref in unverified_refs]
        existing_gaps = raw.get("fields_requiring_verification", "")
        raw["fields_requiring_verification"] = (existing_gaps + "\n" if existing_gaps else "") + "AI-mentioned sources not verified locally: " + ", ".join(labels)
    allowed_fields = set(schema) | {
        "stop_invalidation", "ai_price_estimates", "calculated_setup",
        "provider", "model", "generated_at",
        "current_quote_date", "current_quote_stale",
    }
    raw = {key: value for key, value in raw.items() if key in allowed_fields}
    return raw


def answer_research_question(
    symbol: str, question: str, history: list[dict], *, provider: str,
    model: str, max_output_tokens: int, strategy: dict,
    portfolio_context: list[dict] | None = None,
    research_context: dict | None = None,
    return_result: bool = False,
) -> str | object:
    """Answer a user-submitted question; transcript is passed only on explicit send."""
    messages = [{"role": item["role"], "content": item["content"]} for item in history[-8:]
                if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str)]
    saved_strategy = _saved_strategy_context(strategy, include_sizing=portfolio_context is not None)
    context = {"ticker": symbol.upper(), "saved_user_strategy": saved_strategy,
               "local_research_context": research_context or {}}
    if portfolio_context:
        context["optional_portfolio_context"] = portfolio_context
    transcript = "\n".join(f"{item['role'].upper()}: {item['content']}" for item in messages)
    prompt = ("Discuss this company research workspace and answer the user's actual question. Be willing to disagree. "
              "Use only the provided local observations as verified data. Label user claims, interpretations, hypotheses "
              "and unknowns separately. The source list is empty unless an item is explicitly included; do not treat "
              "model recollection as research. Do not invent current news, facts, prices, sources, or probabilities. "
              "If the user requests calculations, explain the needed inputs and leave arithmetic to Python.\nWORKSPACE_JSON:\n" +
              json.dumps(context, sort_keys=True) + "\nPRIOR CHAT:\n" + transcript +
              "\nUSER QUESTION:\n" + question.strip())
    result = generate_text(
        prompt=prompt, provider=provider, model=model, max_output_tokens=max_output_tokens,
        instructions=_configured_strategist_instructions(),
    )
    return result if return_result else result.text

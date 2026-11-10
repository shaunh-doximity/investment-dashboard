"""Explicit, bounded DeepSeek follow-up chat for public Market snapshot data."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import time
import uuid

import requests

from dashboard.ai_providers import PROJECT_ROOT
from dashboard.data_access import (
    deepseek_market_research_daily_spend, record_deepseek_market_research_usage,
    record_market_ai_request_audit,
)
from dashboard.deepseek_market_research import (
    BASE, DEFAULT_MODEL, MODEL_PRICES, SEARCH_COST_PER_REQUEST,
    _search_results, bounded_deepseek_timeouts, bounded_search_query, extract_responses_text,
    transport_error_category,
)
from dashboard.market_snapshot import SECTION_ORDER
from dashboard.market_snapshot import LEGACY_SECTION_ORDER


CHAT_OUTPUT_TOKENS = 1200
PREMARKET_OUTPUT_TOKENS = 2800
CHAT_RETENTION_MESSAGES = 60
PREMARKET_PATH_FIELDS = ("path_id", "name", "if_condition", "then_interpretation", "why_it_might_happen",
                         "supporting_or_contradicting_evidence", "indicators_to_watch")


def parse_premarket_what_if(text: str) -> dict:
    """Parse and validate a completed conditional setup analysis.

    DeepSeek sometimes wraps an otherwise valid JSON answer in a Markdown code
    fence or a short preamble. Recover the first complete JSON object without
    relaxing the required fields or accepting partial scenario paths.
    """
    raw = str(text or "").strip()
    candidates = []
    if raw:
        candidates.append(raw)
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            candidates.insert(0, fenced.group(1))
        decoder = json.JSONDecoder()
        for index, character in enumerate(raw):
            if character == "{":
                try:
                    parsed, _end = decoder.raw_decode(raw[index:])
                except json.JSONDecodeError:
                    continue
                candidates.append(parsed)
    last_error = None
    payload = None
    for candidate in candidates:
        if isinstance(candidate, dict):
            payload = candidate
            break
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                payload = parsed
                break
        except (TypeError, json.JSONDecodeError) as exc:
            last_error = exc
    if payload is None:
        raise ValueError("Premarket What-If returned no parseable JSON object; retry analysis and ensure the model returns its scenario structure.") from last_error
    for key in ("setup_meaning", "most_important_uncertainty"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"Premarket What-If response is missing {key}.")
    paths = payload.get("paths")
    if not isinstance(paths, list) or not 3 <= len(paths) <= 4:
        raise ValueError("Premarket What-If must provide three or four conditional market paths.")
    for index, path in enumerate(paths, 1):
        if not isinstance(path, dict):
            raise ValueError(f"Path {index} must be an object.")
        missing = [key for key in PREMARKET_PATH_FIELDS if not isinstance(path.get(key), str) or not path[key].strip()]
        if missing:
            raise ValueError(f"Path {index} is missing: {', '.join(missing)}.")
        if any(word in path for word in ("probability", "odds", "confidence_score")):
            raise ValueError(f"Path {index} must not contain probability or confidence fields.")
    watches = payload.get("session_watch")
    if not isinstance(watches, list) or not 1 <= len(watches) <= 6 or any(not isinstance(item, str) or not item.strip() for item in watches):
        raise ValueError("Premarket What-If needs one to six concrete session observations to monitor.")
    return payload


def earlier_market_chat_groups(groups, section_ids, current_fingerprint):
    """Return archived chat contexts relevant to a section, newest first.

    The database's grouped history records contain metadata only, so this helper
    is safe for rendering archive controls without loading message text.
    """
    wanted = {str(section_id) for section_id in section_ids}
    current = str(current_fingerprint or "")
    return [dict(group) for group in (groups or [])
            if str(group.get("section_id", "")) in wanted
            and str(group.get("snapshot_fingerprint", "")) != current]


def _compact_section_context(section: dict, *, focal: bool, max_metrics: int | None = None) -> dict:
    """Keep dated, unit-bearing observations while dropping chart/history payloads."""
    if not isinstance(section, dict):
        return {"status": "unavailable", "metrics": {}}
    result = {key: section.get(key) for key in
              ("status", "as_of", "source", "coverage", "window", "universe", "gaps")
              if key in section}
    metrics = section.get("metrics") or {}
    compact = {}
    metric_items = list(metrics.items())
    if max_metrics is not None and len(metric_items) > max_metrics:
        preferred = ("latest", "20d", "breadth", "participation", "spy", "rsp", "qqq", "dia", "iwm",
                     "tlt", "gld", "uso", "vix", "curve")
        metric_items.sort(key=lambda pair: (min((preferred.index(term) for term in preferred
                                                if term in str(pair[0]).lower()), default=len(preferred)),
                                            str(pair[0])))
        metric_items = metric_items[:max_metrics]
    for metric_id, metric in metric_items:
        if not isinstance(metric, dict):
            continue
        window = str(metric.get("window", "")).lower()
        key = str(metric_id).lower()
        # Related sections provide a small, common-window dashboard context.
        # Keep all focal metrics; chart payloads and raw histories are excluded.
        if not focal and not any(token in f"{key} {window}" for token in
                                 ("latest", "20d", "20 session", "5d", "5 session")):
            continue
        compact[metric_id] = {field: metric.get(field) for field in
                              ("value", "unit", "as_of", "source", "window", "definition", "status")
                              if field in metric}
    result["metrics"] = compact
    if focal:
        # Catalyst records are already concise and include event dates/status.
        for key in ("events", "recent_developments", "recent_developments_refreshed_at",
                    "recent_developments_status", "event_reactions", "classifications",
                    "cycle", "reversals", "basket_configuration"):
            if key in section:
                result[key] = section[key]
    return result


def market_chat_preflight(snapshot: dict, question: str, *, search_mode: str,
                          request_limit_usd: float, daily_limit_usd: float,
                          daily_spend_usd: float, api_configured: bool = True,
                          model: str = DEFAULT_MODEL) -> dict:
    """Explain all local Ask AI disabled conditions without calling a provider."""
    reasons = []
    if not str(question or "").strip():
        reasons.append("Enter a question to enable Ask AI.")
    if not isinstance(snapshot, dict) or not snapshot.get("fingerprint"):
        reasons.append("Market snapshot is unavailable; wait for local data loading to finish.")
    if not api_configured:
        reasons.append("DeepSeek is not configured. Add DEEPSEEK_API_KEY to .env or Streamlit secrets.")
    try:
        estimate = estimate_market_chat_cost({"current_question": question or "Question preview",
                                               "snapshot": snapshot}, search_mode=search_mode, model=model)
    except ValueError as exc:
        estimate = None
        reasons.append(str(exc))
    if estimate is not None and estimate > request_limit_usd:
        reasons.append(f"Estimated ceiling ${estimate:.4f} exceeds per-request cap ${request_limit_usd:.2f}.")
    if estimate is not None and daily_spend_usd + estimate > daily_limit_usd:
        reasons.append(f"Estimated ceiling ${estimate:.4f} exceeds remaining daily budget ${max(0, daily_limit_usd-daily_spend_usd):.4f}.")
    return {"enabled": not reasons, "reasons": reasons, "estimated_cost_usd": estimate}


def deepseek_chat_configured() -> bool:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return True
    try:
        import streamlit as st
        return bool(str(st.secrets.get("DEEPSEEK_API_KEY", "") or "").strip())
    except Exception:
        return False


def build_market_chat_context(snapshot: dict, section_id: str, highlight: dict | None,
                              report: dict | None, history: list[dict], question: str,
                              *, user_observations=(), scenario_notes=()) -> dict:
    """Build bounded section-first chat context without private portfolio fields."""
    if section_id not in (*SECTION_ORDER, *LEGACY_SECTION_ORDER, "market_wide", "market_premarket"):
        raise ValueError("Unknown Market section.")
    sections = snapshot.get("sections") or {}
    if section_id in {"market_wide", "market_premarket"}:
        relevant = {}
        for key, value in sections.items():
            compact = _compact_section_context(value, focal=False, max_metrics=18)
            if isinstance(value, dict):
                for structured_key in ("events", "recent_developments", "recent_developments_refreshed_at",
                                       "recent_developments_status", "risk_appetite", "regime_evidence",
                                       "regime_transition", "turning_point_watch", "broad_index_comparisons",
                                       "basket_configuration", "hypotheses", "historical_context"):
                    if structured_key in value:
                        compact[structured_key] = value[structured_key]
            relevant[key] = compact
    else:
        relevant = {section_id: _compact_section_context(
            sections.get(section_id, {"status": "unavailable", "metrics": {}, "gaps": []}), focal=True)}
    related = {
        "relative_strength": ("sector_rotation", "rates_macro", "regime"),
        "vix": ("regime", "sector_rotation", "rates_macro"),
        "rates_macro": ("vix", "regime", "sector_rotation"),
        "catalysts": ("regime", "sector_rotation", "rates_macro"),
        "sector_rotation": ("relative_strength", "regime", "rates_macro"),
    }.get(section_id, ("regime", "sector_rotation"))
    for key in (() if section_id == "market_wide" else related):
        if key in sections:
            relevant[key] = _compact_section_context(sections[key], focal=False)
    saved = None
    if report:
        saved = {"generated_at": report.get("generated_at"), "snapshot_as_of": (report.get("snapshot") or {}).get("as_of"),
                 "highlight": highlight}
        if section_id in {"market_wide", "market_premarket"}:
            try:
                from dashboard.market_snapshot import parse_market_analysis_json
                parsed = parse_market_analysis_json(report.get("report_text", ""),
                    report.get("snapshot") or {}, report.get("sources") or [])
                briefing = parsed.get("briefing") or {}
                saved["briefing"] = {key: briefing.get(key) for key in (
                    "executive_takeaway", "risks", "supportive", "sector_implications",
                    "conflicting_evidence", "catalysts", "conditions", "hypotheses")}
                transition = briefing.get("transition_outlook") or {}
                saved["transition_outlook"] = {key: transition.get(key) for key in (
                    "current_regime_interpretation", "emerging_direction", "scenarios", "main_uncertainty")}
            except ValueError:
                saved["briefing_unavailable"] = "The saved report predates a compatible structured schema."
    messages = []
    for row in (history or [])[-8:]:
        if row.get("role") not in {"user", "assistant"}:
            continue
        entry = {"role": row.get("role"), "content": str(row.get("content", ""))[:1800],
                 "created_at": row.get("created_at")}
        if row.get("role") == "assistant" and row.get("source_metadata"):
            entry["retrieved_sources"] = [
                {key: source.get(key) for key in ("title", "url", "date") if source.get(key)}
                for source in row.get("source_metadata", [])[:5]
                if isinstance(source, dict) and str(source.get("url", "")).startswith("https://")]
        messages.append(entry)
    # Send each section once: complete focal/related context plus the remaining
    # compact metrics so a user can ask cross-section questions without copying
    # the focal section a second time or sending chart payloads.
    other_sections = ({ } if section_id in {"market_wide", "market_premarket"} else {
        key: _compact_section_context(value, focal=False, max_metrics=10)
        for key, value in sections.items() if key not in relevant})
    view = snapshot.get("view_state") or {}
    selected_comparison = view.get("selected_comparison_result") or {}
    comparison_id = None
    if selected_comparison.get("symbols") and selected_comparison.get("window"):
        comparison_id = f"{selected_comparison['symbols'][0]}_{selected_comparison['symbols'][1]}_{selected_comparison['window']}D"
    comparison_data = ((sections.get("relative_strength") or {}).get("comparisons") or {}).get(comparison_id)
    observations = [{key: item.get(key) for key in ("observation_text", "observed_at", "session_type", "symbols", "verification_status")}
                    for item in (user_observations or [])[-8:] if isinstance(item, dict)]
    return {"schema_version": snapshot.get("schema_version"), "snapshot_fingerprint": snapshot.get("fingerprint"),
            "snapshot_as_of": snapshot.get("as_of"), "section_id": section_id,
            "section_data": relevant, "other_dashboard_sections": other_sections,
            "selected_view": {"relative_strength": view.get("relative_strength"),
                              "selected_comparison": selected_comparison,
                              "standard_comparison_metrics": comparison_data},
            "saved_research": saved,
            "saved_briefing": ((report or {}).get("report_text", "")[:10000]
                               if section_id not in {"market_wide", "market_premarket"} else None),
            "saved_market_briefing_context": saved.get("briefing") if saved else None,
            "saved_scenarios": list(scenario_notes or [])[-12:],
            "user_supplied_observations": observations,
            "saved_sources": [{key: source.get(key) for key in ("title", "url", "date") if source.get(key)}
                              for source in ((report or {}).get("sources") or [])[:10]],
            "recent_messages": messages,
            "current_question": str(question).strip()}


def estimate_market_chat_cost(context: dict, *, search_mode: str, output_tokens: int = CHAT_OUTPUT_TOKENS,
                              model: str = DEFAULT_MODEL) -> float:
    if search_mode not in {"dashboard_analysis", "dashboard_web"}:
        raise ValueError("Unsupported chat research mode.")
    if not 256 <= int(output_tokens) <= 3000:
        raise ValueError("Chat answer allowance must be 256–3,000 tokens.")
    prompt_size = len(json.dumps(context, ensure_ascii=False, default=str))
    input_tokens = max(1000, prompt_size // 3 + 500)
    if model not in MODEL_PRICES:
        raise ValueError("Select a supported DeepSeek model.")
    input_price, output_price = MODEL_PRICES[model]
    search = SEARCH_COST_PER_REQUEST if search_mode == "dashboard_web" else 0
    return search + (input_tokens * input_price + int(output_tokens) * output_price) / 1_000_000


def answer_market_section_question(context: dict, *, search_mode: str, request_limit_usd: float,
                                   daily_limit_usd: float, output_tokens: int = CHAT_OUTPUT_TOKENS,
                                   model: str = DEFAULT_MODEL,
                                   database_path=None, http=requests) -> dict:
    """Answer one user-submitted question. No call is made by this module implicitly."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DeepSeek is not configured; add DEEPSEEK_API_KEY to the local .env or Streamlit secrets.")
    if model not in MODEL_PRICES:
        raise ValueError("Select a supported DeepSeek model.")
    estimate = estimate_market_chat_cost(context, search_mode=search_mode, output_tokens=output_tokens, model=model)
    daily = deepseek_market_research_daily_spend(database_path=database_path) if database_path else deepseek_market_research_daily_spend()
    if estimate > request_limit_usd:
        raise RuntimeError(f"Estimated chat ceiling ${estimate:.4f} exceeds the ${request_limit_usd:.2f} per-request cap.")
    if daily + estimate > daily_limit_usd:
        raise RuntimeError(f"Estimated chat ceiling ${estimate:.4f} exceeds today's remaining ${max(0, daily_limit_usd-daily):.4f} budget.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    sources, search_count, input_tokens, output_count = [], 0, None, None
    reasoning_tokens = visible_output_tokens = None
    reported_cost = None
    actual_model = None
    endpoints = []
    completion_status = "started"
    error_category = ""
    actual_cost = estimate
    cost_kind = "upper-bound estimate"
    ledger_status = "failed"
    connect_timeout, read_timeout = bounded_deepseek_timeouts()
    try:
        if search_mode == "dashboard_web":
            question = str(context["current_question"])
            observation_text = " ".join(
                str(item.get("observation_text", "")) for item in context.get("user_supplied_observations", [])
                if isinstance(item, dict))
            search_fields = bounded_search_query(f"{question} {observation_text} {context['section_id']} market data",
                                                 original_question=question)
            endpoints.append("POST /v1/tools/search")
            response = http.post(f"{BASE}/v1/tools/search", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                 json={**search_fields, "count": 5, "need_content": False}, timeout=(connect_timeout, read_timeout))
            if not response.ok:
                detail = (" Search query fields are limited to 128 characters; the app now bounds them."
                          if response.status_code == 400 else "")
                raise RuntimeError(f"DeepSeek Search returned HTTP {response.status_code}.{detail}")
            sources = _search_results(response)
            search_count = 1
            if not sources:
                raise RuntimeError("Web verification returned no usable sources; no outside claim was verified.")
        premarket_mode = context.get("conversation_mode") == "premarket_what_if"
        premarket_instructions = (
            "This is Premarket What-If analysis. Treat the user's text as an unverified report unless dated evidence in the local snapshot or retrieved sources corroborates it. Never imply a connected live premarket quote feed exists. Identify the local snapshot's last completed session and keep it distinct from the user observation timestamp/session. Explain plausible mechanisms and competing interpretations without assigning probabilities or naming a most-likely path. Use the supplied local historical_context only for actual sample counts/values; if it does not contain an applicable history statistic, say the pattern is a research hypothesis and explain what comparison would test it. Do not invent historical frequencies, seasonal returns, analogues or causal links. For follow-up questions, use prior user/assistant turns and keep the JSON structure. Return one complete JSON object only, with no Markdown fence or introductory prose, using this exact shape: {\"setup_meaning\":string,\"paths\":[3 or 4 objects with \"path_id\",\"name\",\"if_condition\",\"then_interpretation\",\"why_it_might_happen\",\"supporting_or_contradicting_evidence\",\"indicators_to_watch\" all nonempty strings],\"most_important_uncertainty\":string,\"session_watch\":[1–6 short observable checks]}. Do not add probabilities, odds, scores, trade instructions, or fields. Keep each path compact and measurable. Include possible explanations such as earnings, rates/real yields, dollar, positioning, liquidity, policy, supply/demand and catalysts only when relevant; do not claim any is verified without sources. In web mode cite dated sources in the string fields with URLs or source titles/dates."
            if premarket_mode else ""
        )
        prompt = (
            "You are a conversational, educational Market Intelligence Analyst answering a follow-up about a frozen local dashboard snapshot. "
            "Answer the economic or market question directly whenever a sound general mechanism can explain it, even when the snapshot cannot verify the specific event. "
            "For example, yields can fall after a policy-rate increase if the increase was already expected, forward guidance became less restrictive, inflation expectations or real-rate expectations declined, or investors sought duration; explain which mechanisms fit and what would distinguish them. "
            "Correct a materially incorrect premise briefly and respectfully. Keep the answer concise, plain-spoken, and useful to a curious investor. "
            "When helpful, use this order: Short answer; Possible drivers (two or three competing hypotheses); What our dashboard suggests (dated measurements); What to check next (specific indicators or evidence). Do not force headings for a short answer. "
            "Clearly label plausible mechanisms as hypotheses, not verified causes. Relate them to the supplied dated measurements without claiming the local data proves causality. "
            "Any user_supplied_observations are explicitly user-reported, not provider measurements or independently verified facts. Do not overwrite or reinterpret them as cached market data. In Dashboard + Web Verification, verify them only through retrieved sources and state when no corroboration was found. "
            "Dashboard Analysis mode: do not search; provide useful general mechanisms and briefly say which event-specific facts remain unverified. Do not refuse a conceptual answer solely because local evidence is limited. "
            "Dashboard + Web Verification mode: search for outside evidence relevant to the specific question; cite important factual claims with source URLs and dates, distinguish retrieved facts from local measurements, and do not let timing alone establish causation. "
            "Breadth rule: keep absolute returns, relative performance, and changes in each separate. RSP outperforming SPY or sectors outperforming SPY does not by itself mean absolute breadth is improving. If SPY/RSP and most sector ETFs are down, describe relative resilience during a selloff, not healthy broadening. Treat sector ETFs as participation proxies, never constituent-level breadth unless constituent data is supplied. "
            "Recent conversation messages may contain dated retrieved_sources from earlier explicit web checks; you may reuse that saved evidence when it directly applies, and you must not present an older source as current if its date is stale. "
            + premarket_instructions + " "
            "Avoid lengthy disclaimers, repetitive source warnings, unsupported probabilities or return forecasts. Do not change the saved Market Briefing.\n\n"
            f"Mode: {search_mode}\nFrozen context:\n{json.dumps(context, ensure_ascii=False, default=str)}\n"
            f"Retrieved sources:\n{json.dumps(sources, ensure_ascii=False, default=str)}"
        )
        import httpx
        from openai import OpenAI
        timeout = httpx.Timeout(read=read_timeout, connect=connect_timeout, write=min(read_timeout, 30), pool=connect_timeout)
        client = OpenAI(api_key=api_key, base_url=BASE, timeout=timeout, max_retries=0)
        endpoints.append("POST /responses")
        response = client.responses.create(model=model, input=prompt, max_output_tokens=int(output_tokens),
                                           reasoning={"effort": "none"})
        actual_model = getattr(response, "model", None) if not isinstance(response, dict) else response.get("model")
        text = extract_responses_text(response)
        status = getattr(response, "status", None) if not isinstance(response, dict) else response.get("status")
        completion_status = str(status or "unknown")
        if status != "completed" or not text.strip():
            reason = (getattr(getattr(response, "incomplete_details", None), "reason", None)
                      if not isinstance(response, dict) else (response.get("incomplete_details") or {}).get("reason"))
            raise RuntimeError(f"DeepSeek answer was not completed ({status or 'unknown'}; {reason or 'no visible answer'}).")
        if context.get("conversation_mode") == "premarket_what_if":
            parse_premarket_what_if(text)
        usage = getattr(response, "usage", None) if not isinstance(response, dict) else response.get("usage")
        get = (lambda obj, key: obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None))
        input_tokens, output_count = get(usage, "input_tokens"), get(usage, "output_tokens")
        usage_details = get(usage, "output_tokens_details")
        reasoning_tokens = get(usage_details, "reasoning_tokens")
        reported_cost = get(usage, "cost_usd")
        visible_output_tokens = (max(0, output_count - reasoning_tokens)
                                 if output_count is not None and reasoning_tokens is not None else output_count)
        if input_tokens is not None and output_count is not None:
            input_price, output_price = MODEL_PRICES[model]
            actual_cost = search_count * SEARCH_COST_PER_REQUEST + (input_tokens * input_price + output_count * output_price) / 1_000_000
            cost_kind = "usage-based estimate"
        ledger_status = "complete"
        return {"text": text.strip(), "model": actual_model or "Actual model not reported",
                "requested_model": model, "actual_model": actual_model,
                "endpoint": f"{BASE}/responses",
                "search_endpoint": f"{BASE}/v1/tools/search" if search_mode == "dashboard_web" else None,
                "reasoning_effort": "none", "output_token_allowance": int(output_tokens),
                "reasoning_tokens": get(get(usage, "output_tokens_details"), "reasoning_tokens"),
                "reported_cost_usd": reported_cost,
                "sources": sources, "search_succeeded": bool(sources) if search_mode == "dashboard_web" else False,
                "input_tokens": input_tokens, "output_tokens": output_count,
                "actual_cost_usd": actual_cost, "cost_kind": cost_kind,
                "duration_seconds": round(time.monotonic() - started, 2),
                "request_id": request_id, "search_requests": search_count,
                "completion_status": completion_status}
    except Exception as exc:
        if completion_status == "incomplete":
            category = "incomplete_output"
        elif completion_status == "completed":
            category = "empty_or_invalid_answer"
        else:
            category = "timeout" if "timeout" in type(exc).__name__.lower() else transport_error_category(exc)
            completion_status = "provider_error"
        error_category = category
        raise RuntimeError(f"{category}: {str(exc)[:400]}") from None
    finally:
        record_deepseek_market_research_usage(requested_at=now, model=model,
            search_requests=search_count, input_tokens=input_tokens, output_tokens=output_count,
            estimated_cost_usd=float(actual_cost), status=ledger_status,
            database_path=database_path) if database_path else record_deepseek_market_research_usage(
                requested_at=now, model=model, search_requests=search_count,
                input_tokens=input_tokens, output_tokens=output_count,
                estimated_cost_usd=float(actual_cost), status=ledger_status)
        try:
            record_market_ai_request_audit(
                request_id=request_id, requested_at=now,
                purpose=f"section_chat:{context.get('section_id', 'unknown')}",
                evidence_mode=search_mode,
                requested_model=model, actual_model=actual_model,
                endpoint="; ".join(dict.fromkeys(endpoints)) or "No provider endpoint reached",
                reasoning_effort="none", output_token_allowance=int(output_tokens),
                input_tokens=input_tokens, output_tokens=output_count,
                reasoning_tokens=reasoning_tokens, visible_output_tokens=visible_output_tokens,
                search_requests=search_count, estimated_cost_usd=float(actual_cost),
                reported_cost_usd=reported_cost, completion_status=completion_status,
                error_category=error_category,
                database_path=database_path) if database_path else record_market_ai_request_audit(
                    request_id=request_id, requested_at=now,
                    purpose=f"section_chat:{context.get('section_id', 'unknown')}",
                    evidence_mode=search_mode,
                    requested_model=model, actual_model=actual_model,
                    endpoint="; ".join(dict.fromkeys(endpoints)) or "No provider endpoint reached",
                    reasoning_effort="none", output_token_allowance=int(output_tokens),
                    input_tokens=input_tokens, output_tokens=output_count,
                    reasoning_tokens=reasoning_tokens, visible_output_tokens=visible_output_tokens,
                    search_requests=search_count, estimated_cost_usd=float(actual_cost),
                    reported_cost_usd=reported_cost, completion_status=completion_status,
                    error_category=error_category)
        except Exception:
            # Audit failure must not replace the visible provider result/error.
            pass

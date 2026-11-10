"""Explicit, cached DeepSeek web research for public Market Intelligence data."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from types import SimpleNamespace

import requests
from dotenv import load_dotenv

from dashboard.ai_providers import PROJECT_ROOT, normalize_error
from dashboard.data_access import (
    deepseek_market_research_daily_spend, get_deepseek_market_research_by_key,
    get_latest_deepseek_market_research, record_deepseek_market_research_usage,
    record_market_ai_request_audit,
    save_deepseek_market_research_report,
)
from dashboard.market_snapshot import parse_market_analysis_json, snapshot_with_recent_catalysts

BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEEP_RESEARCH_MODEL = "deepseek-v4-pro"
SEARCH_QUERY_MAX_CHARS = 128
MARKET_ANALYST_PROMPT_VERSION = "market-analyst-v13"
# The full-dashboard response contains eight section highlights plus one
# integrated briefing. 2,500 tokens truncated otherwise valid responses in
# live use, so Fast Briefing keeps reasoning disabled but allows a bounded
# 5,000 visible-output budget.
FAST_BRIEFING_OUTPUT_TOKENS = 12_000
DEEP_RESEARCH_OUTPUT_TOKENS = 48_000
FAST_BRIEFING_RETRY_OUTPUT_TOKENS = 24_000
DEEP_RESEARCH_RETRY_OUTPUT_TOKENS = 64_000
DEFAULT_OUTPUT_TOKENS = FAST_BRIEFING_OUTPUT_TOKENS
MAX_MARKET_RESEARCH_OUTPUT_TOKENS = 64_000  # bounded local ceiling; actual account limit checked via /models
DEEP_RESEARCH_OUTPUT_OPTIONS = (DEEP_RESEARCH_OUTPUT_TOKENS,)
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_READ_TIMEOUT_SECONDS = 180
SUPPORTED_RESPONSES_MODELS = {"deepseek-flash", "deepseek-v4-pro"}
SEARCH_COST_PER_REQUEST = 0.00075
MODEL_PRICES = {"deepseek-flash": (0.30, 1.20), "deepseek-v4-pro": (1.32, 3.96)}
TOPICS = {
    "Full Market Intelligence Snapshot": "Analyze the complete canonical Market Intelligence dashboard snapshot as one coherent assessment",
    "Full market regime and capital rotation": "Assess current US market regime and capital rotation",
    "VIX and short-term market stress": "Assess current VIX behavior and short term US market stress",
    "Treasury yields, Fed communication and credit risk": "Research latest US Treasury yields Fed communication and credit risk",
    "Consumer discretionary versus staples": "Assess US consumer discretionary versus staples leadership",
    "Semiconductors versus AI earnings and financing": "Research semiconductor leadership AI earnings and financing",
    "User-selected sector comparison": "Research the selected ETF comparison and current sector evidence",
}


def configured_market_research_model() -> str:
    """Safe default; the Market UI may explicitly select another supported model."""
    return DEFAULT_MODEL


def market_analysis_model(depth: str, selected_model: str | None = None) -> str:
    """Choose the explicit model for full-market analysis without silent fallback.

    Fast Briefing follows the saved user selection. Deep Research is deliberately
    routed to V4 Pro; account access is checked by the normal request preflight.
    Section chats continue using the separately saved selection.
    """
    if depth == "Deep Research":
        return DEEP_RESEARCH_MODEL
    if depth == "Fast Briefing":
        model = (selected_model or DEFAULT_MODEL).strip()
        if model not in SUPPORTED_RESPONSES_MODELS:
            raise ValueError("Choose a supported DeepSeek model for Fast Briefing.")
        return model
    raise ValueError("Choose Fast Briefing or Deep Research.")


def bounded_search_query(query: str, *, original_question: str | None = None) -> dict[str, str]:
    """Conform Search API query fields to DeepSeek's documented 1–128 char limit."""
    def trim(value: str) -> str:
        normalized = " ".join(str(value or "").split())
        if len(normalized) <= SEARCH_QUERY_MAX_CHARS:
            return normalized
        shortened = normalized[:SEARCH_QUERY_MAX_CHARS].rsplit(" ", 1)[0].strip()
        return shortened or normalized[:SEARCH_QUERY_MAX_CHARS]
    result = {"query": trim(query)}
    if original_question is not None:
        result["ori_question"] = trim(original_question)
    if not result["query"]:
        raise ValueError("Search query cannot be empty.")
    return result


def market_research_mode_settings(mode: str, output_token_limit: int | None = None) -> tuple[str, int]:
    """Translate the Market tab's two modes into bounded API settings."""
    if mode == "Fast Briefing":
        if output_token_limit not in (None, FAST_BRIEFING_OUTPUT_TOKENS):
            raise ValueError("Fast Briefing output allowance is selected automatically.")
        return "none", FAST_BRIEFING_OUTPUT_TOKENS
    if mode == "Deep Research":
        if output_token_limit not in (None, DEEP_RESEARCH_OUTPUT_TOKENS):
            raise ValueError("Deep Research output allowance is selected automatically.")
        return "high", DEEP_RESEARCH_OUTPUT_TOKENS
    raise ValueError("Choose Fast Briefing or Deep Research.")


def market_research_retry_settings(mode: str) -> tuple[str, int]:
    """One explicit larger synthesis retry; caller must recheck budget and account limits."""
    if mode == "Fast Briefing":
        return "none", FAST_BRIEFING_RETRY_OUTPUT_TOKENS
    if mode == "Deep Research":
        return "high", DEEP_RESEARCH_RETRY_OUTPUT_TOKENS
    raise ValueError("Choose Fast Briefing or Deep Research.")


class MarketResearchError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


def bounded_deepseek_timeouts(connect_seconds: int | None = None, read_seconds: int | None = None) -> tuple[int, int]:
    """Return safe, bounded connect/read limits; slow generation gets a longer read window."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    connect = connect_seconds if connect_seconds is not None else int(os.getenv("DEEPSEEK_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS))
    read = read_seconds if read_seconds is not None else int(os.getenv("DEEPSEEK_READ_TIMEOUT_SECONDS", DEFAULT_READ_TIMEOUT_SECONDS))
    return max(3, min(int(connect), 30)), max(15, min(int(read), 300))


def timeout_error_category(error: Exception) -> str | None:
    """Classify timeouts without including request URLs, headers, or prompt contents."""
    names = _exception_chain_names(error)
    if any("connecttimeout" in name for name in names):
        return "connect_timeout"
    if any("readtimeout" in name or "timeoutexpired" in name for name in names):
        return "read_timeout"
    if any("timeout" in name for name in names):
        return "timeout"
    return None


def _exception_chain_names(error: Exception) -> list[str]:
    names, current, seen = [], error, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__.lower())
        current = current.__cause__ or current.__context__
    return names


def transport_error_category(error: Exception) -> str:
    """Return a safe transport classification based on exception classes only."""
    timeout = timeout_error_category(error)
    if timeout:
        return timeout
    names = _exception_chain_names(error)
    if any("gaierror" in name or "name resolution" in name or "dns" in name for name in names):
        return "dns_failure"
    if any("ssl" in name or "certificate" in name for name in names):
        return "tls_failure"
    if any("proxy" in name for name in names):
        return "proxy_failure"
    if any("connecterror" in name or "connectionerror" in name or "apiconnectionerror" in name for name in names):
        return "network_connection_failure"
    return "provider_error"


def _progress(callback, phase: str) -> None:
    if callback:
        callback(phase)


def _stream_response(client, request_args: dict, *, progress_callback=None):
    """Stream only semantic progress and visible answer text, never reasoning text."""
    stream = client.responses.create(**request_args, stream=True)
    # Test doubles and some compatible SDK layers may return a final object.
    if not hasattr(stream, "__iter__") or hasattr(stream, "output_text"):
        return stream, [], ""
    event_types, visible_chunks, final_response = [], [], None
    for event in stream:
        event_type = str(_field(event, "type", "unknown"))
        event_types.append(event_type)
        if event_type == "response.created":
            _progress(progress_callback, "Connection established · answer generation starting")
        elif event_type == "response.web_search_call.in_progress":
            _progress(progress_callback, "Search in progress")
        elif event_type == "response.web_search_call.searching":
            _progress(progress_callback, "Search in progress")
        elif event_type == "response.web_search_call.completed":
            _progress(progress_callback, "Search completed · answer generation in progress")
        elif event_type in {"response.reasoning_text.delta", "response.reasoning_text.done"}:
            _progress(progress_callback, "Answer reasoning in progress · private reasoning is not displayed")
        elif event_type in {"response.in_progress", "response.output_item.added"}:
            item = _field(event, "item")
            if _field(item, "type") == "reasoning":
                _progress(progress_callback, "Answer reasoning in progress · private reasoning is not displayed")
        elif event_type == "response.output_text.delta":
            delta = _field(event, "delta", "")
            if isinstance(delta, str):
                visible_chunks.append(delta)
                _progress(progress_callback, "Answer text received")
        elif event_type == "response.output_text.done":
            text = _field(event, "text", "")
            if isinstance(text, str) and text:
                visible_chunks = [text]
        elif event_type in {"response.completed", "response.incomplete", "response.failed"}:
            final_response = _field(event, "response")
            _progress(progress_callback, {"response.completed": "Completed answer",
                                          "response.incomplete": "Incomplete answer",
                                          "response.failed": "Provider reported failure"}[event_type])
    text = "".join(visible_chunks)
    if final_response is None:
        final_response = SimpleNamespace(output_text=text, status="unknown", output=[], usage=None)
    elif not _field(final_response, "output_text", "") and text:
        # Keep semantic final response metadata while recovering visible deltas.
        if isinstance(final_response, dict):
            final_response = {**final_response, "output_text": text}
        else:
            try:
                final_response.output_text = text
            except Exception:
                final_response = SimpleNamespace(output_text=text, status=_field(final_response, "status", "unknown"),
                    output=_field(final_response, "output", []), usage=_field(final_response, "usage"),
                    incomplete_details=_field(final_response, "incomplete_details"), model=_field(final_response, "model"))
    return final_response, event_types, text


def public_market_snapshot(*, comparison=None, vix=None, sector_metrics=None, treasury=None,
                           missing=(), timeframe=None, upcoming_events=()) -> dict:
    """Whitelist public aggregate metrics; intentionally has no portfolio parameter."""
    vix = vix or {}
    safe_comparison = None
    if comparison and comparison.get("status") == "available":
        safe_comparison = {key: comparison.get(key) for key in ("symbols", "window", "as_of", "return_first", "return_second", "excess_pp", "price_basis")}
    public_events = []
    for event in upcoming_events:
        if event.get("category") not in {"Macro", "Fed", "Political/Other"}:
            continue
        public_events.append({key: event.get(key) for key in (
            "name", "category", "start_utc", "source_timezone", "source_url", "provider", "status", "last_verified_at"
        ) if event.get(key) is not None})
    return {
        "as_of": max([str((sector_metrics or {}).get("as_of") or ""), str((vix.get("observation_date") or "")),
                      str((treasury or [{}])[-1].get("observation_date") if treasury else "")], default="") or None,
        "timeframe_sessions": timeframe,
        "selected_comparison": safe_comparison,
        "vix": {key: vix.get(key) for key in ("observation_date", "retrieved_at", "source", "status", "close", "prior_close", "daily_change_points", "daily_change_percent", "change_3_sessions", "change_5_sessions", "open", "high", "low", "peak_surge_percent", "peak_unwound_percent", "peak_retained_percent", "spike_status", "five_session_ohlc", "percentile_1y", "classifications")},
        "sector_metrics": {key: (sector_metrics or {}).get(key) for key in ("as_of", "above_sma50", "above_sma200", "outperformers", "dispersion_pp", "defensive_vs_cyclical_pp")},
        "treasury_latest": ({key: treasury[-1].get(key) for key in ("observation_date", "two_year", "ten_year", "provider")} if treasury else None),
        "upcoming_public_events": public_events[:8],
        "missing_data": list(missing),
    }


def _market_analyst_prompt(question: str, snapshot: dict, sources: list[dict], search_mode: str) -> str:
    from dashboard.market_snapshot import SECTION_ORDER, SECTION_LABELS, metric_references
    allowed_metric_refs = sorted(metric_references(snapshot) - {"catalysts.event_count"})
    allowed_refs_by_section = {
        section_id: sorted(f"{section_id}.{metric_id}" for metric_id, metric in
                           ((snapshot.get("sections", {}).get(section_id) or {}).get("metrics") or {}).items()
                           if isinstance(metric, dict) and metric.get("value") is not None
                           and not (section_id == "catalysts" and metric_id == "event_count"))
        for section_id in SECTION_ORDER
    }
    relative = (snapshot.get("sections") or {}).get("relative_strength") or {}
    comparison_ids = list(relative.get("ai_comparison_ids") or [])
    # Chart selection is intentionally not analysis input: the report covers the
    # whole fixed comparison library and remains reusable as users explore pairs.
    model_snapshot = {key: value for key, value in snapshot.items()
                      if key not in {"view_state", "view_fingerprint"}}
    # Calendar provenance remains in the canonical saved snapshot for audit and
    # display, but it is not useful evidence for market-impact analysis. Sending
    # verification labels beside every event has repeatedly caused the model to
    # discuss verification counts instead of catalyst timing and implications.
    model_snapshot = json.loads(json.dumps(model_snapshot, ensure_ascii=False, default=str))
    catalyst_section = (model_snapshot.get("sections") or {}).get("catalysts") or {}
    catalyst_section.pop("as_of", None)  # event dates below are the useful temporal context
    catalyst_section.pop("source", None)
    catalyst_section.pop("coverage", None)  # routine event counts are not an impact analysis
    catalyst_section["events"] = [
        {key: event.get(key) for key in (
            "name", "category", "event_date", "start_utc", "end_utc",
            "earnings_session", "related_symbols", "date_conflict",
        ) if event.get(key) is not None}
        for event in catalyst_section.get("events", []) if isinstance(event, dict)
    ]
    catalyst_metrics = catalyst_section.get("metrics") or {}
    catalyst_metrics.pop("event_count", None)
    for metric in catalyst_metrics.values():
        if isinstance(metric, dict):
            metric.pop("as_of", None)
    catalyst_section["metrics"] = catalyst_metrics
    model_snapshot["limitations"] = [
        item for item in model_snapshot.get("limitations", [])
        if "catalyst" not in str(item).lower() or "unverified" not in str(item).lower()
    ]
    return (
        "You are the dashboard-first Market Intelligence Analyst. The canonical local snapshot is the authoritative source for numerical market observations. "
        "Write an intellectually curious, decision-useful research briefing. Do not merely restate the indicators: connect combinations across equities, rates, credit, volatility, catalysts and macro proxies, then explain plausible economic mechanisms and why they matter for market risk or sector rotation. "
        "For each major hypothesis, distinguish the measured observations, cross-market interpretation, two or three competing explanations where useful (including earnings, growth/inflation expectations, policy, real yields, dollar, positioning, liquidity, supply/demand or technical effects), and measurable confirmation/invalidation conditions over the next several sessions. Explore forward paths conditionally, never as a prediction. "
        "Use historical or seasonal context only when the supplied local historical_context contains the relevant dated sample, or when web verification retrieved dated evidence. State the sample definition and size when citing a historical result. If evidence is unavailable, frame the relationship as a testable research hypothesis and explain what data would test it; never invent frequencies, analogues, win rates or backtests. "
        "The local snapshot is based on completed observations and may not contain live premarket or intraday quotes. Do not imply live market knowledge. Treat user-supplied observations as unverified unless corroborated by dated retrieved evidence. "
        "Prefer divergences, changes in direction, cross-indicator relationships and competing explanations over reciting values already visible. Routine source caveats belong in concise evidence notes, not the headline. "
        "Treat data as observation and interpretation as hypothesis. Never claim ETF price differences prove fund flows or causation; do not invent probabilities, future returns, catalysts, causes, dates, or missing evidence. "
        "For TLT, GLD and USO consider multiple plausible macro explanations. TLT/GLD are bond/gold ETF price proxies; USO is a futures-based oil ETF proxy and may diverge from spot WTI because of contract rolls and tracking effects. Never equate these proxy prices with policy expectations, physical commodity demand or capital flows. If asked about actual WTI spot prices, use dated external evidence only when web verification is enabled; otherwise identify the missing benchmark data. "
        "For the Calendar highlight, treat the supplied upcoming events as dates listed in the dashboard calendar for planning; the prompt intentionally omits routine verification labels. Do not discuss event counts, confirmation status, source verification, or calendar provenance in the headline or insight. Mention a date conflict only when an explicit date_conflict field is supplied. Lead with the nearest one to three material catalyst dates and event names, then explain the macro mechanism or sensitive sectors/assets, plausible positive and negative interpretations, and which market reaction would support or weaken the current regime hypothesis. Use conditional language for scheduled events; do not claim an outcome in advance. Keep sources and verification detail secondary to market implications. "
        "Use the catalyst section's dated recent_developments cache and retrieved external evidence to connect market moves to real-world developments only where source dates and observed market dates support the comparison. Label source-backed development, observed response, plausible interpretation, and unresolved causal uncertainty distinctly. Consider alternatives; timing alone is not proof of causality. Never invent earnings surprises or numerical facts. "
        "For web verification, external factual claims need dated source IDs; prefer official sources and never let old commentary outweigh newer local measurements. "
        "The sector_rotation snapshot includes deterministic risk_appetite, regime_transition, the versioned three-regime regime_evidence monitor, separate stress-easing/recovery transition characteristics, and five turning_point_watch signals: volatility_stabilization, technology_mag7_recovery, broad_market_participation, defensive_leadership, credit_confirmation. The regime evidence shares are rebuilt from signed group evidence for Risk-on Expansion, Rotation / Mixed and Risk-off Contraction; they are relative compatibility support, not forecast probabilities, likelihoods, or model confidence. Stress easing is not a fourth regime; confirmed recovery requires explicitly observed prior stress. Keep every Python state and historical record unchanged. Interpret why evidence supports or contradicts a regime, but do not rewrite its label or treat shares as probabilities. Rates/macro proxies are context only where identified as non-voting. For technology/Mag 7 compare XLK, SMH, QQQ, and MAGS against SPY at 5D and the leadership window; distinguish a short rebound from a persistent relative-strength crossover. Assess whether technology recovery broadens through RSP/IWM and sector breadth, whether defensives still lead their configured cyclical basket, and whether credit/VIX agree. Treat HYG/LQD and HYG/IEF as credit-sensitive ETF price proxies, not observed spreads. Explain measured changes and contradictions without claiming ETF price action proves flows or causes. No AI field may alter Python state or historical records. "
        "The integrated briefing executive_takeaway summarizes the observed regime in one or two sentences. The transition_outlook focuses on change and includes concise interpretations of all five Python turning-point signals, the most plausible continuation/broadening/handoff/spreading-stress scenarios supported by data, measurable 5–20-session confirmation/invalidation conditions, and the main uncertainty. Set python_alignment to agrees/disagrees/inconclusive; if disagrees, explain why without overwriting Python. Never give reversal probabilities. Avoid duplicating the visible risk/support/catalyst lists. Cite exact local metric_refs. Metric references must be copied as fully qualified section.metric_id strings; never omit the section prefix. If a metric is unavailable or absent from the allowed list, omit that claim/reference rather than inventing an ID. "
        "BREADTH INTERPRETATION RULE: distinguish (1) absolute market/ETF returns, (2) relative outperformance versus SPY, and (3) changes in absolute and relative participation over matched windows. The snapshot supplies SPY and RSP absolute returns, RSP-minus-SPY excess, the count of sector ETFs with positive absolute returns, the count outperforming SPY, and participation changes where history permits. Never call absolute breadth 'improving', 'broadening', or 'healthy' solely because RSP outperforms SPY or sector ETFs outperform SPY. If SPY and RSP are both negative while RSP falls less and sectors are mostly negative, describe relative resilience during a selloff, not healthy broadening. Say 'sector-ETF participation proxy'; do not claim stock-constituent breadth unless constituent data is explicitly present. Explain when absolute and relative measures disagree, use aligned dates/windows, and cite the exact supplied metric IDs. Missing or incomplete participation changes mean the direction is mixed/unknown, not assumed to improve. "
        "Return ONLY valid JSON matching this exact v6 contract: {\"schema_version\":\"market-analyst-response-v6\",\"highlights\":[{\"section_id\":one of the supplied section IDs,\"headline\":short clear interpretation,\"category\":\"constructive\"|\"caution\"|\"mixed\"|\"insufficient evidence\",\"insight\":concise meaning,\"why_it_matters\":risk/rotation relevance,\"monitor_next\":specific measurable condition,\"metric_refs\":[exact available local metric refs]}],\"comparison_highlights\":[{\"comparison_id\":one supplied stable ID,\"headline\":string,\"category\":same enum,\"insight\":brief interpretation,\"monitor_next\":condition,\"metric_refs\":[exact local refs]}],\"briefing\":{\"executive_takeaway\":string,\"risks\":[at most 3 strings],\"supportive\":[at most 3 strings],\"sector_implications\":[strings],\"conflicting_evidence\":[strings],\"catalysts\":[strings],\"conditions\":[strings],\"hypotheses\":[at most 3 objects with title, supporting_evidence, competing_explanation, confirm_or_invalidate, sector_implications as concise text, metric_refs],\"transition_outlook\":{\"current_regime_interpretation\":string,\"direction_classification\":\"strengthening\"|\"weakening\"|\"potential reversal\"|\"conflicting\"|\"no clear emerging direction\",\"emerging_direction\":string,\"scenarios\":[0-4 objects with scenario_id from current_leadership_continues/broader_risk_on_participation/leadership_handoff_without_broad_improvement/stress_spreading_across_sectors, assessment supported/plausible but unconfirmed, interpretation, monitor_next, metric_refs],\"turning_point_interpretations\":[exactly 5 objects, in this order and with a required signal_id field: {\"signal_id\":\"volatility_stabilization\",\"interpretation\":string,\"monitor_next\":string,\"metric_refs\":[exact local refs]}, {\"signal_id\":\"technology_mag7_recovery\",\"interpretation\":string,\"monitor_next\":string,\"metric_refs\":[exact local refs]}, {\"signal_id\":\"broad_market_participation\",\"interpretation\":string,\"monitor_next\":string,\"metric_refs\":[exact local refs]}, {\"signal_id\":\"defensive_leadership\",\"interpretation\":string,\"monitor_next\":string,\"metric_refs\":[exact local refs]}, {\"signal_id\":\"credit_confirmation\",\"interpretation\":string,\"monitor_next\":string,\"metric_refs\":[exact local refs]}],\"confirmation_invalidation\":string,\"main_uncertainty\":string,\"python_alignment\":\"agrees\"|\"disagrees\"|\"inconclusive\",\"disagreement_explanation\":string,\"metric_refs\":[exact local refs]},\"metric_refs\":{each briefing field name including transition_outlook maps to exact metric-ref array}}}. Include every signal_id exactly once; do not omit it even when array order is correct. For a hypothesis sector_implications field, return one concise string (not an array); never omit it. "
        "The regime highlight must compare cap-weighted SPY with equal-weight RSP, growth QQQ, blue-chip DIA, and small-cap IWM. Broad-index-versus-SPY measurements belong to the regime section and use IDs such as regime.broad_qqq_20d_excess_vs_spy; do not relabel them as relative_strength.qqq_spy_20d_excess_pp. Test whether strength/weakness is concentrated or broadening using sector participation and VIX; state what evidence would confirm broader stress. ETF price performance does not establish fund flows. "
        "Return exactly one highlight for each supplied section ID, including when data are unavailable; then return exactly one comparison highlight for every supplied comparison ID. Keep section highlights about 40–70 words and comparison highlights brief. A concise complete response is preferred over extra narrative; include every required field and section. "
        "Every material local claim must cite exact metric_refs that exist in the snapshot. For each section highlight, choose refs ONLY from that section's own allowed list; never derive or abbreviate an ID. For integrated briefing claims, any listed local metric ref is allowed. Use empty refs only for insufficient evidence. Keep the integrated briefing focused on the three most consequential developments; no buy/sell instructions. "
        "For briefing.metric_refs, return an object with exactly one key for every briefing field: executive_takeaway, risks, supportive, sector_implications, conflicting_evidence, catalysts, conditions, hypotheses, and transition_outlook. Use an empty array only when that field has no claim. This citation index is required even though the individual highlights and hypotheses already carry their own refs. "
        "Keep the complete JSON concise: highlights 25–45 words, comparison highlights one short sentence, brief risk/supportive/catalyst lists. Include every section, comparison ID, and required field. Do not omit fields to save tokens. "
        f"Allowed local metric references (copy these exact strings only): {json.dumps(allowed_metric_refs, ensure_ascii=False)}. "
        f"Allowed refs grouped by section (use this list for each highlight): {json.dumps(allowed_refs_by_section, ensure_ascii=False)}. "
        f"\nResearch focus: {question}\nResearch mode: {search_mode}\nSection IDs and labels in reading order: {json.dumps({section: SECTION_LABELS[section] for section in SECTION_ORDER})}\n"
        f"Comparison IDs to cover: {json.dumps(comparison_ids)}\n"
        f"Canonical local snapshot (public market data only; view controls omitted):\n{json.dumps(model_snapshot, ensure_ascii=False, separators=(',', ':'), default=str)}\n"
        f"Retrieved external evidence (empty in Dashboard Analysis mode):\n{json.dumps([{'id': f'S{i+1}', **src} for i, src in enumerate(sources)], ensure_ascii=False)}"
    )


BRIEFING_SECTIONS = {
    "market_regime": "Market regime / key takeaway",
    "risks": "Top 3 accumulating risks",
    "supportive": "Top 3 supportive developments",
    "sector": "Sector rotation implications",
    "catalysts": "Key market catalysts",
    "conflicts": "Conflicting evidence",
    "conditions": "Conditions that would change the assessment",
}
_SECTION_ALIASES = {
    "market regime / key takeaway": "market_regime", "market regime": "market_regime",
    "current market regime": "market_regime", "executive summary": "market_regime",
    "top 3 accumulating risks": "risks", "accumulating risks": "risks", "top risks": "risks",
    "top 3 supportive developments": "supportive", "improving conditions": "supportive",
    "supportive developments": "supportive",
    "sector rotation implications": "sector", "sector rotation": "sector",
    "key market catalysts": "catalysts", "upcoming catalysts": "catalysts", "catalysts": "catalysts",
    "conflicting evidence": "conflicts", "conditions that would change the assessment": "conditions",
    "what would change the assessment": "conditions", "unresolved questions": "conditions",
    "treasury/credit developments": "conflicts",
}


def parse_market_briefing(report_text: str) -> dict:
    """Parse current and legacy report headings without changing the saved text.

    Unrecognized content is retained in `unmapped`; callers must not invent
    missing sections when a saved report uses a different format.
    """
    output = {key: [] for key in BRIEFING_SECTIONS}
    unmapped, current, buffer = [], None, []

    def flush():
        nonlocal buffer
        text = " ".join(part.strip() for part in buffer if part.strip()).strip()
        if text:
            (output[current] if current else unmapped).append(text)
        buffer = []

    for raw_line in (report_text or "").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$", raw_line)
        if heading:
            flush()
            title = heading.group(1).strip().strip("*: ").lower()
            current = _SECTION_ALIASES.get(title)
            continue
        bold_heading = re.fullmatch(r"\*\*([^*].*?)\*\*:?", line)
        if bold_heading:
            flush()
            current = _SECTION_ALIASES.get(bold_heading.group(1).strip().lower())
            continue
        bullet = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", line)
        if bullet:
            flush()
            buffer.append(bullet.group(1))
        elif line:
            buffer.append(line)
        else:
            flush()
    flush()
    return {"sections": output, "unmapped": unmapped,
            "recognized": sum(bool(items) for items in output.values())}


def market_research_cache_key(topic: str, snapshot: dict, *, window: str = "current", model: str = DEFAULT_MODEL,
                              search_mode: str = "search_api", reasoning_effort: str = "none",
                              output_token_limit: int = DEFAULT_OUTPUT_TOKENS) -> str:
    stable_snapshot = {key: value for key, value in snapshot.items()
                       if key not in {"view_state", "view_fingerprint"}}
    payload = json.dumps([MARKET_ANALYST_PROMPT_VERSION, topic, stable_snapshot, window, model, search_mode, reasoning_effort, output_token_limit], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _json_response(response):
    try:
        result = response.json()
    except Exception as exc:
        raise MarketResearchError("DeepSeek returned an unreadable response. Retry manually.") from exc
    if not isinstance(result, dict):
        raise MarketResearchError("DeepSeek returned an unexpected response structure.")
    return result


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def extract_responses_text(response) -> str:
    """Collect assistant message text across every output item, skipping tool calls."""
    direct = _field(response, "output_text", "")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    messages = []
    for item in _field(response, "output", []) or []:
        role = _field(item, "role")
        kind = _field(item, "type")
        if kind != "message" and role != "assistant":
            continue
        if role not in (None, "assistant"):
            continue
        content = _field(item, "content", []) or []
        if isinstance(content, str) and content.strip():
            messages.append(content.strip())
            continue
        if isinstance(content, dict):
            content = [content]
        for part in content:
            kind = _field(part, "type")
            text = _field(part, "text", "")
            if kind in ("output_text", "text") and isinstance(text, str) and text.strip():
                messages.append(text.strip())
    return "\n\n".join(messages)


def _usage_value(response, name):
    usage = _field(response, "usage")
    value = _field(usage, name) if usage else None
    return int(value) if value is not None else None


def _reasoning_token_count(response):
    usage = _field(response, "usage")
    details = _field(usage, "output_tokens_details") if usage else None
    value = _field(details, "reasoning_tokens") if details else None
    if value is None:
        value = _field(usage, "reasoning_tokens") if usage else None
    return int(value) if value is not None else None


def _reported_cost_usd(response):
    """Read an explicit provider cost field; token-derived values remain estimates."""
    usage = _field(response, "usage") or {}
    raw = _field(usage, "cost_usd", _field(response, "cost_usd"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _diagnostic_request(*, model: str, prompt: str, max_tokens: int, reasoning: str,
                        tools=None, progress_callback=None):
    """One bounded Responses call returning safe diagnostics and visible text only."""
    import httpx
    from openai import OpenAI

    connect, read = bounded_deepseek_timeouts()
    timeout = httpx.Timeout(read=read, connect=connect, write=min(read, 30), pool=connect)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE,
                    timeout=timeout, max_retries=0)
    args = {"model": model, "input": prompt, "reasoning": {"effort": reasoning},
            "max_output_tokens": max_tokens}
    if tools:
        args["tools"] = tools
    streaming = model == "deepseek-v4-pro" or reasoning != "none"
    started = time.monotonic()
    if streaming:
        response, event_types, _ = _stream_response(client, args, progress_callback=progress_callback)
    else:
        response = client.responses.create(**args)
        event_types = [str(_field(item, "type", "unknown")) for item in (_field(response, "output", []) or [])]
    usage_in, usage_out = _usage_value(response, "input_tokens"), _usage_value(response, "output_tokens")
    reason_tokens = _reasoning_token_count(response)
    status = str(_field(response, "status", "unknown"))
    details = _field(response, "incomplete_details", {}) or {}
    result = {"model": model, "actual_model": _field(response, "model"), "http_status": 200,
              "duration_seconds": round(time.monotonic() - started, 3), "completion_status": status,
              "completion_reason": _field(details, "reason", "") or "",
              "output_item_types": list(dict.fromkeys(event_types)), "reasoning_tokens": reason_tokens,
              "visible_output_tokens": max(0, usage_out - reason_tokens) if usage_out is not None and reason_tokens is not None else usage_out,
              "input_tokens": usage_in, "output_tokens": usage_out,
              "answer_present": bool(extract_responses_text(response).strip()),
              "search_requests": _responses_search_count(response) if tools else 0,
              "sources": _response_sources(response) if tools else []}
    result["result"] = "success" if status == "completed" and result["answer_present"] else "incomplete_or_empty"
    return result


def run_deepseek_capability_diagnostics(*, request_limit_usd: float = .05, daily_limit_usd: float = .25,
                                        database_path=None, progress_callback=None) -> list[dict]:
    """Run bounded, explicit V4 and Flash reasoning checks without saving briefings.

    V4 Pro tests follow the requested order: tiny request, synthesis from the
    latest locally cached public research sources, then native search only if
    the first two completed with visible answers. A Flash high-reasoning check
    is also run independently. Each call uses existing spending limits and is
    recorded as usage, never as a market briefing.
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise MarketResearchError("DeepSeek is not configured. Add DEEPSEEK_API_KEY to local .env.")
    if not (0 < request_limit_usd <= 5 and 0 < daily_limit_usd <= 20):
        raise MarketResearchError("Spending limits must be positive (request ≤$5; daily ≤$20).")
    from dashboard.data_access import get_latest_deepseek_market_research_with_sources
    cached = (get_latest_deepseek_market_research_with_sources(database_path=database_path)
              if database_path else get_latest_deepseek_market_research_with_sources())
    sources = (cached or {}).get("sources", [])
    public_snapshot = (cached or {}).get("snapshot", {})
    public_snapshot = {key: public_snapshot.get(key) for key in (
        "as_of", "timeframe_sessions", "selected_comparison", "vix", "sector_metrics",
        "treasury_latest", "upcoming_public_events", "missing_data") if key in public_snapshot}
    source_context = [{key: source.get(key) for key in ("title", "url", "date", "snippet", "summary")
                       if source.get(key)} for source in sources[:5]]
    rows = []

    def execute(label, model, prompt, max_tokens, reasoning="none", tools=None):
        # Estimate a conservative ceiling before each explicitly requested call.
        input_tokens = max(1000, len(prompt) // 3 + 400)
        input_price, output_price = MODEL_PRICES[model]
        search_allowance = SEARCH_COST_PER_REQUEST * 5 if tools else 0
        ceiling = search_allowance + (input_tokens * input_price + max_tokens * output_price) / 1_000_000
        spent = deepseek_market_research_daily_spend(database_path=database_path) if database_path else deepseek_market_research_daily_spend()
        if ceiling > request_limit_usd or spent + ceiling > daily_limit_usd:
            rows.append({"test": label, "model": model, "result": "spending_cap", "http_status": None,
                         "error_category": "spending_cap", "estimated_ceiling_usd": round(ceiling, 6),
                         "daily_spend_before_usd": round(spent, 6)})
            return rows[-1]
        requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.monotonic()
        phase_status = "failed"
        try:
            _progress(progress_callback, f"{label} · connecting to {model}")
            result = _diagnostic_request(model=model, prompt=prompt, max_tokens=max_tokens,
                                         reasoning=reasoning, tools=tools,
                                         progress_callback=progress_callback)
            result.update({"test": label, "error_category": None,
                           "estimated_ceiling_usd": round(ceiling, 6),
                           "daily_spend_before_usd": round(spent, 6)})
            actual_in, actual_out = result.get("input_tokens"), result.get("output_tokens")
            search_count = result.get("search_requests", 0)
            if actual_in is not None and actual_out is not None:
                actual_cost = (actual_in * input_price + actual_out * output_price) / 1_000_000 + search_count * SEARCH_COST_PER_REQUEST
            else:
                # Unknown billing on an incomplete transport: reserve the bounded ceiling.
                actual_cost = ceiling
            result["estimated_cost_usd"] = round(actual_cost, 6)
            phase_status = result["result"]
            rows.append(result)
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            category = transport_error_category(exc)
            result = {"test": label, "model": model, "actual_model": None, "http_status": status_code,
                      "duration_seconds": round(time.monotonic() - started, 3), "completion_status": "provider_failure",
                      "completion_reason": "", "output_item_types": [], "reasoning_tokens": None,
                      "visible_output_tokens": None, "result": "failed", "error_category": category,
                      "error_type": type(exc).__name__,
                      "transport_cause_types": _exception_chain_names(exc)[1:],
                      "estimated_ceiling_usd": round(ceiling, 6),
                      "daily_spend_before_usd": round(spent, 6),
                      # DNS, TLS, proxy and connect failures occur before the
                      # request reaches generation; do not charge the local
                      # estimated-spend ledger for a provider call not made.
                      "estimated_cost_usd": 0.0 if category in {"dns_failure", "tls_failure", "proxy_failure",
                                                                 "network_connection_failure", "connect_timeout"}
                      else round(ceiling, 6)}
            rows.append(result)
        finally:
            used = rows[-1]
            record_deepseek_market_research_usage(
                requested_at=requested_at, model=model, search_requests=int(used.get("search_requests", 0)),
                input_tokens=used.get("input_tokens"), output_tokens=used.get("output_tokens"),
                estimated_cost_usd=float(used.get("estimated_cost_usd", 0)), status=phase_status,
                database_path=database_path) if database_path else record_deepseek_market_research_usage(
                requested_at=requested_at, model=model, search_requests=int(used.get("search_requests", 0)),
                input_tokens=used.get("input_tokens"), output_tokens=used.get("output_tokens"),
                estimated_cost_usd=float(used.get("estimated_cost_usd", 0)), status=phase_status)
        return rows[-1]

    tiny = execute("A · Tiny V4 Pro request", "deepseek-v4-pro",
                   "Reply with exactly: OK", 256, "none")
    v4_tiny_ok = tiny.get("result") == "success"
    if not sources:
        rows.append({"test": "B · V4 Pro cached-source synthesis", "model": "deepseek-v4-pro",
                     "result": "skipped_no_cached_sources", "http_status": None,
                     "error_category": "no_cached_sources"})
        v4_synthesis_ok = False
    else:
        prompt = ("Synthesize a concise evidence briefing from ONLY these previously retrieved public sources and local public market facts. "
                  "Cite the exact source URLs and dates; separate observations from interpretations and uncertainties. Do not search the web. "
                  f"Public snapshot: {json.dumps(public_snapshot, ensure_ascii=False, default=str)}\nCached sources: "
                  f"{json.dumps(source_context, ensure_ascii=False, default=str)}")
        synthesis = execute("B · V4 Pro cached-source synthesis", "deepseek-v4-pro", prompt, 1400, "none")
        v4_synthesis_ok = synthesis.get("result") == "success"
    if v4_tiny_ok and v4_synthesis_ok:
        c_prompt = ("Research current, official information on the latest US Treasury 2Y and 10Y yields. "
                    "Give a concise answer with source URLs and observation dates; distinguish facts from interpretations.")
        execute("C · V4 Pro native web search", "deepseek-v4-pro", c_prompt, 1400, "none",
                tools=[{"type": "web_search"}])
        execute("V4 Pro · deeper reasoning", "deepseek-v4-pro",
                "In 3 concise bullets, explain how an analyst should separate measured market observations, interpretations, and unknowns. Avoid unsupported claims.",
                6000, "high")
    else:
        rows.append({"test": "C · V4 Pro native web search", "model": "deepseek-v4-pro",
                     "result": "skipped_A_or_B_failed", "http_status": None,
                     "error_category": "prerequisite_failed"})
    execute("Flash · deeper reasoning", "deepseek-flash",
            "In 3 concise bullets, distinguish observed fact, interpretation, and uncertainty when discussing why market indicators can disagree. Avoid speculation.",
            6000, "high")
    return rows


def _responses_web_search_status(response) -> bool:
    """Require evidence that the Responses API actually ran its search tool."""
    usage = _field(response, "usage")
    tool_use = _field(usage, "server_tool_use") if usage else None
    count = _field(tool_use, "tool_search") if tool_use else None
    if isinstance(count, (int, float)):
        return count > 0
    return any(_field(item, "type", "") in {"web_search_call", "web_search_result"}
               and _field(item, "status", "") in {"completed", "succeeded", "success", ""}
               for item in (_field(response, "output", []) or []))


def _responses_search_count(response) -> int:
    usage = _field(response, "usage")
    tool_use = _field(usage, "server_tool_use") if usage else None
    count = _field(tool_use, "tool_search") if tool_use else None
    if isinstance(count, (int, float)):
        return max(0, int(count))
    return 1 if _responses_web_search_status(response) else 0


def _response_sources(response) -> list[dict]:
    """Recover source URLs/citations across Responses API output shapes."""
    found, seen = [], set()
    def visit(value):
        if isinstance(value, dict):
            url = value.get("url") or value.get("uri")
            if isinstance(url, str) and url.startswith("https://") and url not in seen:
                seen.add(url)
                item = {"url": url}
                for key in ("title", "name", "date", "published_at", "snippet", "text"):
                    if isinstance(value.get(key), str) and value[key].strip():
                        item["title" if key == "name" else key] = value[key].strip()
                found.append(item)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(_field(response, "output", []))
    return found


def estimate_market_research_cost(snapshot: dict, model: str = DEFAULT_MODEL, *, input_token_reserve: int = 6000,
                                   output_token_limit: int = DEFAULT_OUTPUT_TOKENS, search_mode: str = "search_api",
                                   search_cached: bool = False) -> float:
    """Conservative preflight ceiling using documented peak model pricing."""
    if model not in MODEL_PRICES:
        raise ValueError("Unsupported market research model")
    if not isinstance(output_token_limit, int) or not 1_000 <= output_token_limit <= MAX_MARKET_RESEARCH_OUTPUT_TOKENS:
        raise ValueError(f"Market Research output allowance must be between 1,000 and {MAX_MARKET_RESEARCH_OUTPUT_TOKENS:,} tokens.")
    input_price, output_price = MODEL_PRICES[model]
    estimated_input = max(input_token_reserve, len(json.dumps(snapshot, default=str)) // 3 + 1000)
    if search_mode not in {"dashboard_analysis", "dashboard_web", "search_api", "responses_web_search"}:
        raise ValueError("Unsupported DeepSeek market research search mode")
    search_allowance = 0 if search_cached or search_mode == "dashboard_analysis" else SEARCH_COST_PER_REQUEST * (5 if search_mode == "responses_web_search" else 1)
    return search_allowance + (estimated_input * input_price + output_token_limit * output_price) / 1_000_000


def _raise_http(response, phase):
    code = response.status_code
    if code == 401 or code == 403:
        text = "DeepSeek authentication or permission error. Check the local API key and account access."
    elif code == 402:
        text = "DeepSeek reports insufficient balance for this request. Check account credits."
    elif code == 429:
        text = "DeepSeek rate limit reached. Wait before requesting another report."
    elif code == 408:
        text = "DeepSeek request timed out. Retry manually."
    elif code >= 500:
        text = f"DeepSeek is temporarily unavailable during {phase} (HTTP {code}). Retry later."
    elif code == 400 and phase.lower() == "search":
        text = ("DeepSeek Search rejected the request (HTTP 400). Search query fields must be 1–128 characters; "
                "the app now enforces that limit. If this persists, review the request diagnostics.")
    else:
        text = f"DeepSeek {phase} request failed (HTTP {code}). Check model and endpoint support."
    raise MarketResearchError(text)


def _search_results(raw):
    data = _json_response(raw)
    results = data.get("results", data.get("data", []))
    if not isinstance(results, list):
        return []
    output = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"].startswith("https://"):
            continue
        output.append({key: item.get(key) for key in ("title", "url", "snippet", "summary", "content", "site_name", "site_icon", "date", "authority", "rerank_score") if item.get(key) is not None})
    return output


def run_market_research(*, topic: str, snapshot: dict, research_question: str | None = None, request_limit_usd: float = .05,
                        daily_limit_usd: float = .25, force: bool = False,
                        synthesis_only: bool = False, cached_report_key: str | None = None,
                        model: str | None = None, search_mode: str = "search_api",
                        reasoning_effort: str = "none", output_token_limit: int = DEFAULT_OUTPUT_TOKENS,
                        timeout_seconds: int | None = None, connect_timeout_seconds: int | None = None,
                        read_timeout_seconds: int | None = None, progress_callback=None,
                        database_path=None, http=requests):
    """Search, synthesize, and retain failed-stage evidence for explicit retries."""
    if topic not in TOPICS:
        raise MarketResearchError("Choose one of the supported market research topics.")
    if not (0 < request_limit_usd <= 5 and 0 < daily_limit_usd <= 20):
        raise MarketResearchError("Spending limits must be positive (request ≤$5; daily ≤$20).")
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise MarketResearchError("DeepSeek is not configured. Add DEEPSEEK_API_KEY to the local .env file.")
    model = (model or configured_market_research_model()).strip()
    if model not in SUPPORTED_RESPONSES_MODELS:
        raise MarketResearchError("Market research model must be deepseek-flash or deepseek-v4-pro.")
    if search_mode not in {"dashboard_analysis", "dashboard_web", "search_api", "responses_web_search"}:
        raise MarketResearchError("Choose Dashboard Analysis or Dashboard + Web Verification.")
    if search_mode == "responses_web_search" and model != "deepseek-v4-pro":
        raise MarketResearchError("Responses web_search is supported only with deepseek-v4-pro. Select V4 Pro or use Search API mode.")
    if reasoning_effort not in {"none", "high"}:
        raise MarketResearchError("Reasoning must be none or high.")
    if not isinstance(output_token_limit, int) or not 1000 <= output_token_limit <= MAX_MARKET_RESEARCH_OUTPUT_TOKENS:
        raise MarketResearchError(f"Market Research output allowance must be between 1,000 and {MAX_MARKET_RESEARCH_OUTPUT_TOKENS:,} tokens.")
    cache_key = market_research_cache_key(
        topic, snapshot, model=model, search_mode=search_mode, reasoning_effort=reasoning_effort,
        output_token_limit=output_token_limit)
    source_cache_key = cached_report_key if synthesis_only and cached_report_key else cache_key
    if synthesis_only and (not isinstance(source_cache_key, str) or not re.fullmatch(r"[0-9a-f]{64}", source_cache_key)):
        raise MarketResearchError("Saved search report identifier is invalid; run a fresh search explicitly.")
    target_cached = get_deepseek_market_research_by_key(cache_key, database_path) if database_path else get_deepseek_market_research_by_key(cache_key)
    source_cached = (get_deepseek_market_research_by_key(source_cache_key, database_path)
                     if database_path else get_deepseek_market_research_by_key(source_cache_key))
    if synthesis_only and target_cached and target_cached.get("research_status") in {"complete", "complete_cost_warning"}:
        return {**target_cached, "cached": True}
    if synthesis_only and not source_cached:
        raise MarketResearchError("No cached analysis input is available for a synthesis-only retry. Run a fresh analysis explicitly.")
    if synthesis_only and (source_cached.get("snapshot") or {}).get("fingerprint") != (snapshot or {}).get("fingerprint"):
        raise MarketResearchError("The market snapshot changed since the saved search. Start a fresh analysis so the evidence and data snapshot stay aligned.")
    if synthesis_only and search_mode != "dashboard_analysis" and not source_cached.get("sources"):
        raise MarketResearchError("No cached search evidence is available for a synthesis-only retry. Run a fresh search explicitly.")
    cached = source_cached if synthesis_only else target_cached
    if not synthesis_only and cached and not force and cached.get("research_status", "complete") in {"complete", "complete_cost_warning"}:
        return {**cached, "cached": True}
    daily = deepseek_market_research_daily_spend(database_path=database_path) if database_path else deepseek_market_research_daily_spend()
    output_cap = output_token_limit
    estimated_input_tokens = max(6000, len(json.dumps(snapshot, default=str)) // 3 + 1000)
    input_price, output_price = MODEL_PRICES[model]
    search_estimate = (SEARCH_COST_PER_REQUEST * (5 if search_mode == "responses_web_search" else 1)
                       if (not cached or force) and not synthesis_only else 0)
    if search_mode == "dashboard_analysis":
        search_estimate = 0
    upper_bound = search_estimate + (estimated_input_tokens * input_price + output_cap * output_price) / 1_000_000
    if upper_bound > request_limit_usd:
        raise MarketResearchError(f"Estimated request ceiling (${upper_bound:.4f}) exceeds your per-request limit (${request_limit_usd:.2f}). Raise the limit or shorten the context.")
    if daily + upper_bound > daily_limit_usd:
        raise MarketResearchError(f"Estimated request ceiling would exceed today's remaining spending limit (${max(0, daily_limit_usd-daily):.4f}).")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    search_requests = 0
    estimated_cost = 0.0
    input_tokens = output_tokens = None
    status = "failed"
    requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    request_started = time.monotonic()
    request_id = uuid.uuid4().hex
    connect_timeout, read_timeout = bounded_deepseek_timeouts(
        connect_timeout_seconds if connect_timeout_seconds is not None else timeout_seconds,
        read_timeout_seconds if read_timeout_seconds is not None else timeout_seconds)
    sources = list(cached.get("sources", [])) if cached and not force else []
    search_succeeded = bool(sources)
    event_types: list[str] = []
    completion_status = "not_started"
    completion_reason = ""
    error_category = ""
    # Keep request and provider-reported model identities separate. If the API
    # omits its model field, display that fact rather than guessing from billing.
    actual_model = None
    reasoning_tokens = None
    visible_output_tokens = None
    provider_max_output = None
    provider_context_window = None
    response = None
    request_endpoints: list[str] = []

    def diagnostic_metadata():
        return {"request_duration_seconds": round(time.monotonic() - request_started, 3),
                "completion_status": completion_status, "completion_reason": completion_reason,
                "output_item_types": list(dict.fromkeys(str(_field(item, "type", "unknown"))
                                                          for item in (_field(response, "output", []) or []))),
                "stream_event_types": list(dict.fromkeys(event_types)), "actual_model": actual_model,
                "reasoning_tokens": reasoning_tokens, "visible_output_tokens": visible_output_tokens,
                "provider_max_output_tokens": provider_max_output,
                "provider_context_window": provider_context_window,
                "error_category": error_category, "connect_timeout_seconds": connect_timeout,
                "read_timeout_seconds": read_timeout,
                "request_id": request_id,
                "prompt_version": MARKET_ANALYST_PROMPT_VERSION,
                "request_purpose": "synthesis_only_retry" if synthesis_only else "full_market_analysis",
                "endpoint_sequence": list(request_endpoints),
                "reported_cost_usd": _reported_cost_usd(response) if response is not None else None}

    def save_failure(message, code, completion_reason="", partial_text=""):
        nonlocal completion_status
        if completion_status in {"not_started", "unknown"}:
            completion_status = code
        preserve_success = bool(target_cached and target_cached.get("research_status") in {"complete", "complete_cost_warning"}
                                and target_cached.get("report_text"))
        stored_key = (hashlib.sha256(f"{cache_key}|failed-attempt|{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()
                      if preserve_success else cache_key)
        record = {"cache_key": stored_key, "topic": topic, "snapshot": snapshot,
                  "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model,
                  # Failed/incomplete generations are never stored as report text.
                  # In particular, reasoning output is not a user-facing briefing.
                  "report_text": "", "sources": sources,
                  "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                            "search_requests": search_requests, "estimated_cost_usd": round(estimated_cost, 6),
                            "requested_model": model, "actual_model": actual_model,
                            "search_mode": search_mode, "reasoning_effort": reasoning_effort,
                            "diagnostic_response_text": partial_text[:50000] if partial_text else None,
                            "preserved_prior_success": preserve_success,
                            **diagnostic_metadata()},
                  "web_search_succeeded": search_succeeded, "research_status": code,
                  "error_message": message, "completion_reason": completion_reason}
        save_deepseek_market_research_report(record, database_path) if database_path else save_deepseek_market_research_report(record)
        return {**record, "cached": False}

    try:
        try:
            _progress(progress_callback, "Checking configured model access")
            request_endpoints.append("GET /models")
            models_response = http.get(f"{BASE}/models", headers={"Authorization": f"Bearer {key}"}, timeout=(connect_timeout, read_timeout))
        except requests.Timeout as exc:
            error_category = timeout_error_category(exc) or "timeout"
            raise MarketResearchError(f"DeepSeek model check {error_category.replace('_', ' ')} after {time.monotonic()-request_started:.1f}s. Check connectivity and retry.") from exc
        except requests.RequestException as exc:
            error_category = transport_error_category(exc)
            raise MarketResearchError(f"DeepSeek model check failed ({error_category}; {type(exc).__name__}). Check DNS resolution, TLS certificates, proxy/VPN settings, then retry.") from exc
        if not models_response.ok:
            _raise_http(models_response, "model check")
        model_data = _json_response(models_response)
        model_rows = [item for item in model_data.get("data", []) if isinstance(item, dict)]
        model_info = next((item for item in model_rows if item.get("id") == model), {})
        ids = {item.get("id") for item in model_rows}
        if model not in ids:
            raise MarketResearchError(f"This API account does not list {model}. Choose a supported model in local .env; no fallback was used.")
        provider_max_output = model_info.get("max_output_tokens")
        provider_context_window = model_info.get("context_window")
        if provider_max_output is not None and output_cap > int(provider_max_output):
            raise MarketResearchError(f"The API currently advertises a {int(provider_max_output):,}-token output maximum for {model}; your {output_cap:,}-token request was not sent. Select a lower allowance.")
        if provider_context_window is not None and estimated_input_tokens + output_cap > int(provider_context_window):
            raise MarketResearchError(f"Estimated input plus output ({estimated_input_tokens + output_cap:,} tokens) exceeds the advertised {int(provider_context_window):,}-token context window; no search was started. Reduce the allowance or context.")
        question = (research_question or TOPICS[topic]).strip()
        if search_mode in {"dashboard_web", "search_api"} and not sources:
            query = ("major US market-moving company earnings results guidance capital expenditure free cash flow "
                     "Federal Reserve macroeconomic releases market developments in the past seven days") if topic == "Full Market Intelligence Snapshot" else question
            try:
                _progress(progress_callback, "Search in progress")
                request_endpoints.append("POST /v1/tools/search")
                search_response = http.post(f"{BASE}/v1/tools/search", headers=headers,
                                            json={**bounded_search_query(query, original_question=question),
                                                  "count": 5, "need_content": False},
                                            timeout=(connect_timeout, read_timeout))
            except requests.Timeout as exc:
                error_category = timeout_error_category(exc) or "timeout"
                raise MarketResearchError(f"DeepSeek Search {error_category.replace('_', ' ')} after {time.monotonic()-request_started:.1f}s. Retry manually.") from exc
            except requests.RequestException as exc:
                raise MarketResearchError(f"DeepSeek Search network failure ({type(exc).__name__}). Check DNS, proxy, or VPN settings.") from exc
            if not search_response.ok:
                _raise_http(search_response, "Search")
            sources = _search_results(search_response)
            search_requests = 1
            search_succeeded = True
            estimated_cost = SEARCH_COST_PER_REQUEST
            if topic == "Full Market Intelligence Snapshot" and sources:
                try:
                    from dashboard.recent_catalysts import load_recent_catalysts, save_recent_catalysts
                    cache = save_recent_catalysts(sources, database_path=database_path)
                    current_cache = load_recent_catalysts(database_path=database_path)
                    # Web research explicitly refreshes this local cache. Bind the
                    # report to the post-refresh evidence so the immediate rerun
                    # sees its own saved report as current, not stale.
                    snapshot = snapshot_with_recent_catalysts(snapshot, current_cache)
                    cache_key = market_research_cache_key(topic, snapshot, model=model,
                        search_mode=search_mode, reasoning_effort=reasoning_effort,
                        output_token_limit=output_cap)
                    logger.info("Recent market catalyst cache refreshed records=%s dated_window_days=7", len(cache.get("records", [])))
                except Exception as exc:
                    # A cache-write issue must not discard the successful search or block analysis.
                    logger.warning("Recent market catalyst cache write failed exception_type=%s", type(exc).__name__)
            _progress(progress_callback, f"Search completed · {len(sources)} sources retrieved")
        if search_mode in {"dashboard_web", "search_api"} and not sources:
            raise MarketResearchError("DeepSeek Search returned no usable source results. No researched briefing was generated; no-result is not evidence against the topic.")
        source_context = json.dumps(sources, ensure_ascii=False)
        estimated_input_tokens = max(estimated_input_tokens, (len(json.dumps(snapshot, ensure_ascii=False, default=str)) + len(source_context)) // 3 + 1000)
        search_cost = SEARCH_COST_PER_REQUEST if search_requests else (SEARCH_COST_PER_REQUEST * 5 if search_mode == "responses_web_search" and not sources else 0)
        upper_bound = search_cost + (estimated_input_tokens * input_price + output_cap * output_price) / 1_000_000
        if upper_bound > request_limit_usd or daily + upper_bound > daily_limit_usd:
            message = f"Estimated synthesis ceiling (${upper_bound:.4f}) exceeds your configured remaining budget. Search results were saved; increase the cap or retry later."
            status = "spending_cap"
            return save_failure(message, status)
        if snapshot.get("schema_version") in {"market-intelligence-snapshot-v1", "market-intelligence-snapshot-v2", "market-intelligence-snapshot-v3"}:
            prompt = _market_analyst_prompt(question, snapshot, sources, search_mode)
        else:
            prompt = (f"Research question: {question}\nUse only retrieved source material for external factual claims. Cite clickable source URLs and state exact publication/observation dates when available. "
                      "Write a concise analyst briefing, maximum about 500 words. Use exactly these headings: Market regime / key takeaway; Top 3 accumulating risks; Top 3 supportive developments; Sector rotation implications; Key market catalysts; Conflicting evidence; Conditions that would change the assessment. "
                      "Under each heading use no more than three short bullets, generally one or two sentences each. In every bullet distinguish observed evidence from interpretation and uncertainty where relevant. Include at least one competing explanation when evidence is mixed. "
                      "Never invent dates, data, causal claims, probabilities, catalysts, or missing evidence. If information is unavailable, say so briefly.\n"
                      f"Local public market snapshot (no personal portfolio):\n{json.dumps(snapshot, ensure_ascii=False, default=str)}\n"
                      f"Retrieved search evidence:\n{json.dumps([{'id': f'S{i+1}', **src} for i, src in enumerate(sources)], ensure_ascii=False) if sources else 'No external search was requested; use local observations only.'}")
        try:
            from openai import OpenAI
            import httpx
            client_timeout = httpx.Timeout(read=read_timeout, connect=connect_timeout,
                                           write=min(read_timeout, 30), pool=connect_timeout)
            client = OpenAI(api_key=key, base_url=BASE, timeout=client_timeout, max_retries=0)
            request_args = {"model": model, "input": prompt, "max_output_tokens": output_cap,
                            "reasoning": {"effort": reasoning_effort}}
            request_endpoints.append("POST /responses")
            if search_mode == "responses_web_search":
                request_args["tools"] = [{"type": "web_search"}]
            # Preserve the stable Flash/no-thinking path; stream V4 Pro and any
            # explicitly selected reasoning mode so slow generation is visible.
            should_stream = model == "deepseek-v4-pro" or reasoning_effort != "none"
            _progress(progress_callback, "Search complete · answer generation in progress" if sources else "Answer generation in progress")
            if should_stream:
                response, event_types, _streamed_text = _stream_response(
                    client, request_args, progress_callback=progress_callback)
            else:
                response = client.responses.create(**request_args)
                event_types = [str(_field(item, "type", "unknown")) for item in (_field(response, "output", []) or [])]
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            error_name = type(exc).__name__.lower()
            error_category = transport_error_category(exc)
            completion_status = "provider_failure"
            logger.warning("DeepSeek research failed model=%s phase=synthesis category=%s duration_seconds=%.3f",
                           model, error_category, time.monotonic() - request_started)
            if search_mode == "responses_web_search" and status_code == 400:
                message = "DeepSeek rejected the V4 Pro Responses web_search request (HTTP 400). Check account/tool availability or switch explicitly to Search API + synthesis."
            elif status_code or any(term in error_name for term in ("timeout", "connection", "network", "proxy", "dns")):
                if error_category == "connect_timeout":
                    message = f"DeepSeek connection timed out before generation after {time.monotonic()-request_started:.1f}s. Check network/proxy connectivity."
                elif error_category == "read_timeout":
                    message = f"DeepSeek read timed out after {time.monotonic()-request_started:.1f}s while waiting for generation. Increase DEEPSEEK_READ_TIMEOUT_SECONDS within its 300-second limit or retry synthesis from cached sources."
                elif error_category == "dns_failure":
                    message = "DeepSeek DNS resolution failed before an HTTP response. Check DNS, VPN, or proxy settings; this does not establish an API-key or model-access problem."
                elif error_category in {"tls_failure", "proxy_failure", "network_connection_failure"}:
                    message = f"DeepSeek {error_category.replace('_', ' ')} before a response. Check local network, TLS, and proxy/VPN settings; no provider status was received."
                else:
                    message = str(normalize_error("deepseek", model, exc))
            else:
                message = f"DeepSeek synthesis failed ({type(exc).__name__}). Search results are retained; retry synthesis manually."
            status = "synthesis_error"
            return save_failure(message, status)
        input_tokens = _usage_value(response, "input_tokens")
        output_tokens = _usage_value(response, "output_tokens")
        actual_model = _field(response, "model")
        reasoning_tokens = _reasoning_token_count(response)
        visible_output_tokens = (max(0, output_tokens - reasoning_tokens)
                                 if output_tokens is not None and reasoning_tokens is not None else output_tokens)
        if input_tokens is not None and output_tokens is not None:
            estimated_cost += (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        else:
            input_tokens, output_tokens = estimated_input_tokens, output_cap
            estimated_cost += (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        text = extract_responses_text(response)
        if search_mode == "responses_web_search":
            search_succeeded = _responses_web_search_status(response)
            sources = _response_sources(response)
            search_requests = _responses_search_count(response)
            if not search_succeeded:
                message = "DeepSeek returned a response without verifiable web_search execution. No report was saved as researched; use Search API + synthesis mode or check V4 Pro web_search access."
                status = "web_search_not_verified"
                return save_failure(message, status)
            if not sources:
                message = "DeepSeek web_search ran, but no source URLs could be recovered. No briefing was saved; retry with Search API mode."
                status = "web_search_sources_missing"
                return save_failure(message, status)
            estimated_cost = (SEARCH_COST_PER_REQUEST * search_requests
                              + ((input_tokens or estimated_input_tokens) * input_price
                                 + (output_tokens or output_cap) * output_price) / 1_000_000)
        response_status = _field(response, "status", "unknown")
        completion_status = str(response_status)
        details = _field(response, "incomplete_details", {}) or {}
        completion_reason = _field(details, "reason", "") or ""
        if response_status != "completed":
            error_category = "incomplete_output"
        if response_status == "failed":
            error_category = "provider_failure"
            message = "DeepSeek reported a failed response. Retrieved sources are retained; retry manually after checking provider status and limits."
            status = "synthesis_error"
            return save_failure(message, status, completion_reason)
        if response_status == "incomplete" and not text.strip():
            error_category = "incomplete_output"
            message = f"DeepSeek response was incomplete with no visible answer (reason: {completion_reason or 'not provided'}). Sources are retained; retry synthesis explicitly."
            status = "incomplete"
            return save_failure(message, status, completion_reason)
        if not text.strip():
            kinds = [_field(item, "type", "unknown") for item in (_field(response, "output", []) or [])]
            error_category = "empty_synthesis"
            message = f"DeepSeek returned no assistant message text (status={response_status}; output item types={kinds}). Search results are retained for synthesis retry."
            status = "empty_synthesis"
            return save_failure(message, status, completion_reason)
        if response_status != "completed":
            message = f"DeepSeek response was incomplete (reason: {completion_reason or 'not provided'}). Search results are retained; retry explicitly."
            status = "incomplete"
            return save_failure(message, status, completion_reason)
        if snapshot.get("schema_version") in {"market-intelligence-snapshot-v1", "market-intelligence-snapshot-v2", "market-intelligence-snapshot-v3"}:
            try:
                validated_analysis = parse_market_analysis_json(text, snapshot, sources)
                deterministic_transition = (((snapshot.get("sections") or {}).get("sector_rotation") or {}).get("regime_transition"))
                regime_method = (((snapshot.get("configurations") or {}).get("regime_evidence_methodology")) or "")
                required_response_version = "market-analyst-response-v6" if regime_method == "regime-evidence-v2" else "market-analyst-response-v5"
                if isinstance(deterministic_transition, dict) and validated_analysis.get("schema_version") != required_response_version:
                    raise ValueError(f"This snapshot with deterministic regime-transition data requires {required_response_version} and its version-matched transition_outlook subsection.")
            except ValueError as exc:
                error_category = "invalid_analysis_contract"
                logger.warning("DeepSeek market response validation failed category=%s detail=%s snapshot=%s",
                               error_category, str(exc)[:1200], snapshot.get("fingerprint", "")[:16])
                return save_failure(f"DeepSeek completed, but its structured market-analysis response failed validation: {exc}",
                                    "invalid_analysis_contract", completion_reason, partial_text=text)
        if estimated_cost > request_limit_usd or daily + estimated_cost > daily_limit_usd:
            # A completed model call cannot be undone. Preserve its useful output,
            # label the cap overrun, and make the saved result a cache hit so a
            # rerun cannot accidentally submit and charge for it again.
            message = "The completed request's usage estimate exceeded the configured cap. The briefing is saved; future requests remain blocked by the normal preflight budget check."
            status = "complete_cost_warning"
            report = {"cache_key": cache_key, "topic": topic, "snapshot": snapshot,
                      "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model,
                      "report_text": text, "sources": sources, "usage": {"input_tokens": input_tokens,
                      "output_tokens": output_tokens, "search_requests": search_requests,
                      "requested_model": model, "search_mode": search_mode, "reasoning_effort": reasoning_effort,
                      "actual_model": _field(response, "model"), **diagnostic_metadata(),
                      "estimated_cost_usd": round(estimated_cost, 6)}, "web_search_succeeded": search_succeeded,
                      "research_status": status, "error_message": message, "completion_reason": ""}
            save_deepseek_market_research_report(report, database_path) if database_path else save_deepseek_market_research_report(report)
            return {**report, "cached": False}
        report = {"cache_key": cache_key, "topic": topic, "snapshot": snapshot,
                  "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model,
                  "report_text": text, "sources": sources, "usage": {"input_tokens": input_tokens,
                  "output_tokens": output_tokens, "search_requests": search_requests,
                  "requested_model": model, "search_mode": search_mode, "reasoning_effort": reasoning_effort,
                  "actual_model": _field(response, "model"), **diagnostic_metadata(),
                  "estimated_cost_usd": round(estimated_cost, 6)}, "web_search_succeeded": search_succeeded,
                  "research_status": "complete", "error_message": "", "completion_reason": ""}
        save_deepseek_market_research_report(report, database_path) if database_path else save_deepseek_market_research_report(report)
        status = "success"
        return {**report, "cached": False}
    finally:
        record_deepseek_market_research_usage(requested_at=requested_at, model=model,
            search_requests=search_requests, input_tokens=input_tokens, output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost, status=status, database_path=database_path) if database_path else record_deepseek_market_research_usage(requested_at=requested_at, model=model, search_requests=search_requests, input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost_usd=estimated_cost, status=status)
        endpoint_text = "; ".join(dict.fromkeys(request_endpoints)) or "No provider endpoint reached"
        try:
            record_market_ai_request_audit(
                request_id=request_id, requested_at=requested_at,
                purpose=("synthesis_only_retry" if synthesis_only else "full_market_analysis") +
                        (":fast_briefing" if reasoning_effort == "none" else ":deep_research"),
                evidence_mode=search_mode,
                requested_model=model, actual_model=actual_model, endpoint=endpoint_text,
                reasoning_effort=reasoning_effort, output_token_allowance=output_cap,
                input_tokens=input_tokens, output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens, visible_output_tokens=visible_output_tokens,
                search_requests=search_requests, estimated_cost_usd=estimated_cost,
                reported_cost_usd=_reported_cost_usd(response) if response is not None else None,
                completion_status=completion_status,
                error_category=error_category,
                database_path=database_path) if database_path else record_market_ai_request_audit(
                    request_id=request_id, requested_at=requested_at,
                    purpose=("synthesis_only_retry" if synthesis_only else "full_market_analysis") +
                            (":fast_briefing" if reasoning_effort == "none" else ":deep_research"),
                    evidence_mode=search_mode,
                    requested_model=model, actual_model=actual_model, endpoint=endpoint_text,
                    reasoning_effort=reasoning_effort, output_token_allowance=output_cap,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    reasoning_tokens=reasoning_tokens, visible_output_tokens=visible_output_tokens,
                    search_requests=search_requests, estimated_cost_usd=estimated_cost,
                    reported_cost_usd=_reported_cost_usd(response) if response is not None else None,
                    completion_status=completion_status,
                    error_category=error_category)
        except Exception as exc:
            logger.warning("Market AI audit write failed exception_type=%s", type(exc).__name__)



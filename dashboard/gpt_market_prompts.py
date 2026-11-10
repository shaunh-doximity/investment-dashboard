"""Deterministic, copyable research prompts; this module never calls an AI service."""

from __future__ import annotations


PROMPT_TOPICS = {
    "Treasury yields and latest Fed communication": "Verify the latest U.S. Treasury 2-year and 10-year par yields and the latest Federal Reserve policy communication. Explain what is observed versus what is inferred; do not invent market-implied rate expectations.",
    "Corporate credit conditions and AI infrastructure financing": "Investigate corporate credit conditions and financing needs of major AI infrastructure builders. Use dated filings, earnings transcripts, bond disclosures, rating-agency releases, or other credible primary sources. Separate company-reported capex/cash-flow facts from hypotheses.",
    "Consumer discretionary versus staples rotation": "Assess consumer discretionary versus staples fundamentals and recent sector performance. Determine what data can and cannot explain relative performance; do not infer fund flows from ETF prices.",
    "Broad market breadth versus megacap concentration": "Assess broad-market participation versus megacap concentration using verifiable constituent-level breadth and concentration data if available. Clearly distinguish such data from equal-weight/cap-weight ETF proxies.",
    "Semiconductor valuation versus earnings expectations": "Assess semiconductor valuation and earnings expectations using current company filings, earnings releases, and dated credible estimates. Identify evidence both supporting and contradicting the investment case.",
    "Full market regime and sector rebalancing review": "Review the market regime, rates, credit, breadth, sector rotation, and concentration. Identify possible portfolio implications conditionally; do not recommend or execute trades without reviewing the actual portfolio context supplied separately.",
}


BASE_INSTRUCTIONS = """Research carefully using current, credible primary sources whenever available (official data providers, Treasury/Federal Reserve, company filings and investor relations, or regulators). For every important number, report its exact observation date and publication date. Separate factual observations, interpretations, hypotheses, and uncertainties. Show evidence both supporting and contradicting the proposed interpretation. Explain relevant sector implications without treating correlation as causation. Explicitly identify data gaps, stale evidence, and source limitations. Never invent market data, earnings, rate expectations, fund flows, probabilities, or causal news explanations. Return a concise structured summary with: Question; Verified observations and dates; Interpretation; Counter-evidence; Relevant sectors; Uncertainties and missing data; What to monitor next. This prompt is for independent research to inform later Robinhood portfolio analysis; it is not an order or trade instruction."""


def build_gpt_market_prompt(topic: str, *, comparison: dict | None = None,
                            regime_snapshot: dict | None = None,
                            unavailable_data: list[str] | tuple[str, ...] = ()) -> str:
    if topic not in PROMPT_TOPICS:
        raise ValueError("Choose one of the available research prompt topics.")
    context_lines = []
    if comparison:
        context_lines.append(
            f"Dashboard comparison context (cached Tiingo adjusted closes): {comparison.get('first')} vs {comparison.get('second')}; "
            f"window={comparison.get('window')} sessions; as-of={comparison.get('as_of')}; "
            f"first return={comparison.get('return_first')}; second return={comparison.get('return_second')}; "
            f"excess={comparison.get('excess_pp')} percentage points. Treat this as observed price performance, not flows or causation."
        )
    if regime_snapshot:
        context_lines.append("Locally observed dashboard indicators (values/date/status): " + str(regime_snapshot))
    missing = ", ".join(str(item) for item in unavailable_data) or "none reported by the dashboard"
    context_lines.append(f"Dashboard data unavailable or not integrated: {missing}.")
    return "\n\n".join((PROMPT_TOPICS[topic], BASE_INSTRUCTIONS, *context_lines))


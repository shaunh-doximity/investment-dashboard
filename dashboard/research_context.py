"""Local, cache-only evidence assembly for a ticker research workspace.

No provider calls are made here. External fundamental/news evidence can be added
behind the small provider protocol when a verified source is selected.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from math import isfinite
from typing import Protocol

from dashboard.analytics import (
    annualized_realized_volatility, momentum, rsi14, sma20, sma50, sma200,
)
from dashboard.data_access import (
    list_portfolio_classification_records, list_upcoming_market_events, list_watchlist,
)
from dashboard.market_data import quote_is_stale
from dashboard.market_history import SECTOR_SYMBOLS, load_history
from dashboard.history_providers import TiingoDailyProvider
from dashboard.market_visuals import sector_rotation_points


class ResearchEvidenceProvider(Protocol):
    """Future adapter contract for source-backed filings, fundamentals and news."""

    def fetch_evidence(self, symbol: str) -> list[dict]: ...


class NoExternalResearchEvidence:
    """Explicitly reports the current absence of integrated external evidence."""

    def fetch_evidence(self, symbol: str) -> list[dict]:
        return []


def _observed_range(bars: list[dict], sessions: int) -> dict:
    ordered = bars[-sessions:]
    if len(ordered) < sessions:
        return {"sessions": sessions, "low_close": None, "high_close": None, "status": "insufficient_history"}
    try:
        values = [float(row["close"]) for row in ordered]
        if any(not isfinite(value) or value <= 0 for value in values):
            raise ValueError
        return {"sessions": sessions, "low_close": min(values), "high_close": max(values),
                "from": ordered[0]["trading_date"], "through": ordered[-1]["trading_date"],
                "status": "observed_close_range_not_support_prediction"}
    except (KeyError, TypeError, ValueError):
        return {"sessions": sessions, "low_close": None, "high_close": None, "status": "invalid_or_missing_observation"}


def build_research_context(symbol: str, quote: dict | None = None, strategy: dict | None = None) -> dict:
    """Build deterministic context from locally cached quotes, history and events."""
    ticker = symbol.strip().upper()
    bars = load_history(ticker)
    spy = load_history("SPY")
    technical = {
        "latest_adjusted_close": bars[-1].get("close") if bars else None,
        "latest_history_date": bars[-1].get("trading_date") if bars else None,
        "return_5d": momentum(bars, 5), "return_20d": momentum(bars, 20),
        "return_60d": momentum(bars, 60),
        "sma20": sma20(bars), "sma50": sma50(bars), "sma200": sma200(bars),
        "rsi14": rsi14(bars), "annualized_volatility_20d": annualized_realized_volatility(bars, 20),
        "observed_close_ranges": {"20d": _observed_range(bars, 20), "60d": _observed_range(bars, 60)},
    }
    latest_history_date = technical["latest_history_date"]
    try:
        technical["history_stale"] = (date.today() - date.fromisoformat(latest_history_date)).days > 4 if latest_history_date else None
    except ValueError:
        technical["history_stale"] = True
    quote_date = quote.get("quote_date") if quote else None
    if quote and latest_history_date:
        quote_history_status = "same_date_different_basis_unverified" if quote_date == latest_history_date else "different_observation_dates_normal_freshness"
    elif quote:
        quote_history_status = "historical_observation_unavailable"
    elif latest_history_date:
        quote_history_status = "quote_unavailable"
    else:
        quote_history_status = "both_sources_unavailable"
    relative = {}
    for window in (5, 20, 60):
        own, benchmark = momentum(bars, window), momentum(spy, window)
        relative[f"excess_return_{window}d"] = own - benchmark if own is not None and benchmark is not None else None

    classifications = list_portfolio_classification_records()
    classification = classifications.get(ticker)
    sector_rotation = None
    if classification and classification.get("verification_status") == "Verified":
        sector_symbol = classification.get("sector_benchmark")
        if sector_symbol not in SECTOR_SYMBOLS:
            sector_symbol = None
        if sector_symbol:
            points = sector_rotation_points({sector_symbol: load_history(sector_symbol), "SPY": spy}, {sector_symbol: SECTOR_SYMBOLS[sector_symbol]})
            sector_rotation = next((item for item in points if item["symbol"] == sector_symbol), None)

    upcoming = []
    for event in list_upcoming_market_events():
        if event.get("category") != "Earnings" or ticker not in [x.upper() for x in event.get("related_symbols", [])]:
            continue
        if event.get("status") == "Cancelled":
            continue
        upcoming.append({
            "name": event.get("name"), "event_date": event.get("event_date"),
            "start_utc": event.get("start_utc"), "earnings_session": event.get("earnings_session"),
            "status": event.get("status"), "source_verification": event.get("source_verification"),
            "provider": event.get("provider"), "source_url": event.get("source_url"),
        })
    company_name = next((item.get("company_name") for item in list_watchlist() if item["symbol"].upper() == ticker), "")
    evidence = NoExternalResearchEvidence().fetch_evidence(ticker)
    saved_strategy = None
    if strategy:
        saved_strategy = {key: strategy.get(key) for key in (
            "investment_thesis", "investment_horizon", "thesis_status", "strategy_type",
            "entry_price_low", "entry_price_high", "target_price", "invalidation_price",
            "catalysts", "risks", "bullish_scenario", "bearish_scenario", "last_review_date",
            "valuation_notes", "stop_invalidation",
        )}
    return {
        "ticker": ticker,
        "company_name": company_name or None,
        "company_name_provenance": "user-provided watchlist label; not independently verified" if company_name else None,
        "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cached_quote": ({"price": quote.get("price"), "observation_date": quote_date,
                          "provider": "Alpha Vantage", "source": "local quote cache",
                          "stale": quote_is_stale(quote), "price_adjustment_basis": "unverified; quote values are not reconciled to adjusted history"} if quote else None),
        "historical_source": {"provider": TiingoDailyProvider.name,
                              "adjustment_basis": TiingoDailyProvider.adjustment_basis,
                              "latest_observation_date": latest_history_date,
                              "stale": technical["history_stale"], "cached_observations": len(bars)},
        "price_reconciliation": {
            "status": quote_history_status,
            "latest_quote_date": quote_date,
            "latest_technical_history_date": latest_history_date,
            "interpretation": "Different observation dates are a normal freshness difference. Same-date price levels are not compared because quote adjustment basis is unverified.",
            "technical_price_basis": TiingoDailyProvider.adjustment_basis,
            "quote_price_basis": "unverified",
        },
        "tiingo_adjusted_daily_history": technical,
        "spy_relative_performance": relative,
        "sector_rotation": sector_rotation,
        "upcoming_earnings_events": upcoming,
        "saved_user_strategy": saved_strategy,
        "verified_external_evidence": evidence,
        "external_evidence_status": "No filings, fundamentals, valuation feed, or recent-news provider is configured; model recollection is not verified evidence.",
        "calculation_notes": "Daily adjusted closes; returns use N+1 valid contiguous closes. RSI14 uses Wilder smoothing. Volatility is annualized from 20 daily returns with 252 sessions/year.",
    }

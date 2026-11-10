"""Provider-independent orchestration for persistent daily OHLCV history."""

from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from dashboard.data_access import (
    list_daily_price_coverage,
    list_daily_prices,
    reserve_historical_request,
    upsert_daily_prices,
)
from dashboard.database import DEFAULT_DATABASE_PATH
from dashboard.history_providers import DailyPriceProvider, HistoricalDataError, TiingoDailyProvider
from dashboard.market_calendar import expected_sessions_between, latest_completed_us_equity_session


BROAD_MARKET_SYMBOLS = ("SPY", "RSP", "QQQ", "DIA", "IWM", "SMH")
SECTOR_SYMBOLS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Health Care", "XLI": "Industrials", "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication Services",
}
MACRO_PROXY_SYMBOLS = ("TLT", "GLD", "USO")
CREDIT_PROXY_SYMBOLS = ("HYG", "LQD", "IEF")
MARKET_ETF_SYMBOLS = tuple(dict.fromkeys((
    *BROAD_MARKET_SYMBOLS, *SECTOR_SYMBOLS, *MACRO_PROXY_SYMBOLS,
    *CREDIT_PROXY_SYMBOLS, "MAGS",
)))
HISTORY_LOOKBACK_YEARS = 2
OVERLAP_CALENDAR_DAYS = 7


def market_universe(database_path=None) -> list[str]:
    """Compose the requested universe with manually tracked positions and watchlist."""
    from dashboard.data_access import (
        list_holdings, list_market_leadership_symbols, list_market_symbols,
        list_sector_display_symbols, list_watchlist,
    )

    symbols = (list(BROAD_MARKET_SYMBOLS) + list(SECTOR_SYMBOLS)
               + list(MACRO_PROXY_SYMBOLS) + list(CREDIT_PROXY_SYMBOLS))
    symbols.extend(list_market_symbols(database_path) if database_path else list_market_symbols())
    symbols.extend(list_market_leadership_symbols(database_path) if database_path else list_market_leadership_symbols())
    symbols.extend(list_sector_display_symbols(database_path) if database_path else list_sector_display_symbols())
    symbols.extend(item["symbol"] for item in (list_holdings(database_path) if database_path else list_holdings()))
    symbols.extend(item["symbol"] for item in (list_watchlist(database_path) if database_path else list_watchlist()))
    return list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))


def market_etf_universe() -> list[str]:
    """Return the ETF/index set used by Market analytics, excluding user stocks.

    Holdings, watchlists and arbitrary research symbols remain in their own
    workflows and existing history cache; they are not part of Market's daily
    price-refresh job.
    """
    return list(MARKET_ETF_SYMBOLS)


def _two_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - HISTORY_LOOKBACK_YEARS)
    except ValueError:  # leap day
        return day.replace(year=day.year - HISTORY_LOOKBACK_YEARS, day=28)


def refresh_historical_data(
    symbols: Iterable[str],
    provider: DailyPriceProvider | None = None,
    *,
    as_of: date | None = None,
    stale_after_days: int | None = None,
    overlap_days: int = OVERLAP_CALENDAR_DAYS,
    database_path=None,
    on_symbol=None,
) -> dict:
    """Fetch initial lookback or stale tails only; all network calls are explicit.

    An overlap is requested on updates so provider corrections and recent missing
    observations can be repaired. Stored rows are isolated by provider and basis.
    """
    provider = provider or TiingoDailyProvider.from_environment()
    unique = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
    # Observation freshness is compared with the last completed exchange
    # session, not elapsed calendar days or the time a request last ran.
    expected_session = as_of or date.fromisoformat(
        latest_completed_us_equity_session(datetime.now(ZoneInfo("America/New_York")))
    )
    today = expected_session
    result = {
        "provider": provider.name if provider else "tiingo",
        "adjustment_basis": provider.adjustment_basis if provider else TiingoDailyProvider.adjustment_basis,
        "updated": {}, "cached": [], "errors": {}, "limited": False,
        "expected_session": expected_session.isoformat(), "diagnostics": [],
    }
    if provider is None:
        result["errors"]["Setup"] = (
            "No historical-data token is configured. Set TIINGO_API_KEY in the local .env file; "
            "the existing Alpha Vantage quote key remains separate."
        )
        result["coverage"] = _coverage(unique, result["provider"], result["adjustment_basis"], database_path)
        result["diagnostics"] = [{"symbol": symbol, "previous_cached_date": None,
            "expected_session_date": expected_session.isoformat(), "refresh_attempted": False,
            "provider_request_status": "not_configured", "latest_date_returned": None,
            "rows_upserted": 0, "new_cached_date": None, "final_freshness_state": "API error",
            "error_category": "missing_credentials"} for symbol in unique]
        return result

    target_start = _two_years_before(today)
    for symbol in unique:
        if on_symbol:
            on_symbol(symbol, "Checking cached observation")
        coverage = _coverage([symbol], provider.name, provider.adjustment_basis, database_path).get(symbol)
        latest = date.fromisoformat(coverage["latest_date"]) if coverage and coverage.get("latest_date") else None
        diag = {"symbol": symbol, "previous_cached_date": latest.isoformat() if latest else None,
                "expected_session_date": expected_session.isoformat(), "refresh_attempted": False,
                "provider_request_status": "not_requested", "latest_date_returned": None,
                "rows_upserted": 0, "new_cached_date": latest.isoformat() if latest else None,
                "final_freshness_state": "Provider data not yet available", "error_category": None}
        saved_bars = list_daily_prices(
            symbol, provider.name, provider.adjustment_basis,
            target_start.isoformat(), today.isoformat(), database_path,
        ) if database_path else list_daily_prices(
            symbol, provider.name, provider.adjustment_basis,
            target_start.isoformat(), today.isoformat(),
        )
        ranges = _refresh_ranges(saved_bars, target_start, today, latest, stale_after_days, overlap_days)
        if not ranges:
            result["cached"].append(symbol)
            diag["provider_request_status"] = "not_needed"
            diag["final_freshness_state"] = ("Already current" if latest == expected_session
                else "Observation ahead of expected session" if latest and latest > expected_session
                else "Skipped by policy")
            result["diagnostics"].append(diag)
            if on_symbol:
                on_symbol(symbol, diag["final_freshness_state"])
            continue
        diag["refresh_attempted"] = True
        diag["provider_request_status"] = "requested"
        errors_for_symbol = []
        for start, end in ranges:
            allowed, reason = reserve_historical_request(
                provider.name, symbol,
                hourly_limit=provider.max_requests_per_hour,
                daily_limit=provider.max_requests_per_day,
                monthly_symbol_limit=provider.max_symbols_per_month,
                database_path=database_path or DEFAULT_DATABASE_PATH,
            )
            if not allowed:
                result["limited"] = True
                result["errors"][symbol] = reason or "Provider request limit reached."
                diag["provider_request_status"] = "rate_limited" if "limit" in str(reason).lower() else "skipped_by_policy"
                diag["error_category"] = "rate_limited" if "limit" in str(reason).lower() else "request_policy"
                errors_for_symbol.append(result["errors"][symbol])
                continue
            try:
                bars = provider.fetch_daily(symbol, start.isoformat(), end.isoformat())
                if not bars:
                    result["errors"][symbol] = "Provider returned no observations for the requested incremental range; saved history was kept."
                    diag["provider_request_status"] = "no_observations"
                    diag["error_category"] = "no_observations"
                    errors_for_symbol.append(result["errors"][symbol])
                    continue
                returned_latest = max(date.fromisoformat(row["trading_date"]) for row in bars)
                diag["latest_date_returned"] = max(
                    date.fromisoformat(diag["latest_date_returned"]) if diag["latest_date_returned"] else returned_latest,
                    returned_latest,
                ).isoformat()
                saved_count = upsert_daily_prices(bars, database_path) if database_path else upsert_daily_prices(bars)
                result["updated"][symbol] = result["updated"].get(symbol, 0) + saved_count
                diag["rows_upserted"] += saved_count
                diag["provider_request_status"] = "success"
            except HistoricalDataError as error:
                result["errors"][symbol] = str(error)
                message = str(error).lower()
                if any(term in message for term in ("rate limit", "budget", "quota", "endpoint limit")):
                    result["limited"] = True
                    diag["provider_request_status"] = "rate_limited"
                    diag["error_category"] = "rate_limited"
                else:
                    diag["provider_request_status"] = "error"
                    diag["error_category"] = _historical_error_category(error)
                errors_for_symbol.append(str(error))
        updated_coverage = _coverage([symbol], provider.name, provider.adjustment_basis, database_path).get(symbol)
        new_latest = date.fromisoformat(updated_coverage["latest_date"]) if updated_coverage and updated_coverage.get("latest_date") else None
        diag["new_cached_date"] = new_latest.isoformat() if new_latest else None
        if new_latest and new_latest == expected_session:
            diag["final_freshness_state"] = "Updated" if diag["rows_upserted"] else "Already current"
            if diag["provider_request_status"] == "requested":
                diag["provider_request_status"] = "success"
        elif new_latest and new_latest > expected_session:
            diag["final_freshness_state"] = "Observation ahead of expected session"
        elif diag["provider_request_status"] == "no_observations":
            diag["final_freshness_state"] = "Provider publication pending"
        elif diag["error_category"]:
            diag["final_freshness_state"] = "Rate limited" if diag["error_category"] == "rate_limited" else "API error"
        elif diag["provider_request_status"] == "success":
            diag["final_freshness_state"] = "Provider publication pending"
        else:
            diag["final_freshness_state"] = "Skipped by policy"
        if errors_for_symbol:
            diag["error_message"] = "; ".join(dict.fromkeys(errors_for_symbol))
        result["diagnostics"].append(diag)
        if on_symbol:
            on_symbol(symbol, diag["final_freshness_state"])

    result["coverage"] = _coverage(unique, provider.name, provider.adjustment_basis, database_path)
    return result


def _coverage(symbols, provider, adjustment_basis, database_path=None):
    if database_path:
        return list_daily_price_coverage(symbols, provider, adjustment_basis, database_path)
    return list_daily_price_coverage(symbols, provider, adjustment_basis)


def _refresh_ranges(bars, target_start, today, latest, stale_after_days, overlap_days):
    """Find missing history and refresh a tail that misses the expected session."""
    if not bars:
        return [(target_start, today)]
    dates = [date.fromisoformat(row["trading_date"]) for row in bars]
    ranges = []
    # A recent first observation is a missing historical prefix, even when the
    # latest row is fresh. Fetch that prefix instead of treating a short cache as complete.
    earliest = dates[0]
    if earliest > target_start + timedelta(days=4):
        ranges.append((target_start, earliest - timedelta(days=1)))
    for previous, following in zip(dates, dates[1:]):
        if expected_sessions_between(previous, following):
            start, end = previous + timedelta(days=1), following - timedelta(days=1)
            if start <= end:
                ranges.append((start, end))
    if latest and latest < today:
        # Calendar-day cache age is irrelevant: October 30 remains stale on
        # November 3 when November 2 is the latest completed XNYS session.
        start = max(target_start, latest - timedelta(days=overlap_days))
        ranges.append((min(start, today), today))
    return _merge_ranges(ranges)


def _historical_error_category(error: Exception) -> str:
    message = str(error).lower()
    cause = getattr(error, "__cause__", None)
    if "timed out" in message or "timeout" in type(cause).__name__.lower():
        return "timeout"
    if "rejected" in message or "token" in message:
        return "authentication_error"
    if "returned http" in message:
        return "provider_http_error"
    if "malformed" in message or "invalid" in message or "unexpected response" in message:
        return "data_validation_error"
    if "reach tiingo" in message or type(cause).__name__ in {"ConnectionError", "SSLError"}:
        return "network_error"
    return "provider_error"


def _merge_ranges(ranges):
    if not ranges:
        return []
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def load_history(
    symbol: str,
    provider: str = TiingoDailyProvider.name,
    adjustment_basis: str = TiingoDailyProvider.adjustment_basis,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    database_path=None,
) -> list[dict]:
    if database_path:
        return list_daily_prices(symbol, provider, adjustment_basis, start_date, end_date, database_path)
    return list_daily_prices(symbol, provider, adjustment_basis, start_date, end_date)

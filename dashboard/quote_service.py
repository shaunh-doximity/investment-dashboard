"""Coordinates manual quote refreshes, pacing, request budgets, and local caching."""

import time
from collections.abc import Callable

from dashboard.data_access import (
    api_requests_used_today,
    get_cached_quote,
    quote_is_cached_recently,
    record_api_request_if_under_limit,
    save_cached_quote,
    seconds_until_next_api_request,
)
from dashboard.market_data import (
    MarketDataError,
    fetch_global_quote,
    get_api_key,
    quote_is_stale,
)


DAILY_REQUEST_LIMIT = 25
MINIMUM_REQUEST_INTERVAL_SECONDS = 12


def refresh_quotes(
    symbols: list[str], progress: Callable[[str], None] | None = None
) -> dict:
    """Fetch missing/old symbols only. Network calls happen only when a UI button calls this."""
    unique_symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
    result = {"updated": [], "cached": [], "errors": {}, "limited": False}
    api_key = get_api_key()
    if not api_key:
        result["errors"]["Setup"] = "No API key found. Set ALPHA_VANTAGE_API_KEY in the local .env file."
        return result

    for symbol in unique_symbols:
        cached_quote = get_cached_quote(symbol)
        if quote_is_cached_recently(symbol) and cached_quote and not quote_is_stale(cached_quote):
            result["cached"].append(symbol)
            continue

        wait_seconds = seconds_until_next_api_request(MINIMUM_REQUEST_INTERVAL_SECONDS)
        if wait_seconds > 0:
            if progress:
                progress(f"Waiting {wait_seconds:.0f} seconds to stay within the provider's per-minute limit…")
            time.sleep(wait_seconds)

        allowed, _used = record_api_request_if_under_limit(DAILY_REQUEST_LIMIT)
        if not allowed:
            result["limited"] = True
            result["errors"]["Daily API budget"] = (
                "This app has reached its 25-request daily limit. Try again tomorrow, "
                "or check your provider plan."
            )
            break

        if progress:
            progress(f"Requesting {symbol}…")
        try:
            quote = fetch_global_quote(symbol, api_key)
            save_cached_quote(quote)
            result["updated"].append(symbol)
        except MarketDataError as error:
            result["errors"][symbol] = str(error)
            if "request limit" in str(error).lower():
                result["limited"] = True
                break

    result["calls_used_today"] = api_requests_used_today()
    result["calls_remaining_in_app"] = max(
        0, DAILY_REQUEST_LIMIT - result["calls_used_today"]
    )
    result["quotes"] = {symbol: get_cached_quote(symbol) for symbol in unique_symbols}
    return result

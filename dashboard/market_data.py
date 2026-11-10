"""Alpha Vantage quote client. This module never creates fallback prices."""

import os
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_URL = "https://www.alphavantage.co/query"
FRESHNESS_LABEL = "End-of-day quote; not real-time (free-tier default)"


class MarketDataError(Exception):
    """A provider or connection error that is safe to show in the UI."""


def get_api_key(env_path: str | Path = PROJECT_ROOT / ".env") -> str | None:
    """Read the key from the local .env file or an existing environment variable."""
    load_dotenv(dotenv_path=env_path, override=False)
    return os.getenv("ALPHA_VANTAGE_API_KEY")


def _number(value: Any, field_name: str, *, percent: bool = False) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if percent:
        text = text.removesuffix("%").strip()
    try:
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("value is not finite")
        return number
    except ValueError as error:
        raise MarketDataError(f"Alpha Vantage returned an invalid {field_name}.") from error


def parse_global_quote(payload: dict, requested_symbol: str) -> dict:
    """Convert documented GLOBAL_QUOTE fields into the app's quote shape."""
    for message_key in ("Error Message", "Note", "Information"):
        if payload.get(message_key):
            message = " ".join(str(payload[message_key]).split())
            if message_key == "Note" or "rate" in message.lower() or "limit" in message.lower():
                raise MarketDataError(
                    "Alpha Vantage reported a request limit. Wait before refreshing again."
                )
            if message_key == "Information" and any(
                token in message.lower() for token in ("premium", "subscription", "entitlement")
            ):
                raise MarketDataError(
                    "This quote or data entitlement is unavailable for the current API plan."
                )
            raise MarketDataError(f"Alpha Vantage: {message}")

    raw_quote = payload.get("Global Quote")
    if not isinstance(raw_quote, dict) or not raw_quote:
        raise MarketDataError("Alpha Vantage did not return a quote for this symbol.")

    returned_symbol = str(raw_quote.get("01. symbol") or requested_symbol).strip().upper()
    price = _number(raw_quote.get("05. price"), "price")
    quote_date = str(raw_quote.get("07. latest trading day") or "").strip()
    if price is None or price <= 0:
        raise MarketDataError("Alpha Vantage did not provide a usable price.")
    try:
        datetime.strptime(quote_date, "%Y-%m-%d")
    except ValueError as error:
        raise MarketDataError("Alpha Vantage did not provide a valid quote date.") from error

    return {
        "symbol": returned_symbol,
        "price": price,
        "change": _number(raw_quote.get("09. change"), "daily change"),
        "change_percent": _number(
            raw_quote.get("10. change percent"), "daily change percentage", percent=True
        ),
        "quote_date": quote_date,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "freshness": FRESHNESS_LABEL,
    }


def quote_is_stale(quote: dict, today: date | None = None, stale_after_days: int = 4) -> bool:
    """Treat a provider quote as stale when its trading date is several days old."""
    try:
        quote_date = date.fromisoformat(quote["quote_date"])
    except (KeyError, TypeError, ValueError):
        return True
    current_date = today or datetime.now(timezone.utc).date()
    age_days = (current_date - quote_date).days
    return age_days < 0 or age_days > stale_after_days


def fetch_global_quote(symbol: str, api_key: str, timeout_seconds: int = 20) -> dict:
    """Request one quote; callers decide when to make requests and cache success."""
    try:
        response = requests.get(
            API_URL,
            params={
                "function": "GLOBAL_QUOTE",
                "symbol": symbol.strip().upper(),
                "apikey": api_key,
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as error:
        raise MarketDataError("The quote request timed out. Try again later.") from error
    except requests.RequestException as error:
        raise MarketDataError("Could not reach Alpha Vantage. Check your internet connection.") from error
    except ValueError as error:
        raise MarketDataError("Alpha Vantage returned a response that was not valid JSON.") from error

    if not isinstance(payload, dict):
        raise MarketDataError("Alpha Vantage returned an unexpected response format.")
    return parse_global_quote(payload, symbol)

"""Provider interfaces and concrete historical price adapters (no Streamlit imports)."""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HistoricalDataError(Exception):
    """Safe-to-display data provider error without credentials or response URLs."""


class DailyPriceProvider(Protocol):
    name: str
    adjustment_basis: str
    max_requests_per_hour: int
    max_requests_per_day: int
    max_symbols_per_month: int

    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> list[dict]:
        """Return canonical OHLCV records for the requested inclusive date range."""


class TiingoDailyProvider:
    """Official Tiingo EOD endpoint using split- and dividend-adjusted bars."""

    name = "tiingo"
    adjustment_basis = "split_and_dividend_adjusted"
    max_requests_per_hour = 50
    max_requests_per_day = 1000
    max_symbols_per_month = 500
    endpoint = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"

    def __init__(self, token: str, timeout_seconds: int = 30):
        if not token.strip():
            raise HistoricalDataError("Tiingo API token is empty.")
        self._token = token.strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls, env_path: str | Path = PROJECT_ROOT / ".env") -> "TiingoDailyProvider | None":
        load_dotenv(dotenv_path=env_path, override=False)
        token = os.getenv("TIINGO_API_KEY")
        return cls(token) if token and token.strip() else None

    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> list[dict]:
        normalized = symbol.strip().upper()
        try:
            response = requests.get(
                self.endpoint.format(symbol=normalized),
                params={"startDate": start_date, "endDate": end_date},
                headers={"Authorization": f"Token {self._token}", "Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
            if response.status_code == 429:
                raise HistoricalDataError("Tiingo rate limit reached. Wait before refreshing again.")
            if response.status_code in (401, 403):
                raise HistoricalDataError("Tiingo rejected this request. Check the token and plan access.")
            if response.status_code >= 400:
                raise HistoricalDataError(f"Tiingo returned HTTP {response.status_code}; cached history was preserved.")
            response.raise_for_status()
            payload = response.json()
        except HistoricalDataError:
            raise
        except requests.Timeout as error:
            raise HistoricalDataError("Tiingo historical request timed out.") from error
        except requests.RequestException as error:
            raise HistoricalDataError("Could not reach Tiingo. Check the network and retry manually.") from error
        except ValueError as error:
            raise HistoricalDataError("Tiingo returned a response that was not valid JSON.") from error

        if not isinstance(payload, list):
            # Deliberately avoid echoing provider payloads because they may contain request details.
            payload_text = " ".join(str(value) for value in payload.values()) if isinstance(payload, dict) else ""
            if any(term in payload_text.lower() for term in ("quota", "rate limit", "plan limit", "limit reached")):
                raise HistoricalDataError("Tiingo returned an account or endpoint limit response; verify plan/quota and retry later.")
            raise HistoricalDataError("Tiingo returned an unexpected response or plan-limit message.")
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        bars = []
        for item in payload:
            if not isinstance(item, dict):
                raise HistoricalDataError("Tiingo returned a malformed daily bar.")
            try:
                trading_date = str(item["date"])[:10]
                datetime.strptime(trading_date, "%Y-%m-%d")
                adjusted_open = _finite_positive(item.get("adjOpen"), "adjusted open")
                adjusted_high = _finite_positive(item.get("adjHigh"), "adjusted high")
                adjusted_low = _finite_positive(item.get("adjLow"), "adjusted low")
                adjusted_close = _finite_positive(item.get("adjClose"), "adjusted close")
                adjusted_volume = _finite_nonnegative(item.get("adjVolume"), "adjusted volume")
                if adjusted_high < max(adjusted_open, adjusted_low, adjusted_close):
                    raise HistoricalDataError("Tiingo returned inconsistent adjusted OHLC values.")
                bar = {
                    "symbol": normalized,
                    "trading_date": trading_date,
                    "provider": self.name,
                    "adjustment_basis": self.adjustment_basis,
                    "open": adjusted_open,
                    "high": adjusted_high,
                    "low": adjusted_low,
                    "close": adjusted_close,
                    "volume": adjusted_volume,
                    "retrieved_at": fetched_at,
                }
            except (KeyError, TypeError, ValueError) as error:
                if isinstance(error, HistoricalDataError):
                    raise
                raise HistoricalDataError("Tiingo returned an invalid daily OHLCV observation.") from error
            if start_date <= trading_date <= end_date:
                bars.append(bar)
        return sorted(bars, key=lambda bar: bar["trading_date"])


def _finite_positive(value, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise HistoricalDataError(f"Tiingo did not provide a valid {field}.") from error
    if not math.isfinite(number) or number <= 0:
        raise HistoricalDataError(f"Tiingo did not provide a valid {field}.")
    return number


def _finite_nonnegative(value, field: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise HistoricalDataError(f"Tiingo did not provide a valid {field}.") from error
    if not math.isfinite(number) or number < 0:
        raise HistoricalDataError(f"Tiingo did not provide a valid {field}.")
    return number

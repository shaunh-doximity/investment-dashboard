"""Incremental FRED adapter for the Cboe VIX daily close series (VIXCLS)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from dashboard.data_access import list_volatility_observations, upsert_volatility_observations


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROVIDER = "fred_vixcls"
SERIES_ID = "VIXCLS"
ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"
FIRST_OBSERVATION = "1990-01-02"


class FredVolatilityError(RuntimeError):
    """Safe-to-display FRED client error; never contains the request URL or key."""


def fred_api_configured() -> bool:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return bool(os.environ.get("FRED_API_KEY", "").strip())


def parse_fred_observations(payload: dict, retrieved_at: str | None = None) -> list[dict]:
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        raise FredVolatilityError("FRED returned an unexpected response for VIXCLS.")
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = []
    for item in observations:
        try:
            observed = date.fromisoformat(item["date"]).isoformat()
            raw = str(item["value"]).strip()
            if raw in ("", "."):
                continue  # FRED dots represent missing/non-trading observations; never forward-fill.
            close = float(raw)
            if not math.isfinite(close) or close <= 0:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        result.append({"symbol": "VIX", "observation_date": observed, "close": close,
                       "provider": PROVIDER, "retrieved_at": timestamp})
    if not result:
        raise FredVolatilityError("FRED returned no valid VIXCLS closes for the requested dates; saved data was kept.")
    return result


def refresh_fred_vix(*, database_path=None, timeout_seconds: int = 25, today: date | None = None) -> dict:
    """Fetch only the recent tail plus a short overlap; the first request fetches full history."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    result = {"provider": PROVIDER, "series_id": SERIES_ID, "updated": 0, "errors": {}}
    if not api_key:
        result["errors"]["Setup"] = "FRED API key is not configured. Add FRED_API_KEY to the local .env file; existing cached data was kept."
        return result

    cached = list_volatility_observations(("VIX",), PROVIDER, database_path) if database_path else list_volatility_observations(("VIX",), PROVIDER)
    saved = cached.get("VIX", [])
    if saved:
        latest = max(row["observation_date"] for row in saved)
        start = max(date.fromisoformat(latest) - timedelta(days=7), date.fromisoformat(FIRST_OBSERVATION)).isoformat()
    else:
        start = FIRST_OBSERVATION
    end = (today or datetime.now(timezone.utc).date()).isoformat()
    try:
        response = requests.get(
            ENDPOINT,
            params={"series_id": SERIES_ID, "api_key": api_key, "file_type": "json",
                    "observation_start": start, "observation_end": end, "sort_order": "asc"},
            timeout=timeout_seconds,
        )
        if response.status_code == 429:
            raise FredVolatilityError("FRED rate limit reached. Wait before trying another manual refresh.")
        if response.status_code in (401, 403):
            raise FredVolatilityError("FRED rejected the API key. Check FRED_API_KEY and account access; cached data was kept.")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise FredVolatilityError("FRED returned a response that was not valid JSON; cached data was kept.") from error
        if isinstance(payload, dict) and payload.get("error_code"):
            raise FredVolatilityError("FRED rejected the VIXCLS request. Check the API key and series access; cached data was kept.")
        rows = parse_fred_observations(payload)
        result["updated"] = upsert_volatility_observations(rows, database_path) if database_path else upsert_volatility_observations(rows)
        result["requested_start"] = start
        result["observation_end"] = max(row["observation_date"] for row in rows)
    except FredVolatilityError as error:
        result["errors"]["FRED"] = str(error)
    except requests.Timeout:
        result["errors"]["FRED"] = "FRED request timed out. Existing cached data was kept; retry manually later."
    except requests.RequestException:
        # Do not display exception text: request URLs may contain the API key.
        result["errors"]["FRED"] = "Could not reach FRED. Existing cached data was kept; check connectivity and retry manually."
    return result


def load_fred_vix(database_path=None) -> list[dict]:
    data = list_volatility_observations(("VIX",), PROVIDER, database_path) if database_path else list_volatility_observations(("VIX",), PROVIDER)
    return data.get("VIX", [])

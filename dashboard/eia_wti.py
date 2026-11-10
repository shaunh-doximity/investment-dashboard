"""Manual EIA Cushing WTI spot-price adapter (distinct from crude futures/ETFs)."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from dashboard.data_access import list_macro_series_observations, upsert_macro_series_observations
from dashboard.ai_providers import PROJECT_ROOT

PROVIDER = "eia_api_v2"
SERIES_ID = "PET.RWTC.D"
SERIES_LABEL = "Cushing, OK WTI Spot Price FOB"
UNITS = "USD per barrel"
SOURCE_URL = "https://www.eia.gov/dnav/pet/hist/RWTCD.htm"
ENDPOINT = "https://api.eia.gov/v2/seriesid/PET.RWTC.D/data/"


def _api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    key = os.getenv("EIA_API_KEY", "").strip()
    if key:
        return key
    try:
        import streamlit as st
        return str(st.secrets.get("EIA_API_KEY", "") or "").strip()
    except Exception:
        return ""


def load_wti_spot(database_path=None) -> list[dict]:
    cached = (list_macro_series_observations([SERIES_ID], PROVIDER, database_path)
              if database_path else list_macro_series_observations([SERIES_ID], PROVIDER))
    return cached.get(SERIES_ID, [])


def parse_eia_wti_response(payload: dict, *, retrieved_at: str | None = None) -> list[dict]:
    """Parse APIv2 records; malformed/missing values are ignored, never fabricated."""
    rows = payload.get("response", {}).get("data", []) if isinstance(payload, dict) else []
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    output = []
    for row in rows if isinstance(rows, list) else []:
        try:
            day = datetime.strptime(str(row["period"]), "%Y-%m-%d").date().isoformat()
            value = float(row["value"])
            if value <= 0:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        output.append({"series_id": SERIES_ID, "observation_date": day, "value": value,
                       "provider": PROVIDER, "units": UNITS,
                       "initial_release_only": True, "retrieved_at": timestamp})
    return sorted(output, key=lambda item: item["observation_date"])


def refresh_wti_spot(*, database_path=None, http=requests) -> dict:
    """Fetch manually on explicit user action; a failed refresh preserves SQLite cache."""
    key = _api_key()
    if not key:
        return {"status": "missing_credentials", "message": "Set EIA_API_KEY in .env or Streamlit secrets; cached WTI data were kept."}
    try:
        response = http.get(ENDPOINT, params={"api_key": key, "frequency": "daily",
            "data[0]": "value", "sort[0][column]": "period", "sort[0][direction]": "desc",
            "length": 500}, timeout=(8, 25))
        if response.status_code in (401, 403):
            return {"status": "authentication_error", "message": "EIA rejected the request; verify EIA_API_KEY. Cached WTI data were kept."}
        response.raise_for_status()
        rows = parse_eia_wti_response(response.json())
        if not rows:
            return {"status": "no_observations", "message": "EIA returned no valid WTI spot observations; cached data were kept."}
        (upsert_macro_series_observations(rows, database_path) if database_path
         else upsert_macro_series_observations(rows))
        return {"status": "success", "saved": len(rows), "latest_date": rows[-1]["observation_date"]}
    except requests.Timeout:
        return {"status": "timeout", "message": "EIA request timed out; cached WTI data were kept."}
    except requests.RequestException as exc:
        return {"status": "network_error", "message": f"EIA request failed ({type(exc).__name__}); cached WTI data were kept."}
    except (ValueError, TypeError):
        return {"status": "invalid_response", "message": "EIA response was not valid JSON; cached WTI data were kept."}


def wti_window_change(rows: list[dict], sessions: int) -> dict:
    ordered = sorted(rows or [], key=lambda item: item.get("observation_date", ""))
    if len(ordered) < sessions + 1:
        return {"status": "insufficient_history", "required": sessions + 1, "available": len(ordered)}
    sample = ordered[-(sessions + 1):]
    try:
        start, end = float(sample[0]["value"]), float(sample[-1]["value"])
        if start <= 0:
            raise ValueError
        return {"status": "available", "change_percent": (end / start - 1) * 100,
                "start_date": sample[0]["observation_date"], "end_date": sample[-1]["observation_date"],
                "start_value": start, "end_value": end}
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid_observation"}

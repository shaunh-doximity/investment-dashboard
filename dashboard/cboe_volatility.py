"""Explicitly refreshed Cboe volatility-index history and local analytics."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO
import math

import requests

from dashboard.data_access import (
    list_volatility_observations, upsert_volatility_observations,
)


PROVIDER = "cboe_official_csv"
HISTORY_URLS = {
    "VIX": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VIX9D": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
}


class CboeDataError(Exception):
    """Safe-to-display error from an official Cboe daily CSV download."""


def parse_cboe_history(text: str, symbol: str, retrieved_at: str | None = None) -> list[dict]:
    """Parse a Cboe daily history CSV. Invalid/missing observations stay missing."""
    symbol = symbol.strip().upper()
    if symbol not in HISTORY_URLS:
        raise ValueError("Unsupported Cboe volatility symbol.")
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise CboeDataError(f"Cboe returned an empty {symbol} history file.")
    normalized_headers = {name.strip().lower(): name for name in reader.fieldnames if name}
    date_key = next((normalized_headers[name] for name in ("date", "observation_date") if name in normalized_headers), None)
    close_key = next((normalized_headers[name] for name in ("close", "closing value", "closing_value") if name in normalized_headers), None)
    open_key = next((normalized_headers[name] for name in ("open", "opening value", "opening_value") if name in normalized_headers), None)
    high_key = next((normalized_headers[name] for name in ("high", "daily high", "daily_high") if name in normalized_headers), None)
    low_key = next((normalized_headers[name] for name in ("low", "daily low", "daily_low") if name in normalized_headers), None)
    if not date_key or not close_key:
        raise CboeDataError(f"Cboe {symbol} file did not contain date and close columns.")
    fetched_at = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    output = []
    for row in reader:
        raw_date, raw_close = (row.get(date_key) or "").strip(), (row.get(close_key) or "").strip()
        if not raw_date or not raw_close:
            continue
        parsed_day = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                parsed_day = datetime.strptime(raw_date, fmt).date().isoformat()
                break
            except ValueError:
                pass
        try:
            close = float(raw_close.replace(",", ""))
        except ValueError:
            continue
        if parsed_day and math.isfinite(close) and close > 0:
            item = {"symbol": symbol, "observation_date": parsed_day, "close": close,
                    "provider": PROVIDER, "retrieved_at": fetched_at}
            for field, key in (("open", open_key), ("high", high_key), ("low", low_key)):
                try:
                    value = float((row.get(key) or "").replace(",", "")) if key else None
                except ValueError:
                    value = None
                item[field] = value if value is not None and math.isfinite(value) and value > 0 else None
            if item["open"] and item["high"] and item["low"] and not (
                item["low"] <= min(item["open"], close) <= max(item["open"], close) <= item["high"]
            ):
                item.update(open=None, high=None, low=None)
            output.append(item)
    if not output:
        raise CboeDataError(f"Cboe {symbol} file contained no valid daily closes.")
    return output


def refresh_cboe_volatility(*, timeout_seconds: int = 25, database_path=None) -> dict:
    """Fetch supported Cboe CSV files only when the user explicitly requests it."""
    result = {"updated": {}, "errors": {}, "provider": PROVIDER}
    for symbol, url in HISTORY_URLS.items():
        try:
            response = requests.get(url, headers={"Accept": "text/csv"}, timeout=timeout_seconds)
            if response.status_code == 429:
                raise CboeDataError("Cboe rate limited the download. Wait and refresh manually later.")
            response.raise_for_status()
            rows = parse_cboe_history(response.text, symbol)
            count = upsert_volatility_observations(rows, database_path) if database_path else upsert_volatility_observations(rows)
            result["updated"][symbol] = count
        except CboeDataError as error:
            result["errors"][symbol] = str(error)
        except requests.Timeout:
            result["errors"][symbol] = "Cboe historical download timed out. Retry manually later."
        except requests.RequestException:
            result["errors"][symbol] = "Could not reach the official Cboe history file. Existing cached values were kept."
    return result


def load_cboe_volatility(database_path=None) -> dict[str, list[dict]]:
    return list_volatility_observations(database_path=database_path) if database_path else list_volatility_observations()


def volatility_summary(histories: dict[str, list[dict]]) -> dict:
    """Compute VIX changes, average, percentile and same-date term ratios."""
    vix = sorted(histories.get("VIX", []), key=lambda row: row["observation_date"])
    vix9d = {row["observation_date"]: row["close"] for row in histories.get("VIX9D", [])}
    if not vix:
        return {"status": "unavailable", "as_of": None, "source": "Cboe official daily CSV"}
    latest = vix[-1]
    values = [float(row["close"]) for row in vix]
    observation_dates = [datetime.strptime(row["observation_date"], "%Y-%m-%d").date() for row in vix]
    current = values[-1]
    prior_close = values[-2] if len(values) > 1 else None
    def contiguous(start_index: int) -> bool:
        segment = observation_dates[start_index:]
        # Weekends and ordinary market holidays may span up to four calendar days.
        # Larger gaps mean the requested observation window has missing dates.
        return all((right - left).days <= 4 for left, right in zip(segment, segment[1:]))
    def change(n):
        if len(values) <= n or not contiguous(len(values) - 1 - n):
            return None
        return current - values[-1-n]
    trailing20 = values[-20:] if len(values) >= 20 and contiguous(len(values) - 20) else []
    trailing252 = values[-252:]
    percentile = (sum(value <= current for value in trailing252) / len(trailing252) * 100) if trailing252 else None
    common = sorted(set(vix9d) & {row["observation_date"] for row in vix})
    date9 = common[-1] if common else None
    vix_at_date = next((float(row["close"]) for row in reversed(vix) if row["observation_date"] == date9), None) if date9 else None
    ratio9 = vix9d[date9] / vix_at_date if date9 and vix_at_date else None
    try:
        ratio_age = (datetime.now(timezone.utc).date() - datetime.strptime(date9, "%Y-%m-%d").date()).days if date9 else None
    except ValueError:
        ratio_age = None
    ratio_status = "unavailable" if ratio9 is None else "stale" if ratio_age is not None and ratio_age > 4 else "available"
    vix3m_ratio = None  # No official historical VIX3M CSV is linked from Cboe's history page.
    level = "Normal" if current < 20 else "Elevated" if current < 30 else "Stressed"
    sma20 = sum(trailing20) / 20 if len(trailing20) == 20 else None
    provider = latest.get("provider", "cboe_official_csv")
    source = ("FRED VIXCLS historical reference" if provider == "fred_vixcls"
              else "Yahoo Finance ^VIX history (unofficial)" if provider == "yfinance_unofficial"
              else "Cboe official historical CSV")
    latest_date = datetime.strptime(latest["observation_date"], "%Y-%m-%d").date()
    age = (datetime.now(timezone.utc).date() - latest_date).days
    return {
        "status": "stale" if age > 4 else "available", "close": current,
        "as_of": latest["observation_date"], "retrieved_at": latest.get("retrieved_at"),
        "provider": provider, "previous_close": prior_close,
        "previous_change_points": current - prior_close if prior_close is not None and contiguous(len(values) - 2) else None,
        "previous_change_percent": (current / prior_close - 1) * 100 if prior_close and contiguous(len(values) - 2) else None,
        "change_5d_points": change(5), "change_20d_points": change(20),
        "sma20": sma20,
        "vs_sma20": "above" if sma20 is not None and current > sma20 else "below" if sma20 is not None and current < sma20 else "at_or_unavailable",
        "vs_sma20_points": current - sma20 if sma20 is not None else None,
        "percentile_252": percentile, "percentile_observations": len(trailing252),
        "vix9d_ratio": ratio9, "vix9d_as_of": date9, "vix9d_status": ratio_status,
        "vix3m_ratio": vix3m_ratio,
        "condition": level, "source": source, "history": vix,
    }

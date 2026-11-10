"""Official U.S. Treasury daily par-yield feed and SQLite cache."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import math
import xml.etree.ElementTree as ET

import requests

from dashboard.data_access import list_treasury_yields, upsert_treasury_yields


PROVIDER = "us_treasury_xml"
ENDPOINT = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
SERIES = "daily_treasury_yield_curve"


class TreasuryYieldError(RuntimeError):
    """Safe, provider-independent Treasury refresh error."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def parse_treasury_xml(payload: str | bytes, *, retrieved_at: str | None = None,
                       through: date | None = None) -> list[dict]:
    """Parse official OData XML entries; omit incomplete, invalid and future rows."""
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, TypeError) as error:
        raise TreasuryYieldError("Treasury returned invalid XML; cached yields were preserved.") from error
    retrieved = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    end_date = through or datetime.now(timezone.utc).date()
    output = []
    for entry in root.iter():
        if _local_name(entry.tag) != "entry":
            continue
        values = {_local_name(child.tag): (child.text or "").strip() for child in entry.iter()}
        raw_day = values.get("NEW_DATE", "")
        try:
            observation_date = date.fromisoformat(raw_day[:10])
            two_year = float(values.get("BC_2YEAR", ""))
            ten_year = float(values.get("BC_10YEAR", ""))
        except (TypeError, ValueError):
            continue
        if observation_date > end_date or not (math.isfinite(two_year) and math.isfinite(ten_year)):
            continue
        if two_year <= 0 or ten_year <= 0:
            continue
        output.append({"observation_date": observation_date.isoformat(), "two_year": two_year,
                       "ten_year": ten_year, "provider": PROVIDER, "retrieved_at": retrieved})
    return sorted({row["observation_date"]: row for row in output}.values(), key=lambda row: row["observation_date"])


def refresh_treasury_yields(*, database_path=None, today: date | None = None,
                            timeout_seconds: int = 25) -> dict:
    """Manually refresh recent annual feed slices; preserve cached values on failure."""
    through = today or datetime.now(ZoneInfo("America/New_York")).date()
    cached = list_treasury_yields(database_path=database_path) if database_path else list_treasury_yields()
    result = {"provider": PROVIDER, "updated": 0, "errors": {}, "requested_years": []}
    # Request time is not observation freshness. If today's fetch saw only an
    # older release, a later explicit retry must still ask Treasury again.
    latest_cached_date = cached[-1]["observation_date"] if cached else None
    if latest_cached_date and latest_cached_date >= through.isoformat():
        result["cached"] = True
        result["latest_date"] = latest_cached_date
        return result
    cached_years = {int(row["observation_date"][:4]) for row in cached}
    years = [through.year]
    if not cached or through.year - 1 not in cached_years:
        years.append(max(1990, through.year - 1))
    # First initialization includes two annual slices; the cache remains local thereafter.
    for year in dict.fromkeys(years):
        result["requested_years"].append(year)
        try:
            response = requests.get(
                ENDPOINT,
                params={"data": SERIES, "field_tdr_date_value": str(year)},
                headers={"Accept": "application/xml"}, timeout=timeout_seconds,
            )
            response.raise_for_status()
            rows = parse_treasury_xml(response.content or response.text, through=through)
            rows = [row for row in rows if row["observation_date"].startswith(str(year))]
            if not rows:
                result["errors"][str(year)] = "Treasury returned no valid 2Y/10Y yield observations for this year; cached data were preserved."
                continue
            count = upsert_treasury_yields(rows, database_path) if database_path else upsert_treasury_yields(rows)
            result["updated"] += count
        except requests.Timeout:
            result["errors"][str(year)] = "Treasury request timed out; cached yields were preserved."
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            result["errors"][str(year)] = (f"Treasury returned HTTP {status}; cached yields were preserved."
                                            if status else "Could not reach the official Treasury XML feed; cached yields were preserved.")
        except TreasuryYieldError as error:
            result["errors"][str(year)] = str(error)
    refreshed = list_treasury_yields(database_path=database_path) if database_path else list_treasury_yields()
    result["latest_date"] = refreshed[-1]["observation_date"] if refreshed else None
    return result


def treasury_freshness(rows: list[dict], *, completed_session: str | None) -> dict:
    """Compare the latest published date with the latest completed equity session."""
    if not rows:
        return {"status": "unavailable", "observation_date": None, "lag_sessions": None}
    latest = rows[-1]["observation_date"]
    if not completed_session:
        return {"status": "unknown", "observation_date": latest, "lag_sessions": None}
    if latest > completed_session:
        return {"status": "ahead_of_equity_cache", "observation_date": latest, "lag_sessions": 0}
    lag = (date.fromisoformat(completed_session) - date.fromisoformat(latest)).days
    status = "current" if latest == completed_session else "lagging"
    return {"status": status, "observation_date": latest, "lag_days": lag}

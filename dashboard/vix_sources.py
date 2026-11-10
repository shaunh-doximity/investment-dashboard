"""Source selection for completed-session VIX observations.

Cboe's published historical close is primary. Yahoo Finance history is an
unofficial opt-in fallback; FRED VIXCLS remains historical reference only.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from importlib.util import find_spec
import math

from dashboard.cboe_volatility import PROVIDER as CBOE_PROVIDER, load_cboe_volatility, refresh_cboe_volatility
from dashboard.data_access import upsert_volatility_observations
from dashboard.fred_volatility import load_fred_vix


YFINANCE_PROVIDER = "yfinance_unofficial"
PROVIDER_PRIORITY = {CBOE_PROVIDER: 0, YFINANCE_PROVIDER: 1, "fred_vixcls": 2}


def parse_yfinance_vix(frame, *, retrieved_at: str | None = None,
                       through: date | None = None) -> list[dict]:
    """Validate yfinance ^VIX daily closes as index points, without adjustment."""
    if frame is None or getattr(frame, "empty", True):
        return []
    close = frame["Close"] if "Close" in frame else None
    if close is None:
        return []
    # Avoid importing pandas in this provider-independent parser. MultiIndex
    # downloads can produce a nested frame; accept only a single close column.
    if hasattr(close, "columns"):
        if len(close.columns) != 1:
            return []
        close = close.iloc[:, 0]
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    through_date = through or date.today()
    output = []
    for raw_day, raw_value in close.items():
        try:
            observed = raw_day.date() if hasattr(raw_day, "date") else date.fromisoformat(str(raw_day)[:10])
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if observed > through_date or not math.isfinite(value) or value <= 0:
            continue
        output.append({"symbol": "VIX", "observation_date": observed.isoformat(), "close": value,
                       "provider": YFINANCE_PROVIDER, "retrieved_at": timestamp})
    return sorted({row["observation_date"]: row for row in output}.values(), key=lambda row: row["observation_date"])


def fetch_yfinance_vix(*, through: date | None = None, period: str = "5y") -> list[dict]:
    """Fetch fallback history only after explicit user action and opt-in."""
    if find_spec("yfinance") is None:
        raise RuntimeError("The optional yfinance package is not installed.")
    try:
        import yfinance as yf
        frame = yf.Ticker("^VIX").history(period=period, interval="1d", auto_adjust=False, actions=False)
    except Exception as error:
        # Do not include arbitrary provider exception text in the UI.
        raise RuntimeError(f"The yfinance ^VIX request failed ({type(error).__name__}).") from error
    rows = parse_yfinance_vix(frame, through=through)
    if not rows:
        raise RuntimeError("yfinance returned no valid completed ^VIX daily observations.")
    return rows


def refresh_vix_sources(*, completed_session: str | None, allow_yfinance: bool = False,
                         database_path=None, today: date | None = None) -> dict:
    """Refresh Cboe first; optionally use Yahoo only if Cboe misses the completed session."""
    result = {"provider": CBOE_PROVIDER, "updated": {}, "errors": {}, "fallback_used": False}
    cboe_refresh = refresh_cboe_volatility(database_path=database_path) if database_path else refresh_cboe_volatility()
    result["updated"].update(cboe_refresh.get("updated", {}))
    result["errors"].update({f"Cboe {key}": value for key, value in cboe_refresh.get("errors", {}).items()})
    cboe = load_cboe_volatility(database_path) if database_path else load_cboe_volatility()
    latest_cboe = max((row["observation_date"] for row in cboe.get("VIX", [])), default=None)
    result["latest_cached"] = {"source": CBOE_PROVIDER, "date": latest_cboe} if latest_cboe else None
    if completed_session and latest_cboe == completed_session:
        result["current_candidate"] = {"source": CBOE_PROVIDER, "date": latest_cboe}
        return result
    if allow_yfinance:
        try:
            through = date.fromisoformat(completed_session) if completed_session else today
            rows = fetch_yfinance_vix(through=through)
            count = upsert_volatility_observations(rows, database_path) if database_path else upsert_volatility_observations(rows)
            result["updated"][YFINANCE_PROVIDER] = count
            result["fallback_used"] = True
            latest_yahoo = rows[-1]["observation_date"]
            result["latest_cached"] = {"source": YFINANCE_PROVIDER, "date": latest_yahoo}
            result["current_candidate"] = {"source": YFINANCE_PROVIDER, "date": latest_yahoo}
            if completed_session and latest_yahoo != completed_session:
                result["errors"]["yfinance freshness"] = "The returned fallback history does not include the latest completed SPY session."
        except RuntimeError as error:
            result["errors"]["yfinance"] = str(error)
    else:
        result["errors"]["current source"] = "Cboe did not provide the latest completed session; the Yahoo fallback is disabled. Current VIX will be unavailable until refreshed data catches up."
    return result


def select_vix_series(*, completed_session: str | None, cboe=None, yahoo=None, fred=None) -> dict:
    """Choose latest non-FRED source at/before a completed session; Cboe wins same-date ties."""
    candidates = []
    for provider, rows in ((CBOE_PROVIDER, (cboe or {}).get("VIX", [])),
                           (YFINANCE_PROVIDER, (yahoo or {}).get("VIX", []))):
        valid = [row for row in rows if not completed_session or row["observation_date"] <= completed_session]
        if valid:
            row = max(valid, key=lambda item: item["observation_date"])
            candidates.append(row)
    selected = max(candidates, key=lambda row: (row["observation_date"], -PROVIDER_PRIORITY.get(row["provider"], 99))) if candidates else None
    fresh = bool(selected and completed_session and selected["observation_date"] == completed_session)
    reference = sorted(fred or [], key=lambda row: row["observation_date"])
    selected_history = ((cboe or {}).get("VIX", []) if selected and selected.get("provider") == CBOE_PROVIDER
                        else (yahoo or {}).get("VIX", []) if selected else [])
    return {"current_available": fresh, "selected": selected if fresh else None,
            "latest_candidate": selected, "source": selected["provider"] if fresh else None,
            "as_of": selected["observation_date"] if fresh else None,
            "history": sorted(selected_history, key=lambda row: row["observation_date"]),
            "historical_reference": reference[-1] if reference else None,
            "status": "current" if fresh else "current_unavailable"}


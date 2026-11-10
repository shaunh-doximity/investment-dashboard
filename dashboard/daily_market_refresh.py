"""One explicit, idempotent daily refresh for local Market data sources."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from dashboard.event_calendar import refresh_official_calendars
from dashboard.market_calendar import latest_completed_us_equity_session
from dashboard.market_history import refresh_historical_data
from dashboard.recent_catalysts import load_recent_catalysts
from dashboard.treasury_yields import refresh_treasury_yields
from dashboard.vix_sources import refresh_vix_sources


def run_daily_market_refresh(symbols, *, now: datetime | None = None,
                             allow_yfinance: bool = False, database_path=None,
                             on_stage=None, on_history_symbol=None,
                             history_refresh=refresh_historical_data,
                             vix_refresh=refresh_vix_sources,
                             treasury_refresh=refresh_treasury_yields,
                             calendar_refresh=refresh_official_calendars):
    """Refresh sources sequentially and preserve successful results on failure.

    AI is intentionally not called here. A web-enabled full analysis can update
    the recent-catalyst evidence cache only after the user separately opts in.
    """
    instant = now or datetime.now(ZoneInfo("America/New_York"))
    if instant.tzinfo:
        instant = instant.astimezone(ZoneInfo("America/New_York"))
    expected = latest_completed_us_equity_session(instant)
    result = {"expected_session": expected, "stages": {}, "data_changed": False,
              "completed_at": datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")}

    def stage(name, status, detail=""):
        item = {"status": status, "detail": str(detail)[:600]}
        result["stages"][name] = item
        if on_stage:
            on_stage(name, status, item["detail"])

    stage("session", "complete", f"Latest completed XNYS session: {expected}")
    try:
        history = history_refresh(symbols, as_of=date.fromisoformat(expected), database_path=database_path,
                                  on_symbol=on_history_symbol)
        result["historical"] = history
        updated = history.get("updated", {})
        result["data_changed"] |= bool(updated)
        pending = sum(row.get("final_freshness_state") == "Provider publication pending"
                      for row in history.get("diagnostics", []))
        stage("historical", "complete_with_gaps" if history.get("errors") or pending else "complete",
              f"{len(updated)} symbols updated; {pending} provider-pending; {len(history.get('errors', {}))} errors")
    except Exception as error:
        safe_error = f"Tiingo refresh failed ({type(error).__name__}); existing history was preserved."
        result["historical"] = {"updated": {}, "errors": {"stage": safe_error},
                                "diagnostics": [], "expected_session": expected}
        stage("historical", "failed", safe_error)

    try:
        vix = vix_refresh(completed_session=expected, allow_yfinance=allow_yfinance,
                          database_path=database_path)
        result["vix"] = vix
        result["data_changed"] |= any(int(value or 0) > 0 for value in vix.get("updated", {}).values())
        stage("vix", "complete_with_gaps" if vix.get("errors") else "complete",
              f"Cboe / optional fallback through {expected}; updated {sum(vix.get('updated', {}).values())} rows")
    except Exception as error:
        safe_error = f"VIX refresh failed ({type(error).__name__}); existing observations were preserved."
        result["vix"] = {"updated": {}, "errors": {"stage": safe_error}}
        stage("vix", "failed", safe_error)

    try:
        treasury = treasury_refresh(today=date.fromisoformat(expected), database_path=database_path)
        result["treasury"] = treasury
        result["data_changed"] |= bool(treasury.get("updated"))
        stage("treasury", "complete_with_gaps" if treasury.get("errors") else "complete",
              f"Official par-yield observations through {treasury.get('latest_date') or 'unavailable'}")
    except Exception as error:
        safe_error = f"Treasury refresh failed ({type(error).__name__}); existing observations were preserved."
        result["treasury"] = {"updated": 0, "errors": {"stage": safe_error}}
        stage("treasury", "failed", safe_error)

    try:
        calendar = calendar_refresh(today=date.fromisoformat(expected), database_path=database_path)
        result["calendar"] = calendar
        result["data_changed"] |= bool(calendar.get("updated"))
        stage("calendar", "complete_with_gaps" if calendar.get("errors") else "complete",
              f"{len(calendar.get('updated', {}))} calendar source(s) reconciled")
    except Exception as error:
        safe_error = f"Calendar refresh failed ({type(error).__name__}); saved events were preserved."
        result["calendar"] = {"updated": {}, "errors": {"stage": safe_error}}
        stage("calendar", "failed", safe_error)

    cache = load_recent_catalysts(database_path=database_path) if database_path else load_recent_catalysts()
    result["recent_catalysts"] = {"status": cache.get("status", "unavailable"),
                                  "records": len(cache.get("records", [])),
                                  "refreshed_at": cache.get("refreshed_at")}
    stage("recent_catalysts", "cached_only",
          "Existing evidence retained. It refreshes during an explicitly requested web-enabled DeepSeek analysis.")
    stage("snapshot", "recalculated_after_rerun",
          "Deterministic indicators and the canonical snapshot rebuild from SQLite on the next render.")
    result["data_changed"] = bool(result["data_changed"])
    return result

"""Manual, incremental FRED adapter for Treasury, policy-rate, and credit data."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import os
from pathlib import Path
import ssl

import requests
from dotenv import dotenv_values

from dashboard.data_access import (
    list_macro_series_observations, macro_series_coverage,
    upsert_macro_series_observations,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROVIDER = "fred_initial_release"
ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"
LOOKBACK_DAYS = 730
OVERLAP_DAYS = 7
SERIES = {
    "DGS2": {"name": "2-year Treasury constant-maturity yield", "units": "percent", "kind": "yield", "source": "Board of Governors H.15", "url": "https://fred.stlouisfed.org/series/DGS2", "copyrighted": False},
    "DGS10": {"name": "10-year Treasury constant-maturity yield", "units": "percent", "kind": "yield", "source": "Board of Governors H.15", "url": "https://fred.stlouisfed.org/series/DGS10", "copyrighted": False},
    "DFF": {"name": "Federal funds effective rate", "units": "percent", "kind": "policy_rate", "source": "Board of Governors H.15", "url": "https://fred.stlouisfed.org/series/DFF", "copyrighted": False},
    "BAMLH0A0HYM2": {"name": "ICE BofA US High Yield Index option-adjusted spread", "units": "percent", "kind": "credit_oas", "source": "ICE Data Indices via FRED", "url": "https://fred.stlouisfed.org/series/BAMLH0A0HYM2", "copyrighted": True},
    "BAMLC0A0CM": {"name": "ICE BofA US Corporate Index option-adjusted spread", "units": "percent", "kind": "credit_oas", "source": "ICE Data Indices via FRED", "url": "https://fred.stlouisfed.org/series/BAMLC0A0CM", "copyrighted": True},
}


class FredMacroError(RuntimeError):
    """Safe-to-display FRED adapter error; never includes credentials or URLs."""


def _fred_credentials() -> tuple[str | None, str | None]:
    """Return key and safe source label; never expose the secret to UI/logs."""
    environment_key = os.environ.get("FRED_API_KEY", "").strip()
    if environment_key:
        return environment_key, "environment"

    # dotenv_values handles an empty inherited variable correctly and avoids
    # writing a secret into process-global state just to make a request.
    file_values = dotenv_values(PROJECT_ROOT / ".env")
    file_key = str(file_values.get("FRED_API_KEY") or "").strip()
    if file_key:
        return file_key, ".env"

    try:
        import streamlit as st
        secret_key = str(st.secrets.get("FRED_API_KEY", "") or "").strip()
        if secret_key:
            return secret_key, "Streamlit secrets"
    except Exception:
        # Streamlit secrets are optional and can be unavailable outside `streamlit run`.
        pass
    return None, None


def fred_macro_configured() -> bool:
    key, _source = _fred_credentials()
    return bool(key)


def _error_category(error: BaseException) -> tuple[str, str]:
    if isinstance(error, requests.Timeout):
        return "timeout", "Request timed out. Check connection/VPN stability and retry once."
    if isinstance(error, requests.exceptions.SSLError) or isinstance(error.__cause__, ssl.SSLError):
        return "tls_failure", "TLS certificate negotiation failed. Check system time, VPN HTTPS inspection, and Python certificate settings."
    if isinstance(error, requests.ConnectionError):
        nested = repr(error).lower()
        if any(word in nested for word in ("name or service not known", "getaddrinfo", "nodename nor servname", "name resolution")):
            return "dns_failure", "Python could not resolve api.stlouisfed.org. Check DNS, VPN, and proxy settings."
        return "network_or_proxy_failure", "Python could not connect to FRED. Check VPN/proxy settings and whether HTTPS to api.stlouisfed.org is allowed."
    if isinstance(error, requests.HTTPError):
        status = getattr(getattr(error, "response", None), "status_code", None)
        return f"http_{status or 'error'}", f"FRED returned HTTP {status or 'error'}. Cached observations were preserved."
    return type(error).__name__.lower(), "The FRED request failed. Cached observations were preserved."


def _fred_payload_error(series_id: str, payload: dict) -> dict:
    """Translate FRED's JSON error body into a safe, useful diagnostic."""
    message = str(payload.get("error_message", ""))
    normalized = message.lower()
    if "rate limit" in normalized or "too many requests" in normalized:
        return {"status": "rate_limit", "error_category": "fred_rate_limit",
                "message": "FRED reports a request limit. Wait before retrying."}
    if any(term in normalized for term in ("api key", "api_key", "not registered", "authentication")):
        return {"status": "authentication_error", "error_category": "fred_api_authentication",
                "message": "FRED's API response indicates a credential problem; verify the key at the official FRED API site."}
    if "series" in normalized and any(term in normalized for term in ("does not exist", "invalid", "not found")):
        return {"status": "invalid_series", "error_category": "invalid_series",
                "message": f"FRED rejected the {series_id} series identifier."}
    return {"status": "api_error", "error_category": f"fred_api_{payload.get('error_code', 'error')}",
            "message": "FRED returned an API error. Check request parameters and API access."}


def diagnose_fred_connection(series_id: str = "DGS10", *, timeout_seconds: int = 15,
                             today: date | None = None) -> dict:
    """Make one explicit, credential-safe connectivity check for one valid series."""
    normalized = str(series_id).strip().upper()
    if normalized not in SERIES:
        return {"status": "invalid_series", "series_id": normalized,
                "error_category": "invalid_series", "message": "Choose one of the configured FRED series."}
    api_key, source = _fred_credentials()
    if not api_key:
        return {"status": "missing_credentials", "series_id": normalized,
                "error_category": "missing_credentials",
                "message": "FRED_API_KEY was not found in a non-empty process environment variable, project .env, or Streamlit secrets."}
    through = today or datetime.now(timezone.utc).date()
    try:
        response = requests.get(
            ENDPOINT,
            params={"series_id": normalized, "api_key": api_key, "file_type": "json",
                    "observation_start": (through - timedelta(days=14)).isoformat(),
                    "observation_end": through.isoformat(), "sort_order": "asc", "output_type": 4},
            timeout=timeout_seconds,
        )
        if response.status_code == 429:
            return {"status": "rate_limit", "series_id": normalized, "credential_source": source,
                    "error_category": "http_429", "message": "FRED rate limit reached. Wait before retrying."}
        if response.status_code in (401, 403):
            return {"status": "authentication_error", "series_id": normalized, "credential_source": source,
                    "error_category": f"http_{response.status_code}",
                    "message": "FRED rejected the credential. Check its validity and API access; this is an HTTP authentication response."}
        try:
            payload = response.json()
        except ValueError:
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                category, message = _error_category(error)
                return {"status": "network_dns_proxy_failure", "series_id": normalized,
                        "credential_source": source, "error_category": category,
                        "message": message}
            return {"status": "invalid_response", "series_id": normalized, "credential_source": source,
                    "error_category": "invalid_json", "message": "FRED returned HTTP success but the response was not valid JSON."}
        if isinstance(payload, dict) and payload.get("error_code"):
            classified = _fred_payload_error(normalized, payload)
            return {**classified, "series_id": normalized, "credential_source": source}
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            category, message = _error_category(error)
            return {"status": "network_dns_proxy_failure", "series_id": normalized,
                    "credential_source": source, "error_category": category, "message": message}
        rows = parse_fred_macro_observations(normalized, payload, today=through)
        if not rows:
            return {"status": "valid_response_no_observations", "series_id": normalized,
                    "credential_source": source, "error_category": "no_observations",
                    "message": "FRED accepted the request but returned no valid observations for the diagnostic window."}
        latest = max(rows, key=lambda row: row["observation_date"])
        return {"status": "success", "series_id": normalized, "credential_source": source,
                "error_category": "none", "message": "FRED returned valid JSON observations.",
                "latest_observation": latest["observation_date"], "observation_count": len(rows)}
    except requests.RequestException as error:
        category, message = _error_category(error)
        status = "timeout" if category == "timeout" else "network_dns_proxy_failure"
        return {"status": status, "series_id": normalized, "credential_source": source,
                "error_category": category, "message": message}


def parse_fred_macro_observations(series_id: str, payload: dict, *, retrieved_at: str | None = None,
                                  today: date | None = None) -> list[dict]:
    series_id = str(series_id).upper()
    if series_id not in SERIES:
        raise FredMacroError("Unsupported FRED macro series.")
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        raise FredMacroError(f"FRED returned an unexpected response for {series_id}.")
    retrieved = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    through = today or datetime.now(timezone.utc).date()
    result = []
    for item in observations:
        try:
            observed = date.fromisoformat(item["date"])
            raw = str(item["value"]).strip()
            if observed > through or raw in {"", "."}:
                continue
            value = float(raw)
            if not math.isfinite(value):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        result.append({"series_id": series_id, "observation_date": observed.isoformat(),
                       "value": value, "provider": PROVIDER, "units": SERIES[series_id]["units"],
                       "initial_release_only": True, "retrieved_at": retrieved})
    return result


def refresh_fred_macro(*, database_path=None, timeout_seconds: int = 25,
                       today: date | None = None) -> dict:
    """Fetch an incremental two-year tail using FRED's initial-release output type."""
    api_key, credential_source = _fred_credentials()
    through = today or datetime.now(timezone.utc).date()
    result = {"provider": PROVIDER, "updated": {}, "errors": {}, "series": SERIES}
    if not api_key:
        result["errors"]["Setup"] = "FRED API key is not configured. Add FRED_API_KEY to the local .env file; cached observations were kept."
        result["diagnostics"] = {series_id: {"status": "missing_credentials", "error_category": "missing_credentials"} for series_id in SERIES}
        return result

    cached = (list_macro_series_observations(tuple(SERIES), PROVIDER, database_path)
              if database_path else list_macro_series_observations(tuple(SERIES), PROVIDER))
    # Test the most common Treasury series first. If credentials/network fail,
    # do not issue four redundant requests that are likely to fail identically.
    request_order = ("DGS10", *(series_id for series_id in SERIES if series_id != "DGS10"))
    diagnostics = {}
    for series_id in request_order:
        saved = cached.get(series_id, [])
        if saved:
            latest = max(date.fromisoformat(row["observation_date"]) for row in saved)
            start = max(latest - timedelta(days=OVERLAP_DAYS), through - timedelta(days=LOOKBACK_DAYS)).isoformat()
        else:
            start = (through - timedelta(days=LOOKBACK_DAYS)).isoformat()
        try:
            response = requests.get(
                ENDPOINT,
                params={"series_id": series_id, "api_key": api_key, "file_type": "json",
                        "observation_start": start, "observation_end": through.isoformat(),
                        "sort_order": "asc", "output_type": 4},
                timeout=timeout_seconds,
            )
            if response.status_code == 429:
                diagnostics[series_id] = {"status": "rate_limit", "error_category": "http_429", "message": "FRED rate limit reached."}
                result["errors"][series_id] = "FRED rate limit reached; cached observations were kept."
                break
            if response.status_code in (401, 403):
                diagnostics[series_id] = {"status": "authentication_error", "error_category": f"http_{response.status_code}", "message": "FRED rejected the credential."}
                result["errors"][series_id] = "FRED returned an authentication/authorization HTTP response; cached observations were kept."
                break
            try:
                payload = response.json()
            except ValueError as error:
                try:
                    response.raise_for_status()
                except requests.HTTPError as http_error:
                    category, message = _error_category(http_error)
                    diagnostics[series_id] = {"status": "network_dns_proxy_failure", "error_category": category, "message": message}
                    result["errors"][series_id] = message + " Cached observations were kept."
                    if series_id == "DGS10":
                        break
                    continue
                raise FredMacroError(f"FRED returned invalid JSON for {series_id}; cached observations were kept.") from error
            if isinstance(payload, dict) and payload.get("error_code"):
                diagnostics[series_id] = _fred_payload_error(series_id, payload)
                result["errors"][series_id] = diagnostics[series_id]["message"] + " Cached observations were kept."
                if series_id == "DGS10":
                    break
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                category, message = _error_category(error)
                diagnostics[series_id] = {"status": "network_dns_proxy_failure", "error_category": category, "message": message}
                result["errors"][series_id] = message + " Cached observations were kept."
                if series_id == "DGS10":
                    break
                continue
            rows = parse_fred_macro_observations(series_id, payload, today=through)
            if not rows:
                diagnostics[series_id] = {"status": "valid_response_no_observations", "error_category": "no_observations", "message": "Valid response contained no usable observations."}
                result["errors"][series_id] = "FRED returned no valid initial-release observations for this range; cached values were kept."
                continue
            count = upsert_macro_series_observations(rows, database_path) if database_path else upsert_macro_series_observations(rows)
            result["updated"][series_id] = count
            diagnostics[series_id] = {"status": "success", "error_category": "none", "credential_source": credential_source}
        except FredMacroError as error:
            result["errors"][series_id] = str(error)
            diagnostics[series_id] = {"status": "api_error", "error_category": type(error).__name__.lower(), "message": str(error)}
        except requests.RequestException as error:
            category, message = _error_category(error)
            diagnostics[series_id] = {"status": "timeout" if category == "timeout" else "network_dns_proxy_failure",
                                      "error_category": category, "message": message}
            result["errors"][series_id] = f"{message} Cached observations were kept."
            if series_id == "DGS10":
                break
    if diagnostics.get("DGS10", {}).get("status") in {"authentication_error", "rate_limit", "timeout", "network_dns_proxy_failure", "api_error", "invalid_series"}:
        for series_id in SERIES:
            if series_id not in diagnostics and series_id not in result["updated"]:
                diagnostics[series_id] = {"status": "not_tested", "error_category": "not_attempted_after_DGS10", "message": "Not requested because the initial DGS10 connection check failed."}
                result["errors"][series_id] = "Not requested: the initial DGS10 connection check failed."
    result["diagnostics"] = diagnostics
    result["coverage"] = (macro_series_coverage(tuple(SERIES), PROVIDER, database_path)
                           if database_path else macro_series_coverage(tuple(SERIES), PROVIDER))
    return result


def load_fred_macro(database_path=None) -> dict[str, list[dict]]:
    return (list_macro_series_observations(tuple(SERIES), PROVIDER, database_path)
            if database_path else list_macro_series_observations(tuple(SERIES), PROVIDER))


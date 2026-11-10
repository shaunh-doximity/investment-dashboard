"""Small local cache of dated market developments from explicit web research."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import re

from dashboard.data_access import get_app_preference, save_app_preference

CACHE_KEY = "recent_market_catalysts_v1"
WINDOW_DAYS = 7
MAX_RECORDS = 24
_COMPANY_TICKERS = {
    "alphabet": "GOOGL", "google": "GOOGL", "amazon": "AMZN", "intel": "INTC",
    "nvidia": "NVDA", "microsoft": "MSFT", "apple": "AAPL", "meta": "META",
    "tesla": "TSLA", "broadcom": "AVGO", "advanced micro devices": "AMD",
    "amd": "AMD", "oracle": "ORCL", "qualcomm": "QCOM", "berkshire": "BRK.B",
}
_SEGMENT_TERMS = {"technology": ("AI", "semiconductor", "cloud", "technology", "data center"),
                  "consumer": ("consumer", "retail", "discretionary", "staples"),
                  "energy": ("crude", "oil", "energy", "OPEC"),
                  "rates_credit": ("Treasury", "yield", "credit", "Federal Reserve", "Fed")}


def _date(value):
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text[:10],):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            pass
    return None


def catalysts_from_search_sources(sources, *, now=None) -> list[dict]:
    """Turn dated result metadata into traceable short records; no factual enrichment."""
    current = now or datetime.now(timezone.utc)
    today = current.date()
    output = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        published = _date(source.get("date"))
        if not isinstance(url, str) or not url.startswith("https://") or not published:
            continue
        age = (today - published).days
        if not 0 <= age <= WINDOW_DAYS:
            continue
        title = re.sub(r"\s+", " ", str(source.get("title") or "")).strip()[:240]
        excerpt = re.sub(r"\s+", " ", str(source.get("snippet") or source.get("summary") or "")).strip()[:700]
        if not title and not excerpt:
            continue
        search_text = f"{title} {excerpt}".lower()
        ticker = next((ticker for name, ticker in _COMPANY_TICKERS.items()
                       if re.search(rf"\b{re.escape(name)}\b", search_text)), None)
        category = ("Macro" if any(term in search_text for term in ("federal reserve", "fed meeting", "fomc", "cpi", "inflation", "treasury", "gdp", "payroll"))
                    else "Company earnings/development" if ticker else "Market development")
        hypotheses = [segment for segment, terms in _SEGMENT_TERMS.items()
                      if any(term.lower() in search_text for term in terms)]
        identity = hashlib.sha256(url.encode()).hexdigest()[:20]
        output.append({"event_id": identity, "ticker": ticker, "category": category,
            "event_date": None, "publication_date": published.isoformat(),
            "development": title + (f" — {excerpt}" if excerpt else ""), "numerical_values": [],
            "source_url": url, "source_title": title, "source_verification": "Retrieved source; not independently verified",
            "affected_segments_hypothesis": hypotheses, "cache_created_at": current.isoformat(timespec="seconds"),
            "cache_expires_at": (current + timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")})
    # A small cache, deduplicated by canonical URL identity and newest publication first.
    return sorted({item["event_id"]: item for item in output}.values(),
                  key=lambda item: (item["publication_date"], item["event_id"]), reverse=True)[:MAX_RECORDS]


def save_recent_catalysts(sources, *, now=None, database_path=None) -> dict:
    current = now or datetime.now(timezone.utc)
    records = catalysts_from_search_sources(sources, now=current)
    value = {"version": 1, "refreshed_at": current.isoformat(timespec="seconds"),
             "expires_at": (current + timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds"),
             "records": records}
    save_app_preference(CACHE_KEY, value, database_path) if database_path else save_app_preference(CACHE_KEY, value)
    return value


def load_recent_catalysts(*, now=None, database_path=None) -> dict:
    current = now or datetime.now(timezone.utc)
    cached = get_app_preference(CACHE_KEY, {}, database_path) if database_path else get_app_preference(CACHE_KEY, {})
    if not isinstance(cached, dict):
        return {"status": "unavailable", "records": []}
    records = [row for row in cached.get("records", []) if isinstance(row, dict)
               and (_date(row.get("publication_date")) is not None)
               and 0 <= (current.date() - _date(row["publication_date"])).days <= WINDOW_DAYS]
    return {**cached, "records": records,
            "status": "available" if records else "expired_or_empty",
            "age_days": ((current.date() - _date(cached.get("refreshed_at"))).days
                         if _date(cached.get("refreshed_at")) else None)}

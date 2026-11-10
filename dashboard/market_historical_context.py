"""Small descriptive market-history summaries for AI research context.

These are backward-looking sample statistics, not forecasts or trading tests.
Only fully completed calendar months at or before ``as_of`` are included.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import math


DEFAULT_SEASONALITY_SYMBOLS = (
    "SPY", "RSP", "QQQ", "DIA", "IWM", "XLK", "SMH", "MAGS", "XLC", "XLF",
    "XLE", "XLI", "XLY", "XLP", "XLV", "XLU", "XLB", "XLRE", "TLT", "GLD",
    "USO", "HYG", "LQD", "IEF",
)


def completed_month_seasonality(histories: dict, *, as_of: str | date | None,
                                symbols: tuple[str, ...] = DEFAULT_SEASONALITY_SYMBOLS) -> dict:
    """Summarize intramonth first-close-to-last-close returns by calendar month.

    The current (possibly partial) month is excluded. Each symbol uses its own
    valid adjusted-close observations; outputs include sample years and are
    descriptive only, with no out-of-sample or predictive claim.
    """
    if not as_of:
        return {"status": "unavailable", "reason": "No market observation date.", "method": "completed_month_intramonth_return_v1"}
    cutoff = date.fromisoformat(str(as_of)[:10]) if not isinstance(as_of, date) else as_of
    grouped: dict[str, dict[tuple[int, int], list[tuple[date, float]]]] = defaultdict(lambda: defaultdict(list))
    for symbol in symbols:
        for row in histories.get(symbol, []) or []:
            try:
                day = date.fromisoformat(str(row.get("trading_date"))[:10])
                close = float(row.get("close"))
            except (TypeError, ValueError):
                continue
            # Exclude the as-of month even if it has observations: it is not complete.
            if day >= cutoff or (day.year, day.month) >= (cutoff.year, cutoff.month):
                continue
            if math.isfinite(close) and close > 0:
                grouped[symbol][(day.year, day.month)].append((day, close))
    current_month = cutoff.month
    summaries = {}
    for symbol in symbols:
        observations = []
        for (year, month), rows in sorted(grouped.get(symbol, {}).items()):
            if month != current_month:
                continue
            rows.sort(key=lambda pair: pair[0])
            if len(rows) >= 2:
                observations.append({"year": year, "start_date": rows[0][0].isoformat(),
                                     "end_date": rows[-1][0].isoformat(),
                                     "return_pct": (rows[-1][1] / rows[0][1] - 1) * 100})
        values = [item["return_pct"] for item in observations]
        if values:
            ordered = sorted(values)
            middle = len(ordered) // 2
            median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
            summaries[symbol] = {"sample_count": len(values), "sample_years": [item["year"] for item in observations],
                "mean_return_pct": sum(values) / len(values), "median_return_pct": median,
                "positive_months": sum(value > 0 for value in values),
                "observations": observations[-12:]}
        else:
            summaries[symbol] = {"sample_count": 0, "sample_years": [], "observations": []}
    return {"status": "available" if any(item["sample_count"] for item in summaries.values()) else "insufficient_history",
            "as_of": cutoff.isoformat(), "calendar_month": current_month,
            "window": "fully completed prior calendar months only", "return_definition": "last adjusted close / first adjusted close within month − 1",
            "method": "completed_month_intramonth_return_v1", "predictive_claim": False,
            "interpretation_limit": "Retrospective sample description, not a seasonal forecast or evidence of causation.",
            "symbols": summaries}


def completed_month_relative_history(histories: dict, *, as_of: str | date | None,
                                     pairs: tuple[tuple[str, str], ...] = (
                                         ("RSP", "SPY"), ("QQQ", "SPY"), ("SMH", "SPY"),
                                         ("XLY", "XLP"), ("HYG", "LQD"), ("TLT", "GLD"),
                                     )) -> dict:
    """Compare month-window relative-price changes using only matched dates.

    Each prior same-calendar-month sample uses the first/last dates shared by
    both series. Results are descriptive relative returns, not flow or causal
    evidence. The current (potentially partial) month is excluded.
    """
    if not as_of:
        return {"status": "unavailable", "reason": "No market observation date."}
    cutoff = date.fromisoformat(str(as_of)[:10]) if not isinstance(as_of, date) else as_of
    output = {}
    for first, second in pairs:
        left, right = {}, {}
        for symbol, target in ((first, left), (second, right)):
            for row in histories.get(symbol, []) or []:
                try:
                    day = date.fromisoformat(str(row.get("trading_date"))[:10])
                    close = float(row.get("close"))
                except (TypeError, ValueError):
                    continue
                if day < cutoff and (day.year, day.month) < (cutoff.year, cutoff.month) and math.isfinite(close) and close > 0:
                    target[day] = close
        common = sorted(set(left).intersection(right))
        by_year = defaultdict(list)
        for day in common:
            if day.month == cutoff.month:
                by_year[day.year].append(day)
        observations = []
        for year, days in sorted(by_year.items()):
            if len(days) < 2:
                continue
            start, end = days[0], days[-1]
            start_ratio, end_ratio = left[start] / right[start], left[end] / right[end]
            observations.append({"year": year, "start_date": start.isoformat(), "end_date": end.isoformat(),
                                 "relative_return_pct": (end_ratio / start_ratio - 1) * 100,
                                 "matched_observations": len(days)})
        values = [row["relative_return_pct"] for row in observations]
        output[f"{first}/{second}"] = {"sample_count": len(values), "sample_years": [row["year"] for row in observations],
            "positive_periods": sum(value > 0 for value in values), "observations": observations[-12:],
            "method": "matched-date monthly relative-price change"}
    return {"status": "available" if any(row["sample_count"] for row in output.values()) else "insufficient_history",
            "as_of": cutoff.isoformat(), "calendar_month": cutoff.month,
            "window": "fully completed prior same-calendar months only", "pairs": output,
            "interpretation_limit": "Descriptive relative-price history; not evidence of investor flows, causation, or predictive edge."}


def summarize_vix_stress_history(vix_rows: list[dict], *, as_of: str | date | None,
                                 stress_threshold: float = 30.0) -> dict:
    """Count dated historical VIX closes above a transparent descriptive threshold."""
    if not as_of:
        return {"status": "unavailable", "reason": "No VIX observation date."}
    cutoff = str(as_of)[:10]
    values = []
    for row in vix_rows or []:
        try:
            day = str(row.get("observation_date", ""))[:10]
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if day and day <= cutoff and math.isfinite(close):
            values.append({"date": day, "close": close})
    values.sort(key=lambda item: item["date"])
    elevated = [item for item in values if item["close"] >= float(stress_threshold)]
    return {"status": "available" if values else "insufficient_history", "as_of": values[-1]["date"] if values else None,
            "threshold_points": float(stress_threshold), "observations": len(values),
            "sessions_at_or_above_threshold": len(elevated),
            "first_five_dated_examples": elevated[:5],
            "interpretation_limit": "Session counts use a descriptive VIX level threshold; this does not estimate future returns or reversal odds."}

"""Deterministic Treasury curve summaries and non-causal event studies.

Yield inputs are percentage-point values (4.56 means 4.56%). A yield or curve
change is converted to basis points exactly once by multiplying percentage-point
difference by 100.
"""
from __future__ import annotations

from datetime import date
from math import isfinite


WINDOWS = (5, 20, 60, 120)


def align_treasury_observations(rows):
    """Keep valid dated 2Y/10Y observations together; never align unlike dates."""
    aligned = {}
    for row in rows or []:
        try:
            day = date.fromisoformat(str(row["observation_date"])[:10]).isoformat()
            two, ten = float(row["two_year"]), float(row["ten_year"])
        except (KeyError, TypeError, ValueError):
            continue
        if isfinite(two) and isfinite(ten):
            aligned[day] = {**row, "observation_date": day, "two_year": two, "ten_year": ten}
    return [aligned[day] for day in sorted(aligned)]


def treasury_window_summary(rows, sessions: int = 20) -> dict:
    if sessions not in WINDOWS:
        raise ValueError(f"sessions must be one of {WINDOWS}")
    aligned = align_treasury_observations(rows)
    if not aligned:
        return {"status": "unavailable", "sessions": sessions}
    latest = aligned[-1]
    past = aligned[-sessions-1] if len(aligned) >= sessions + 1 else None
    sample = aligned[-min(sessions + 1, len(aligned)):]
    def bp_change(key):
        return (latest[key] - past[key]) * 100 if past else None
    spread = (latest["ten_year"] - latest["two_year"]) * 100
    past_spread = (past["ten_year"] - past["two_year"]) * 100 if past else None
    change2, change10 = bp_change("two_year"), bp_change("ten_year")
    spread_change = spread - past_spread if past_spread is not None else None
    def trend(value):
        return "rising" if value is not None and value > 5 else "falling" if value is not None and value < -5 else "consolidating" if value is not None else "insufficient history"
    return {
        "status": "available", "sessions": sessions, "as_of": latest["observation_date"],
        "observations": len(sample), "required_observations": sessions + 1,
        "two_year": latest["two_year"], "ten_year": latest["ten_year"],
        "two_year_change_bp": change2, "ten_year_change_bp": change10,
        "spread_bp": spread, "spread_change_bp": spread_change,
        "two_year_peak": max(row["two_year"] for row in sample),
        "two_year_trough": min(row["two_year"] for row in sample),
        "ten_year_peak": max(row["ten_year"] for row in sample),
        "ten_year_trough": min(row["ten_year"] for row in sample),
        "two_year_trend": trend(change2), "ten_year_trend": trend(change10),
        "spread_trend": trend(spread_change),
        "basis": "Percentage-point yield levels; differences ×100 = basis points.",
        "matched_start_date": past["observation_date"] if past else None,
    }


def treasury_event_study(rows, event_date: str) -> dict:
    """Compare prior observed session with first later session; associations only.

    The event date is excluded from both sides because daily observations do not
    establish whether publication preceded or followed the event timestamp.
    """
    try:
        event_day = date.fromisoformat(str(event_date)[:10]).isoformat()
    except ValueError:
        raise ValueError("Enter an event date as YYYY-MM-DD.")
    aligned = align_treasury_observations(rows)
    before_idx = max((i for i, row in enumerate(aligned) if row["observation_date"] < event_day), default=None)
    after_idx = next((i for i, row in enumerate(aligned) if row["observation_date"] > event_day), None)
    if before_idx is None or after_idx is None:
        return {"status": "insufficient_event_window", "event_date": event_day}
    before, after = aligned[before_idx], aligned[after_idx]
    def changes(base_idx, offset):
        idx = base_idx + offset
        if idx >= len(aligned):
            return None
        return {"two_year_bp": (aligned[idx]["two_year"] - after["two_year"]) * 100,
                "ten_year_bp": (aligned[idx]["ten_year"] - after["ten_year"]) * 100,
                "observation_date": aligned[idx]["observation_date"]}
    immediate = {"two_year_bp": (after["two_year"] - before["two_year"]) * 100,
                 "ten_year_bp": (after["ten_year"] - before["ten_year"]) * 100}
    result = {"status": "available", "event_date": event_day,
              "before_date": before["observation_date"], "after_date": after["observation_date"],
              "before": {"two_year": before["two_year"], "ten_year": before["ten_year"]},
              "after": {"two_year": after["two_year"], "ten_year": after["ten_year"]},
              "immediate_change": immediate, "forward_5": changes(after_idx, 5), "forward_10": changes(after_idx, 10)}
    def persistence(key, forward):
        if not forward or immediate[key] == 0:
            return "incomplete" if not forward else "no initial move"
        net = (forward["two_year_bp"] if key == "two_year_bp" else forward["ten_year_bp"]) + immediate[key]
        return "persisted" if net * immediate[key] > 0 else "reversed" if net * immediate[key] < 0 else "fully retraced"
    result["persistence"] = {"two_year_5": persistence("two_year_bp", result["forward_5"]),
                              "ten_year_5": persistence("ten_year_bp", result["forward_5"]),
                              "two_year_10": persistence("two_year_bp", result["forward_10"]),
                              "ten_year_10": persistence("ten_year_bp", result["forward_10"])}
    return result

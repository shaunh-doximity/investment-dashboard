"""Deterministic parser for compact, user-entered earnings date lists."""
from __future__ import annotations

from datetime import date
import re

from dashboard.data_access import normalize_market_ticker


_DATE = r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{4})?)"
_TIMING = (
    r"BMO|AMC|TBD|pre[\s-]?market(?:\s+open)?|post[\s-]?market(?:\s+close)?|"
    r"before\s+(?:market(?:\s+open)?|open)|after\s+(?:market(?:\s+close)?|close)"
)
_PAIR = re.compile(
    rf"(?<![A-Za-z0-9.-])(?P<ticker>[A-Za-z0-9](?:[A-Za-z0-9.-]{{0,8}}[A-Za-z0-9])?)"
    rf"(?:\s+|,\s*)(?P<date>{_DATE})(?:\s+(?P<timing>{_TIMING}))?",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[\s,;]+")


def _parse_date(token: str, *, as_of: date, calendar_year: int | None) -> tuple[date, bool, str]:
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", token):
        year, month, day = map(int, token.split("-"))
        try:
            parsed = date(year, month, day)
        except ValueError as error:
            raise ValueError(f"Invalid calendar date: {error}") from error
        return parsed, False, "Year provided"
    pieces = [int(piece) for piece in token.split("/")]
    month, day = pieces[:2]
    if len(pieces) == 3:
        try:
            parsed = date(pieces[2], month, day)
        except ValueError as error:
            raise ValueError(f"Invalid calendar date: {error}") from error
        return parsed, False, "Year provided"
    if calendar_year is not None:
        try:
            parsed = date(calendar_year, month, day)
        except ValueError as error:
            raise ValueError(f"Invalid calendar date: {error}") from error
        return parsed, True, f"Configured calendar year {calendar_year}"

    # Explicit rule: an MM/DD date means its next occurrence on or after as_of.
    # Show this inference in the preview so the user can review or edit the year.
    for year in range(as_of.year, as_of.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= as_of:
            return candidate, True, "Year inferred: next upcoming date"
    raise ValueError("Invalid calendar date: this month/day does not occur in the next eight calendar years.")


def _normalize_timing(value: str | None) -> str:
    if not value:
        return "TBD"
    normalized = re.sub(r"[\s-]+", " ", value.strip().lower())
    if normalized in {"bmo", "pre market", "pre market open", "before open", "before market", "before market open"}:
        return "BMO"
    if normalized in {"amc", "post market", "post market close", "after close", "after market", "after market close"}:
        return "AMC"
    return "TBD"


def parse_quick_earnings(
    text: str,
    *,
    as_of: date | None = None,
    calendar_year: int | None = None,
) -> dict:
    """Parse ticker/date pairs and return valid events plus every malformed segment.

    Dates without a year resolve to the next occurrence relative to ``as_of``.
    Supplying ``calendar_year`` instead pins those dates to that year exactly.
    """
    original = str(text or "")
    reference_date = as_of or date.today()
    if calendar_year is not None and not 1900 <= int(calendar_year) <= 9999:
        raise ValueError("Calendar year must be a four-digit year.")
    matches = list(_PAIR.finditer(original))
    events: list[dict] = []
    errors: list[dict] = []
    cursor = 0
    for match in matches:
        gap = original[cursor:match.start()]
        if gap and not _SEPARATORS.fullmatch(gap):
            errors.append({"text": gap.strip(), "error": "Could not parse this text as a ticker/date pair."})
        segment = match.group(0).strip(" ,;\n\t")
        try:
            symbol = normalize_market_ticker(match.group("ticker"))
            parsed_date, inferred, resolution = _parse_date(
                match.group("date"), as_of=reference_date, calendar_year=calendar_year,
            )
            session = _normalize_timing(match.group("timing"))
            events.append({
                "ticker": symbol,
                "date": parsed_date.isoformat(),
                "session": session,
                "year_inferred": inferred,
                "year_resolution": resolution,
                "input_segment": segment,
            })
        except (ValueError, TypeError) as error:
            errors.append({"text": segment, "error": str(error) or "Invalid ticker or date."})
        cursor = match.end()
    tail = original[cursor:]
    if tail and not _SEPARATORS.fullmatch(tail):
        errors.append({"text": tail.strip(), "error": "Could not parse this text as a ticker/date pair."})
    if not matches and original.strip():
        errors.append({"text": original.strip(), "error": "No ticker/date pairs were found."})
    elif not matches:
        errors.append({"text": "", "error": "Enter at least one ticker/date pair."})
    return {"events": events, "errors": errors, "original_input": original, "as_of": reference_date.isoformat()}

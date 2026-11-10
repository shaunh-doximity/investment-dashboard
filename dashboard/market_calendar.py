"""NYSE session dates and closes, including holidays and early closes.

Prefer the XNYS schedule from ``exchange-calendars``. A standard-library
fallback covers current US market holidays and regular early-close rules so a
missing optional runtime install never silently falls back to weekday math.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    day = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter, used only to identify Good Friday market closures."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nyse_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)), _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3), _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0), _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    # New Year's Day can be observed on December 31 of the prior calendar year.
    holidays.add(_observed(date(year + 1, 1, 1)))
    if year in {2001}:
        holidays.update(date(2001, 9, day) for day in range(11, 15))
    if year == 2004:
        holidays.add(date(2004, 6, 11))
    if year == 2012:
        holidays.update({date(2012, 10, 29), date(2012, 10, 30)})
    return holidays


def _fallback_session_close(day: date) -> datetime | None:
    if day.weekday() >= 5 or day in _nyse_holidays(day.year):
        return None
    early_close = False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        early_close = True
    if day.month == 12 and day.day == 24:
        early_close = True
    july_4 = date(day.year, 7, 4)
    candidate = date(day.year, 7, 3)
    if july_4.weekday() == 5:
        candidate = date(day.year, 7, 2)
    if day == candidate and day not in _nyse_holidays(day.year):
        early_close = True
    return datetime.combine(day, time(13 if early_close else 16, 0), NEW_YORK)


def _session_closes(start: date, end: date):
    """Yield (session date, close) from exchange_calendars or safe fallback."""
    try:
        import exchange_calendars as xcals
        calendar = xcals.get_calendar("XNYS")
        sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
        for session in sessions:
            close = calendar.session_close(session)
            if hasattr(close, "to_pydatetime"):
                close = close.to_pydatetime()
            if close.tzinfo is None:
                close = close.replace(tzinfo=timezone.utc)
            session_date = session.date() if hasattr(session, "date") else date.fromisoformat(str(session)[:10])
            yield session_date, close.astimezone(NEW_YORK)
        return
    except ImportError:
        pass
    day = start
    while day <= end:
        close = _fallback_session_close(day)
        if close:
            yield day, close
        day += timedelta(days=1)


def latest_completed_us_equity_session(now: datetime | None = None) -> str:
    """Latest XNYS session whose scheduled close has passed in America/New_York."""
    current = now or datetime.now(NEW_YORK)
    current = current.astimezone(NEW_YORK) if current.tzinfo else current.replace(tzinfo=NEW_YORK)
    start, end = current.date() - timedelta(days=20), current.date()
    completed = [session for session, close in _session_closes(start, end) if close <= current]
    if not completed:
        # The range spans three weekends, so reaching this means a calendar
        # implementation failed rather than an ordinary holiday/weekend case.
        raise RuntimeError("Could not determine a completed XNYS trading session.")
    return completed[-1].isoformat()


def expected_completed_session(now: datetime | None = None) -> date:
    return date.fromisoformat(latest_completed_us_equity_session(now))


def expected_sessions_between(start_exclusive: date, end_exclusive: date) -> list[date]:
    """XNYS sessions missing strictly between two cached observation dates."""
    if end_exclusive <= start_exclusive + timedelta(days=1):
        return []
    return [session for session, _ in _session_closes(
        start_exclusive + timedelta(days=1), end_exclusive - timedelta(days=1)
    )]

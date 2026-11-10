"""Official-source market catalyst adapters and local event-calendar refreshes.

All network activity is isolated behind refresh_official_calendars(), called only by
an explicit UI action. Stored event times are UTC; source-local dates and zones are
retained for all-day events and audit display.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from dashboard.data_access import (
    mark_missing_market_source_events, save_market_event_source_status, upsert_market_events,
)

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_ICS_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
FED_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_SPEECHES_URL = "https://www.federalreserve.gov/newsevents/{year}-speeches.htm"
FEC_ELECTION_URL = "https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/"
ET = "America/New_York"


def _unfold_ics(text: str) -> list[str]:
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _ics_unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def localize_source_time(local: datetime, timezone_name: str) -> datetime:
    """Attach an IANA timezone, rejecting nonexistent DST wall-clock times.

    An ambiguous fall-back time uses fold=0 (the first occurrence), matching the
    usual calendar convention. The UI labels the source timezone for review.
    """
    zone = ZoneInfo(timezone_name)
    first = local.replace(tzinfo=zone, fold=0)
    roundtrip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if roundtrip != local:
        raise ValueError("The local event time does not exist because clocks move forward for daylight saving time.")
    return first


def _parse_ics_datetime(value: str, params: dict[str, str], default_tz: str):
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(day, time.min, tzinfo=timezone.utc), True, default_tz
    if value.endswith("Z"):
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        # Feeds may serialize U.S. release times as Zulu; retain the source
        # calendar's advertised Eastern timezone while preserving the instant.
        return parsed, False, default_tz
    parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    source_tz = params.get("TZID", default_tz)
    return localize_source_time(parsed, source_tz), False, source_tz


def parse_ics_events(text: str, *, provider: str, source_url: str, category: str,
                     source_timezone: str = ET, status_default: str = "Tentative",
                     title_filter=None) -> list[dict]:
    """Parse VEVENTs without external iCalendar dependencies; preserve TZID/DST."""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ValueError(f"{provider} returned a response that is not an iCalendar feed.")
    output, current = [], None
    for line in _unfold_ics(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if not current:
                continue
            summary = _ics_unescape(current.get("SUMMARY", ({}, ""))[1])
            if not summary or (title_filter and not title_filter(summary)):
                continue
            try:
                params, value = current["DTSTART"]
                start, all_day, tz_name = _parse_ics_datetime(value, params, source_timezone)
            except (KeyError, ValueError, ZoneInfoNotFoundError):
                continue
            status = current.get("STATUS", ({}, ""))[1].upper()
            mapped = {
                "CANCELLED": "Cancelled",
                "CONFIRMED": "Confirmed",
                "TENTATIVE": "Tentative",
                "ESTIMATED": "Estimated",
            }.get(status, status_default)
            uid = _ics_unescape(current.get("UID", ({}, ""))[1])
            url = _ics_unescape(current.get("URL", ({}, ""))[1]) or source_url
            description = _ics_unescape(current.get("DESCRIPTION", ({}, ""))[1])
            output.append({
                "name": summary, "category": category,
                "start_utc": start.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "source_timezone": tz_name, "source_url": url, "provider": provider, "source_uid": uid,
                "status": mapped, "related_symbols": [], "notes": description,
                "importance_tier": "High" if category in ("Fed", "Macro") else "Standard", "all_day": all_day,
            })
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        parts = left.split(";")
        key = parts[0].upper()
        params = {}
        for parameter in parts[1:]:
            if "=" in parameter:
                name, val = parameter.split("=", 1)
                params[name.upper()] = val.strip('"')
        current[key] = (params, value)
    return output


_BLS_CATALYST = re.compile(r"consumer price index|producer price index|employment situation|job openings and labor turnover|employment cost index|consumer expenditure|import and export price", re.I)
_BEA_CATALYST = re.compile(r"gross domestic product|personal income and outlays", re.I)


def parse_bls_calendar(text: str) -> list[dict]:
    events = parse_ics_events(text, provider="bls_ical", source_url="https://www.bls.gov/schedule/", category="Macro",
                              source_timezone=ET, status_default="Confirmed", title_filter=_BLS_CATALYST.search)
    for event in events:
        year = event["start_utc"][:4]
        event["source_url"] = f"https://www.bls.gov/schedule/{year}/home.htm"
        if not event["notes"]:
            event["notes"] = "Date published by the official BLS release schedule; check the source for any later revision."
    return events


def parse_bea_calendar(text: str) -> list[dict]:
    # BEA's own calendar deliberately publishes a subscription ICS and JSON format.
    events = parse_ics_events(text, provider="bea_ical", source_url="https://www.bea.gov/news/schedule/", category="Macro",
                              source_timezone=ET, status_default="Confirmed", title_filter=_BEA_CATALYST.search)
    for event in events:
        if event["name"].lower().startswith("personal income and outlays"):
            event["name"] = "PCE / Core PCE · " + event["name"]
    return events


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"br", "p", "h1", "h2", "h3", "h4", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in {"p", "h1", "h2", "h3", "h4", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def _html_text(html: str) -> str:
    parser = _PlainText()
    parser.feed(html)
    return "\n".join(part.strip() for part in parser.parts if part.strip())


def parse_fed_fomc_calendar(html: str, *, today: date | None = None) -> list[dict]:
    """Read public meeting dates from the Fed's official calendar page.

    The Fed says future meeting dates remain tentative until the preceding
    meeting. Once that meeting has concluded, its next scheduled date is treated
    as confirmed. Decisions and press conferences are at 2:00 p.m. Eastern on
    meeting day two.
    """
    text = _html_text(html)
    sections = list(re.finditer(r"(?m)(20\d{2})\s+FOMC Meetings", text))
    now = today or datetime.now(timezone.utc).date()
    meetings = []
    month_names = {name.lower(): index for index, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
    for i, match in enumerate(sections):
        year = int(match.group(1))
        end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        lines = [line.strip() for line in text[match.end():end].splitlines() if line.strip()]
        month = None
        j = 0
        while j < len(lines):
            candidate = lines[j].lower()
            if candidate in month_names:
                month = month_names[candidate]
            range_match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})\*?", lines[j])
            if month and range_match:
                decision_day = int(range_match.group(2))
                try:
                    local = localize_source_time(datetime(year, month, decision_day, 14, 0), ET)
                except ValueError:
                    j += 1
                    continue
                meeting_start = date(year, month, int(range_match.group(1)))
                meetings.append((meeting_start, local.date(), year, month, decision_day, local))
            j += 1
    meetings.sort(key=lambda meeting: (meeting[0], meeting[1]))
    events = []
    for index, (meeting_start, meeting_end, year, month, decision_day, local) in enumerate(meetings):
        if meeting_end < now:
            continue
        previous_meeting_is_complete = index > 0 and meetings[index - 1][1] < now
        status = "Confirmed" if previous_meeting_is_complete else "Tentative"
        status_note = ("The preceding scheduled FOMC meeting has concluded, confirming this next scheduled date."
                       if status == "Confirmed" else "The Fed marks future meeting dates tentative until confirmation at the preceding meeting.")
        events.append({
                        "name": "FOMC decision & press conference", "category": "Fed",
                        "start_utc": local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "source_timezone": ET, "source_url": FED_FOMC_URL, "provider": "federal_reserve_fomc",
                        "source_uid": f"fomc-{year}-{month:02d}-{decision_day:02d}", "status": status,
                        "related_symbols": [], "notes": f"Scheduled for the second day of the meeting. {status_note}",
                        "importance_tier": "High", "all_day": False,
                    })
    return events


def parse_fed_speeches(html: str, *, year: int, today: date | None = None) -> list[dict]:
    """Extract only future, materially policy/economy-related speeches from the Fed list."""
    text = _html_text(html)
    matches = list(re.finditer(r"(?m)(\d{1,2}/\d{1,2}/20\d{2})", text))
    now = today or datetime.now(timezone.utc).date()
    material = re.compile(r"chair|monetary policy|economic outlook|inflation|labor market|interest rate|financial conditions|financial stability|jackson hole|policy communication", re.I)
    events = []
    for index, match in enumerate(matches):
        try:
            day = datetime.strptime(match.group(1), "%m/%d/%Y").date()
        except ValueError:
            continue
        if day < now or day.year != year:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = [line.strip() for line in text[match.end():end].splitlines() if line.strip()]
        block = [line for line in block if line.lower() not in ("watch live", "video", "speech", "testimony")]
        title = next((line for line in block if len(line) > 8), "")
        context = " ".join(block[:8])
        if not title or not material.search(context):
            continue
        local = localize_source_time(datetime.combine(day, time.min), ET)
        digest = hashlib.sha256(f"{day.isoformat()}|{title}".encode()).hexdigest()[:16]
        events.append({"name": "Fed speech · " + title, "category": "Fed",
                       "start_utc": local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                       "source_timezone": ET, "source_url": FED_SPEECHES_URL.format(year=year),
                       "provider": "federal_reserve_speeches", "source_uid": f"fed-speech-{digest}",
                       "status": "Confirmed", "related_symbols": [],
                       "notes": "The Federal Reserve lists this future speech; no start time is listed in the source.",
                       "importance_tier": "High", "all_day": True})
    return events


def parse_fec_election_dates(html: str, *, today: date | None = None) -> list[dict]:
    text = _html_text(html)
    match = re.search(r"(?:Tuesday|Wednesday|Thursday|Friday|Monday|Saturday|Sunday),\s+([A-Z][a-z]+\s+\d{1,2},\s+20\d{2}),\s+is the next regularly scheduled federal General Election date", text, re.I)
    if not match:
        return []
    day = datetime.strptime(match.group(1), "%B %d, %Y").date()
    if day < (today or datetime.now(timezone.utc).date()):
        return []
    return [{"name": "U.S. federal general election", "category": "Political/Other",
             "start_utc": f"{day.isoformat()}T00:00:00Z", "source_timezone": ET,
             "source_url": FEC_ELECTION_URL, "provider": "fec_election_dates", "source_uid": f"fec-general-{day.year}",
             "status": "Tentative", "related_symbols": [],
             "notes": "FEC identifies this as the next regularly scheduled federal general election; future dates are subject to change.",
             "importance_tier": "High", "all_day": True}]


def filter_upcoming_events(events: list[dict], *, category: str = "All", horizon_days: int = 30,
                           now_utc: datetime | None = None) -> list[dict]:
    """Filter canceled/past/out-of-window records, then sort chronologically."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = now + timedelta(days=horizon_days)
    selected = []
    for event in events:
        if event.get("status") == "Cancelled" or (category != "All" and event.get("category") != category):
            continue
        start = datetime.fromisoformat(event["start_utc"].replace("Z", "+00:00"))
        if event.get("all_day"):
            source_zone = ZoneInfo(event.get("source_timezone") or ET)
            today_in_source = now.astimezone(source_zone).date()
            end_in_source = end.astimezone(source_zone).date()
            if start.date() < today_in_source or start.date() > end_in_source:
                continue
        elif start < now or start > end:
            continue
        selected.append(event)
    return sorted(selected, key=lambda event: (event["start_utc"], event["name"].lower()))


def _fetch(url: str, timeout: int) -> str:
    response = requests.get(url, headers={"User-Agent": "InvestmentCommandCenter/1.0 (local calendar refresh)"}, timeout=timeout)
    if response.status_code == 429:
        raise RuntimeError("Source rate limited the calendar request; try again later.")
    response.raise_for_status()
    return response.text


def refresh_official_calendars(*, timeout_seconds: int = 20, database_path=None, today: date | None = None) -> dict:
    """Explicitly refresh public calendars; per-provider failures retain saved events."""
    from dashboard.data_access import list_earnings_watchlist
    sources = (
        ("bls_ical", BLS_ICS_URL, parse_bls_calendar),
        ("bea_ical", BEA_ICS_URL, parse_bea_calendar),
    )
    result = {"updated": {}, "errors": {}}

    def persist(provider: str, records: list[dict]) -> dict:
        stats = upsert_market_events(records, database_path) if database_path else upsert_market_events(records)
        mark_missing_market_source_events(provider, [item.get("source_uid") for item in records], database_path=database_path) if database_path else mark_missing_market_source_events(provider, [item.get("source_uid") for item in records])
        return stats

    for provider, url, parser in sources:
        try:
            records = parser(_fetch(url, timeout_seconds))
            stats = persist(provider, records)
            result["updated"][provider] = stats
            save_market_event_source_status(provider, "ok", f"Loaded {len(records)} official calendar entries.", database_path=database_path) if database_path else save_market_event_source_status(provider, "ok", f"Loaded {len(records)} official calendar entries.")
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else "Could not refresh this official calendar; cached events were retained."
            result["errors"][provider] = message
            save_market_event_source_status(provider, "error", message, database_path=database_path) if database_path else save_market_event_source_status(provider, "error", message)
    try:
        fed_records = parse_fed_fomc_calendar(_fetch(FED_FOMC_URL, timeout_seconds), today=today)
        stats = persist("federal_reserve_fomc", fed_records)
        result["updated"]["federal_reserve_fomc"] = stats
        save_market_event_source_status("federal_reserve_fomc", "ok", f"Loaded {len(fed_records)} tentative FOMC dates.", database_path=database_path) if database_path else save_market_event_source_status("federal_reserve_fomc", "ok", f"Loaded {len(fed_records)} tentative FOMC dates.")
    except Exception:
        result["errors"]["federal_reserve_fomc"] = "Could not refresh the Fed calendar; cached events were retained."
        save_market_event_source_status("federal_reserve_fomc", "error", result["errors"]["federal_reserve_fomc"], database_path=database_path) if database_path else save_market_event_source_status("federal_reserve_fomc", "error", result["errors"]["federal_reserve_fomc"])
    speeches_url = FED_SPEECHES_URL.format(year=(today or datetime.now(timezone.utc).date()).year)
    try:
        speech_records = parse_fed_speeches(_fetch(speeches_url, timeout_seconds),
                                            year=(today or datetime.now(timezone.utc).date()).year, today=today)
        stats = persist("federal_reserve_speeches", speech_records)
        result["updated"]["federal_reserve_speeches"] = stats
        message = f"Loaded {len(speech_records)} future policy/economy-related speech dates." if speech_records else "No qualifying future speech dates are currently listed."
        save_market_event_source_status("federal_reserve_speeches", "ok", message, database_path=database_path) if database_path else save_market_event_source_status("federal_reserve_speeches", "ok", message)
    except Exception:
        result["errors"]["federal_reserve_speeches"] = "Could not refresh Fed speech dates; cached events were retained."
        save_market_event_source_status("federal_reserve_speeches", "error", result["errors"]["federal_reserve_speeches"], database_path=database_path) if database_path else save_market_event_source_status("federal_reserve_speeches", "error", result["errors"]["federal_reserve_speeches"])
    try:
        fec_records = parse_fec_election_dates(_fetch(FEC_ELECTION_URL, timeout_seconds), today=today)
        stats = persist("fec_election_dates", fec_records)
        result["updated"]["fec_election_dates"] = stats
        save_market_event_source_status("fec_election_dates", "ok", f"Loaded {len(fec_records)} election dates.", database_path=database_path) if database_path else save_market_event_source_status("fec_election_dates", "ok", f"Loaded {len(fec_records)} election dates.")
    except Exception:
        result["errors"]["fec_election_dates"] = "Could not refresh the FEC calendar; cached events were retained."
        save_market_event_source_status("fec_election_dates", "error", result["errors"]["fec_election_dates"], database_path=database_path) if database_path else save_market_event_source_status("fec_election_dates", "error", result["errors"]["fec_election_dates"])
    return result

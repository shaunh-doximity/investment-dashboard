"""Parser and preview adapters for short, human-reviewable catalyst lines.

This module is offline-only. Imported rows are always unverified, regardless of
any wording in the pasted text or prompt-generated source claims.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import math
import re
from zoneinfo import ZoneInfo

from dashboard.event_import import normalize_event_import

ET = "America/New_York"
DATE_TOKEN = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
DATE_RE = re.compile(DATE_TOKEN)
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,9}$")
TIME_RE = re.compile(r"^(?:BMO|AMC|TBD|\d{1,2}:\d{2})$", re.I)
# Registry entries are canonical_event_token: (calendar category, display name).
# Add entries with register_event_type; plain valid ticker symbols remain earnings.
EVENT_REGISTRY = {
    "CPI": ("Macro", "CPI release"), "CORE_CPI": ("Macro", "Core CPI release"),
    "PPI": ("Macro", "PPI release"), "PCE": ("Macro", "PCE release"),
    "CORE_PCE": ("Macro", "Core PCE release"), "JOLTS": ("Macro", "JOLTS release"),
    "NFP": ("Macro", "Nonfarm Payrolls"), "JOBS": ("Macro", "Employment report"),
    "EMPLOYMENT": ("Macro", "Employment report"), "ECI": ("Macro", "Employment Cost Index"),
    "GDP": ("Macro", "GDP release"), "RETAIL_SALES": ("Macro", "Retail sales release"),
    "FOMC": ("Fed", "FOMC meeting"), "FOMC_MINUTES": ("Fed", "FOMC minutes"),
    "TREASURY_AUCTION": ("Macro", "Treasury auction"),
    "TREASURY_FINANCING": ("Macro", "Treasury financing announcement"),
    "TREASURY_REFUNDING": ("Macro", "Treasury quarterly refunding"),
    "TREASURY_BORROWING": ("Macro", "Treasury borrowing announcement"),
    "TRADE_BALANCE": ("Macro", "Trade balance release"),
    "PRODUCTIVITY": ("Macro", "Productivity release"),
    "TREASURY_3Y_AUCTION": ("Macro", "3-year Treasury auction"),
    "HOUSING_STARTS": ("Macro", "Housing starts release"),
    "INDUSTRIAL_PRODUCTION": ("Macro", "Industrial production release"),
    "INVESTOR_DAY": ("Political/Other", "Company investor day"),
    "CORPORATE_FINANCING": ("Political/Other", "Corporate financing event"),
    "OTHER": ("Political/Other", "Other scheduled event"),
}


def register_event_type(token: str, category: str, display_name: str) -> None:
    """Register an unambiguous event token for the compact importer."""
    token = re.sub(r"\s+", "_", str(token).strip().upper())
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", token):
        raise ValueError("Event token must use letters, numbers, or underscores.")
    if category not in {"Fed", "Macro", "Political/Other"} or not str(display_name).strip():
        raise ValueError("Registry category/name is invalid.")
    EVENT_REGISTRY[token] = (category, str(display_name).strip())


def _resolve_date(token: str, window_start: date, window_end: date) -> tuple[date, bool]:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        parsed = date.fromisoformat(token)
        inferred = False
    else:
        month, day, year = (token.split("/") + [None])[:3]
        month, day = int(month), int(day)
        try:
            date(2000, month, day)  # validate month/day, allowing Feb. 29
        except ValueError as exc:
            raise ValueError(f"Date {token} is an impossible calendar date.") from exc
        if year:
            year = int(year)
            if year < 100:
                year += 2000
            parsed = date(year, month, day)
            inferred = False
        else:
            candidates = []
            for year in range(window_start.year - 1, window_end.year + 2):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if window_start <= candidate <= window_end:
                    candidates.append(candidate)
            if len(candidates) != 1:
                reason = "ambiguous across the selected calendar window" if candidates else "outside the selected calendar window"
                raise ValueError(f"Date {token} is {reason}; enter YYYY-MM-DD or widen the window.")
            parsed, inferred = candidates[0], True
    if not window_start <= parsed <= window_end:
        raise ValueError(f"Date {parsed.isoformat()} is outside the selected {window_start.isoformat()}–{window_end.isoformat()} window.")
    return parsed, inferred


def _parse_one(line: str, window_start: date, window_end: date,
               registry: dict[str, tuple[str, str]]) -> tuple[dict, list[str]]:
    original = line
    cleaned = re.sub(r"^\s*(?:[-*•]\s*|\d+[.)]\s*)", "", line.strip())
    cleaned = cleaned.strip().strip("`\"'")
    source_match = re.search(r"\bURL=(https?://\S+)", cleaned, flags=re.I)
    source_url = source_match.group(1).rstrip(")]}") if source_match else ""
    if source_match:
        cleaned = (cleaned[:source_match.start()] + " " + cleaned[source_match.end():]).strip()
    # Copy/paste lists sometimes use a bullet, quote, or a trailing ET marker.
    tokens = cleaned.split()
    if len(tokens) < 3:
        raise ValueError("Expected exactly EVENT DATE TIME, for example AAPL 11/02 AMC.")
    event_token, date_token, time_token = tokens[:3]
    event_token = event_token.strip().rstrip(":").upper()
    # A standalone ET is harmless paste metadata; EST/EDT are deliberately
    # rejected because a fixed offset breaks daylight-saving conversion.
    extras = [token.upper() for token in tokens[3:]]
    if any(token in {"EST", "EDT"} for token in extras):
        raise ValueError("Use ET or omit the timezone token; fixed EST/EDT offsets are not accepted.")
    extras = [token for token in extras if token != "ET"]
    if extras and not (len(extras) == 1 and extras[0].upper().startswith("URL=https://")):
        raise ValueError(f"Unrecognized extra field '{tokens[3]}'; use only EVENT DATE TIME (optional URL= metadata is allowed).")
    if not TIME_RE.fullmatch(time_token):
        raise ValueError("TIME must be BMO, AMC, TBD, or an exact HH:MM value.")
    start_date, inferred = _resolve_date(date_token, window_start, window_end)
    time_token = time_token.upper()
    if time_token not in {"BMO", "AMC", "TBD"}:
        try:
            parsed_time = time.fromisoformat(time_token)
        except ValueError as exc:
            raise ValueError("Invalid time; use 24-hour HH:MM such as 08:30.") from exc
    else:
        parsed_time = None

    if event_token in registry:
        category, display_name = registry[event_token]
        ticker = ""
        if time_token in {"BMO", "AMC"}:
            raise ValueError(f"{event_token} is a scheduled event, not an earnings ticker; use HH:MM or TBD.")
        if parsed_time is None and time_token != "TBD":
            raise ValueError(f"{event_token} requires a time such as 08:30 or TBD.")
        earnings_session = None
    elif re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", event_token):
        # Unknown uppercase identifiers with underscores are explicit event
        # names, not tickers. Preserve the token so users can refine its label.
        ticker = ""
        category, display_name = "Macro", event_token.replace("_", " ").title()
        if time_token in {"BMO", "AMC"}:
            raise ValueError(f"{event_token} is a scheduled event; use HH:MM or TBD, not earnings timing.")
        earnings_session = None
    elif TICKER_RE.fullmatch(event_token):
        ticker = event_token
        category, display_name = "Earnings", f"{ticker} earnings"
        if time_token not in {"BMO", "AMC", "TBD"}:
            raise ValueError(f"{ticker} looks like a ticker; earnings timing must be BMO, AMC, or TBD.")
        earnings_session = time_token
    else:
        raise ValueError(f"Unrecognized event '{event_token}'. Use a valid ticker or an uppercase event identifier with underscores.")

    tz = ZoneInfo(ET)
    local_time = parsed_time or time.min
    naive_local = datetime.combine(start_date, local_time)
    local_start = naive_local.replace(tzinfo=tz, fold=0)
    if local_start.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive_local:
        raise ValueError("That local time does not exist because of the daylight-saving clock change; choose another time.")
    alternate = naive_local.replace(tzinfo=tz, fold=1)
    if parsed_time and alternate.utcoffset() != local_start.utcoffset():
        raise ValueError("That local time is ambiguous during the daylight-saving clock change; choose an unambiguous time.")
    source_uid = "compact-" + re.sub(r"[^A-Z0-9]+", "-", f"{event_token}-{start_date.isoformat()}").strip("-")
    record = {
        "category": category, "canonical_name": display_name, "company_symbol": ticker or None,
        "start_date": start_date.isoformat(), "start_time": parsed_time.strftime("%H:%M") if parsed_time else None,
        "original_timezone": ET, "earnings_session": earnings_session,
        "source_provider": "user compact import", "source_url": source_url,
        "source_id": source_uid, "status": "unverified", "related_symbols": [ticker] if ticker else [],
        "market_relevance": "User/GPT-entered event; verify source independently.",
        "date": start_date.isoformat(), "time": parsed_time.strftime("%H:%M") if parsed_time else None,
        "timezone": ET, "all_day": parsed_time is None,
    }
    event = normalize_event_import(json.dumps({"schema_version": 1, "events": [record]}))["events"][0]
    event["source_identifier"] = source_uid
    event["source_provider"] = "user compact import"
    event["original_input"] = original
    event["notes"] = record["market_relevance"]
    warnings = [f"Year inferred as {start_date.year} from the selected calendar window."] if inferred else []
    return event, warnings


def _split_import_text(text: str) -> list[str]:
    """Split event separators while retaining commas inside URL metadata."""
    text = (text or "").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    text = re.sub(r"(?m)^\s*```(?:text|plain|plaintext)?\s*$", "", text, flags=re.I)
    pieces, current, index, in_url = [], [], 0, False
    while index < len(text):
        if text[index:index + 4].upper() == "URL=":
            in_url = True
            current.extend(text[index:index + 4])
            index += 4
            continue
        char = text[index]
        if char.isspace() and char not in ",;":
            in_url = False
        separator = char in ",;\n"
        if separator:
            lookahead = text[index + 1:]
            looks_like_event = re.match(r"\s*[A-Za-z0-9][A-Za-z0-9_.-]{0,31}\s+" + DATE_TOKEN + r"(?:\s|$)", lookahead)
            if char in "\n;" or looks_like_event:
                value = "".join(current).strip()
                if value:
                    pieces.append(value)
                current = []
                in_url = False
                index += 1
                continue
            if char == "," and not in_url:
                value = "".join(current).strip()
                if value:
                    pieces.append(value)
                current = []
                index += 1
                continue
        current.append(char)
        index += 1
    value = "".join(current).strip()
    if value:
        pieces.append(value)
    return pieces


def parse_compact_events(text: str, *, window_start: str | date, window_end: str | date,
                         registry: dict[str, tuple[str, str]] | None = None) -> dict:
    """Parse compact EVENT DATE TIME lines, accepting comma/newline separators."""
    start = date.fromisoformat(window_start) if isinstance(window_start, str) else window_start
    end = date.fromisoformat(window_end) if isinstance(window_end, str) else window_end
    if end < start:
        raise ValueError("Calendar window end precedes its start.")
    pieces = _split_import_text(text)
    active_registry = {**EVENT_REGISTRY, **{str(key).upper(): value for key, value in (registry or {}).items()}}
    events, invalid, warnings = [], [], []
    for index, piece in enumerate(pieces, 1):
        try:
            event, event_warnings = _parse_one(piece, start, end, active_registry)
            event["compact_line_number"] = index
            events.append(event)
            warnings.extend(f"Line {index}: {warning}" for warning in event_warnings)
        except (ValueError, TypeError) as exc:
            invalid.append({"line": index, "text": piece, "error": str(exc)})
    return {"events": events, "invalid_lines": invalid, "warnings": warnings,
            "window_start": start.isoformat(), "window_end": end.isoformat()}


def editable_preview_items(parsed: dict) -> list[dict]:
    """Adapt compact rows to the existing safe deduplication/revision workflow."""
    from dashboard.event_import import preview_event_import
    normalized = {"schema_version": 1, "events": parsed["events"]}
    return preview_event_import(normalized)


def _is_missing(value) -> bool:
    if value is None or type(value).__name__ in {"NAType", "NaTType"}:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return isinstance(value, (int, float)) and math.isnan(value)
    except (TypeError, ValueError):
        return False


def _as_bool(value, *, default: bool, row_id: str) -> bool:
    if _is_missing(value):
        return default
    if isinstance(value, bool):
        return value
    # pandas/numpy booleans arrive from Streamlit's editor as scalar bool-like values.
    if type(value).__name__ == "bool_":
        return bool(value)
    raise ValueError(f"Row {row_id}: checkbox value is invalid; select or clear the checkbox.")


def _preview_date(value, *, row_id: str) -> date:
    if _is_missing(value):
        raise ValueError(f"Row {row_id}: date is blank; enter a date or exclude this row.")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (AttributeError, TypeError, ValueError):
            pass
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Row {row_id}: date must be a real calendar date in YYYY-MM-DD format.") from exc


def _event_local_datetime(day: date, clock: time | None, *, row_id: str) -> datetime:
    tz = ZoneInfo(ET)
    naive = datetime.combine(day, clock or time.min)
    local = naive.replace(tzinfo=tz, fold=0)
    if local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        raise ValueError(f"Row {row_id}: that Eastern time does not exist during the daylight-saving change.")
    if clock is not None and naive.replace(tzinfo=tz, fold=1).utcoffset() != local.utcoffset():
        raise ValueError(f"Row {row_id}: that Eastern time is ambiguous during the daylight-saving change.")
    return local


def apply_compact_preview_edits(preview: list[dict], edited_rows, *, window_start: str, window_end: str) -> list[dict]:
    """Normalize Streamlit/pandas editor values and validate included rows as a batch.

    Rows are associated by their immutable preview row ID, never by editor sort
    order. Invalid excluded rows are ignored; all included rows validate before
    any database write is attempted.
    """
    if hasattr(edited_rows, "to_dict"):
        edited_rows = edited_rows.to_dict("records")
    if not isinstance(edited_rows, list) or len(edited_rows) != len(preview):
        raise ValueError("Preview row count changed unexpectedly; parse the preserved text again.")
    preview_by_id = {}
    for index, row in enumerate(preview, 1):
        event = row.get("event") or {}
        row_id = str(event.get("compact_line_number") or index)
        if row_id in preview_by_id:
            raise ValueError(f"Preview contains a duplicate row ID ({row_id}); parse again.")
        preview_by_id[row_id] = row
    start, end = date.fromisoformat(window_start), date.fromisoformat(window_end)
    edits_by_id = {}
    for index, edit in enumerate(edited_rows, 1):
        if not isinstance(edit, dict):
            raise ValueError(f"Edited row {index} is malformed; restore or re-parse the preview.")
        raw_id = edit.get("Row", index)
        if _is_missing(raw_id):
            raise ValueError(f"Edited row {index} lost its Row identifier; re-parse the preview.")
        row_id = str(int(raw_id)) if isinstance(raw_id, (int, float)) and not isinstance(raw_id, bool) else str(raw_id).strip()
        if row_id not in preview_by_id or row_id in edits_by_id:
            raise ValueError(f"Edited row {index} has an unknown or duplicate Row identifier ({row_id}).")
        edits_by_id[row_id] = edit
    errors, output, seen = [], [], set()
    for row_id, original in preview_by_id.items():
        edit = edits_by_id.get(row_id)
        if edit is None:
            errors.append(f"Row {row_id}: row was removed from the editor; re-parse the preview.")
            continue
        try:
            # Missing checkbox values fail closed; an editor sentinel must not
            # approve a row by inheriting its initially checked state.
            include = _as_bool(edit.get("Import"), default=False, row_id=row_id)
            override = _as_bool(edit.get("Override conflicting date"), default=False, row_id=row_id)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not include:
            output.append({**original, "include": False, "override_conflict": override})
            continue
        try:
            item = dict(original["event"])
            raw_name = edit.get("Event", item.get("name"))
            if _is_missing(raw_name):
                raise ValueError(f"Row {row_id}: event name is blank; enter a name or exclude this row.")
            item["name"] = item["canonical_name"] = str(raw_name).strip()
            day = _preview_date(edit.get("Date", item.get("start_date")), row_id=row_id)
            if not start <= day <= end:
                raise ValueError(f"Row {row_id}: date must remain within {start.isoformat()}–{end.isoformat()}.")
            raw_timing = edit.get("Timing", item.get("earnings_session") or item.get("start_time") or "TBD")
            timing = "TBD" if _is_missing(raw_timing) else str(raw_timing).strip().upper()
            clock = None
            if item.get("category") == "Earnings":
                if timing not in {"BMO", "AMC", "TBD"}:
                    raise ValueError(f"Row {row_id} ({item['name']}): earnings timing must be BMO, AMC, or TBD.")
                item["earnings_session"], item["start_time"], item["all_day"] = timing, None, True
            else:
                item["earnings_session"] = None
                if timing not in {"", "TBD"}:
                    try:
                        clock = time.fromisoformat(timing)
                    except ValueError as exc:
                        raise ValueError(f"Row {row_id} ({item['name']}): enter a valid HH:MM time or TBD.") from exc
                    item["start_time"], item["all_day"] = clock.strftime("%H:%M"), False
                else:
                    item["start_time"], item["all_day"] = None, True
            local = _event_local_datetime(day, clock, row_id=row_id)
            item["start_date"] = item["event_date"] = day.isoformat()
            item["start_utc"] = local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            item["source_url"] = str(edit.get("Source URL", item.get("source_url", "")) or "").strip()
            item["notes"] = str(edit.get("Notes", item.get("notes", "")) or "").strip()
            identity = (item.get("category"), tuple(item.get("related_symbols", [])), item["name"].casefold(), day.isoformat())
            if identity in seen:
                output.append({**original, "include": False, "classification": "duplicate in preview", "override_conflict": False})
                continue
            seen.add(identity)
            output.append({**original, "event": item, "include": True, "override_conflict": override})
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("Import preview needs correction before saving: " + "; ".join(errors))
    return output


def build_compact_discovery_prompt(start_date: str, end_date: str, *, coverage: str, tickers=()) -> str:
    """Create a concise prompt; Robinhood MCP is used only in ChatGPT, not locally."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if end < start:
        raise ValueError("End date must not precede start date.")
    allowed = {"Holdings/watchlist + macro", "Holdings + major market catalysts", "Broad market"}
    if coverage not in allowed:
        raise ValueError("Choose a supported catalyst coverage mode.")
    # Keep this argument for compatibility with older callers, but never copy
    # a dashboard-generated ticker list into a prompt that asks MCP for holdings.
    from datetime import timedelta as _timedelta
    windows, cursor = [], start
    while cursor <= end:
        last = min(end, cursor + _timedelta(days=30))
        windows.append(f"{cursor.isoformat()}–{last.isoformat()}")
        cursor = last + _timedelta(days=1)
    scope = "Prioritize accessible account holdings." if coverage != "Broad market" else "Use broad market coverage; do not require account context."
    outside = "Include a limited number of major market-moving earnings outside holdings." if coverage != "Holdings/watchlist + macro" else "Keep earnings focused on accessible holdings and the most market-moving names."
    macro = "Include official FOMC, CPI/PPI/PCE, labor, GDP, consumption/retail sales and Treasury events."
    return f"""Find scheduled U.S. market catalysts from {start.isoformat()} through {end.isoformat()} (Robinhood earnings queries must use windows no longer than 31 days: {'; '.join(windows)}).
Coverage: {coverage}. {scope} In ChatGPT, if authorized and available, use the connected Robinhood MCP to retrieve current accessible account holdings and prioritize their upcoming earnings. Use ticker symbols only for event discovery; do not include share counts, account IDs, balances, or other private financial data in your output. If Robinhood MCP is unavailable or no holdings are accessible, ask me for a public ticker list or use broad market catalysts; do not invent holdings. {outside}
Use official Federal Reserve, BLS, BEA, and U.S. Treasury schedules for macro events. {macro} Add major investor or financing events only when dates are verified. Avoid irrelevant earnings and duplicates.
Return ONLY concise, deduplicated lines in exactly this syntax: EVENT DATE TIME. Examples: `AAPL 11/02 AMC`, `FOMC 12/09 14:00`, `CPI 11/10 08:30`. Use BMO/AMC/TBD for earnings; use America/New_York HH:MM or TBD for macro events. Never invent a date or time. No JSON, URLs, descriptions, or narrative. Pasted dates remain unverified in the local dashboard until reviewed. The local Streamlit app does not invoke Robinhood MCP."""

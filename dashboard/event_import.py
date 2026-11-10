"""Versioned, offline GPT event import validation and explicit batch approval."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
import re
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashboard.database import DEFAULT_DATABASE_PATH, connect

SCHEMA_VERSION = 1
CATEGORIES = {"macro": "Macro", "economic": "Macro", "macroeconomic": "Macro",
             "fed": "Fed", "monetary policy": "Fed", "earnings": "Earnings",
             "company earnings": "Earnings", "political/other": "Political/Other",
             "political": "Political/Other", "other": "Political/Other"}
STATUSES = {"confirmed", "estimated", "tentative", "unverified", "cancelled", "canceled"}


def _pick(item, *keys, default=None):
    for key in keys:
        if key in item:
            return item[key]
    return default


def _iso_date(value, path):
    if not isinstance(value, str):
        raise ValueError(f"{path}: expected ISO date YYYY-MM-DD; received {type(value).__name__}.")
    try:
        parsed = date.fromisoformat(value)
        if value != parsed.isoformat():
            raise ValueError
        return parsed.isoformat()
    except ValueError as exc:
        raise ValueError(f"{path}: invalid calendar date; use YYYY-MM-DD.") from exc


def _url(value, path):
    if not value:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{path}: expected URL text.")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError(f"{path}: expected a complete http:// or https:// source URL.")
    return value.strip()


def _ticker(value):
    if not value:
        return ""
    ticker = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,9}", ticker):
        raise ValueError("ticker: use a valid company symbol such as BRK.B; no other data was changed.")
    return ticker


def normalize_event_import(raw_json: str) -> dict:
    """Normalize straightforward aliases; date-only/session events stay untimed."""
    try:
        payload = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Import must be valid JSON; the pasted text is retained in the UI.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Root JSON must be an object containing schema_version and events.")
    version = payload.get("schema_version", payload.get("version"))
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported event schema_version {version!r}; expected {SCHEMA_VERSION}.")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("events: expected an array of event objects.")
    normalized = []
    warnings = []
    for index, item in enumerate(events):
        path = f"events[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path}: expected an object.")
        category_raw = str(_pick(item, "category", "event_type", "type", default="")).strip().lower()
        category = CATEGORIES.get(category_raw)
        if not category:
            raise ValueError(f"{path}.category: expected Macro, Fed, Earnings, or Political/Other.")
        name = _pick(item, "canonical_name", "name", "title")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path}.canonical_name: expected non-empty text.")
        symbol = _ticker(_pick(item, "company_symbol", "ticker", "symbol", default=""))
        related = item.get("related_symbols", item.get("affected_symbols", [])) or []
        if not isinstance(related, list):
            raise ValueError(f"{path}.related_symbols: expected an array of ticker strings.")
        related = list(dict.fromkeys([normalized_symbol for value in [*([symbol] if symbol else []), *related]
                                      if (normalized_symbol := _ticker(value))]))
        sectors = item.get("affected_sectors", item.get("sectors", [])) or []
        if not isinstance(sectors, list) or any(not isinstance(sector, str) for sector in sectors):
            raise ValueError(f"{path}.affected_sectors: expected an array of text labels.")
        timezone_name = str(_pick(item, "original_timezone", "source_timezone", "timezone", default="America/New_York"))
        try:
            tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{path}.original_timezone: use a valid IANA timezone.") from exc
        start_date_value = _pick(item, "start_date", "event_date", "date")
        start_time = _pick(item, "start_time", "time")
        session = str(_pick(item, "earnings_session", "release_timing", "timing", default="TBD" if category == "Earnings" else "") or "").upper()
        if category == "Earnings" and session not in {"BMO", "AMC", "TBD"}:
            raise ValueError(f"{path}.earnings_session: expected BMO, AMC, or TBD.")
        if start_date_value is None and item.get("start_utc"):
            try:
                parsed_utc = datetime.fromisoformat(str(item["start_utc"]).replace("Z", "+00:00"))
                if parsed_utc.tzinfo is None:
                    raise ValueError
                local_start = parsed_utc.astimezone(tz)
                start_date_value = local_start.date().isoformat()
                start_time = local_start.strftime("%H:%M:%S") if not item.get("all_day", False) else None
            except ValueError as exc:
                raise ValueError(f"{path}.start_utc: expected an ISO timestamp with timezone.") from exc
        elif start_date_value is not None and item.get("start_utc"):
            try:
                raw_instant = datetime.fromisoformat(str(item["start_utc"]).replace("Z", "+00:00"))
                if raw_instant.tzinfo is None:
                    raise ValueError
                provided_date = date.fromisoformat(str(start_date_value))
                if raw_instant.astimezone(tz).date() != provided_date:
                    raise ValueError("date mismatch")
            except ValueError as exc:
                raise ValueError(f"{path}.start_utc: timestamp must be timezone-aware and match start_date in original_timezone.") from exc
        start_date = _iso_date(start_date_value, f"{path}.start_date")
        # A BMO/AMC label is a session convention, not an exact timestamp. Only
        # retain a clock time when one was explicitly included in the JSON.
        all_day = not bool(start_time)
        if start_time and not all_day:
            try:
                parsed_time = time.fromisoformat(str(start_time))
            except ValueError as exc:
                raise ValueError(f"{path}.start_time: expected a valid local time such as 14:00; omit it when not known.") from exc
            start_local = datetime.combine(date.fromisoformat(start_date), parsed_time, tzinfo=tz)
        else:
            start_local = datetime.combine(date.fromisoformat(start_date), time.min, tzinfo=tz)
        start_utc = start_local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        end_utc = None
        end_date_value = _pick(item, "end_date")
        end_time = _pick(item, "end_time")
        if end_date_value or end_time:
            end_date = _iso_date(end_date_value or start_date, f"{path}.end_date")
            if not end_time:
                warnings.append(f"{path}: end date retained as date-only; no exact end time was supplied.")
            else:
                try:
                    end_local = datetime.combine(date.fromisoformat(end_date), time.fromisoformat(str(end_time)), tzinfo=tz)
                except ValueError as exc:
                    raise ValueError(f"{path}.end_time: expected a local time such as 16:00.") from exc
                end_utc = end_local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        source_url = _url(_pick(item, "source_url", "url", default=""), f"{path}.source_url")
        source_provider = _pick(item, "source_provider", "provider", default="")
        if not isinstance(source_provider, str):
            raise ValueError(f"{path}.source_provider: expected text.")
        source_identifier = _pick(item, "source_uid", "source_id", "event_id", default="")
        if source_identifier is not None and not isinstance(source_identifier, (str, int)):
            raise ValueError(f"{path}.source_id: expected text or integer identifier.")
        published = _pick(item, "source_published_at", "publication_date", "verified_date")
        if published:
            published = _iso_date(published, f"{path}.source_published_at")
        status_claim = str(item.get("status", "unverified")).strip().lower()
        if status_claim not in STATUSES:
            raise ValueError(f"{path}.status: expected confirmed, estimated, tentative, or unverified.")
        relevance = _pick(item, "market_relevance", "notes", default="")
        if not isinstance(relevance, str):
            raise ValueError(f"{path}.market_relevance: expected text.")
        normalized.append({
            "name": name.strip(), "category": category, "ticker": symbol,
            "related_symbols": related, "start_date": start_date, "start_time": start_time if not all_day else None,
            "end_date": end_date_value, "end_time": end_time, "end_utc": end_utc,
            "start_utc": start_utc, "source_timezone": timezone_name, "all_day": all_day,
            "earnings_session": session if category == "Earnings" else None,
            "source_provider": source_provider.strip(), "source_url": source_url,
            "source_identifier": str(source_identifier or "").strip(),
            "source_published_at": published, "status_claim": status_claim,
            "status": "Unverified", "source_verification": "GPT import · Unverified",
            "affected_sectors": list(dict.fromkeys(s.strip() for s in sectors if s.strip())),
            "notes": relevance.strip(), "original_input": json.dumps(item, ensure_ascii=False, sort_keys=True),
        })
    return {"schema_version": SCHEMA_VERSION, "events": normalized, "warnings": warnings}


def build_gpt_discovery_prompt(start_date: str, end_date: str, priority_tickers=()) -> str:
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if end < start:
        raise ValueError("End date must not be earlier than the start date.")
    tickers = list(dict.fromkeys(_ticker(t) for t in priority_tickers if str(t).strip()))
    windows = []
    cursor = start
    from datetime import timedelta
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=30))
        windows.append(f"{cursor.isoformat()} through {window_end.isoformat()}")
        cursor = window_end + timedelta(days=1)
    ticker_text = ", ".join(tickers) if tickers else "No priority tickers supplied; discover broad market events."
    return f"""Find scheduled, upcoming U.S. market catalysts for {start.isoformat()} through {end.isoformat()}.
Use Robinhood MCP get_earnings_calendar for broad earnings discovery, in query windows no longer than 31 days. Use get_earnings_results for specific priority tickers when available. Query windows: {'; '.join(windows)}.
Priority tickers: {ticker_text}. These are user-selected symbols only; no private portfolio data is included.
Also check official Federal Reserve FOMC calendars, official BLS and BEA release schedules, and credible public primary sources for Treasury auctions/issuance, major company investor days, verified corporate financing/capital-markets events, and documented regulatory or geopolitical deadlines.
Include: company earnings; FOMC decisions/minutes; CPI/PPI/employment/PCE; GDP/retail sales; Treasury auctions/issuance; investor days; verified financing events; sufficiently documented scheduled policy/geopolitical events.
Do not invent dates for unscheduled news or unconfirmed earnings. Include source URL and source publication/verification date. Preserve date-only events without an exact time; use America/New_York for known U.S. times. Earnings timing must be BMO, AMC, or TBD.
Return JSON only in this shape: {{"schema_version":1,"events":[{{"category":"Macro|Fed|Earnings|Political/Other","canonical_name":"...","company_symbol":null,"start_date":"YYYY-MM-DD","start_time":null,"end_date":null,"end_time":null,"original_timezone":"America/New_York","earnings_session":null,"source_provider":"...","source_url":"https://...","source_published_at":"YYYY-MM-DD or null","status":"confirmed|estimated|tentative|unverified","affected_sectors":[],"related_symbols":[],"market_relevance":"..."}}]}}.
Only use confirmed/estimated status when the source supports it. The dashboard will treat every imported event as unverified until independently checked. Do not include unsupported certainty or an importance score."""


def event_import_template() -> dict:
    return {"schema_version": SCHEMA_VERSION, "events": [{
        "category": "Earnings", "canonical_name": "Example Corp earnings", "company_symbol": "EXM",
        "start_date": "2027-01-20", "start_time": None, "end_date": None, "end_time": None,
        "original_timezone": "America/New_York", "earnings_session": "TBD",
        "source_provider": "Company investor relations", "source_url": "https://example.com/investors",
        "source_published_at": None, "status": "unverified", "affected_sectors": [],
        "related_symbols": ["EXM"], "market_relevance": "Example only; replace with a sourced event.",
    }]}


def _identity(event):
    canonical_name = re.sub(r"[^a-z0-9]+", " ", event["name"].lower()).strip()
    symbols = ",".join(event.get("related_symbols", []))
    return event["category"], symbols, canonical_name


def _import_source_uid(event):
    source_id = event.get("source_identifier")
    if source_id:
        identity = f"{event.get('source_provider','')}|{source_id}"
    else:
        identity = "|".join((*_identity(event), event["start_date"]))
    return "gpt-" + sha256(identity.encode()).hexdigest()


def preview_event_import(normalized: dict, *, database_path=DEFAULT_DATABASE_PATH) -> list[dict]:
    from dashboard.data_access import list_market_events
    current = list_market_events(database_path, include_cancelled=True)
    by_identity = {}
    by_source = {}
    for event in current:
        event["related_symbols"] = event.get("related_symbols", [])
        by_identity.setdefault(_identity(event), []).append(event)
        if event.get("source_uid"):
            by_source[(event.get("provider"), event["source_uid"])] = event
    preview = []
    seen = set()
    for item in normalized["events"]:
        key = _identity(item)
        date_key = item["start_date"]
        unique = (key, date_key)
        if unique in seen:
            preview.append({"event": item, "classification": "duplicate in pasted JSON", "include": False, "existing": None})
            continue
        seen.add(unique)
        source_uid = _import_source_uid(item)
        source_match = by_source.get(("gpt_bulk_import", source_uid))
        exact = source_match if source_match and (source_match.get("event_date") or source_match["start_utc"][:10]) == date_key else next((event for event in by_identity.get(key, []) if (event.get("event_date") or event["start_utc"][:10]) == date_key), None)
        identity_matches = by_identity.get(key, [])
        protected = next((event for event in identity_matches if
                          event.get("source_verification") == "Official source"
                          or event.get("provider", "").startswith("manual_")), None)
        conflict = source_match or protected or next(iter(identity_matches), None)
        is_cancel = item.get("status_claim") in {"cancelled", "canceled"}
        classification = ("cancellation claim" if (exact or conflict) and is_cancel
                          else "unmatched cancellation" if is_cancel else "unchanged duplicate" if exact
                          else "conflicting date" if conflict else "new · unverified")
        preview.append({"event": item, "classification": classification,
                        "include": classification in {"new · unverified", "conflicting date", "cancellation claim"},
                        "existing": exact or conflict})
    return preview


def approve_event_import(preview: list[dict], *, original_json: str, database_path=DEFAULT_DATABASE_PATH) -> dict:
    """Persist explicitly selected rows; conflicts need an explicit override flag."""
    # Validate the entire selected batch before opening a write transaction.
    # The connection context below is atomic as a second line of defense, but
    # preflight also gives callers a useful row-specific error with no batch
    # record or partial event revisions created.
    selected_rows = []
    for index, row in enumerate(preview or [], 1):
        if not isinstance(row, dict):
            raise ValueError(f"Row {index}: preview entry must be an object.")
        include = row.get("include", False)
        if not isinstance(include, bool):
            raise ValueError(f"Row {index}: Import must be checked or unchecked.")
        if not include:
            continue
        item = row.get("event")
        if not isinstance(item, dict):
            raise ValueError(f"Row {index}: included event data are missing; exclude the row or re-parse.")
        try:
            _iso_date(item.get("start_date"), f"Row {index}.date")
            if not str(item.get("name", "")).strip():
                raise ValueError(f"Row {index}.event: event name is required.")
            if item.get("category") not in {"Macro", "Fed", "Earnings", "Political/Other"}:
                raise ValueError(f"Row {index}.category: unsupported calendar category.")
            if item.get("category") == "Earnings" and item.get("earnings_session") not in {"BMO", "AMC", "TBD"}:
                raise ValueError(f"Row {index}.timing: earnings must use BMO, AMC, or TBD.")
            try:
                instant = datetime.fromisoformat(str(item.get("start_utc", "")).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"Row {index}.start_utc: expected a valid timezone-aware event timestamp.") from exc
            if instant.tzinfo is None:
                raise ValueError(f"Row {index}.start_utc: expected a timezone-aware UTC timestamp.")
            if item.get("source_url"):
                _url(item["source_url"], f"Row {index}.source_url")
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        override = row.get("override_conflict", False)
        if not isinstance(override, bool):
            raise ValueError(f"Row {index}: conflict override must be checked or unchecked.")
        selected_rows.append(row)
    batch_id = str(uuid4())
    inserted = updated = unchanged = conflicts = skipped = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(database_path) as connection:
        connection.execute("INSERT INTO market_event_import_batches(batch_id,created_at,original_json,status) VALUES (?,?,?,'active')",
                           (batch_id, now, original_json))
        for row in preview:
            if not row.get("include"):
                skipped += 1
                continue
            item = dict(row["event"])
            key = _identity(item)
            existing_rows = connection.execute("SELECT * FROM market_events WHERE category=?", (item["category"],)).fetchall()
            same_identity = []
            for raw in existing_rows:
                old = dict(raw)
                old_symbols = json.loads(old.get("related_symbols_json") or "[]")
                old["related_symbols"] = old_symbols
                if _identity(old) == key:
                    same_identity.append(old)
            source_uid = _import_source_uid(item)
            source_match_row = connection.execute("SELECT * FROM market_events WHERE provider='gpt_bulk_import' AND source_uid=?", (source_uid,)).fetchone()
            source_match = dict(source_match_row) if source_match_row else None
            existing = source_match if source_match and (source_match.get("event_date") or source_match["start_utc"][:10]) == item["start_date"] else next((old for old in same_identity if (old.get("event_date") or old["start_utc"][:10]) == item["start_date"]), None)
            protected = next((old for old in same_identity if
                              old.get("source_verification") == "Official source"
                              or old.get("provider", "").startswith("manual_")), None)
            conflict = source_match or protected or next(iter(same_identity), None)
            if item.get("status_claim") in {"cancelled", "canceled"} and not existing and not conflict:
                conflicts += 1
                continue
            if not existing and conflict and item.get("status_claim") in {"cancelled", "canceled"}:
                existing = conflict
            if existing:
                if item.get("status_claim") not in {"cancelled", "canceled"}:
                    unchanged += 1
                    continue
                if (existing.get("source_verification") == "Official source"
                        or existing.get("provider", "").startswith("manual_")):
                    conflicts += 1
                    continue
                if not row.get("override_conflict", False):
                    conflicts += 1
                    continue
                before_record = dict(existing)
                before_record.pop("related_symbols", None)
                existing["status"] = "Cancelled"
                existing["notes"] = (existing.get("notes", "") + " User-approved cancellation claim from GPT import; verify source independently.").strip()
                existing["source_verification"] = "User override · unverified source"
                existing["import_batch_id"] = batch_id
                existing.pop("related_symbols", None)  # decoded comparison helper, not a table column
                columns = [column for column in existing if column != "event_id"]
                connection.execute("UPDATE market_events SET " + ",".join(f"{column}=?" for column in columns) + " WHERE event_id=?",
                                   [existing[column] for column in columns] + [existing["event_id"]])
                prior_json = json.dumps(before_record, ensure_ascii=False, sort_keys=True)
                after_json = json.dumps(existing, ensure_ascii=False, sort_keys=True)
                connection.execute("INSERT INTO market_event_revisions(event_id,changed_at,action,previous_json,current_json) VALUES (?,?,?,?,?)",
                                   (existing["event_id"], now, "cancelled", prior_json, after_json))
                connection.execute("INSERT INTO market_event_import_items(batch_id,event_id,action,before_json,after_json) VALUES (?,?,?,?,?)",
                                   (batch_id, existing["event_id"], "cancelled", prior_json, after_json))
                updated += 1
                continue
            if conflict and not row.get("override_conflict", False):
                conflicts += 1
                continue
            # Officially sourced rows and user-authored rows are never replaced
            # by an imported GPT/user text event, even when override is checked.
            if conflict and (conflict.get("source_verification") == "Official source"
                             or conflict.get("provider", "").startswith("manual_")):
                conflicts += 1
                continue
            event_id = conflict["event_id"] if conflict else sha256(f"gpt_import|{batch_id}|{item['start_date']}|{item['name']}".encode()).hexdigest()
            before_record = dict(conflict) if conflict else None
            if before_record is not None:
                before_record.pop("related_symbols", None)  # preview-only decoded convenience field
            old_json = json.dumps(before_record, ensure_ascii=False, sort_keys=True) if before_record else None
            provider = "gpt_bulk_import"
            source_uid = _import_source_uid(item)
            dedupe_key = sha256("|".join((*key, item["start_date"])).encode()).hexdigest()
            record = {
                "event_id": event_id, "dedupe_key": dedupe_key, "provider": provider,
                "source_uid": source_uid, "name": item["name"], "category": item["category"],
                "start_utc": item["start_utc"], "source_timezone": item["source_timezone"],
                "source_url": item["source_url"], "status": "Unverified", "last_verified_at": "",
                "related_symbols_json": json.dumps(item["related_symbols"]), "notes": item["notes"],
                "importance_tier": "Standard", "all_day": int(item["all_day"]),
                "event_date": item["start_date"], "earnings_session": item["earnings_session"],
                "exact_announcement_time_utc": item["start_utc"] if item["category"] == "Earnings" and not item["all_day"] else None,
                "source_verification": "GPT import · Unverified",
                "original_input": item["original_input"], "end_utc": item["end_utc"],
                "source_published_at": item["source_published_at"],
                "source_provider": item["source_provider"], "affected_sectors_json": json.dumps(item["affected_sectors"]),
                "import_batch_id": batch_id,
            }
            if conflict:
                columns = [key for key in record if key != "event_id"]
                connection.execute("UPDATE market_events SET " + ",".join(f"{column}=?" for column in columns) + " WHERE event_id=?",
                                   [record[column] for column in columns] + [event_id])
                action = "cancelled" if item.get("status_claim") in {"cancelled", "canceled"} else "rescheduled"
                if action == "cancelled":
                    connection.execute("UPDATE market_events SET status='Cancelled',source_verification='User override · unverified source',notes=notes || ' User-approved cancellation claim from GPT import; verify source independently.' WHERE event_id=?", (event_id,))
                updated += 1
            else:
                columns = list(record)
                connection.execute("INSERT INTO market_events(" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                                   [record[column] for column in columns])
                action = "created"
                inserted += 1
            current_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
            connection.execute("INSERT INTO market_event_revisions(event_id,changed_at,action,previous_json,current_json) VALUES (?,?,?,?,?)",
                               (event_id, now, action, old_json, current_json))
            connection.execute("INSERT INTO market_event_import_items(batch_id,event_id,action,before_json,after_json) VALUES (?,?,?,?,?)",
                               (batch_id, event_id, action, old_json, current_json))
        result = {"batch_id": batch_id, "inserted": inserted, "updated": updated,
                  "unchanged": unchanged, "conflicts": conflicts, "skipped": skipped}
        connection.execute("UPDATE market_event_import_batches SET result_json=? WHERE batch_id=?",
                           (json.dumps(result, ensure_ascii=False), batch_id))
    return result


def rollback_event_import(batch_id: str, *, database_path=DEFAULT_DATABASE_PATH) -> dict:
    """Undo a batch only when imported rows have not subsequently changed."""
    rolled_back = conflicts = 0
    with connect(database_path) as connection:
        batch = connection.execute("SELECT * FROM market_event_import_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] != "active":
            raise ValueError("Import batch is missing or already rolled back.")
        items = connection.execute("SELECT * FROM market_event_import_items WHERE batch_id=?", (batch_id,)).fetchall()
        for item in items:
            current = connection.execute("SELECT * FROM market_events WHERE event_id=?", (item["event_id"],)).fetchone()
            if not current or current["import_batch_id"] != batch_id:
                conflicts += 1
                continue
            expected = json.loads(item["after_json"] or "{}")
            current_record = dict(current)
            if any(current_record.get(key) != value for key, value in expected.items() if key != "updated_at"):
                conflicts += 1
                continue
            if item["action"] == "created":
                restored = current_record
                restored["status"] = "Cancelled"
                restored["notes"] = (restored.get("notes", "") + " Import batch rolled back by user.").strip()
                columns = [key for key in restored if key != "event_id"]
                connection.execute("UPDATE market_events SET " + ",".join(f"{column}=?" for column in columns) + " WHERE event_id=?",
                                   [restored[column] for column in columns] + [item["event_id"]])
                connection.execute("INSERT INTO market_event_revisions(event_id,changed_at,action,previous_json,current_json) VALUES (?,?,?,?,?)",
                                   (item["event_id"], datetime.now(timezone.utc).isoformat(timespec="seconds"), "cancelled", item["after_json"], json.dumps(restored, ensure_ascii=False, sort_keys=True)))
            else:
                before = json.loads(item["before_json"] or "{}")
                columns = [key for key in before if key != "event_id"]
                connection.execute("UPDATE market_events SET " + ",".join(f"{column}=?" for column in columns) + " WHERE event_id=?",
                                   [before[column] for column in columns] + [item["event_id"]])
                connection.execute("INSERT INTO market_event_revisions(event_id,changed_at,action,previous_json,current_json) VALUES (?,?,?,?,?)",
                                   (item["event_id"], datetime.now(timezone.utc).isoformat(timespec="seconds"), "rescheduled", json.dumps(dict(current), ensure_ascii=False, sort_keys=True), item["before_json"]))
            rolled_back += 1
        connection.execute("UPDATE market_event_import_batches SET status='rolled_back' WHERE batch_id=?", (batch_id,))
    return {"rolled_back": rolled_back, "conflicts": conflicts}


def list_import_batches(*, database_path=DEFAULT_DATABASE_PATH, limit=20):
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT batch_id,created_at,status,result_json FROM market_event_import_batches ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [{**dict(row), "result": json.loads(row["result_json"] or "{}")} for row in rows]

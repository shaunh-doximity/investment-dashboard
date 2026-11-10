"""Local, versioned market observations and explicitly approved scenarios.

Scenarios are conditional research notes, never forecasts or trade instructions.
Free-text conditions remain human-reviewed unless the user supplies objective
metric rules that can be checked against the current local snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashboard.database import DEFAULT_DATABASE_PATH, connect


SCENARIO_STATUSES = (
    "Watching", "Supported by new evidence", "Challenged", "Resolved", "Archived", "Unassessed",
)
SESSION_TYPES = ("premarket", "regular", "after_hours", "unknown")
SCENARIO_FIELDS = (
    "title", "hypothesis", "market_context", "if_conditions", "then_interpretation",
    "competing_interpretation", "indicators_to_monitor", "invalidation_conditions",
    "review_horizon", "source_observations", "source_links", "condition_rules", "status",
)
CONDITION_OPERATORS = {">", ">=", "<", "<=", "=="}
_TICKER_TOKEN = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z][A-Z0-9.-]{0,5})(?![A-Za-z0-9])")


def extract_market_symbols(text: str, known_symbols=()) -> list[str]:
    """Extract only explicit $TICKER tokens or tickers known to the snapshot."""
    known = {str(symbol).upper() for symbol in (known_symbols or ())}
    result = []
    for match in _TICKER_TOKEN.finditer(str(text or "").upper()):
        token = match.group(1).rstrip(".")
        explicit = match.group(0).startswith("$")
        if token in known or (explicit and len(token) <= 5):
            if token not in result:
                result.append(token)
    return result


def normalize_observation_time(value: datetime, timezone_name: str = "America/New_York") -> str:
    """Normalize a user-entered local observation time to ISO UTC, DST-aware."""
    if not isinstance(value, datetime):
        raise ValueError("Enter a valid observation date and time.")
    try:
        zone = ZoneInfo(str(timezone_name))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Choose a supported time zone.") from exc
    if value.tzinfo is None:
        first = value.replace(tzinfo=zone, fold=0)
        second = value.replace(tzinfo=zone, fold=1)
        roundtrip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if roundtrip != value:
            raise ValueError("That local time does not exist because of a daylight-saving clock change.")
        if first.utcoffset() != second.utcoffset():
            raise ValueError("That local time is ambiguous because clocks change; provide an unambiguous observation time.")
        local = first
    else:
        local = value.astimezone(zone)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def validate_user_observation(*, observation_text: str, observed_at: datetime,
                              timezone_name: str = "America/New_York", session_type: str = "unknown",
                              symbols=(), source_urls=()) -> dict:
    text = str(observation_text or "").strip()
    if not text or len(text) > 2000:
        raise ValueError("Observation text must contain 1–2,000 characters.")
    session = str(session_type or "unknown").strip().lower()
    if session not in SESSION_TYPES:
        raise ValueError("Select premarket, regular, after-hours, or unknown session type.")
    urls = [str(url).strip() for url in (source_urls or ()) if str(url).strip()]
    if any(not url.startswith("https://") for url in urls):
        raise ValueError("Observation source links must use HTTPS.")
    clean_symbols = list(dict.fromkeys(str(symbol).strip().upper() for symbol in (symbols or ()) if str(symbol).strip()))
    return {
        "observation_text": text,
        "observed_at": normalize_observation_time(observed_at, timezone_name),
        "session_type": session,
        "symbols": clean_symbols,
        "verification_status": "user_supplied_unverified",
        "source_urls": urls,
    }


def save_market_user_observation(*, observation: dict, origin_conversation_id: str,
                                 origin_message_id: int | None,
                                 database_path=DEFAULT_DATABASE_PATH) -> dict:
    """Save one user observation independently from official price history."""
    if not isinstance(observation, dict) or not observation.get("observation_text"):
        raise ValueError("A validated user observation is required.")
    if not origin_conversation_id or origin_message_id is None:
        raise ValueError("Observation provenance needs its conversation and message reference.")
    observation_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(database_path) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO market_user_observations
               (observation_id,observation_text,observed_at,session_type,symbols_json,
                verification_status,origin_conversation_id,origin_message_id,source_urls_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (observation_id, observation["observation_text"], observation["observed_at"],
             observation["session_type"], json.dumps(observation.get("symbols", [])),
             "user_supplied_unverified", str(origin_conversation_id), int(origin_message_id),
             json.dumps(observation.get("source_urls", [])), created_at),
        )
        row = connection.execute(
            "SELECT * FROM market_user_observations WHERE origin_conversation_id=? AND origin_message_id=?",
            (str(origin_conversation_id), int(origin_message_id)),
        ).fetchone()
    return _decode_observation(row)


def _decode_observation(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["symbols"] = json.loads(item.pop("symbols_json") or "[]")
    item["source_urls"] = json.loads(item.pop("source_urls_json") or "[]")
    return item


def list_market_user_observations(*, limit: int = 20, database_path=DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM market_user_observations ORDER BY observed_at DESC,created_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_decode_observation(row) for row in rows]


def save_market_research_note(*, conversation_id: str, message_id: int, note_text: str,
                              snapshot_fingerprint: str = "", source_metadata=(),
                              database_path=DEFAULT_DATABASE_PATH) -> dict:
    """Persist a chat answer only after an explicit Save as Research Note action."""
    text = str(note_text or "").strip()
    if not text or len(text) > 12000:
        raise ValueError("Research note must contain 1–12,000 characters.")
    if not conversation_id or message_id is None:
        raise ValueError("Research note needs its originating conversation and answer.")
    sources = [item for item in (source_metadata or []) if isinstance(item, dict)
               and str(item.get("url", "")).startswith("https://")][:10]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    note_id = uuid.uuid4().hex
    with connect(database_path) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO market_ai_research_notes
               (note_id,conversation_id,message_id,note_text,snapshot_fingerprint,source_metadata_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (note_id, str(conversation_id), int(message_id), text, str(snapshot_fingerprint or ""),
             json.dumps(sources, ensure_ascii=False), now),
        )
        row = connection.execute(
            "SELECT * FROM market_ai_research_notes WHERE conversation_id=? AND message_id=?",
            (str(conversation_id), int(message_id)),
        ).fetchone()
    result = dict(row)
    result["source_metadata"] = json.loads(result.pop("source_metadata_json") or "[]")
    return result


def list_market_research_notes(*, limit: int = 50, database_path=DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM market_ai_research_notes ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["source_metadata"] = json.loads(item.pop("source_metadata_json") or "[]")
        result.append(item)
    return result


def validate_scenario(scenario: dict) -> dict:
    if not isinstance(scenario, dict):
        raise ValueError("Scenario draft must be an object.")
    output = {key: scenario.get(key) for key in SCENARIO_FIELDS}
    for key in ("title", "hypothesis", "if_conditions", "then_interpretation",
                "competing_interpretation", "invalidation_conditions", "review_horizon"):
        value = str(output.get(key) or "").strip()
        if not value:
            raise ValueError(f"Scenario {key.replace('_', ' ')} is required.")
        output[key] = value[:5000]
    output["market_context"] = str(output.get("market_context") or "").strip()[:5000]
    output["status"] = str(output.get("status") or "Watching")
    if output["status"] not in SCENARIO_STATUSES:
        raise ValueError("Choose a supported scenario status.")
    for key in ("indicators_to_monitor", "source_observations", "source_links", "condition_rules"):
        if output.get(key) is None:
            output[key] = []
        if not isinstance(output[key], list):
            raise ValueError(f"Scenario {key.replace('_', ' ')} must be a list.")
    output["indicators_to_monitor"] = [str(value).strip() for value in output["indicators_to_monitor"] if str(value).strip()]
    output["source_observations"] = output["source_observations"][:20]
    output["source_links"] = list(dict.fromkeys(str(url).strip() for url in output["source_links"] if str(url).strip()))
    if any(not url.startswith("https://") for url in output["source_links"]):
        raise ValueError("Scenario source links must use HTTPS.")
    if len(output["condition_rules"]) > 20:
        raise ValueError("At most 20 objective condition rules may be saved.")
    for index, rule in enumerate(output["condition_rules"]):
        if not isinstance(rule, dict) or not isinstance(rule.get("metric_ref"), str) or not rule["metric_ref"].strip():
            raise ValueError(f"Objective rule {index + 1} needs a local metric reference.")
        if rule.get("operator") not in CONDITION_OPERATORS:
            raise ValueError(f"Objective rule {index + 1} has an unsupported comparison operator.")
        try:
            rule["threshold"] = float(rule["threshold"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Objective rule {index + 1} needs a numeric threshold.") from exc
        if not rule.get("unit"):
            raise ValueError(f"Objective rule {index + 1} must specify its measurement unit.")
    output["origin_conversation_id"] = str(scenario.get("origin_conversation_id") or "market-wide-main")
    output["origin_message_id"] = scenario.get("origin_message_id")
    return output


def save_approved_market_scenario(scenario: dict, *, scenario_id: str | None = None,
                                  database_path=DEFAULT_DATABASE_PATH) -> dict:
    """Create a scenario or append a revision in one SQLite transaction."""
    clean = validate_scenario(scenario)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scenario_id = str(scenario_id or uuid.uuid4().hex)
    clean["scenario_id"] = scenario_id
    clean["updated_at"] = now
    clean["created_at"] = scenario.get("created_at") or now
    with connect(database_path) as connection:
        existing = connection.execute(
            "SELECT created_at FROM market_scenario_records WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if scenario_id and existing is None and scenario.get("scenario_id"):
            raise ValueError("This scenario no longer exists; create a new draft instead.")
        if existing:
            clean["created_at"] = existing["created_at"]
            revision_number = connection.execute(
                "SELECT COALESCE(MAX(revision_number),0)+1 FROM market_scenario_revisions WHERE scenario_id=?",
                (scenario_id,),
            ).fetchone()[0]
        else:
            revision_number = 1
            connection.execute(
                "INSERT INTO market_scenario_records(scenario_id,status,created_at,updated_at) VALUES (?,?,?,?)",
                (scenario_id, clean["status"], now, now),
            )
        cursor = connection.execute(
            """INSERT INTO market_scenario_revisions
               (scenario_id,revision_number,scenario_json,created_at,source_conversation_id,source_message_id)
               VALUES (?,?,?,?,?,?)""",
            (scenario_id, revision_number, json.dumps(clean, ensure_ascii=False, sort_keys=True), now,
             clean["origin_conversation_id"], clean["origin_message_id"]),
        )
        revision_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE market_scenario_records SET current_revision_id=?,status=?,updated_at=? WHERE scenario_id=?",
            (revision_id, clean["status"], now, scenario_id),
        )
    return {"scenario_id": scenario_id, "revision_id": revision_id,
            "revision_number": revision_number, **clean}


def list_market_scenarios(*, include_archived: bool = True, database_path=DEFAULT_DATABASE_PATH) -> list[dict]:
    query = """SELECT r.scenario_id,r.status,r.created_at,r.updated_at,v.revision_id,
                     v.revision_number,v.scenario_json
              FROM market_scenario_records r JOIN market_scenario_revisions v
                ON v.revision_id=r.current_revision_id"""
    params = ()
    if not include_archived:
        query += " WHERE r.status <> 'Archived'"
    query += " ORDER BY r.updated_at DESC,r.scenario_id"
    with connect(database_path) as connection:
        rows = connection.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item.update(json.loads(item.pop("scenario_json") or "{}"))
        result.append(item)
    return result


def list_market_scenario_revisions(scenario_id: str, *, database_path=DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT revision_id,revision_number,scenario_json,created_at FROM market_scenario_revisions WHERE scenario_id=? ORDER BY revision_number DESC",
            (str(scenario_id),),
        ).fetchall()
    return [{**dict(row), "scenario": json.loads(row["scenario_json"] or "{}")} for row in rows]


def evaluate_scenario_conditions(scenario: dict, snapshot: dict) -> dict:
    """Evaluate only explicit numeric rules against compatible local metrics."""
    try:
        clean = validate_scenario(scenario)
    except ValueError as exc:
        return {"status": "Insufficient observation precision", "results": [], "reason": str(exc)}
    rules = clean.get("condition_rules") or []
    if not rules:
        return {"status": "Requires human interpretation", "results": [],
                "reason": "The saved conditions are narrative and have no explicit numeric rule."}
    sections = (snapshot or {}).get("sections") or {}
    results = []
    for rule in rules:
        reference = rule["metric_ref"]
        if "." not in reference:
            results.append({**rule, "status": "Awaiting data", "reason": "Use a section-qualified snapshot metric ID."})
            continue
        section_id, metric_id = reference.split(".", 1)
        metric = ((sections.get(section_id) or {}).get("metrics") or {}).get(metric_id)
        if not isinstance(metric, dict) or metric.get("value") is None:
            results.append({**rule, "status": "Awaiting data", "reason": "Metric is not present in this snapshot."})
            continue
        if metric.get("status") in {"stale", "unavailable", "insufficient_history"}:
            results.append({**rule, "status": "Awaiting data", "reason": f"Metric status is {metric.get('status')}."})
            continue
        if str(metric.get("unit") or "") != str(rule["unit"]):
            results.append({**rule, "status": "Insufficient observation precision",
                            "reason": f"Rule unit {rule['unit']} does not match snapshot unit {metric.get('unit') or 'unspecified'}."})
            continue
        try:
            actual = float(metric["value"])
            threshold = float(rule["threshold"])
        except (TypeError, ValueError):
            results.append({**rule, "status": "Requires human interpretation", "reason": "The referenced value is not numeric."})
            continue
        op = rule["operator"]
        met = {">": actual > threshold, ">=": actual >= threshold, "<": actual < threshold,
               "<=": actual <= threshold, "==": actual == threshold}[op]
        results.append({**rule, "actual": actual, "as_of": metric.get("as_of"),
                        "status": "Condition met" if met else "Condition not met"})
    states = {result["status"] for result in results}
    if states == {"Condition met"}:
        status = "Condition met"
    elif "Awaiting data" in states:
        status = "Awaiting data"
    elif "Insufficient observation precision" in states:
        status = "Insufficient observation precision"
    elif "Requires human interpretation" in states:
        status = "Requires human interpretation"
    else:
        status = "Condition not met"
    return {"status": status, "results": results, "reason": "Local deterministic metric comparison only; economic hypotheses remain unproven."}

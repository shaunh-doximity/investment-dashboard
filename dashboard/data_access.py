"""Small, explicit database operations used by the UI."""

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

from dashboard.database import DEFAULT_DATABASE_PATH, DEFAULT_MARKET_SYMBOLS, connect


_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,9}$")


def normalize_market_ticker(symbol: str) -> str:
    """Normalize a ticker and reject characters unsuitable for provider symbols."""
    normalized = str(symbol).strip().upper()
    if not _TICKER_PATTERN.fullmatch(normalized):
        raise ValueError("Enter a valid ticker using letters, numbers, dots, or hyphens (up to 10 characters).")
    return normalized


def _default_sector_symbols() -> tuple[str, ...]:
    # Lazy import avoids a module cycle: market_history uses data_access functions.
    from dashboard.market_history import SECTOR_SYMBOLS
    return tuple(SECTOR_SYMBOLS)


def _list_chart_symbols(key: str, defaults: tuple[str, ...], database_path) -> list[str]:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT symbols_json FROM market_chart_preferences WHERE preference_key = ?", (key,)
        ).fetchone()
    if row is None:
        return list(defaults)
    try:
        raw_symbols = json.loads(row["symbols_json"])
    except (TypeError, json.JSONDecodeError):
        return list(defaults)
    if not isinstance(raw_symbols, list):
        return list(defaults)
    try:
        return list(dict.fromkeys(normalize_market_ticker(item) for item in raw_symbols))
    except (TypeError, ValueError):
        return list(defaults)


def _save_chart_symbols(key: str, symbols, database_path) -> list[str]:
    normalized = list(dict.fromkeys(normalize_market_ticker(item) for item in symbols))
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO market_chart_preferences (preference_key, symbols_json, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(preference_key) DO UPDATE SET
                 symbols_json=excluded.symbols_json, updated_at=CURRENT_TIMESTAMP""",
            (key, json.dumps(normalized)),
        )
    return normalized


def list_market_leadership_symbols(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    return _list_chart_symbols("leadership", DEFAULT_MARKET_SYMBOLS, database_path)


def save_market_leadership_symbols(symbols, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    return _save_chart_symbols("leadership", symbols, database_path)


def list_sector_display_symbols(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    return _list_chart_symbols("sector_display", _default_sector_symbols(), database_path)


def save_sector_display_symbols(symbols, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    return _save_chart_symbols("sector_display", symbols, database_path)


def add_chart_symbol(group: str, symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    """Add one ticker to either the leadership or sector display configuration."""
    if group == "leadership":
        current = list_market_leadership_symbols(database_path)
        save = save_market_leadership_symbols
    elif group == "sector_display":
        current = list_sector_display_symbols(database_path)
        save = save_sector_display_symbols
    else:
        raise ValueError("Unknown chart-symbol group.")
    normalized = normalize_market_ticker(symbol)
    if normalized not in current:
        current.append(normalized)
    return save(current, database_path)


def remove_chart_symbol(group: str, symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    if group == "leadership":
        current = list_market_leadership_symbols(database_path)
        save = save_market_leadership_symbols
    elif group == "sector_display":
        current = list_sector_display_symbols(database_path)
        save = save_sector_display_symbols
    else:
        raise ValueError("Unknown chart-symbol group.")
    normalized = normalize_market_ticker(symbol)
    return save([item for item in current if item != normalized], database_path)


def move_chart_symbol(group: str, symbol: str, direction: int, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 (up) or 1 (down)")
    if group == "leadership":
        current, save = list_market_leadership_symbols(database_path), save_market_leadership_symbols
    elif group == "sector_display":
        current, save = list_sector_display_symbols(database_path), save_sector_display_symbols
    else:
        raise ValueError("Unknown chart-symbol group.")
    normalized = normalize_market_ticker(symbol)
    if normalized in current:
        index = current.index(normalized)
        other = index + direction
        if 0 <= other < len(current):
            current[index], current[other] = current[other], current[index]
    return save(current, database_path)


def restore_chart_symbol_defaults(group: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    if group == "leadership":
        return save_market_leadership_symbols(DEFAULT_MARKET_SYMBOLS, database_path)
    if group == "sector_display":
        return save_sector_display_symbols(_default_sector_symbols(), database_path)
    raise ValueError("Unknown chart-symbol group.")


def get_app_preference(key: str, default=None, database_path: str | Path = DEFAULT_DATABASE_PATH):
    """Read one JSON-encoded local UI preference without provider access."""
    with connect(database_path) as connection:
        row = connection.execute("SELECT value FROM app_metadata WHERE key=?", (str(key),)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return default


def save_app_preference(key: str, value, database_path: str | Path = DEFAULT_DATABASE_PATH):
    """Persist a small JSON UI preference; never stores credentials."""
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO app_metadata(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(key), encoded),
        )
    return value


def save_market_regime_transition_record(record: dict, database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    """Insert an immutable transition record; same-session/source reruns are idempotent.

    A changed source fingerprint on the same session is stored as a separate
    revision rather than rewriting the prior calculation.
    """
    as_of = str(record.get("as_of") or "")
    methodology = str(record.get("methodology_version") or "")
    fingerprint = str(record.get("source_fingerprint") or "")
    if not as_of or not methodology or not fingerprint:
        raise ValueError("Transition records require an as-of session, methodology version, and source fingerprint.")
    encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str)
    with connect(database_path) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO market_regime_transition_records
               (as_of_session, methodology_version, source_fingerprint, transition_json)
               VALUES (?, ?, ?, ?)""", (as_of, methodology, fingerprint, encoded))
        inserted = bool(connection.execute("SELECT changes()").fetchone()[0])
        row = connection.execute(
            """SELECT id, as_of_session, methodology_version, source_fingerprint,
                      transition_json, created_at
               FROM market_regime_transition_records
               WHERE as_of_session=? AND methodology_version=? AND source_fingerprint=?""",
            (as_of, methodology, fingerprint)).fetchone()
    if not row:
        raise RuntimeError("Transition record could not be loaded after insert.")
    return {"id": row["id"], "as_of_session": row["as_of_session"],
            "methodology_version": row["methodology_version"],
            "source_fingerprint": row["source_fingerprint"],
            "record": json.loads(row["transition_json"]), "created_at": row["created_at"],
            "inserted": inserted}


def list_market_regime_transition_records(*, limit: int = 100,
                                         database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    """Read immutable transition records, latest session/revision first."""
    limit = max(1, min(int(limit), 1000))
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT id, as_of_session, methodology_version, source_fingerprint,
                      transition_json, created_at
               FROM market_regime_transition_records
               ORDER BY as_of_session DESC, id DESC LIMIT ?""", (limit,)).fetchall()
    return [{"id": row["id"], "as_of_session": row["as_of_session"],
             "methodology_version": row["methodology_version"],
             "source_fingerprint": row["source_fingerprint"],
             "record": json.loads(row["transition_json"]), "created_at": row["created_at"]}
            for row in rows]


def list_market_explorer_favorites(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    value = get_app_preference("market_explorer_favorites_v1", [], database_path)
    return value if isinstance(value, list) else []


def save_market_explorer_favorite(first: str, second: str, window: int, mode: str,
                                  database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    from dashboard.market_explorer import COMPARISON_UNIVERSE, COMPARISON_WINDOWS
    first, second = normalize_market_ticker(first), normalize_market_ticker(second)
    if first not in COMPARISON_UNIVERSE or second not in COMPARISON_UNIVERSE or first == second:
        raise ValueError("Favorite pairs must contain two different supported ETFs.")
    if int(window) not in COMPARISON_WINDOWS or mode not in {"excess", "relative_ratio", "two_line"}:
        raise ValueError("Favorite comparison settings are invalid.")
    favorites = list_market_explorer_favorites(database_path)
    favorite = {"first": first, "second": second, "window": int(window), "mode": mode}
    favorites = [item for item in favorites if not (item.get("first") == first and item.get("second") == second
                 and item.get("window") == int(window) and item.get("mode") == mode)]
    favorites.append(favorite)
    save_app_preference("market_explorer_favorites_v1", favorites, database_path)
    return favorites


def remove_market_explorer_favorite(index: int, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    favorites = list_market_explorer_favorites(database_path)
    if 0 <= int(index) < len(favorites):
        favorites.pop(int(index))
    save_app_preference("market_explorer_favorites_v1", favorites, database_path)
    return favorites


def save_deepseek_market_research_report(report: dict, database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO deepseek_market_research_reports
               (cache_key,topic,snapshot_json,generated_at,model,report_text,sources_json,usage_json,web_search_succeeded,research_status,error_message,completion_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
               generated_at=excluded.generated_at,model=excluded.model,report_text=excluded.report_text,
               sources_json=excluded.sources_json,usage_json=excluded.usage_json,
               web_search_succeeded=excluded.web_search_succeeded,research_status=excluded.research_status,
               error_message=excluded.error_message,completion_reason=excluded.completion_reason""",
            (report["cache_key"], report["topic"], json.dumps(report["snapshot"], ensure_ascii=False),
             report["generated_at"], report["model"], report["report_text"],
             json.dumps(report.get("sources", []), ensure_ascii=False),
             json.dumps(report.get("usage", {}), ensure_ascii=False), int(bool(report.get("web_search_succeeded"))),
             report.get("research_status", "complete"), report.get("error_message", ""), report.get("completion_reason", "")),
        )
        row = connection.execute("SELECT id FROM deepseek_market_research_reports WHERE cache_key=?", (report["cache_key"],)).fetchone()
    return int(row["id"])


def _decode_deepseek_market_report(row):
    if not row:
        return None
    item = dict(row)
    for column, key, fallback in (("snapshot_json", "snapshot", {}), ("sources_json", "sources", []), ("usage_json", "usage", {})):
        try:
            item[key] = json.loads(item.pop(column))
        except (TypeError, json.JSONDecodeError):
            item[key] = fallback
    item["web_search_succeeded"] = bool(item["web_search_succeeded"])
    return item


def get_deepseek_market_research_by_key(cache_key: str, database_path: str | Path = DEFAULT_DATABASE_PATH):
    with connect(database_path) as connection:
        row = connection.execute("SELECT * FROM deepseek_market_research_reports WHERE cache_key=?", (cache_key,)).fetchone()
    return _decode_deepseek_market_report(row)


def get_latest_deepseek_market_research(topic: str | None = None, database_path: str | Path = DEFAULT_DATABASE_PATH):
    query, params = "SELECT * FROM deepseek_market_research_reports", []
    if topic is not None:
        query += " WHERE topic=?"
        params.append(topic)
    query += " ORDER BY generated_at DESC,id DESC LIMIT 1"
    with connect(database_path) as connection:
        row = connection.execute(query, params).fetchone()
    return _decode_deepseek_market_report(row)


def get_latest_deepseek_market_research_with_sources(database_path: str | Path = DEFAULT_DATABASE_PATH):
    """Return the newest locally cached report containing reusable search sources."""
    with connect(database_path) as connection:
        row = connection.execute(
            """SELECT * FROM deepseek_market_research_reports
               WHERE sources_json IS NOT NULL AND sources_json NOT IN ('', '[]', 'null')
               ORDER BY generated_at DESC,id DESC LIMIT 1"""
        ).fetchone()
    return _decode_deepseek_market_report(row)


def get_latest_completed_deepseek_market_research(topic: str | None = None,
                                                  database_path: str | Path = DEFAULT_DATABASE_PATH):
    """Read the last nonempty completed report, even when a newer retry failed."""
    query = """SELECT * FROM deepseek_market_research_reports
               WHERE research_status IN ('complete','complete_cost_warning')
                 AND report_text IS NOT NULL AND trim(report_text) <> ''"""
    params = []
    if topic is not None:
        query += " AND topic=?"
        params.append(topic)
    query += " ORDER BY generated_at DESC,id DESC LIMIT 1"
    with connect(database_path) as connection:
        row = connection.execute(query, params).fetchone()
    return _decode_deepseek_market_report(row)


def list_deepseek_market_research_reports(limit: int = 100, database_path: str | Path = DEFAULT_DATABASE_PATH):
    """Read recent saved report snapshots for local history comparison and highlights."""
    limit = max(1, min(int(limit), 500))
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM deepseek_market_research_reports ORDER BY generated_at DESC,id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_decode_deepseek_market_report(row) for row in rows]


def record_deepseek_market_research_usage(*, requested_at: str, model: str, search_requests: int,
                                          input_tokens: int | None, output_tokens: int | None,
                                          estimated_cost_usd: float, status: str,
                                          database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO deepseek_market_research_usage
               (requested_at,model,search_requests,input_tokens,output_tokens,estimated_cost_usd,status)
               VALUES (?,?,?,?,?,?,?)""",
            (requested_at, model, int(search_requests), input_tokens, output_tokens, float(estimated_cost_usd), status),
        )


def record_market_ai_request_audit(*, request_id: str, requested_at: str, purpose: str,
                                  evidence_mode: str,
                                  requested_model: str, actual_model: str | None, endpoint: str,
                                  reasoning_effort: str | None, output_token_allowance: int | None,
                                  input_tokens: int | None, output_tokens: int | None,
                                  reasoning_tokens: int | None, visible_output_tokens: int | None,
                                  search_requests: int, estimated_cost_usd: float,
                                  reported_cost_usd: float | None, completion_status: str,
                                  error_category: str = "", database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    """Persist model/request diagnostics without storing prompts or sensitive context."""
    with connect(database_path) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO market_ai_request_audit
               (request_id,requested_at,purpose,evidence_mode,requested_model,actual_model,endpoint,reasoning_effort,
                output_token_allowance,input_tokens,output_tokens,reasoning_tokens,visible_output_tokens,
                search_requests,estimated_cost_usd,reported_cost_usd,completion_status,error_category)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (request_id, requested_at, purpose, evidence_mode, requested_model, actual_model, endpoint,
             reasoning_effort, output_token_allowance, input_tokens, output_tokens, reasoning_tokens,
             visible_output_tokens, int(search_requests), float(estimated_cost_usd), reported_cost_usd,
             completion_status, error_category),
        )


def list_market_ai_request_audit(*, limit: int = 50,
                                 database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    """Read bounded AI request diagnostics without making any network requests."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM market_ai_request_audit ORDER BY requested_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def deepseek_market_research_daily_spend(*, day_utc: str | None = None,
                                         database_path: str | Path = DEFAULT_DATABASE_PATH) -> float:
    from datetime import datetime, timezone
    day = day_utc or datetime.now(timezone.utc).date().isoformat()
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd),0) total FROM deepseek_market_research_usage WHERE substr(requested_at,1,10)=?",
            (day,),
        ).fetchone()
    return float(row["total"] or 0)


def list_market_section_chat(section_id: str, snapshot_fingerprint: str, *, limit: int = 60,
                             database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    """Load one section/snapshot conversation; no market data is fetched."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT * FROM market_section_chat_messages
               WHERE section_id=? AND snapshot_fingerprint=? ORDER BY id DESC LIMIT ?""",
            (str(section_id), str(snapshot_fingerprint), max(1, min(int(limit), 100))),
        ).fetchall()
    output = []
    for row in reversed(rows):
        item = dict(row)
        try:
            item["source_metadata"] = json.loads(item.pop("source_metadata_json"))
        except (TypeError, json.JSONDecodeError):
            item["source_metadata"] = []
        output.append(item)
    return output


def latest_market_section_chat_context(section_id: str,
                                      database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict | None:
    """Return context metadata for the most recent turn, for stale-chat warnings."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT snapshot_fingerprint,created_at FROM market_section_chat_messages WHERE section_id=? ORDER BY id DESC LIMIT 1",
            (str(section_id),),
        ).fetchone()
    return dict(row) if row else None


def save_market_section_chat_message(*, section_id: str, snapshot_fingerprint: str,
                                     request_id: str | None, role: str, content: str,
                                     model: str = "", report_id: int | None = None,
                                     source_metadata=(), input_tokens: int | None = None,
                                     output_tokens: int | None = None, cost_usd: float | None = None,
                                     cost_kind: str = "estimate",
                                     retention: int = 60,
                                     database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    """Append one bounded chat turn message, isolated by section and snapshot."""
    if role not in {"user", "assistant"} or not str(content).strip():
        raise ValueError("Chat messages need a valid role and non-empty content.")
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(database_path) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO market_section_chat_messages
               (section_id,snapshot_fingerprint,report_id,request_id,role,content,created_at,model,
                source_metadata_json,input_tokens,output_tokens,cost_usd,cost_kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(section_id), str(snapshot_fingerprint), report_id, request_id, role, str(content), created,
             str(model), json.dumps(list(source_metadata or ()), ensure_ascii=False), input_tokens, output_tokens,
             cost_usd, str(cost_kind)),
        )
        row = connection.execute(
            "SELECT id FROM market_section_chat_messages WHERE section_id=? AND snapshot_fingerprint=? AND role=? AND request_id IS ? ORDER BY id DESC LIMIT 1",
            (str(section_id), str(snapshot_fingerprint), role, request_id),
        ).fetchone()
        keep = max(2, min(int(retention), 100))
        connection.execute(
            """DELETE FROM market_section_chat_messages WHERE section_id=? AND snapshot_fingerprint=?
               AND id NOT IN (SELECT id FROM market_section_chat_messages WHERE section_id=? AND snapshot_fingerprint=? ORDER BY id DESC LIMIT ?)""",
            (str(section_id), str(snapshot_fingerprint), str(section_id), str(snapshot_fingerprint), keep),
        )
    return int(row["id"]) if row else 0


def clear_market_section_chat(section_id: str, snapshot_fingerprint: str,
                              database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM market_section_chat_messages WHERE section_id=? AND snapshot_fingerprint=?",
            (str(section_id), str(snapshot_fingerprint)),
        )
    return int(cursor.rowcount)


def list_market_section_chat_groups(*, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    """List saved chat histories by section and frozen context without returning message text."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT section_id,snapshot_fingerprint,COUNT(*) AS message_count,
                      MIN(created_at) AS first_message_at,MAX(created_at) AS last_message_at,
                      GROUP_CONCAT(DISTINCT model) AS models
               FROM market_section_chat_messages
               GROUP BY section_id,snapshot_fingerprint
               ORDER BY MAX(id) DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def clear_market_section_chat_groups(groups, *, database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    """Delete selected section/context histories atomically; unrelated chats are preserved."""
    normalized = list(dict.fromkeys((str(section), str(fingerprint)) for section, fingerprint in (groups or [])
                                    if str(section).strip() and str(fingerprint).strip()))
    if not normalized:
        return 0
    deleted = 0
    with connect(database_path) as connection:
        for section, fingerprint in normalized:
            cursor = connection.execute(
                "DELETE FROM market_section_chat_messages WHERE section_id=? AND snapshot_fingerprint=?",
                (section, fingerprint),
            )
            deleted += max(0, int(cursor.rowcount))
    return deleted


def upsert_treasury_yields(rows: list[dict], database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    if not rows:
        return 0
    with connect(database_path) as connection:
        connection.executemany(
            """INSERT INTO treasury_yield_history
               (observation_date,two_year,ten_year,provider,retrieved_at)
               VALUES (?,?,?,?,?) ON CONFLICT(observation_date) DO UPDATE SET
               two_year=excluded.two_year,ten_year=excluded.ten_year,
               provider=excluded.provider,retrieved_at=excluded.retrieved_at""",
            [(row["observation_date"], float(row["two_year"]), float(row["ten_year"]),
              row.get("provider", "us_treasury_xml"), row["retrieved_at"]) for row in rows],
        )
    return len(rows)


def list_treasury_yields(start_date: str | None = None, end_date: str | None = None,
                         database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    clauses, params = [], []
    if start_date:
        clauses.append("observation_date>=?")
        params.append(start_date)
    if end_date:
        clauses.append("observation_date<=?")
        params.append(end_date)
    query = "SELECT * FROM treasury_yield_history"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY observation_date"
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def list_holdings(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM holdings ORDER BY symbol").fetchall()
    return [dict(row) for row in rows]


def list_portfolio_position_order(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    """Read custom order, append new holdings, and prune removed tickers.

    The initial order is deterministic alphabetical order. Reconciliation only
    touches the separate ordering table; portfolio position records are read-only.
    """
    symbols = [row["symbol"].upper() for row in list_holdings(database_path)]
    active = set(symbols)
    with connect(database_path) as connection:
        rows = connection.execute("SELECT symbol,sort_order FROM portfolio_position_order ORDER BY sort_order").fetchall()
        if not rows:
            ordered = sorted(symbols)
            connection.executemany(
                "INSERT INTO portfolio_position_order(symbol,sort_order) VALUES (?,?)",
                [(symbol, index) for index, symbol in enumerate(ordered)],
            )
        else:
            known = {row["symbol"].upper() for row in rows}
            for removed in known - active:
                connection.execute("DELETE FROM portfolio_position_order WHERE symbol=?", (removed,))
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order),-1)+1 FROM portfolio_position_order"
            ).fetchone()[0]
            for symbol in symbols:
                if symbol not in known:
                    connection.execute(
                        "INSERT INTO portfolio_position_order(symbol,sort_order) VALUES (?,?)",
                        (symbol, next_order),
                    )
                    next_order += 1
        result = connection.execute(
            "SELECT symbol FROM portfolio_position_order ORDER BY sort_order"
        ).fetchall()
    return [row["symbol"].upper() for row in result]


def save_portfolio_position_order(symbols, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    """Persist a complete permutation of current holdings, without modifying them."""
    normalized = [normalize_market_ticker(symbol) for symbol in symbols]
    if len(normalized) != len(set(normalized)):
        raise ValueError("A ticker may appear only once in custom position order.")
    active = {row["symbol"].upper() for row in list_holdings(database_path)}
    if set(normalized) != active:
        raise ValueError("Custom order must contain every current holding exactly once.")
    list_portfolio_position_order(database_path)
    with connect(database_path) as connection:
        connection.execute("UPDATE portfolio_position_order SET sort_order=-sort_order-1")
        connection.executemany(
            "UPDATE portfolio_position_order SET sort_order=? WHERE symbol=?",
            [(index, symbol) for index, symbol in enumerate(normalized)],
        )
    return normalized


def move_portfolio_position(symbol: str, direction: int,
                            database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    if direction not in (-1, 1):
        raise ValueError("Direction must be -1 or 1.")
    order = list_portfolio_position_order(database_path)
    normalized = normalize_market_ticker(symbol)
    if normalized in order:
        index = order.index(normalized)
        other = index + direction
        if 0 <= other < len(order):
            order[index], order[other] = order[other], order[index]
            save_portfolio_position_order(order, database_path)
    return order


def list_portfolio_sector_classifications(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, str]:
    """Return only user-entered classifications; absent symbols stay unclassified."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT symbol, sector FROM portfolio_sector_classifications ORDER BY symbol"
        ).fetchall()
    return {row["symbol"].upper(): row["sector"] for row in rows}


def list_portfolio_classification_records(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, dict]:
    """Return full local classification metadata keyed by normalized ticker."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM portfolio_sector_classifications ORDER BY symbol"
        ).fetchall()
    return {row["symbol"].upper(): dict(row) for row in rows}


def ensure_portfolio_classifications(symbols, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    """Add conservative local suggestions only for symbols without a saved row."""
    from dashboard.sector_classification import local_classification
    with connect(database_path) as connection:
        for symbol in dict.fromkeys(str(item).strip().upper() for item in symbols if str(item).strip()):
            suggestion = local_classification(symbol)
            connection.execute(
                """INSERT OR IGNORE INTO portfolio_sector_classifications
                   (symbol, sector, asset_type, sector_benchmark, benchmark_type,
                    classification_source, verification_status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (symbol, suggestion["sector"], suggestion["asset_type"],
                 suggestion["sector_benchmark"], suggestion["benchmark_type"],
                 suggestion["classification_source"], suggestion["verification_status"]),
            )


def save_portfolio_classification_records(records, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    """Save the editable multi-position classification table in one local transaction."""
    from dashboard.sector_classification import (
        ASSET_TYPES, BENCHMARK_TYPES, CLASSIFICATION_SOURCES,
        VERIFICATION_STATUSES,
    )
    with connect(database_path) as connection:
        for record in records:
            symbol = normalize_market_ticker(record.get("symbol", ""))
            sector = str(record.get("sector", "")).strip()
            asset = record.get("asset_type")
            benchmark_text = str(record.get("sector_benchmark") or "").strip()
            benchmark = normalize_market_ticker(benchmark_text) if benchmark_text else ""
            benchmark_type = record.get("benchmark_type")
            source = record.get("classification_source")
            verification = record.get("verification_status")
            if asset not in ASSET_TYPES or benchmark_type not in BENCHMARK_TYPES:
                raise ValueError(f"Invalid classification options for {symbol}.")
            if source not in CLASSIFICATION_SOURCES or verification not in VERIFICATION_STATUSES:
                raise ValueError(f"Invalid classification status for {symbol}.")
            if not sector or len(sector) > 100:
                raise ValueError(f"Sector classification for {symbol} must be a short label.")
            connection.execute(
                """INSERT INTO portfolio_sector_classifications
                   (symbol, sector, asset_type, sector_benchmark, benchmark_type,
                    classification_source, verification_status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(symbol) DO UPDATE SET
                    sector=excluded.sector, asset_type=excluded.asset_type,
                    sector_benchmark=excluded.sector_benchmark,
                    benchmark_type=excluded.benchmark_type,
                    classification_source=excluded.classification_source,
                    verification_status=excluded.verification_status,
                    updated_at=CURRENT_TIMESTAMP""",
                (symbol, sector or "Other / unclassified", asset, benchmark,
                 benchmark_type, source, verification),
            )


def save_portfolio_sector_classification(
    symbol: str, sector: str, database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> None:
    normalized_symbol = symbol.strip().upper()
    normalized_sector = sector.strip()
    if not normalized_symbol or not normalized_sector:
        raise ValueError("Ticker and sector classification are required.")
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO portfolio_sector_classifications
                   (symbol, sector, asset_type, classification_source, verification_status, updated_at)
               VALUES (?, ?, 'Other', 'Manual', 'Unverified', CURRENT_TIMESTAMP)
               ON CONFLICT(symbol) DO UPDATE SET
                 sector=excluded.sector, classification_source='Manual', updated_at=CURRENT_TIMESTAMP""",
            (normalized_symbol, normalized_sector),
        )


def save_holding(
    symbol: str,
    shares: float,
    average_cost: float,
    current_price: float = 0,
    daily_change_per_share: float = 0,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    investment_horizon: str = "Medium",
    thesis_status: str = "Active",
) -> None:
    normalized_symbol = symbol.strip().upper()
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO holdings
               (symbol, shares, average_cost, current_price, daily_change_per_share,
                investment_horizon, thesis_status)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 shares=excluded.shares, average_cost=excluded.average_cost,
                 current_price=excluded.current_price,
                 daily_change_per_share=excluded.daily_change_per_share,
                 investment_horizon=excluded.investment_horizon,
                 thesis_status=excluded.thesis_status,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                normalized_symbol, shares, average_cost, current_price,
                daily_change_per_share, investment_horizon, thesis_status,
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) VALUES (?)",
            (normalized_symbol,),
        )
    strategy = get_strategy_record(normalized_symbol, database_path)
    if strategy and (
        strategy["investment_horizon"] != investment_horizon
        or strategy["thesis_status"] != thesis_status
    ):
        strategy["investment_horizon"] = investment_horizon
        strategy["thesis_status"] = thesis_status
        save_strategy_record(normalized_symbol, strategy, database_path)
    list_portfolio_position_order(database_path)


def get_cash_balance(database_path: str | Path = DEFAULT_DATABASE_PATH) -> float:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT cash_balance FROM portfolio_settings WHERE id = 1"
        ).fetchone()
    return float(row["cash_balance"]) if row else 0.0


def save_cash_balance(cash_balance: float, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO portfolio_settings (id, cash_balance) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET cash_balance=excluded.cash_balance""",
            (cash_balance,),
        )


def get_portfolio_strategy_horizon(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> str | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT active_horizon FROM portfolio_strategy_preferences WHERE symbol=?",
            (symbol.strip().upper(),),
        ).fetchone()
    return str(row["active_horizon"]) if row else None


def save_portfolio_strategy_horizon(symbol: str, horizon: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    if horizon not in {"Swing / short-term", "Medium-term", "Long-term"}:
        raise ValueError("Choose a saved approved strategy horizon.")
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO portfolio_strategy_preferences(symbol,active_horizon,updated_at)
               VALUES (?,?,CURRENT_TIMESTAMP) ON CONFLICT(symbol) DO UPDATE SET
               active_horizon=excluded.active_horizon,updated_at=CURRENT_TIMESTAMP""",
            (symbol.strip().upper(), horizon),
        )


def get_portfolio_monitor_settings(database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT stop_near_pct,target_near_pct,remaining_rrr_below FROM portfolio_monitor_settings WHERE id=1"
        ).fetchone()
    return dict(row) if row else {"stop_near_pct": 3.0, "target_near_pct": 3.0, "remaining_rrr_below": 1.0}


def save_portfolio_monitor_settings(stop_near_pct: float, target_near_pct: float,
                                    remaining_rrr_below: float,
                                    database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    values = tuple(float(value) for value in (stop_near_pct, target_near_pct, remaining_rrr_below))
    if any(value < 0 for value in values):
        raise ValueError("Alert thresholds must be zero or greater.")
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO portfolio_monitor_settings(id,stop_near_pct,target_near_pct,remaining_rrr_below)
               VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET stop_near_pct=excluded.stop_near_pct,
               target_near_pct=excluded.target_near_pct,remaining_rrr_below=excluded.remaining_rrr_below""",
            values,
        )


def get_cached_quote(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM quote_cache WHERE symbol = ?", (symbol.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def list_cached_quotes(database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict[str, dict]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM quote_cache").fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def save_cached_quote(quote: dict, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO quote_cache
               (symbol, price, change, change_percent, quote_date, fetched_at, freshness)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,
                 change=excluded.change, change_percent=excluded.change_percent,
                 quote_date=excluded.quote_date, fetched_at=excluded.fetched_at,
                 freshness=excluded.freshness""",
            (
                quote["symbol"], quote["price"], quote["change"],
                quote["change_percent"], quote["quote_date"], quote["fetched_at"],
                quote["freshness"],
            ),
        )


def quote_is_cached_recently(
    symbol: str,
    max_age_hours: int = 20,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> bool:
    quote = get_cached_quote(symbol, database_path)
    if not quote:
        return False
    try:
        fetched = datetime.fromisoformat(quote["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds() / 3600
        return 0 <= age_hours < max_age_hours
    except (TypeError, ValueError):
        return False


def record_api_request_if_under_limit(
    daily_limit: int = 25, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> tuple[bool, int]:
    """Count this app's calls and atomically enforce its daily request budget."""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with connect(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM api_request_log WHERE requested_at >= ?",
            (day_start,),
        ).fetchone()["count"]
        if count >= daily_limit:
            return False, int(count)
        connection.execute(
            "INSERT INTO api_request_log (requested_at) VALUES (?)", (now.isoformat(),)
        )
        return True, int(count) + 1


def api_requests_used_today(database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with connect(database_path) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) AS count FROM api_request_log WHERE requested_at >= ?",
            (day_start,),
        ).fetchone()["count"])


def seconds_until_next_api_request(
    minimum_interval_seconds: int = 12, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> float:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT requested_at FROM api_request_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return 0.0
    previous = datetime.fromisoformat(row["requested_at"])
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - previous.astimezone(timezone.utc)).total_seconds()
    return max(0.0, minimum_interval_seconds - elapsed)


def delete_holding(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    with connect(database_path) as connection:
        connection.execute("DELETE FROM holdings WHERE symbol = ?", (symbol,))
    list_portfolio_position_order(database_path)


def list_watchlist(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM watchlist ORDER BY symbol").fetchall()
    return [dict(row) for row in rows]


def add_watchlist_item(
    symbol: str, company_name: str = "", database_path: str | Path = DEFAULT_DATABASE_PATH
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO watchlist (symbol, company_name) VALUES (?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET company_name=excluded.company_name",
            (symbol.strip().upper(), company_name.strip()),
        )
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) VALUES (?)",
            (symbol.strip().upper(),),
        )


def delete_watchlist_item(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    with connect(database_path) as connection:
        connection.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))


def get_research_note(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM research_notes WHERE symbol = ?", (symbol.strip().upper(),)
        ).fetchone()
    return dict(row) if row else {}


def list_research_symbols(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT symbol FROM research_notes ORDER BY symbol").fetchall()
    return [row["symbol"] for row in rows]


POSITION_PLAN_POLICY_DEFAULTS = {
    "max_position_allocation": 0.15,
    "target_position_allocation": 0.08,
    "max_loss_per_position": 0.01,
    "minimum_cash_reserve": 0.0,
    "max_sector_allocation": 0.30,
    "rrr_monitoring_threshold": 1.0,
}


def get_position_planner_policy(database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    with connect(database_path) as connection:
        row = connection.execute("SELECT * FROM position_planner_policy WHERE id=1").fetchone()
    return dict(row) if row else dict(POSITION_PLAN_POLICY_DEFAULTS)


def save_position_planner_policy(policy: dict, database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    clean = {}
    for key, default in POSITION_PLAN_POLICY_DEFAULTS.items():
        value = float(policy.get(key, default))
        if not math.isfinite(value) or value < 0 or (key != "minimum_cash_reserve" and key != "rrr_monitoring_threshold" and value > 1):
            raise ValueError(f"{key} is outside its permitted range.")
        clean[key] = value
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO position_planner_policy
               (id,max_position_allocation,target_position_allocation,max_loss_per_position,
                minimum_cash_reserve,max_sector_allocation,rrr_monitoring_threshold,updated_at)
               VALUES (1,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET max_position_allocation=excluded.max_position_allocation,
                target_position_allocation=excluded.target_position_allocation,
                max_loss_per_position=excluded.max_loss_per_position,
                minimum_cash_reserve=excluded.minimum_cash_reserve,
                max_sector_allocation=excluded.max_sector_allocation,
                rrr_monitoring_threshold=excluded.rrr_monitoring_threshold,
                updated_at=CURRENT_TIMESTAMP""",
            tuple(clean[key] for key in POSITION_PLAN_POLICY_DEFAULTS),
        )
    return clean


def save_position_plan(symbol: str, horizon: str, plan: dict, *,
                       strategy_revision_id: int | None = None,
                       database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    """Append a user-approved planner revision tied to the current strategy revision."""
    normalized = normalize_market_ticker(symbol)
    if horizon not in {"Swing / short-term", "Medium-term", "Long-term"}:
        raise ValueError("Choose a supported approved strategy horizon.")
    if strategy_revision_id is None:
        raise ValueError("A position plan must be tied to an approved strategy revision.")
    if not isinstance(plan, dict):
        raise ValueError("Position plan must be an object.")
    if plan.get("schema_version") != 1:
        raise ValueError("Position plan schema_version must be 1.")
    if plan.get("position_type", "long_equity") not in {"long_equity", "long_etf"}:
        raise ValueError("Only long stock and ETF plans can be saved.")
    for field in ("target_allocation", "max_allocation", "max_loss_per_position", "max_sector_allocation"):
        value = plan.get(field)
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1):
            raise ValueError(f"{field} must be a fraction from 0 to 1.")
    for field in ("entry_tranches", "exit_tranches"):
        if not isinstance(plan.get(field, []), list):
            raise ValueError(f"{field} must be a list.")
    entry = float(plan.get("entry_reference") or 0)
    stop = plan.get("stop_price")
    target = plan.get("target_reference")
    if entry <= 0:
        raise ValueError("Position plan requires a positive, explicitly selected entry reference.")
    if stop is not None and float(stop) >= entry:
        raise ValueError("For a long plan, stop/invalidation must be below the selected entry reference.")
    if target is not None and float(target) <= entry:
        raise ValueError("For a long plan, target must be above the selected entry reference.")
    fraction_sum = 0.0
    for index, row in enumerate(plan.get("entry_tranches", [])):
        if not isinstance(row, dict):
            raise ValueError(f"entry_tranches[{index}] must be an object.")
        fraction = float(row.get("fraction"))
        if not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError(f"entry_tranches[{index}].fraction must be between 0 and 1.")
        trigger = row.get("trigger_price")
        if trigger is None and row.get("status", "planned") in {"canceled", "skipped", "filled"}:
            pass
        elif trigger is None or not math.isfinite(float(trigger)) or float(trigger) <= 0:
            raise ValueError(f"entry_tranches[{index}].trigger_price must be a positive, explicitly selected price.")
        fraction_sum += fraction
        if row.get("status", "planned") not in {"planned", "eligible", "canceled", "skipped", "blocked"}:
            raise ValueError(f"entry_tranches[{index}].status is not supported.")
    if fraction_sum > 1 + 1e-9:
        raise ValueError("Entry tranche fractions cannot exceed 100% of intended position.")
    for index, row in enumerate(plan.get("exit_tranches", [])):
        if not isinstance(row, dict):
            raise ValueError(f"exit_tranches[{index}] must be an object.")
        percent = row.get("percent")
        if percent is not None and (not math.isfinite(float(percent)) or not 0 <= float(percent) <= 1):
            raise ValueError(f"exit_tranches[{index}].percent must be between 0 and 1.")
        specified = int(row.get("shares") is not None) + int(percent is not None) + int(bool(row.get("sell_all", False)))
        if specified != 1 and row.get("status", "planned") not in {"canceled", "skipped"}:
            raise ValueError(f"exit_tranches[{index}] requires exactly one of shares, percent, or sell_all=true.")
        trigger = row.get("trigger_price")
        if trigger is not None and (not math.isfinite(float(trigger)) or float(trigger) <= 0):
            raise ValueError(f"exit_tranches[{index}].trigger_price must be positive or null.")
        if row.get("percent_basis", "remaining") not in {"original", "remaining"}:
            raise ValueError(f"exit_tranches[{index}].percent_basis must be original or remaining.")
        if row.get("status", "planned") not in {"planned", "eligible", "canceled", "skipped"}:
            raise ValueError(f"exit_tranches[{index}].status is not supported.")
    payload = json.dumps(plan, sort_keys=True, allow_nan=False)
    with connect(database_path) as connection:
        approved_revision = connection.execute(
            "SELECT 1 FROM strategy_revisions WHERE id=? AND symbol=?",
            (int(strategy_revision_id), normalized),
        ).fetchone()
        if not approved_revision:
            raise ValueError("The linked approved strategy revision does not belong to this ticker.")
        cursor = connection.execute(
            """INSERT INTO position_plan_revisions
               (symbol,strategy_revision_id,horizon,plan_json)
               VALUES (?,?,?,?)""",
            (normalized, strategy_revision_id, horizon, payload),
        )
        return int(cursor.lastrowid)


def get_position_plan(symbol: str, horizon: str | None = None,
                      database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict | None:
    normalized = normalize_market_ticker(symbol)
    with connect(database_path) as connection:
        if horizon:
            row = connection.execute(
                """SELECT * FROM position_plan_revisions WHERE symbol=? AND horizon=?
                   ORDER BY approved_at DESC,id DESC LIMIT 1""", (normalized, horizon),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT p.* FROM position_plan_revisions p
                   WHERE p.symbol=? AND p.id=(SELECT p2.id FROM position_plan_revisions p2
                     WHERE p2.symbol=p.symbol ORDER BY p2.approved_at DESC,p2.id DESC LIMIT 1)""",
                (normalized,),
            ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["plan"] = json.loads(result.pop("plan_json"))
    except (TypeError, json.JSONDecodeError):
        result["plan"] = {}
    return result


def list_position_plan_revisions(symbol: str, horizon: str | None = None,
                                 database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    normalized = normalize_market_ticker(symbol)
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM position_plan_revisions WHERE symbol=?" +
            (" AND horizon=?" if horizon else "") + " ORDER BY approved_at DESC,id DESC",
            (normalized, horizon) if horizon else (normalized,),
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        try:
            item["plan"] = json.loads(item.pop("plan_json"))
        except (TypeError, json.JSONDecodeError):
            item["plan"] = {}
        output.append(item)
    return output


def latest_strategy_revision_id(symbol: str,
                                database_path: str | Path = DEFAULT_DATABASE_PATH) -> int | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT id FROM strategy_revisions WHERE symbol=? ORDER BY recorded_at DESC,id DESC LIMIT 1",
            (normalize_market_ticker(symbol),),
        ).fetchone()
    return int(row["id"]) if row else None


def record_position_plan_execution(plan_revision_id: int, symbol: str, tranche_id: str,
                                   action: str, filled_shares: float, fill_price: float,
                                   filled_at: str, notes: str = "", *, confirmed: bool = False,
                                   database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    """Append an explicitly confirmed fill record; does not edit holdings or cash."""
    if not confirmed:
        raise ValueError("Confirm that this is an actual executed fill before recording it.")
    if action not in {"buy", "sell"}:
        raise ValueError("Execution action must be buy or sell.")
    quantity, price = float(filled_shares), float(fill_price)
    if not math.isfinite(quantity) or not math.isfinite(price) or quantity <= 0 or price <= 0:
        raise ValueError("Actual filled shares and fill price must be positive.")
    try:
        date.fromisoformat(filled_at)
    except (TypeError, ValueError) as error:
        raise ValueError("Fill date must be an ISO calendar date (YYYY-MM-DD).") from error
    normalized = normalize_market_ticker(symbol)
    with connect(database_path) as connection:
        exists = connection.execute("SELECT plan_json FROM position_plan_revisions WHERE id=? AND symbol=?",
                                    (int(plan_revision_id), normalized)).fetchone()
        if not exists:
            raise ValueError("Approved position-plan revision was not found for this ticker.")
        plan = json.loads(exists["plan_json"])
        tranche_list = plan.get("entry_tranches" if action == "buy" else "exit_tranches", [])
        if not any(str(row.get("id") or f"{action}-{index + 1}") == str(tranche_id)
                   and row.get("status", "planned") not in {"canceled", "skipped"}
                   for index, row in enumerate(tranche_list)):
            raise ValueError("The selected active tranche was not found in this approved plan revision.")
        cursor = connection.execute(
            """INSERT INTO position_plan_executions
               (plan_revision_id,symbol,tranche_id,action,filled_shares,fill_price,filled_at,notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (int(plan_revision_id), normalized, str(tranche_id), action, quantity, price,
             filled_at, str(notes or "")),
        )
        return int(cursor.lastrowid)


def list_position_plan_executions(symbol: str,
                                  database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM position_plan_executions WHERE symbol=? ORDER BY filled_at DESC,id DESC",
            (normalize_market_ticker(symbol),),
        ).fetchall()
    return [dict(row) for row in rows]


def save_research_note(
    symbol: str,
    thesis: str,
    catalysts: str,
    valuation: str,
    entry_zone: str,
    invalidation: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO research_notes
               (symbol, thesis, catalysts, valuation, entry_zone, invalidation)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET thesis=excluded.thesis,
                 catalysts=excluded.catalysts, valuation=excluded.valuation,
                 entry_zone=excluded.entry_zone, invalidation=excluded.invalidation,
                 updated_at=CURRENT_TIMESTAMP""",
            (symbol.strip().upper(), thesis, catalysts, valuation, entry_zone, invalidation),
        )


def list_risk_rules(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM risk_rules ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def add_risk_rule(
    name: str, description: str, threshold: float, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO risk_rules (name, description, threshold) VALUES (?, ?, ?)",
            (name.strip(), description.strip(), threshold),
        )


def delete_risk_rule(rule_id: int, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    with connect(database_path) as connection:
        connection.execute("DELETE FROM risk_rules WHERE id = ?", (rule_id,))


def list_market_symbols(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT symbol FROM market_symbols ORDER BY sort_order"
        ).fetchall()
    return [row["symbol"] for row in rows]


def add_market_symbol(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    normalized = symbol.strip().upper()
    with connect(database_path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM market_symbols WHERE symbol = ?", (normalized,)
        ).fetchone()
        if exists:
            return
        next_order = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM market_symbols"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO market_symbols (symbol, sort_order) VALUES (?, ?)",
            (normalized, next_order),
        )


def remove_market_symbol(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    with connect(database_path) as connection:
        connection.execute("DELETE FROM market_symbols WHERE symbol = ?", (symbol.strip().upper(),))
        _renumber_market_symbols(connection)


def move_market_symbol(
    symbol: str, direction: int, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> None:
    """Move one symbol up (-1) or down (+1) in the saved list."""
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 (up) or 1 (down)")
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT symbol FROM market_symbols ORDER BY sort_order"
        ).fetchall()
        symbols = [row["symbol"] for row in rows]
        try:
            index = symbols.index(symbol.strip().upper())
        except ValueError:
            return
        other = index + direction
        if not 0 <= other < len(symbols):
            return
        symbols[index], symbols[other] = symbols[other], symbols[index]
        _write_market_symbol_order(connection, symbols)


def _renumber_market_symbols(connection) -> None:
    symbols = [
        row["symbol"]
        for row in connection.execute(
            "SELECT symbol FROM market_symbols ORDER BY sort_order"
        ).fetchall()
    ]
    _write_market_symbol_order(connection, symbols)


def _write_market_symbol_order(connection, symbols: list[str]) -> None:
    connection.execute("UPDATE market_symbols SET sort_order = -sort_order - 1")
    connection.executemany(
        "UPDATE market_symbols SET sort_order = ? WHERE symbol = ?",
        [(index, symbol) for index, symbol in enumerate(symbols)],
    )


def create_research_workspace(
    symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) VALUES (?)",
            (symbol.strip().upper(),),
        )


def list_research_workspaces(database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT symbol FROM research_workspaces
               UNION SELECT symbol FROM holdings
               UNION SELECT symbol FROM watchlist
               UNION SELECT symbol FROM strategy_records
               ORDER BY symbol"""
        ).fetchall()
    return [row["symbol"] for row in rows]


def list_research_chat_messages(
    symbol: str, *, limit: int = 200, database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> list[dict]:
    """Load one ticker's persistent conversation in chronological order."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT role, content, provider, model, created_at FROM research_chat_messages
               WHERE symbol=? ORDER BY id DESC LIMIT ?""",
            (symbol.strip().upper(), max(1, min(int(limit), 1000))),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def append_research_chat_messages(
    symbol: str, messages: list[dict], *, provider: str = "", model: str = "",
    input_tokens: int | None = None, output_tokens: int | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> None:
    """Append user/assistant turns without replacing another ticker's history."""
    normalized = symbol.strip().upper()
    valid = [(item.get("role"), item.get("content")) for item in messages]
    if any(role not in ("user", "assistant") or not isinstance(content, str) or not content.strip()
           for role, content in valid):
        raise ValueError("Chat messages must have a user/assistant role and non-empty text.")
    if not valid:
        return
    with connect(database_path) as connection:
        connection.execute("INSERT OR IGNORE INTO research_workspaces(symbol) VALUES (?)", (normalized,))
        connection.executemany(
            """INSERT INTO research_chat_messages
               (symbol, role, content, provider, model, input_tokens, output_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(normalized, role, content.strip(), provider, model,
              input_tokens if role == "assistant" else None,
              output_tokens if role == "assistant" else None) for role, content in valid],
        )


def save_research_strategy_draft(
    symbol: str, draft: dict, *, provider: str = "", model: str = "",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> None:
    """Persist an unapproved strategy draft separately from the approved record."""
    normalized = symbol.strip().upper()
    if not normalized or not isinstance(draft, dict):
        raise ValueError("A ticker and structured draft are required.")
    with connect(database_path) as connection:
        connection.execute("INSERT OR IGNORE INTO research_workspaces(symbol) VALUES (?)", (normalized,))
        connection.execute(
            """INSERT INTO research_strategy_drafts(symbol, draft_json, provider, model, generated_at, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(symbol) DO UPDATE SET draft_json=excluded.draft_json,
                 provider=excluded.provider, model=excluded.model,
                 generated_at=excluded.generated_at, updated_at=CURRENT_TIMESTAMP""",
            (normalized, json.dumps(draft, sort_keys=True), provider or draft.get("provider", ""),
             model or draft.get("model", ""), draft.get("generated_at") or datetime.now(timezone.utc).isoformat()),
        )


def get_research_strategy_draft(
    symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT draft_json FROM research_strategy_drafts WHERE symbol=?", (symbol.strip().upper(),)
        ).fetchone()
    if not row:
        return None
    try:
        draft = json.loads(row["draft_json"])
        return draft if isinstance(draft, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None


def delete_research_strategy_draft(
    symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM research_strategy_drafts WHERE symbol=?", (symbol.strip().upper(),)
        )
        return cursor.rowcount > 0


def save_research_strategy_scenarios(
    symbol: str, scenarios: list[dict], *, provider: str, model: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> int:
    """Save only explicitly approved horizon scenarios and append revisions."""
    normalized = symbol.strip().upper()
    allowed = {"Swing / short-term", "Medium-term", "Long-term"}
    clean = []
    seen_horizons = set()
    for scenario in scenarios:
        horizon = str(scenario.get("horizon") or "").strip()
        if horizon not in allowed:
            raise ValueError("Scenario horizon must be Swing / short-term, Medium-term, or Long-term.")
        if horizon in seen_horizons:
            raise ValueError(f"Only one approved scenario per horizon is allowed ({horizon}).")
        seen_horizons.add(horizon)
        clean.append((horizon, json.dumps(scenario, sort_keys=True, separators=(",", ":"))))
    with connect(database_path) as connection:
        connection.execute("INSERT OR IGNORE INTO research_workspaces(symbol) VALUES (?)", (normalized,))
        for horizon, payload in clean:
            connection.execute(
                """INSERT INTO research_strategy_scenario_revisions(symbol,horizon,scenario_json,provider,model)
                   VALUES (?,?,?,?,?)""", (normalized, horizon, payload, provider, model),
            )
            connection.execute(
                """INSERT INTO research_strategy_scenarios(symbol,horizon,scenario_json,provider,model,approved_at)
                   VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(symbol,horizon) DO UPDATE SET scenario_json=excluded.scenario_json,
                   provider=excluded.provider,model=excluded.model,approved_at=CURRENT_TIMESTAMP""",
                (normalized, horizon, payload, provider, model),
            )
    return len(clean)


def list_research_strategy_scenarios(
    symbol: str, *, history: bool = False, database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> list[dict]:
    table = "research_strategy_scenario_revisions" if history else "research_strategy_scenarios"
    with connect(database_path) as connection:
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE symbol=? ORDER BY approved_at DESC, id DESC" if history else
            f"SELECT * FROM {table} WHERE symbol=? ORDER BY CASE horizon WHEN 'Swing / short-term' THEN 1 WHEN 'Medium-term' THEN 2 ELSE 3 END",
            (symbol.strip().upper(),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["scenario"] = json.loads(item.pop("scenario_json"))
        result.append(item)
    return result


STRATEGY_FIELDS = (
    "investment_thesis",
    "investment_horizon",
    "thesis_status",
    "catalysts",
    "risks",
    "entry_price_low",
    "entry_price_high",
    "target_price",
    "stop_invalidation",
    "position_sizing_notes",
    "last_review_date",
    "valuation_notes",
    "legacy_entry_zone",
    "strategy_type",
    "expected_holding_window",
    "invalidation_price",
    "max_planned_dollar_risk",
    "bullish_scenario",
    "bearish_scenario",
    "assumptions",
    "uncertainties",
    "ai_price_estimates",
    "structured_analysis_json",
)


def get_strategy_record(
    symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM strategy_records WHERE symbol = ?", (symbol.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def save_strategy_record(
    symbol: str, record: dict, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> bool:
    """Save the latest strategy and append a revision only when its content changes."""
    normalized = {field: record.get(field) for field in STRATEGY_FIELDS}
    normalized["investment_horizon"] = normalized["investment_horizon"] or "Medium"
    normalized["thesis_status"] = normalized["thesis_status"] or "Active"
    for field in (
        "investment_thesis", "catalysts", "risks", "stop_invalidation",
        "position_sizing_notes", "last_review_date", "valuation_notes", "legacy_entry_zone",
        "strategy_type", "expected_holding_window", "bullish_scenario", "bearish_scenario",
        "assumptions", "uncertainties",
    ):
        normalized[field] = normalized[field] or ""
    normalized["ai_price_estimates"] = int(bool(normalized["ai_price_estimates"]))
    normalized["structured_analysis_json"] = normalized["structured_analysis_json"] or "{}"
    existing = get_strategy_record(symbol, database_path)
    if "structured_analysis_json" not in record:
        normalized["structured_analysis_json"] = (existing or {}).get("structured_analysis_json", "{}")
    changed = existing is None or any(existing.get(field) != value for field, value in normalized.items())
    normalized_symbol = symbol.strip().upper()
    with connect(database_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) VALUES (?)",
            (normalized_symbol,),
        )
        connection.execute(
            """INSERT INTO strategy_records (
                   symbol, investment_thesis, investment_horizon, thesis_status,
                   catalysts, risks, entry_price_low, entry_price_high, target_price,
                   stop_invalidation, position_sizing_notes, last_review_date,
                   valuation_notes, legacy_entry_zone, strategy_type, expected_holding_window,
                   invalidation_price, max_planned_dollar_risk, bullish_scenario, bearish_scenario,
                   assumptions, uncertainties, ai_price_estimates, structured_analysis_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                   investment_thesis=excluded.investment_thesis,
                   investment_horizon=excluded.investment_horizon,
                   thesis_status=excluded.thesis_status,
                   catalysts=excluded.catalysts, risks=excluded.risks,
                   entry_price_low=excluded.entry_price_low,
                   entry_price_high=excluded.entry_price_high,
                   target_price=excluded.target_price,
                   stop_invalidation=excluded.stop_invalidation,
                   position_sizing_notes=excluded.position_sizing_notes,
                   last_review_date=excluded.last_review_date,
                   legacy_entry_zone=excluded.legacy_entry_zone,
                   strategy_type=excluded.strategy_type,
                   expected_holding_window=excluded.expected_holding_window,
                   invalidation_price=excluded.invalidation_price,
                   max_planned_dollar_risk=excluded.max_planned_dollar_risk,
                   bullish_scenario=excluded.bullish_scenario,
                   bearish_scenario=excluded.bearish_scenario,
                   assumptions=excluded.assumptions,
                   uncertainties=excluded.uncertainties,
                   ai_price_estimates=excluded.ai_price_estimates,
                   structured_analysis_json=excluded.structured_analysis_json,
                   valuation_notes=excluded.valuation_notes,
                   updated_at=CURRENT_TIMESTAMP""",
            (normalized_symbol, *(normalized[field] for field in STRATEGY_FIELDS)),
        )
        connection.execute(
            """UPDATE holdings SET investment_horizon = ?, thesis_status = ?
               WHERE symbol = ?""",
            (normalized["investment_horizon"], normalized["thesis_status"], normalized_symbol),
        )
        if changed:
            connection.execute(
                """INSERT INTO strategy_revisions (
                       symbol, investment_thesis, investment_horizon, thesis_status,
                       catalysts, risks, entry_price_low, entry_price_high, target_price,
                       stop_invalidation, position_sizing_notes, last_review_date,
                       valuation_notes, legacy_entry_zone, strategy_type, expected_holding_window,
                       invalidation_price, max_planned_dollar_risk, bullish_scenario, bearish_scenario,
                       assumptions, uncertainties, ai_price_estimates, structured_analysis_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (normalized_symbol, *(normalized[field] for field in STRATEGY_FIELDS)),
            )
    return changed


def list_strategy_revisions(
    symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT * FROM strategy_revisions WHERE symbol = ?
               ORDER BY recorded_at DESC, id DESC""",
            (symbol.strip().upper(),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_buy_zone_alert(
    symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH
) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM buy_zone_alerts WHERE symbol = ?", (symbol.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def upsert_daily_prices(
    rows: list[dict], database_path: str | Path = DEFAULT_DATABASE_PATH
) -> int:
    """Persist OHLCV bars without mixing provider or adjustment bases."""
    if not rows:
        return 0
    for row in rows:
        _validate_daily_price_row(row)
    with connect(database_path) as connection:
        connection.executemany(
            """INSERT INTO daily_price_history (
                   symbol, trading_date, provider, adjustment_basis,
                   open, high, low, close, volume, retrieved_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, trading_date, provider, adjustment_basis) DO UPDATE SET
                   open=excluded.open, high=excluded.high, low=excluded.low,
                   close=excluded.close, volume=excluded.volume,
                   retrieved_at=excluded.retrieved_at""",
            [(
                row["symbol"].strip().upper(), row["trading_date"], row["provider"],
                row["adjustment_basis"], row.get("open"), row.get("high"),
                row.get("low"), row["close"], row.get("volume"), row["retrieved_at"],
            ) for row in rows],
        )
    return len(rows)


def _validate_daily_price_row(row: dict) -> None:
    try:
        date.fromisoformat(row["trading_date"])
        if not row["symbol"].strip() or not row["provider"].strip() or not row["adjustment_basis"].strip():
            raise ValueError("identity fields are empty")
        prices = [float(row[field]) for field in ("open", "high", "low", "close")]
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise ValueError("OHLC values must be finite and positive")
        opening, high, low, close = prices
        if high < max(opening, low, close):
            raise ValueError("OHLC values are inconsistent")
        volume = row.get("volume")
        if volume is not None and (not math.isfinite(float(volume)) or float(volume) < 0):
            raise ValueError("volume must be finite and nonnegative")
        if not row["retrieved_at"]:
            raise ValueError("retrieval timestamp is missing")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid daily price record; no data was stored.") from error


def list_daily_prices(
    symbol: str,
    provider: str,
    adjustment_basis: str,
    start_date: str | None = None,
    end_date: str | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> list[dict]:
    clauses = ["symbol = ?", "provider = ?", "adjustment_basis = ?"]
    params: list = [symbol.strip().upper(), provider, adjustment_basis]
    if start_date:
        clauses.append("trading_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("trading_date <= ?")
        params.append(end_date)
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM daily_price_history WHERE " + " AND ".join(clauses) +
            " ORDER BY trading_date",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_daily_price_coverage(
    symbols: list[str], provider: str, adjustment_basis: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, dict]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""SELECT symbol, COUNT(*) AS observations,
                       MIN(trading_date) AS first_date, MAX(trading_date) AS latest_date,
                       MAX(retrieved_at) AS retrieved_at
                FROM daily_price_history
                WHERE provider = ? AND adjustment_basis = ?
                  AND symbol IN ({placeholders})
                GROUP BY symbol""",
            [provider, adjustment_basis, *(symbol.upper() for symbol in symbols)],
        ).fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def reserve_historical_request(
    provider: str,
    symbol: str,
    *,
    hourly_limit: int,
    daily_limit: int,
    monthly_symbol_limit: int,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> tuple[bool, str | None]:
    """Atomically enforce conservative request limits before a provider call."""
    now = datetime.now(timezone.utc)
    iso_now = now.isoformat(timespec="seconds")
    hour_start = (now - timedelta(hours=1)).isoformat()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        counts = connection.execute(
            """SELECT
                 SUM(CASE WHEN requested_at >= ? THEN 1 ELSE 0 END) AS hour_count,
                 SUM(CASE WHEN requested_at >= ? THEN 1 ELSE 0 END) AS day_count
               FROM historical_request_log WHERE provider = ?""",
            (hour_start, day_start, provider),
        ).fetchone()
        hour_count, day_count = (int(counts[0] or 0), int(counts[1] or 0))
        if hour_count >= hourly_limit:
            return False, f"{provider} hourly request budget reached ({hourly_limit}/hour)."
        if day_count >= daily_limit:
            return False, f"{provider} daily request budget reached ({daily_limit}/day)."
        distinct = connection.execute(
            """SELECT COUNT(DISTINCT symbol) FROM historical_request_log
               WHERE provider = ? AND requested_at >= ?""",
            (provider, month_start),
        ).fetchone()[0]
        normalized = symbol.strip().upper()
        already_requested = connection.execute(
            """SELECT 1 FROM historical_request_log
               WHERE provider = ? AND symbol = ? AND requested_at >= ? LIMIT 1""",
            (provider, normalized, month_start),
        ).fetchone()
        if not already_requested and distinct >= monthly_symbol_limit:
            return False, f"{provider} monthly unique-symbol limit reached ({monthly_symbol_limit}/month)."
        connection.execute(
            "INSERT INTO historical_request_log (provider, symbol, requested_at) VALUES (?, ?, ?)",
            (provider, normalized, iso_now),
        )
    return True, None


def save_buy_zone_alert(
    symbol: str,
    lower_price: float,
    upper_price: float,
    enabled: bool,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> None:
    normalized_symbol = symbol.strip().upper()
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO buy_zone_alerts (symbol, lower_price, upper_price, enabled)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET lower_price=excluded.lower_price,
                 upper_price=excluded.upper_price, enabled=excluded.enabled,
                 updated_at=CURRENT_TIMESTAMP""",
            (normalized_symbol, lower_price, upper_price, int(enabled)),
        )
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) VALUES (?)",
            (normalized_symbol,),
        )


def save_market_briefing(briefing: dict, database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    """Persist a requested AI report and its exact local-data snapshot."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """INSERT INTO market_briefings (
                   generated_at, provider, model, web_search_enabled, input_snapshot_json,
                   report_text, source_metadata_json, input_tokens, output_tokens,
                   total_tokens, estimated_cost_usd, market_data_as_of, market_data_as_of_timestamp,
                   regime_classification, indicator_snapshot_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                briefing["generated_at"], briefing.get("provider", "openai"), briefing["model"],
                int(briefing.get("web_search_enabled", False)),
                json.dumps(briefing["input_snapshot"], sort_keys=True),
                briefing["report_text"],
                json.dumps(briefing.get("source_metadata", []), sort_keys=True),
                briefing.get("input_tokens"), briefing.get("output_tokens"),
                briefing.get("total_tokens"), briefing.get("estimated_cost_usd"),
                briefing.get("market_data_as_of"), briefing.get("market_data_as_of_timestamp"), briefing.get("regime_classification"),
                json.dumps(briefing.get("indicator_snapshot", briefing.get("input_snapshot", {}).get("regime_features", {})), sort_keys=True),
            ),
        )
        return int(cursor.lastrowid)


def get_latest_market_briefing(database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM market_briefings ORDER BY generated_at DESC, id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["web_search_enabled"] = bool(result["web_search_enabled"])
    result["input_snapshot"] = json.loads(result.pop("input_snapshot_json"))
    try:
        result["indicator_snapshot"] = json.loads(result.pop("indicator_snapshot_json"))
    except (KeyError, TypeError, json.JSONDecodeError):
        result["indicator_snapshot"] = result["input_snapshot"].get("regime_features", {})
    if not result["indicator_snapshot"]:
        result["indicator_snapshot"] = result["input_snapshot"].get("regime_features", {})
    result["source_metadata"] = json.loads(result.pop("source_metadata_json"))
    return result


def list_market_briefings(limit: int = 10, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    """Return recent assessments with their as-of date, regime and indicators."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM market_briefings ORDER BY generated_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["web_search_enabled"] = bool(item["web_search_enabled"])
        item["input_snapshot"] = json.loads(item.pop("input_snapshot_json"))
        item["source_metadata"] = json.loads(item.pop("source_metadata_json"))
        try:
            item["indicator_snapshot"] = json.loads(item.pop("indicator_snapshot_json"))
        except (KeyError, TypeError, json.JSONDecodeError):
            item["indicator_snapshot"] = item["input_snapshot"].get("regime_features", {})
        if not item["indicator_snapshot"]:
            item["indicator_snapshot"] = item["input_snapshot"].get("regime_features", {})
        result.append(item)
    return result


def upsert_volatility_observations(
    rows: list[dict], database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> int:
    """Persist official volatility-index closes independently from ETF OHLCV."""
    if not rows:
        return 0
    with connect(database_path) as connection:
        connection.executemany(
            """INSERT INTO volatility_index_history
               (symbol, observation_date, close, open, high, low, provider, retrieved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, observation_date, provider) DO UPDATE SET
                 close=excluded.close, open=excluded.open,
                 high=excluded.high, low=excluded.low,
                 retrieved_at=excluded.retrieved_at""",
            [(r["symbol"].upper(), r["observation_date"], float(r["close"]),
              float(r["open"]) if r.get("open") is not None else None,
              float(r["high"]) if r.get("high") is not None else None,
              float(r["low"]) if r.get("low") is not None else None,
              r["provider"], r["retrieved_at"]) for r in rows],
        )
    return len(rows)


def list_volatility_observations(
    symbols: tuple[str, ...] = ("VIX", "VIX9D"), provider: str = "cboe_official_csv",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with connect(database_path) as connection:
        rows = connection.execute(
            f"SELECT symbol, observation_date, close, open, high, low, provider, retrieved_at FROM volatility_index_history "
            f"WHERE provider=? AND symbol IN ({placeholders}) ORDER BY observation_date",
            (provider, *[symbol.upper() for symbol in symbols]),
        ).fetchall()
    result = {symbol.upper(): [] for symbol in symbols}
    for row in rows:
        item = dict(row)
        result.setdefault(item["symbol"], []).append(item)
    return result


def volatility_history_status(
    database_path: str | Path = DEFAULT_DATABASE_PATH, provider: str = "cboe_official_csv",
) -> dict[str, dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT symbol, COUNT(*) observations, MIN(observation_date) first_date,
                      MAX(observation_date) latest_date, MAX(retrieved_at) retrieved_at
               FROM volatility_index_history WHERE provider=? GROUP BY symbol""", (provider,)
        ).fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def upsert_macro_series_observations(
    rows: list[dict], database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> int:
    """Persist release-aware macro observations without mixing them with prices."""
    if not rows:
        return 0
    with connect(database_path) as connection:
        connection.executemany(
            """INSERT INTO macro_series_observations
               (series_id, observation_date, value, provider, units, initial_release_only, retrieved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(series_id, observation_date, provider) DO UPDATE SET
                 value=excluded.value, units=excluded.units,
                 initial_release_only=excluded.initial_release_only,
                 retrieved_at=excluded.retrieved_at""",
            [(str(row["series_id"]).upper(), row["observation_date"], float(row["value"]),
              row["provider"], row["units"], int(bool(row.get("initial_release_only", True))),
              row["retrieved_at"]) for row in rows],
        )
    return len(rows)


def list_macro_series_observations(
    series_ids: tuple[str, ...] | list[str] | None = None,
    provider: str = "fred_initial_release",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, list[dict]]:
    """Read cached macro observations by series; no provider access occurs."""
    with connect(database_path) as connection:
        if series_ids:
            placeholders = ",".join("?" for _ in series_ids)
            rows = connection.execute(
                f"SELECT * FROM macro_series_observations WHERE provider=? AND series_id IN ({placeholders}) ORDER BY observation_date",
                (provider, *[str(item).upper() for item in series_ids]),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM macro_series_observations WHERE provider=? ORDER BY series_id,observation_date",
                (provider,),
            ).fetchall()
    result = {str(item).upper(): [] for item in (series_ids or [])}
    for row in rows:
        item = dict(row)
        result.setdefault(item["series_id"], []).append(item)
    return result


def macro_series_coverage(
    series_ids: tuple[str, ...] | list[str], provider: str = "fred_initial_release",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, dict]:
    if not series_ids:
        return {}
    placeholders = ",".join("?" for _ in series_ids)
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""SELECT series_id,COUNT(*) observations,MIN(observation_date) first_date,
                       MAX(observation_date) latest_date,MAX(retrieved_at) retrieved_at
                FROM macro_series_observations WHERE provider=? AND series_id IN ({placeholders})
                GROUP BY series_id""",
            (provider, *[str(item).upper() for item in series_ids]),
        ).fetchall()
    return {row["series_id"]: dict(row) for row in rows}


def upsert_market_events(events: list[dict], database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    """Upsert calendar records and append audit entries for created/changed dates."""
    import hashlib
    from datetime import datetime, timezone

    inserted = changed = unchanged = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(database_path) as connection:
        for raw in events:
            name = str(raw["name"]).strip()
            category = str(raw["category"])
            start_utc = str(raw["start_utc"])
            provider = str(raw["provider"])
            source_uid = str(raw.get("source_uid") or "")
            symbols = list(dict.fromkeys(normalize_market_ticker(s) for s in raw.get("related_symbols", [])))
            date_key = start_utc[:10] if raw.get("all_day") else start_utc
            canonical = "|".join((category.lower(), re.sub(r"[^a-z0-9]+", " ", name.lower()).strip(), ",".join(symbols), date_key))
            dedupe_key = hashlib.sha256(canonical.encode()).hexdigest()
            event_id = hashlib.sha256(f"{provider}|{source_uid}".encode()).hexdigest() if source_uid else hashlib.sha256(dedupe_key.encode()).hexdigest()
            item = {
                "event_id": event_id, "dedupe_key": dedupe_key, "provider": provider, "source_uid": source_uid,
                "name": name, "category": category, "start_utc": start_utc,
                "source_timezone": str(raw.get("source_timezone") or "America/New_York"),
                "source_url": str(raw.get("source_url") or ""),
                "status": str(raw.get("status") or "Tentative"),
                "last_verified_at": str(raw.get("last_verified_at") or ("" if provider.startswith("manual_") else now)),
                "related_symbols_json": json.dumps(symbols), "notes": str(raw.get("notes") or ""),
                "importance_tier": str(raw.get("importance_tier") or "Standard"), "all_day": int(bool(raw.get("all_day"))),
                "event_date": raw.get("event_date"), "earnings_session": raw.get("earnings_session"),
                "exact_announcement_time_utc": raw.get("exact_announcement_time_utc"),
                "source_verification": str(raw.get("source_verification") or ("User-reported" if provider.startswith("manual_") else "Official source")),
                "original_input": str(raw.get("original_input") or ""),
            }
            existing = None
            matched_source_uid = False
            if source_uid:
                existing = connection.execute(
                    "SELECT * FROM market_events WHERE provider=? AND source_uid=?", (provider, source_uid)
                ).fetchone()
                matched_source_uid = existing is not None
            if existing is None:
                existing = connection.execute("SELECT * FROM market_events WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if existing:
                old = dict(existing)
                event_id = old["event_id"]
                # A cross-source duplicate is acknowledged without replacing the
                # primary source/UID that can later identify a reschedule.
                if not matched_source_uid and (old["provider"] != provider or old["source_uid"] != source_uid):
                    connection.execute("UPDATE market_events SET last_verified_at=? WHERE event_id=?", (now, event_id))
                    unchanged += 1
                    continue
                item["event_id"] = event_id
                current = {key: item[key] for key in item if key not in ("event_id", "last_verified_at")}
                previous = {key: old.get(key) for key in current}
                if current != previous:
                    action = "cancelled" if item["status"] == "Cancelled" and old["status"] != "Cancelled" else "rescheduled" if item["start_utc"] != old["start_utc"] else "updated"
                    connection.execute(
                        """UPDATE market_events SET dedupe_key=?, provider=?, source_uid=?, name=?, category=?,
                           start_utc=?, source_timezone=?, source_url=?, status=?, last_verified_at=?,
                           related_symbols_json=?, notes=?, importance_tier=?, all_day=?, event_date=?, earnings_session=?,
                           exact_announcement_time_utc=?, source_verification=?, original_input=?, updated_at=CURRENT_TIMESTAMP
                           WHERE event_id=?""",
                        (item["dedupe_key"], item["provider"], item["source_uid"], item["name"], item["category"],
                         item["start_utc"], item["source_timezone"], item["source_url"], item["status"],
                         item["last_verified_at"], item["related_symbols_json"], item["notes"],
                         item["importance_tier"], item["all_day"], item["event_date"], item["earnings_session"],
                         item["exact_announcement_time_utc"], item["source_verification"], item["original_input"], event_id),
                    )
                    connection.execute(
                        "INSERT INTO market_event_revisions(event_id, changed_at, action, previous_json, current_json) VALUES (?,?,?,?,?)",
                        (event_id, now, action, json.dumps(previous, sort_keys=True), json.dumps(current, sort_keys=True)),
                    )
                    changed += 1
                else:
                    connection.execute("UPDATE market_events SET last_verified_at=? WHERE event_id=?", (now, event_id))
                    unchanged += 1
            else:
                connection.execute(
                    """INSERT INTO market_events(event_id,dedupe_key,provider,source_uid,name,category,start_utc,
                       source_timezone,source_url,status,last_verified_at,related_symbols_json,notes,importance_tier,all_day,
                       event_date,earnings_session,exact_announcement_time_utc,source_verification,original_input)
                       VALUES (:event_id,:dedupe_key,:provider,:source_uid,:name,:category,:start_utc,
                       :source_timezone,:source_url,:status,:last_verified_at,:related_symbols_json,:notes,:importance_tier,:all_day,
                       :event_date,:earnings_session,:exact_announcement_time_utc,:source_verification,:original_input)""",
                    item,
                )
                connection.execute(
                    "INSERT INTO market_event_revisions(event_id, changed_at, action, previous_json, current_json) VALUES (?,?,?,?,?)",
                    (event_id, now, "created", None, json.dumps(item, sort_keys=True)),
                )
                inserted += 1
    return {"inserted": inserted, "changed": changed, "unchanged": unchanged}


def list_market_events(database_path: str | Path = DEFAULT_DATABASE_PATH, include_cancelled: bool = False) -> list[dict]:
    with connect(database_path) as connection:
        query = "SELECT * FROM market_events" + ("" if include_cancelled else " WHERE status <> 'Cancelled'") + " ORDER BY start_utc, name"
        rows = connection.execute(query).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["related_symbols"] = json.loads(item.pop("related_symbols_json"))
        item["all_day"] = bool(item["all_day"])
        result.append(item)
    return result


def mark_missing_market_source_events(provider: str, current_source_uids, *,
                                      database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    """Retain upstream-omitted future events but demote them to Unverified."""
    from datetime import datetime, timezone
    current = {str(uid) for uid in current_source_uids if uid}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = 0
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM market_events WHERE provider=? AND status <> 'Cancelled' AND start_utc >= ?",
            (provider, now[:10]),
        ).fetchall()
        for row in rows:
            old = dict(row)
            if not old["source_uid"] or old["source_uid"] in current:
                continue
            if old["status"] == "Unverified":
                continue
            current_record = dict(old)
            current_record["status"] = "Unverified"
            note = "Not present in the most recently refreshed official feed; verify before relying on this date."
            current_record["notes"] = (old.get("notes", "") + " " + note).strip()
            connection.execute(
                "UPDATE market_events SET status='Unverified', notes=?, updated_at=CURRENT_TIMESTAMP WHERE event_id=?",
                (current_record["notes"], old["event_id"]),
            )
            connection.execute(
                "INSERT INTO market_event_revisions(event_id,changed_at,action,previous_json,current_json) VALUES (?,?,?,?,?)",
                (old["event_id"], now, "updated", json.dumps(old, sort_keys=True), json.dumps(current_record, sort_keys=True)),
            )
            changed += 1
    return changed


def list_upcoming_market_events(*, now_utc: datetime | None = None, limit: int | None = None,
                                database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    from zoneinfo import ZoneInfo
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    events = [item for item in list_market_events(database_path)
              if (date.fromisoformat(item["start_utc"][:10]) >= now.astimezone(ZoneInfo(item.get("source_timezone") or "America/New_York")).date() if item["all_day"]
                  else datetime.fromisoformat(item["start_utc"].replace("Z", "+00:00")) >= now)]
    return events[:limit] if limit is not None else events


def save_market_event_source_status(provider: str, status: str, message: str = "", *,
                                    attempted_at: str | None = None, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    now = attempted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO market_event_source_status(provider,last_attempt_at,last_success_at,status,message)
               VALUES (?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
               last_success_at=CASE WHEN excluded.status='ok' THEN excluded.last_success_at ELSE market_event_source_status.last_success_at END,
               status=excluded.status,message=excluded.message""",
            (provider, now, now if status == "ok" else None, status, message),
        )


def list_market_event_source_status(database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict[str, dict]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM market_event_source_status").fetchall()
    return {row["provider"]: dict(row) for row in rows}


def list_market_event_revisions(event_id: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM market_event_revisions WHERE event_id=? ORDER BY changed_at DESC, revision_id DESC", (event_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["previous"] = json.loads(item.pop("previous_json")) if item.get("previous_json") else None
        item["current"] = json.loads(item.pop("current_json"))
        result.append(item)
    return result


def list_earnings_watchlist(database_path: str | Path = DEFAULT_DATABASE_PATH, include_user_symbols: bool = True) -> list[str]:
    with connect(database_path) as connection:
        tracked = [row[0].upper() for row in connection.execute("SELECT symbol FROM earnings_watchlist ORDER BY symbol")]
        if include_user_symbols:
            extra = [row[0].upper() for row in connection.execute(
                "SELECT symbol FROM holdings UNION SELECT symbol FROM research_workspaces UNION SELECT symbol FROM watchlist"
            )]
            tracked.extend(extra)
    return sorted(set(tracked))


def add_earnings_watch_symbol(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> str:
    normalized = normalize_market_ticker(symbol)
    with connect(database_path) as connection:
        connection.execute("INSERT OR IGNORE INTO earnings_watchlist(symbol) VALUES (?)", (normalized,))
    return normalized


def remove_earnings_watch_symbol(symbol: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    normalized = normalize_market_ticker(symbol)
    with connect(database_path) as connection:
        connection.execute("DELETE FROM earnings_watchlist WHERE symbol=?", (normalized,))


def get_market_event_timezone(database_path: str | Path = DEFAULT_DATABASE_PATH) -> str:
    with connect(database_path) as connection:
        row = connection.execute("SELECT value FROM market_event_preferences WHERE key='timezone'").fetchone()
    return row[0] if row else "America/New_York"


def save_market_event_timezone(timezone_name: str, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Choose a valid IANA timezone.") from error
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO market_event_preferences(key,value) VALUES ('timezone',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""", (timezone_name,)
        )


def save_manual_market_event(event: dict, database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    from datetime import datetime, timezone
    from urllib.parse import urlparse
    from dashboard.event_calendar import localize_source_time
    symbol = normalize_market_ticker(event["symbol"])
    local_date = date.fromisoformat(event["date"])
    local_time = event.get("time")
    timezone_name = event.get("source_timezone", "America/New_York")
    if event.get("status") not in ("Confirmed", "Estimated", "Unverified"):
        raise ValueError("Choose Confirmed or Estimated for a manual earnings event.")
    source_url = str(event.get("source_url") or "").strip()
    if source_url:
        parsed_url = urlparse(source_url)
        if parsed_url.scheme not in ("https", "http") or not parsed_url.netloc:
            raise ValueError("Source URL must be a complete http:// or https:// address.")
    session = str(event.get("earnings_session") or "").upper()
    if session and session not in ("BMO", "AMC", "TBD"):
        raise ValueError("Earnings timing must be BMO, AMC, or TBD.")
    # Session labels are not exact times. Keep the date anchor at local midnight
    # (converted with the IANA zone) for legacy ordering; the event date/session are
    # also stored independently and no announcement clock time is implied.
    instant = (localize_source_time(datetime.combine(local_date, datetime.strptime(local_time, "%H:%M").time()), timezone_name)
               if local_time else localize_source_time(datetime.combine(local_date, datetime.min.time()), timezone_name))
    start_utc = instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    name = f"{symbol} earnings"
    record = {"name": name, "category": "Earnings", "start_utc": start_utc,
              "source_timezone": timezone_name, "source_url": source_url,
              "provider": "manual_earnings",
              "status": event["status"], "related_symbols": [symbol],
              "notes": event.get("notes", ""), "importance_tier": event.get("importance_tier", "High"),
              "all_day": not bool(local_time), "event_date": local_date.isoformat(),
              "earnings_session": session or None,
              "exact_announcement_time_utc": start_utc if local_time else None,
              "source_verification": "User-reported",
              "source_uid": str(event.get("source_uid") or (f"manual-{symbol}-{local_date.isoformat()}" if event.get("original_input") else f"manual-{symbol}")),
              "original_input": str(event.get("original_input") or "")}
    return upsert_market_events([record], database_path)


def save_quick_earnings_events(
    events: list[dict], *, original_input: str, database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict:
    """Save parsed earnings rows without replacing official source events."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    result = {"added": 0, "updated": 0, "skipped": 0, "rejected": 0, "details": []}
    stored = list_market_events(database_path, include_cancelled=True)
    seen: set[tuple[str, str]] = set()

    def local_event_day(item: dict) -> str:
        if item.get("event_date"):
            return str(item["event_date"])
        if item.get("all_day"):
            return str(item["start_utc"])[:10]
        instant = datetime.fromisoformat(str(item["start_utc"]).replace("Z", "+00:00"))
        return instant.astimezone(ZoneInfo(item.get("source_timezone") or "America/New_York")).date().isoformat()

    def event_mentions_symbol(item: dict, symbol: str) -> bool:
        if symbol in item.get("related_symbols", []):
            return True
        name_symbols = {token.upper() for token in re.findall(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*", item.get("name", ""), re.IGNORECASE)}
        return symbol in name_symbols

    for row in events:
        try:
            symbol = normalize_market_ticker(row.get("ticker") or row.get("symbol") or "")
            local_day = date.fromisoformat(str(row.get("date") or ""))
            session = str(row.get("session") or row.get("earnings_session") or "TBD").upper()
            if session not in ("BMO", "AMC", "TBD"):
                raise ValueError("Release timing must be BMO, AMC, or TBD.")
        except (TypeError, ValueError) as error:
            result["rejected"] += 1
            result["details"].append({"ticker": row.get("ticker", ""), "status": "rejected", "message": str(error)})
            continue

        key = (symbol, local_day.isoformat())
        if key in seen:
            result["skipped"] += 1
            result["details"].append({"ticker": symbol, "date": key[1], "status": "skipped", "message": "Duplicate in this input."})
            continue
        seen.add(key)

        related = [item for item in stored if event_mentions_symbol(item, symbol)
                   and item.get("category") == "Earnings" and local_event_day(item) == key[1]
                   and item.get("status") != "Cancelled"]
        official = next((item for item in related if not item.get("provider", "").startswith("manual_")), None)
        if official:
            result["skipped"] += 1
            result["details"].append({"ticker": symbol, "date": key[1], "status": "skipped",
                                      "message": "Officially sourced event already exists; it was left unchanged."})
            continue
        existing_manual = next((item for item in related if item.get("provider", "").startswith("manual_")), None)
        try:
            saved = save_manual_market_event({
                "symbol": symbol, "date": key[1], "earnings_session": session,
                "source_timezone": "America/New_York", "status": "Unverified",
                "notes": str(row.get("notes") or ""), "source_url": str(row.get("source_url") or ""),
                "original_input": original_input,
                "source_uid": existing_manual.get("source_uid") if existing_manual else f"manual-{symbol}-{key[1]}",
            }, database_path)
        except (TypeError, ValueError, KeyError) as error:
            result["rejected"] += 1
            result["details"].append({"ticker": symbol, "date": key[1], "status": "rejected", "message": str(error)})
            continue

        if saved["inserted"]:
            result["added"] += 1
            message, status = "Saved as user-entered / unverified.", "added"
        elif saved["changed"]:
            result["updated"] += 1
            message, status = "Updated existing user-entered event.", "updated"
        else:
            result["skipped"] += 1
            message, status = "No change needed or an existing event took precedence.", "skipped"
        result["details"].append({"ticker": symbol, "date": key[1], "status": status, "message": message})
    return result


def save_manual_public_event(event: dict, database_path: str | Path = DEFAULT_DATABASE_PATH) -> dict:
    """Save a sourced policy/election deadline entered by the user."""
    import hashlib
    from datetime import datetime, timezone
    from dashboard.event_calendar import localize_source_time
    name = str(event.get("name") or "").strip()
    if not name or len(name) > 180:
        raise ValueError("Enter an event name of 1–180 characters.")
    if event.get("status") not in ("Confirmed", "Estimated", "Tentative", "Unverified"):
        raise ValueError("Choose a valid event date status.")
    source_url = str(event.get("source_url") or "").strip()
    if not source_url.startswith(("https://", "http://")):
        raise ValueError("A source URL is required for a manual event.")
    local_date = date.fromisoformat(event["date"])
    timezone_name = event.get("source_timezone", "America/New_York")
    local_time = event.get("time")
    if local_time:
        local = localize_source_time(datetime.combine(local_date, datetime.strptime(local_time, "%H:%M").time()), timezone_name)
        start_utc = local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    else:
        start_utc = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    uid = hashlib.sha256(name.lower().encode()).hexdigest()[:20]
    return upsert_market_events([{"name": name, "category": "Political/Other", "start_utc": start_utc,
                                 "source_timezone": timezone_name, "source_url": source_url,
                                 "provider": "manual_public_event", "source_uid": uid,
                                 "status": event["status"], "related_symbols": [], "notes": event.get("notes", ""),
                                 "importance_tier": "High", "all_day": not bool(local_time)}], database_path)

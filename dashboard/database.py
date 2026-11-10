"""SQLite schema and connection helpers for local dashboard data."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "investment_dashboard.db"
DEFAULT_MARKET_SYMBOLS = ("SPY", "RSP", "QQQ", "DIA", "IWM", "SMH")


@contextmanager
def connect(database_path: str | Path = DEFAULT_DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection and return rows readable by column name."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    """Create the local tables if they do not exist yet."""
    with connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE COLLATE NOCASE,
                shares REAL NOT NULL CHECK (shares >= 0),
                average_cost REAL NOT NULL CHECK (average_cost >= 0),
                current_price REAL NOT NULL CHECK (current_price >= 0),
                daily_change_per_share REAL NOT NULL DEFAULT 0,
                investment_horizon TEXT NOT NULL DEFAULT 'Medium'
                    CHECK (investment_horizon IN ('Long', 'Medium', 'Short')),
                thesis_status TEXT NOT NULL DEFAULT 'Active'
                    CHECK (thesis_status IN ('Active', 'Review Needed', 'Invalidated')),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE COLLATE NOCASE,
                company_name TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS research_notes (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                thesis TEXT NOT NULL DEFAULT '',
                catalysts TEXT NOT NULL DEFAULT '',
                valuation TEXT NOT NULL DEFAULT '',
                entry_zone TEXT NOT NULL DEFAULT '',
                invalidation TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS risk_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                threshold REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS portfolio_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash_balance REAL NOT NULL DEFAULT 0 CHECK (cash_balance >= 0)
            );

            CREATE TABLE IF NOT EXISTS portfolio_strategy_preferences (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                active_horizon TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS portfolio_monitor_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                stop_near_pct REAL NOT NULL DEFAULT 3 CHECK (stop_near_pct >= 0),
                target_near_pct REAL NOT NULL DEFAULT 3 CHECK (target_near_pct >= 0),
                remaining_rrr_below REAL NOT NULL DEFAULT 1 CHECK (remaining_rrr_below >= 0)
            );

            CREATE TABLE IF NOT EXISTS portfolio_position_order (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                sort_order INTEGER NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS position_plan_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL COLLATE NOCASE,
                strategy_revision_id INTEGER,
                horizon TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_position_plan_symbol_horizon
                ON position_plan_revisions(symbol, horizon, approved_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS position_plan_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_revision_id INTEGER NOT NULL,
                symbol TEXT NOT NULL COLLATE NOCASE,
                tranche_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('buy','sell')),
                filled_shares REAL NOT NULL CHECK (filled_shares > 0),
                fill_price REAL NOT NULL CHECK (fill_price > 0),
                filled_at TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_plan_execution_revision
                ON position_plan_executions(plan_revision_id, recorded_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS position_planner_policy (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                max_position_allocation REAL NOT NULL DEFAULT 0.15,
                target_position_allocation REAL NOT NULL DEFAULT 0.08,
                max_loss_per_position REAL NOT NULL DEFAULT 0.01,
                minimum_cash_reserve REAL NOT NULL DEFAULT 0,
                max_sector_allocation REAL NOT NULL DEFAULT 0.30,
                rrr_monitoring_threshold REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quote_cache (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                price REAL NOT NULL,
                change REAL,
                change_percent REAL,
                quote_date TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                freshness TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_price_history (
                symbol TEXT NOT NULL COLLATE NOCASE,
                trading_date TEXT NOT NULL,
                provider TEXT NOT NULL,
                adjustment_basis TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL NOT NULL,
                volume REAL,
                retrieved_at TEXT NOT NULL,
                PRIMARY KEY (symbol, trading_date, provider, adjustment_basis)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_history_symbol_date
                ON daily_price_history(symbol, trading_date);

            CREATE TABLE IF NOT EXISTS historical_request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL COLLATE NOCASE,
                requested_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_historical_request_provider_time
                ON historical_request_log(provider, requested_at);

            CREATE TABLE IF NOT EXISTS api_request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS market_symbols (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                sort_order INTEGER NOT NULL UNIQUE
            );

            -- Independent chart selections; defaults are supplied by data_access.
            -- Keeping these separate preserves the existing spot-quote symbol list.
            CREATE TABLE IF NOT EXISTS market_chart_preferences (
                preference_key TEXT PRIMARY KEY,
                symbols_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS portfolio_sector_classifications (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                sector TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS market_regime_transition_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of_session TEXT NOT NULL,
                methodology_version TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                transition_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(as_of_session, methodology_version, source_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_regime_transition_session
                ON market_regime_transition_records(as_of_session DESC, methodology_version);

            CREATE TABLE IF NOT EXISTS market_briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'openai',
                model TEXT NOT NULL,
                web_search_enabled INTEGER NOT NULL DEFAULT 0,
                input_snapshot_json TEXT NOT NULL,
                report_text TEXT NOT NULL,
                source_metadata_json TEXT NOT NULL DEFAULT '[]',
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                estimated_cost_usd REAL
            );

            -- Volatility indices are observations, not tradable ETF price history.
            CREATE TABLE IF NOT EXISTS volatility_index_history (
                symbol TEXT NOT NULL COLLATE NOCASE,
                observation_date TEXT NOT NULL,
                close REAL NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                provider TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                PRIMARY KEY (symbol, observation_date, provider)
            );
            CREATE INDEX IF NOT EXISTS idx_volatility_history_symbol_date
                ON volatility_index_history(symbol, observation_date);

            -- FRED macro observations are separate from tradable ETF OHLCV and VIX.
            CREATE TABLE IF NOT EXISTS macro_series_observations (
                series_id TEXT NOT NULL COLLATE NOCASE,
                observation_date TEXT NOT NULL,
                value REAL NOT NULL,
                provider TEXT NOT NULL,
                units TEXT NOT NULL,
                initial_release_only INTEGER NOT NULL DEFAULT 1,
                retrieved_at TEXT NOT NULL,
                PRIMARY KEY (series_id, observation_date, provider)
            );
            CREATE INDEX IF NOT EXISTS idx_macro_series_date
                ON macro_series_observations(series_id, observation_date);

            -- Official Treasury daily par yields, kept apart from FRED series.
            CREATE TABLE IF NOT EXISTS treasury_yield_history (
                observation_date TEXT PRIMARY KEY,
                two_year REAL NOT NULL,
                ten_year REAL NOT NULL,
                provider TEXT NOT NULL DEFAULT 'us_treasury_xml',
                retrieved_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_treasury_yield_date
                ON treasury_yield_history(observation_date);

            CREATE TABLE IF NOT EXISTS deepseek_market_research_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT NOT NULL UNIQUE,
                topic TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                model TEXT NOT NULL,
                report_text TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '[]',
                usage_json TEXT NOT NULL DEFAULT '{}',
                web_search_succeeded INTEGER NOT NULL DEFAULT 0,
                research_status TEXT NOT NULL DEFAULT 'complete',
                error_message TEXT NOT NULL DEFAULT '',
                completion_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_deepseek_market_research_topic
                ON deepseek_market_research_reports(topic, generated_at DESC);

            CREATE TABLE IF NOT EXISTS deepseek_market_research_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requested_at TEXT NOT NULL,
                model TEXT NOT NULL,
                search_requests INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER,
                output_tokens INTEGER,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_deepseek_market_research_usage_date
                ON deepseek_market_research_usage(requested_at);

            -- Safe request metadata only; prompts, secrets, and private records are excluded.
            CREATE TABLE IF NOT EXISTS market_ai_request_audit (
                request_id TEXT PRIMARY KEY,
                requested_at TEXT NOT NULL,
                purpose TEXT NOT NULL,
                evidence_mode TEXT NOT NULL DEFAULT '',
                requested_model TEXT NOT NULL,
                actual_model TEXT,
                endpoint TEXT NOT NULL,
                reasoning_effort TEXT,
                output_token_allowance INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                visible_output_tokens INTEGER,
                search_requests INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                reported_cost_usd REAL,
                completion_status TEXT NOT NULL,
                error_category TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_market_ai_request_audit_date
                ON market_ai_request_audit(requested_at DESC);

            -- Follow-up market chat is isolated by section and frozen snapshot.
            CREATE TABLE IF NOT EXISTS market_section_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id TEXT NOT NULL,
                snapshot_fingerprint TEXT NOT NULL,
                report_id INTEGER,
                request_id TEXT,
                role TEXT NOT NULL CHECK (role IN ('user','assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                source_metadata_json TEXT NOT NULL DEFAULT '[]',
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                cost_kind TEXT NOT NULL DEFAULT 'estimate',
                UNIQUE(request_id, role)
            );
            CREATE INDEX IF NOT EXISTS idx_market_section_chat_context
                ON market_section_chat_messages(section_id,snapshot_fingerprint,id);

            -- User-entered observations stay separate from provider price data.
            CREATE TABLE IF NOT EXISTS market_user_observations (
                observation_id TEXT PRIMARY KEY,
                observation_text TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                session_type TEXT NOT NULL CHECK (session_type IN ('premarket','regular','after_hours','unknown')),
                symbols_json TEXT NOT NULL DEFAULT '[]',
                verification_status TEXT NOT NULL DEFAULT 'user_supplied_unverified',
                origin_conversation_id TEXT NOT NULL DEFAULT '',
                origin_message_id INTEGER,
                source_urls_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(origin_conversation_id, origin_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_user_observations_created
                ON market_user_observations(created_at DESC);

            -- Scenario records point to immutable, explicitly approved revisions.
            CREATE TABLE IF NOT EXISTS market_scenario_records (
                scenario_id TEXT PRIMARY KEY,
                current_revision_id INTEGER,
                status TEXT NOT NULL DEFAULT 'Watching'
                    CHECK (status IN ('Watching','Supported by new evidence','Challenged','Resolved','Archived','Unassessed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_scenario_revisions (
                revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                scenario_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_conversation_id TEXT NOT NULL DEFAULT '',
                source_message_id INTEGER,
                UNIQUE(scenario_id, revision_number),
                FOREIGN KEY(scenario_id) REFERENCES market_scenario_records(scenario_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_scenario_revision_history
                ON market_scenario_revisions(scenario_id, revision_number DESC);

            -- Explicitly saved chat answers are research notes, not scenarios or trades.
            CREATE TABLE IF NOT EXISTS market_ai_research_notes (
                note_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                note_text TEXT NOT NULL,
                snapshot_fingerprint TEXT NOT NULL DEFAULT '',
                source_metadata_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(conversation_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_ai_research_notes_created
                ON market_ai_research_notes(created_at DESC);

            -- Public upcoming catalysts and locally verified manual earnings dates.
            CREATE TABLE IF NOT EXISTS market_events (
                event_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                source_uid TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('Macro', 'Fed', 'Earnings', 'Political/Other')),
                start_utc TEXT NOT NULL,
                source_timezone TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('Confirmed', 'Estimated', 'Tentative', 'Unverified', 'Cancelled')),
                last_verified_at TEXT NOT NULL,
                related_symbols_json TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT '',
                importance_tier TEXT NOT NULL DEFAULT 'Standard',
                all_day INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_market_events_upcoming
                ON market_events(start_utc, category, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_market_events_provider_uid
                ON market_events(provider, source_uid) WHERE source_uid <> '';

            CREATE TABLE IF NOT EXISTS market_event_revisions (
                revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('created', 'updated', 'rescheduled', 'cancelled')),
                previous_json TEXT,
                current_json TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES market_events(event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_event_revisions_event
                ON market_event_revisions(event_id, changed_at DESC);

            CREATE TABLE IF NOT EXISTS market_event_source_status (
                provider TEXT PRIMARY KEY,
                last_attempt_at TEXT NOT NULL,
                last_success_at TEXT,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS earnings_watchlist (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS market_event_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_market_briefings_generated
                ON market_briefings(generated_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS research_workspaces (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS research_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL COLLATE NOCASE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER,
                output_tokens INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_research_chat_symbol_date
                ON research_chat_messages(symbol, created_at, id);

            CREATE TABLE IF NOT EXISTS research_strategy_drafts (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                draft_json TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS research_strategy_scenarios (
                symbol TEXT NOT NULL COLLATE NOCASE,
                horizon TEXT NOT NULL,
                scenario_json TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(symbol, horizon)
            );
            CREATE TABLE IF NOT EXISTS research_strategy_scenario_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL COLLATE NOCASE,
                horizon TEXT NOT NULL,
                scenario_json TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_research_scenario_revisions
                ON research_strategy_scenario_revisions(symbol, horizon, approved_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS strategy_records (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                investment_thesis TEXT NOT NULL DEFAULT '',
                investment_horizon TEXT NOT NULL DEFAULT 'Medium',
                thesis_status TEXT NOT NULL DEFAULT 'Active',
                catalysts TEXT NOT NULL DEFAULT '',
                risks TEXT NOT NULL DEFAULT '',
                entry_price_low REAL,
                entry_price_high REAL,
                target_price REAL,
                stop_invalidation TEXT NOT NULL DEFAULT '',
                position_sizing_notes TEXT NOT NULL DEFAULT '',
                last_review_date TEXT NOT NULL DEFAULT '',
                valuation_notes TEXT NOT NULL DEFAULT '',
                legacy_entry_zone TEXT NOT NULL DEFAULT '',
                strategy_type TEXT NOT NULL DEFAULT '',
                expected_holding_window TEXT NOT NULL DEFAULT '',
                invalidation_price REAL,
                max_planned_dollar_risk REAL,
                bullish_scenario TEXT NOT NULL DEFAULT '',
                bearish_scenario TEXT NOT NULL DEFAULT '',
                assumptions TEXT NOT NULL DEFAULT '',
                uncertainties TEXT NOT NULL DEFAULT '',
                ai_price_estimates INTEGER NOT NULL DEFAULT 0 CHECK (ai_price_estimates IN (0, 1)),
                structured_analysis_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS strategy_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL COLLATE NOCASE,
                investment_thesis TEXT NOT NULL,
                investment_horizon TEXT NOT NULL,
                thesis_status TEXT NOT NULL,
                catalysts TEXT NOT NULL,
                risks TEXT NOT NULL,
                entry_price_low REAL,
                entry_price_high REAL,
                target_price REAL,
                stop_invalidation TEXT NOT NULL,
                position_sizing_notes TEXT NOT NULL,
                last_review_date TEXT NOT NULL,
                valuation_notes TEXT NOT NULL,
                legacy_entry_zone TEXT NOT NULL,
                strategy_type TEXT NOT NULL DEFAULT '',
                expected_holding_window TEXT NOT NULL DEFAULT '',
                invalidation_price REAL,
                max_planned_dollar_risk REAL,
                bullish_scenario TEXT NOT NULL DEFAULT '',
                bearish_scenario TEXT NOT NULL DEFAULT '',
                assumptions TEXT NOT NULL DEFAULT '',
                uncertainties TEXT NOT NULL DEFAULT '',
                ai_price_estimates INTEGER NOT NULL DEFAULT 0 CHECK (ai_price_estimates IN (0, 1)),
                structured_analysis_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_strategy_revisions_symbol_date
                ON strategy_revisions(symbol, recorded_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS buy_zone_alerts (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                lower_price REAL NOT NULL,
                upper_price REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (lower_price <= upper_price)
            );

            INSERT OR IGNORE INTO portfolio_settings (id, cash_balance) VALUES (1, 0);
            INSERT OR IGNORE INTO portfolio_monitor_settings (id) VALUES (1);
            INSERT OR IGNORE INTO position_planner_policy (id) VALUES (1);
            """
        )
        # One-time deterministic initial ordering for holdings already present
        # when this additive table is introduced. Subsequent additions append.
        ordering_count = connection.execute(
            "SELECT COUNT(*) FROM portfolio_position_order"
        ).fetchone()[0]
        if ordering_count == 0:
            existing_symbols = connection.execute(
                "SELECT symbol FROM holdings ORDER BY symbol COLLATE NOCASE"
            ).fetchall()
            connection.executemany(
                "INSERT OR IGNORE INTO portfolio_position_order(symbol,sort_order) VALUES (?,?)",
                [(row["symbol"].upper(), index) for index, row in enumerate(existing_symbols)],
            )
        watchlist_initialized = connection.execute(
            "SELECT 1 FROM app_metadata WHERE key='earnings_watchlist_initialized'"
        ).fetchone()
        if not watchlist_initialized:
            connection.executemany(
                "INSERT OR IGNORE INTO earnings_watchlist(symbol) VALUES (?)",
                [(symbol,) for symbol in ("NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA")],
            )
            connection.execute(
                "INSERT INTO app_metadata(key, value) VALUES('earnings_watchlist_initialized', '1')"
            )
        _add_column_if_missing(
            connection,
            "holdings",
            "investment_horizon",
            "TEXT NOT NULL DEFAULT 'Medium' CHECK (investment_horizon IN ('Long', 'Medium', 'Short'))",
        )
        for column in ("open", "high", "low"):
            _add_column_if_missing(connection, "volatility_index_history", column, "REAL")
        _add_column_if_missing(connection, "deepseek_market_research_reports", "research_status", "TEXT NOT NULL DEFAULT 'complete'")
        _add_column_if_missing(connection, "deepseek_market_research_reports", "error_message", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "deepseek_market_research_reports", "completion_reason", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "market_section_chat_messages", "cost_usd", "REAL")
        _add_column_if_missing(connection, "market_section_chat_messages", "cost_kind", "TEXT NOT NULL DEFAULT 'estimate'")
        _add_column_if_missing(connection, "market_ai_request_audit", "evidence_mode", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(
            connection,
            "holdings",
            "thesis_status",
            "TEXT NOT NULL DEFAULT 'Active' CHECK (thesis_status IN ('Active', 'Review Needed', 'Invalidated'))",
        )
        _add_column_if_missing(connection, "market_briefings", "provider", "TEXT NOT NULL DEFAULT 'openai'")
        _add_column_if_missing(connection, "research_chat_messages", "input_tokens", "INTEGER")
        _add_column_if_missing(connection, "research_chat_messages", "output_tokens", "INTEGER")
        _add_column_if_missing(connection, "market_briefings", "market_data_as_of", "TEXT")
        _add_column_if_missing(connection, "market_briefings", "market_data_as_of_timestamp", "TEXT")
        _add_column_if_missing(connection, "market_briefings", "regime_classification", "TEXT")
        _add_column_if_missing(connection, "market_briefings", "indicator_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
        # Calendar additions are nullable/defaulted so existing records remain readable.
        _add_column_if_missing(connection, "market_events", "event_date", "TEXT")
        _add_column_if_missing(connection, "market_events", "earnings_session", "TEXT")
        _add_column_if_missing(connection, "market_events", "exact_announcement_time_utc", "TEXT")
        _add_column_if_missing(connection, "market_events", "source_verification", "TEXT NOT NULL DEFAULT 'Unverified'")
        _add_column_if_missing(connection, "market_events", "original_input", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "market_events", "end_utc", "TEXT")
        _add_column_if_missing(connection, "market_events", "source_published_at", "TEXT")
        _add_column_if_missing(connection, "market_events", "source_provider", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "market_events", "affected_sectors_json", "TEXT NOT NULL DEFAULT '[]'")
        _add_column_if_missing(connection, "market_events", "import_batch_id", "TEXT")
        connection.executescript(
            """CREATE TABLE IF NOT EXISTS market_event_import_batches (
                   batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, original_json TEXT NOT NULL,
                   result_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active'
               );
               CREATE TABLE IF NOT EXISTS market_event_import_items (
                   batch_id TEXT NOT NULL, event_id TEXT NOT NULL, action TEXT NOT NULL,
                   before_json TEXT, after_json TEXT NOT NULL,
                   PRIMARY KEY(batch_id,event_id),
                   FOREIGN KEY(batch_id) REFERENCES market_event_import_batches(batch_id)
               );
               CREATE INDEX IF NOT EXISTS idx_market_event_import_items_event ON market_event_import_items(event_id);"""
        )
        connection.execute(
            """UPDATE market_events SET source_verification=CASE
                   WHEN provider LIKE 'manual_%' THEN 'User-reported'
                   WHEN last_verified_at <> '' THEN 'Official source'
                   ELSE 'Unverified' END
               WHERE source_verification='Unverified'"""
        )
        # Add classification metadata without replacing existing user-entered sectors.
        _add_column_if_missing(connection, "portfolio_sector_classifications", "asset_type", "TEXT NOT NULL DEFAULT 'Other'")
        _add_column_if_missing(connection, "portfolio_sector_classifications", "sector_benchmark", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "portfolio_sector_classifications", "benchmark_type", "TEXT NOT NULL DEFAULT 'None'")
        _add_column_if_missing(connection, "portfolio_sector_classifications", "classification_source", "TEXT NOT NULL DEFAULT 'Manual'")
        _add_column_if_missing(connection, "portfolio_sector_classifications", "verification_status", "TEXT NOT NULL DEFAULT 'Unverified'")
        _add_column_if_missing(
            connection, "strategy_records", "legacy_entry_zone", "TEXT NOT NULL DEFAULT ''"
        )
        _add_column_if_missing(
            connection, "strategy_revisions", "legacy_entry_zone", "TEXT NOT NULL DEFAULT ''"
        )
        for table in ("strategy_records", "strategy_revisions"):
            _add_column_if_missing(connection, table, "strategy_type", "TEXT NOT NULL DEFAULT ''")
            _add_column_if_missing(connection, table, "expected_holding_window", "TEXT NOT NULL DEFAULT ''")
            _add_column_if_missing(connection, table, "invalidation_price", "REAL")
            _add_column_if_missing(connection, table, "max_planned_dollar_risk", "REAL")
            _add_column_if_missing(connection, table, "bullish_scenario", "TEXT NOT NULL DEFAULT ''")
            _add_column_if_missing(connection, table, "bearish_scenario", "TEXT NOT NULL DEFAULT ''")
            _add_column_if_missing(connection, table, "assumptions", "TEXT NOT NULL DEFAULT ''")
            _add_column_if_missing(connection, table, "uncertainties", "TEXT NOT NULL DEFAULT ''")
            _add_column_if_missing(connection, table, "ai_price_estimates", "INTEGER NOT NULL DEFAULT 0 CHECK (ai_price_estimates IN (0, 1))")
            _add_column_if_missing(connection, table, "structured_analysis_json", "TEXT NOT NULL DEFAULT '{}'")

        initialized = connection.execute(
            "SELECT 1 FROM app_metadata WHERE key = 'market_symbols_initialized'"
        ).fetchone()
        if not initialized:
            market_count = connection.execute("SELECT COUNT(*) FROM market_symbols").fetchone()[0]
            if market_count == 0:
                connection.executemany(
                    "INSERT INTO market_symbols (symbol, sort_order) VALUES (?, ?)",
                    [(symbol, index) for index, symbol in enumerate(DEFAULT_MARKET_SYMBOLS)],
                )
            connection.execute(
                "INSERT INTO app_metadata (key, value) VALUES ('market_symbols_initialized', '1')"
            )

        # Copy legacy notes into the structured workspace without removing or changing
        # their original rows. The old valuation text is retained verbatim.
        connection.execute(
            """INSERT OR IGNORE INTO strategy_records (
                   symbol, investment_thesis, catalysts, entry_price_low,
                   entry_price_high, stop_invalidation, last_review_date,
                   valuation_notes, updated_at, legacy_entry_zone
               )
               SELECT symbol, thesis, catalysts, NULL, NULL, invalidation,
                      substr(updated_at, 1, 10), valuation, updated_at, entry_zone
               FROM research_notes"""
        )
        connection.execute(
            """INSERT INTO strategy_revisions (
                   symbol, investment_thesis, investment_horizon, thesis_status,
                   catalysts, risks, entry_price_low, entry_price_high, target_price,
                   stop_invalidation, position_sizing_notes, last_review_date,
                   valuation_notes, recorded_at, legacy_entry_zone
               )
               SELECT s.symbol, s.investment_thesis, s.investment_horizon,
                      s.thesis_status, s.catalysts, s.risks, s.entry_price_low,
                      s.entry_price_high, s.target_price, s.stop_invalidation,
                      s.position_sizing_notes, s.last_review_date, s.valuation_notes,
                      s.updated_at, s.legacy_entry_zone
               FROM strategy_records AS s
               WHERE NOT EXISTS (
                   SELECT 1 FROM strategy_revisions AS r WHERE r.symbol = s.symbol
               )"""
        )
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) SELECT symbol FROM holdings"
        )
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) SELECT symbol FROM watchlist"
        )
        connection.execute(
            "INSERT OR IGNORE INTO research_workspaces (symbol) SELECT symbol FROM strategy_records"
        )


def _add_column_if_missing(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {
        row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if column not in existing:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')

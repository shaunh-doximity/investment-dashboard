import sqlite3
import tempfile
import unittest
from pathlib import Path

from dashboard.database import initialize_database
from dashboard.data_access import (get_cached_quote, get_strategy_record,
                                   list_portfolio_position_order, list_strategy_revisions)


class AdditiveMigrationTests(unittest.TestCase):
    def test_migration_keeps_existing_rows_and_copies_legacy_research(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    shares REAL NOT NULL,
                    average_cost REAL NOT NULL,
                    current_price REAL NOT NULL,
                    daily_change_per_share REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO holdings (symbol, shares, average_cost, current_price)
                    VALUES ('MSFT', 3, 100, 120);

                CREATE TABLE quote_cache (
                    symbol TEXT PRIMARY KEY COLLATE NOCASE,
                    price REAL NOT NULL,
                    change REAL,
                    change_percent REAL,
                    quote_date TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    freshness TEXT NOT NULL
                );
                INSERT INTO quote_cache VALUES
                    ('SPY', 500, 2, 0.4, '2025-01-02', '2025-01-02T22:00:00+00:00', 'end-of-day');

                CREATE TABLE research_notes (
                    symbol TEXT PRIMARY KEY COLLATE NOCASE,
                    thesis TEXT NOT NULL DEFAULT '',
                    catalysts TEXT NOT NULL DEFAULT '',
                    valuation TEXT NOT NULL DEFAULT '',
                    entry_zone TEXT NOT NULL DEFAULT '',
                    invalidation TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO research_notes
                    (symbol, thesis, catalysts, valuation, entry_zone, invalidation, updated_at)
                    VALUES ('MSFT', 'Legacy thesis', 'Legacy catalyst', 'Legacy valuation',
                            '100-110', 'Cloud growth reverses', '2025-01-02 12:00:00');
                """
            )
            connection.commit()
            connection.close()

            initialize_database(database_path)

            connection = sqlite3.connect(database_path)
            holding = connection.execute(
                "SELECT symbol, shares, average_cost, current_price, investment_horizon, thesis_status FROM holdings"
            ).fetchone()
            self.assertEqual(holding, ("MSFT", 3.0, 100.0, 120.0, "Medium", "Active"))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holdings").fetchone()[0], 1)
            connection.close()

            self.assertEqual(list_portfolio_position_order(database_path), ["MSFT"])

            self.assertEqual(get_cached_quote("SPY", database_path)["price"], 500.0)
            migrated = get_strategy_record("MSFT", database_path)
            self.assertEqual(migrated["investment_thesis"], "Legacy thesis")
            self.assertEqual(migrated["valuation_notes"], "Legacy valuation")
            self.assertEqual(migrated["legacy_entry_zone"], "100-110")
            self.assertEqual(migrated["strategy_type"], "")
            self.assertIsNone(migrated["invalidation_price"])
            revisions = list_strategy_revisions("MSFT", database_path)
            self.assertEqual(len(revisions), 1)
            self.assertEqual(revisions[0]["investment_thesis"], "Legacy thesis")
            self.assertEqual(migrated["assumptions"], "")
            self.assertEqual(revisions[0]["ai_price_estimates"], 0)

    def test_market_briefing_provider_column_is_added_without_replacing_saved_report(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "briefings.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """CREATE TABLE market_briefings (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, generated_at TEXT NOT NULL,
                       model TEXT NOT NULL, web_search_enabled INTEGER NOT NULL DEFAULT 0,
                       input_snapshot_json TEXT NOT NULL, report_text TEXT NOT NULL,
                       source_metadata_json TEXT NOT NULL DEFAULT '[]', input_tokens INTEGER,
                       output_tokens INTEGER, total_tokens INTEGER, estimated_cost_usd REAL
                   );
                   INSERT INTO market_briefings
                       (generated_at, model, input_snapshot_json, report_text)
                   VALUES ('2025-01-01T00:00:00Z', 'legacy-model', '{}', 'Saved report');"""
            )
            connection.commit()
            connection.close()

            initialize_database(database_path)

            connection = sqlite3.connect(database_path)
            saved = connection.execute(
                "SELECT provider, model, report_text FROM market_briefings WHERE id=1"
            ).fetchone()
            connection.close()
            self.assertEqual(saved, ("openai", "legacy-model", "Saved report"))


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from dashboard.data_access import (
    add_chart_symbol,
    add_watchlist_item,
    add_market_symbol,
    delete_holding,
    get_cash_balance,
    get_cached_quote,
    list_watchlist,
    list_market_leadership_symbols,
    list_sector_display_symbols,
    list_market_symbols,
    list_holdings,
    list_portfolio_position_order,
    list_portfolio_sector_classifications,
    list_portfolio_classification_records,
    ensure_portfolio_classifications,
    save_portfolio_classification_records,
    move_chart_symbol,
    move_market_symbol,
    move_portfolio_position,
    remove_market_symbol,
    remove_chart_symbol,
    restore_chart_symbol_defaults,
    save_sector_display_symbols,
    save_portfolio_sector_classification,
    save_cached_quote,
    save_cash_balance,
    save_holding,
)
from dashboard.database import initialize_database


class LocalPersistenceTests(unittest.TestCase):
    def test_independent_chart_ticker_preferences_normalize_deduplicate_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            initialize_database(database_path)
            self.assertEqual(list_market_leadership_symbols(database_path), ["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"])
            self.assertEqual(len(list_sector_display_symbols(database_path)), 11)

            add_chart_symbol("leadership", " igv ", database_path)
            add_chart_symbol("leadership", "IGV", database_path)
            self.assertEqual(list_market_leadership_symbols(database_path).count("IGV"), 1)
            move_chart_symbol("leadership", "IGV", -1, database_path)
            self.assertEqual(list_market_leadership_symbols(database_path)[-2:], ["IGV", "SMH"])
            remove_chart_symbol("leadership", "RSP", database_path)
            self.assertNotIn("RSP", list_market_leadership_symbols(database_path))

            add_chart_symbol("sector_display", "MAGS", database_path)
            remove_chart_symbol("sector_display", "MAGS", database_path)
            self.assertNotIn("MAGS", list_sector_display_symbols(database_path))
            add_chart_symbol("sector_display", "MAGS", database_path)
            save_sector_display_symbols(["XLK", "XLF", "MAGS"], database_path)
            initialize_database(database_path)
            self.assertEqual(list_market_leadership_symbols(database_path), ["SPY", "QQQ", "DIA", "IWM", "IGV", "SMH"])
            self.assertEqual(list_sector_display_symbols(database_path), ["XLK", "XLF", "MAGS"])
            # The two chart preferences are independent from each other and spot quotes.
            self.assertEqual(list_market_symbols(database_path), ["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"])

    def test_chart_defaults_restore_and_tickers_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            initialize_database(database_path)
            add_chart_symbol("leadership", "IGV", database_path)
            add_chart_symbol("sector_display", "MAGS", database_path)
            self.assertEqual(restore_chart_symbol_defaults("leadership", database_path), ["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"])
            self.assertEqual(len(restore_chart_symbol_defaults("sector_display", database_path)), 11)
            with self.assertRaises(ValueError):
                add_chart_symbol("leadership", "bad ticker", database_path)

    def test_sector_classifications_are_additive_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            initialize_database(database_path)
            add_market_symbol("AAPL", database_path)
            save_portfolio_sector_classification("aapl", "Technology", database_path)
            initialize_database(database_path)
            self.assertEqual(list_market_symbols(database_path), ["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH", "AAPL"])
            self.assertEqual(list_portfolio_sector_classifications(database_path), {"AAPL": "Technology"})

    def test_multi_position_classifications_seed_and_manual_verification_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            initialize_database(database_path)
            ensure_portfolio_classifications(["ADSK", "QQQ", "TCEHY"], database_path)
            records = list_portfolio_classification_records(database_path)
            self.assertEqual(records["ADSK"]["sector"], "Technology - XLK")
            self.assertEqual(records["QQQ"]["sector"], "Diversified / Nasdaq-100")
            self.assertEqual(records["TCEHY"]["asset_type"], "Foreign/OTC Stock")
            records["ADSK"].update(classification_source="Manual", verification_status="Verified")
            save_portfolio_classification_records(records.values(), database_path)
            ensure_portfolio_classifications(["ADSK"], database_path)
            saved = list_portfolio_classification_records(database_path)["ADSK"]
            self.assertEqual(saved["classification_source"], "Manual")
            self.assertEqual(saved["verification_status"], "Verified")

    def test_market_symbol_preferences_are_ordered_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            initialize_database(database_path)
            self.assertEqual(list_market_symbols(database_path), ["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"])
            add_market_symbol("aapl", database_path)
            move_market_symbol("AAPL", -1, database_path)
            remove_market_symbol("RSP", database_path)
            initialize_database(database_path)
            self.assertEqual(list_market_symbols(database_path), ["SPY", "QQQ", "DIA", "IWM", "AAPL", "SMH"])

    def test_watchlist_item_is_saved_and_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            initialize_database(database_path)
            add_watchlist_item(" msft ", "Microsoft", database_path)
            items = list_watchlist(database_path)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["symbol"], "MSFT")
            self.assertEqual(items[0]["company_name"], "Microsoft")

    def test_cash_and_quotes_survive_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            initialize_database(database_path)
            save_cash_balance(1250.5, database_path)
            quote = {
                "symbol": "SPY",
                "price": 500.0,
                "change": 1.5,
                "change_percent": 0.3,
                "quote_date": "2025-01-02",
                "fetched_at": "2025-01-02T23:00:00+00:00",
                "freshness": "End-of-day quote",
            }
            save_cached_quote(quote, database_path)
            self.assertEqual(get_cash_balance(database_path), 1250.5)
            self.assertEqual(get_cached_quote("spy", database_path), quote)

    def test_custom_position_order_persists_and_reconciles_added_removed_tickers(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "portfolio.db"
            initialize_database(database_path)
            # Simulate holdings already present when this additive feature ships.
            connection = sqlite3.connect(database_path)
            connection.executemany(
                "INSERT INTO holdings(symbol,shares,average_cost,current_price) VALUES (?,?,?,?)",
                [("QQQ", 3, 100, 120), ("MSFT", 2, 100, 120), ("AAPL", 1, 100, 120)],
            )
            connection.commit()
            connection.close()
            self.assertEqual(list_portfolio_position_order(database_path), ["AAPL", "MSFT", "QQQ"])

            move_portfolio_position("QQQ", -1, database_path)
            self.assertEqual(list_portfolio_position_order(database_path), ["AAPL", "QQQ", "MSFT"])
            initialize_database(database_path)
            self.assertEqual(list_portfolio_position_order(database_path), ["AAPL", "QQQ", "MSFT"])

            save_holding("TSLA", 1, 50, database_path=database_path)
            self.assertEqual(list_portfolio_position_order(database_path), ["AAPL", "QQQ", "MSFT", "TSLA"])
            delete_holding("QQQ", database_path)
            self.assertEqual(list_portfolio_position_order(database_path), ["AAPL", "MSFT", "TSLA"])
            save_holding("QQQ", 4, 75, database_path=database_path)
            self.assertEqual(list_portfolio_position_order(database_path), ["AAPL", "MSFT", "TSLA", "QQQ"])

            before = {row["symbol"]: (row["shares"], row["average_cost"]) for row in list_holdings(database_path)}
            move_portfolio_position("QQQ", -1, database_path)
            after = {row["symbol"]: (row["shares"], row["average_cost"]) for row in list_holdings(database_path)}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

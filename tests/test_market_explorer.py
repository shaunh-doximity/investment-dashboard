import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from dashboard.database import initialize_database
from dashboard.data_access import list_market_explorer_favorites, remove_market_explorer_favorite, save_market_explorer_favorite
from dashboard.gpt_market_prompts import PROMPT_TOPICS, build_gpt_market_prompt
from dashboard.market_explorer import (basket_performance, compare_etfs, normalize_basket_config,
                                       standard_comparison_library, STANDARD_COMPARISONS,
                                       BROAD_EQUITY_INDEX_SYMBOLS, broad_index_chart_data,
                                       broad_index_comparison_library)
from dashboard.market_history import BROAD_MARKET_SYMBOLS, SECTOR_SYMBOLS


def bars(symbol, days, values):
    return [{"symbol": symbol, "trading_date": day.isoformat(), "close": value} for day, value in zip(days, values)]


class MarketExplorerTests(unittest.TestCase):
    def setUp(self):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
        days = [day for day in days if day.weekday() < 5]
        self.histories = {"SPY": bars("SPY", days, [100 + i for i in range(len(days))]),
                          "SMH": bars("SMH", days, [100 + i * 1.5 for i in range(len(days))]),
                          "RSP": bars("RSP", days[:-1], [100 + i for i in range(len(days)-1)])}

    def test_comparison_returns_and_common_date_alignment(self):
        result = compare_etfs(self.histories, "SMH", "SPY", 5)
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["dates"]), 6)
        self.assertAlmostEqual(result["cumulative_excess_pp"][-1], result["excess_pp"])
        self.assertGreater(result["excess_pp"], 0)
        incomplete = {"SPY": self.histories["SPY"], "SMH": self.histories["SMH"][:-5]}
        self.assertEqual(compare_etfs(incomplete, "SMH", "SPY", 20)["status"], "insufficient_history")

    def test_custom_basket_weights_and_validation(self):
        config = normalize_basket_config({"cyclical": {"XLY": 2, "XLK": 2}, "defensive": {"XLV": 1, "XLP": 1}})
        self.assertAlmostEqual(sum(config["cyclical"].values()), 1)
        with self.assertRaises(ValueError):
            normalize_basket_config({"cyclical": {"SPY": 1}, "defensive": {"XLV": 1}})
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
        days = [day for day in days if day.weekday() < 5]
        histories = {symbol: bars(symbol, days, [100 + i * (2 if symbol == "XLY" else 1) for i in range(len(days))]) for symbol in ("XLY", "XLK", "XLV", "XLP")}
        result = basket_performance(histories, {"cyclical": {"XLY": 1}, "defensive": {"XLV": 1}}, periods=2)
        self.assertEqual(result["status"], "available")
        self.assertLess(result["defensive_minus_cyclical_pp"], 0)

    def test_saved_pair_favorites_persist_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "prefs.sqlite"
            initialize_database(db)
            save_market_explorer_favorite("smh", "spy", 20, "excess", db)
            save_market_explorer_favorite("SMH", "SPY", 20, "excess", db)
            self.assertEqual(len(list_market_explorer_favorites(db)), 1)
            self.assertEqual(list_market_explorer_favorites(db)[0]["first"], "SMH")
            remove_market_explorer_favorite(0, db)
            self.assertEqual(list_market_explorer_favorites(db), [])

    def test_standard_comparison_library_covers_all_sectors_and_required_pairs(self):
        self.assertTrue(all((symbol, "SPY") in STANDARD_COMPARISONS for symbol in SECTOR_SYMBOLS))
        for pair in (("RSP", "SPY"), ("IWM", "SPY"), ("SMH", "SPY"),
                     ("SMH", "QQQ"), ("XLY", "XLP"), ("XLY", "SPY")):
            self.assertIn(pair, STANDARD_COMPARISONS)
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(390)]
        days = [day for day in days if day.weekday() < 5]
        universe = {"SPY", "RSP", "IWM", "QQQ", "SMH", *SECTOR_SYMBOLS}
        histories = {symbol: bars(symbol, days,
            [100 + index * (1 + (len(symbol) % 3) / 10) for index in range(len(days))])
            for symbol in universe}
        library = standard_comparison_library(histories, windows=(5, 20))
        self.assertEqual(len(library), len(STANDARD_COMPARISONS) * 2)
        self.assertEqual(library["XLY_XLP_20D"]["status"], "available")
        self.assertEqual(library["SMH_SPY_5D"]["as_of"], days[-1].isoformat())
        self.assertIn("relative_momentum_change_pp", library["SMH_SPY_20D"])

    def test_broad_index_universe_and_matched_window_returns(self):
        self.assertEqual(BROAD_EQUITY_INDEX_SYMBOLS, ("SPY", "RSP", "QQQ", "DIA", "IWM"))
        self.assertIn("DIA", BROAD_MARKET_SYMBOLS)
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
        days = [day for day in days if day.weekday() < 5]
        symbols = BROAD_EQUITY_INDEX_SYMBOLS
        histories = {symbol: bars(symbol, days,
            [100 + index * (1 + position * .1) for index in range(len(days))])
            for position, symbol in enumerate(symbols)}
        library = broad_index_comparison_library(histories, windows=(5, 20))
        self.assertEqual(len(library), 10)
        self.assertEqual(library["DIA_SPY_5D"]["observations"], 6)
        self.assertEqual(library["DIA_SPY_5D"]["as_of"], days[-1].isoformat())
        self.assertGreater(library["IWM_SPY_20D"]["excess_return_pp"], 0)
        self.assertEqual(library["SPY_SPY_20D"]["excess_return_pp"], 0)
        self.assertNotEqual(library["SPY_SPY_20D"]["cumulative_return_pct"], 0)

    def test_broad_index_chart_modes_units_visibility_and_missing_data(self):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(35)]
        days = [day for day in days if day.weekday() < 5]
        histories = {symbol: bars(symbol, days,
            [100 + index * (1 + position * .2) for index in range(len(days))])
            for position, symbol in enumerate(BROAD_EQUITY_INDEX_SYMBOLS)}
        normalized = broad_index_chart_data(histories, 5,
            "Normalized cumulative performance", ("SPY", "DIA"))
        self.assertEqual(normalized["status"], "available")
        self.assertEqual(len(normalized["dates"]), 6)
        from dashboard.cross_market import price_map
        spy_prices = price_map(histories["SPY"])
        self.assertAlmostEqual(normalized["series"]["SPY"][-1],
            (spy_prices[normalized["dates"][-1]] / spy_prices[normalized["dates"][0]] - 1) * 100)
        relative = broad_index_chart_data(histories, 5,
            "Relative cumulative performance vs SPY", ("DIA",))
        self.assertEqual(len(relative["series"]["DIA"][0]), 6)
        self.assertAlmostEqual(relative["series"]["DIA"][1][-1],
            broad_index_comparison_library(histories, (5,))["DIA_SPY_5D"]["excess_return_pp"])
        self.assertEqual(broad_index_chart_data(histories, 5,
            "Relative cumulative performance vs SPY", ())["status"], "no_visible_indexes")
        incomplete = {**histories, "DIA": histories["DIA"][:-6]}
        self.assertEqual(broad_index_chart_data(incomplete, 20,
            "Normalized cumulative performance", BROAD_EQUITY_INDEX_SYMBOLS)["status"], "missing_observations")

    def test_incomplete_dia_is_explicit_in_index_matrix(self):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(40)]
        days = [day for day in days if day.weekday() < 5]
        histories = {symbol: bars(symbol, days, [100 + i for i in range(len(days))])
                     for symbol in BROAD_EQUITY_INDEX_SYMBOLS if symbol != "DIA"}
        result = broad_index_comparison_library(histories, windows=(5,))
        self.assertEqual(result["DIA_SPY_5D"]["status"], "insufficient_history")

    def test_gpt_prompt_presets_are_copyable_evidence_requests_only(self):
        self.assertEqual(len(PROMPT_TOPICS), 6)
        prompt = build_gpt_market_prompt("Semiconductor valuation versus earnings expectations", comparison={"first":"SMH", "second":"SPY", "window":20, "as_of":"2026-10-30", "return_first":0.04, "return_second":0.02, "excess_pp":2})
        for phrase in ("observation date", "Counter-evidence", "Never invent", "correlation as causation", "SMH vs SPY"):
            self.assertIn(phrase.lower(), prompt.lower())


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from dashboard.database import initialize_database
from dashboard.data_access import (get_portfolio_monitor_settings, get_portfolio_strategy_horizon,
                                   save_portfolio_monitor_settings, save_portfolio_strategy_horizon)
from dashboard.portfolio_monitor import (monitor_alerts, price_position_status,
                                         order_holdings, resolve_approved_strategy,
                                         scenario_levels, target_range_text)


def record(horizon="Medium"):
    return {"investment_horizon": horizon, "investment_thesis": "Approved MCD thesis",
            "thesis_status": "Active", "target_price": None, "invalidation_price": 228.0,
            "structured_analysis_json": "{}"}


def scenarios():
    return [
        {"horizon": "Swing / short-term", "scenario": {"entry_price_low": 248.46, "entry_price_high": 248.46,
         "target_price_low": 260, "target_price_high": 265, "stop_price": 238, "holding_horizon": "weeks"}},
        {"horizon": "Medium-term", "scenario": {"entry_price_low": 248.46, "entry_price_high": 248.46,
         "target_price_low": 284.56, "target_price_high": 285, "stop_price": 228, "holding_horizon": "months"}},
        {"horizon": "Long-term", "scenario": {"entry_price_low": None, "entry_price_high": None,
         "target_price_low": None, "target_price_high": None, "stop_price": None,
         "fundamental_invalidation": "Thesis changes"}},
    ]


class PortfolioMonitorTests(unittest.TestCase):
    def test_sort_modes_are_display_only_and_diversified_funds_can_stay_last(self):
        holdings = [
            {"symbol": "QQQ", "shares": 10, "average_cost": 10},
            {"symbol": "MSFT", "shares": 2, "average_cost": 10},
            {"symbol": "AAPL", "shares": 1, "average_cost": 10},
        ]
        quotes = {"QQQ": {"price": 100}, "MSFT": {"price": 50}, "AAPL": {"price": 75}}
        classes = {"QQQ": {"asset_type": "ETF", "sector": "Diversified / Nasdaq-100"}}
        custom = ["MSFT", "QQQ", "AAPL"]
        self.assertEqual([r["symbol"] for r in order_holdings(holdings, "Custom Order", custom_order=custom)], custom)
        self.assertEqual([r["symbol"] for r in order_holdings(holdings, "Ticker")], ["AAPL", "MSFT", "QQQ"])
        self.assertEqual([r["symbol"] for r in order_holdings(holdings, "Position Size", quotes=quotes, classifications=classes)],
                         ["MSFT", "AAPL", "QQQ"])
        self.assertEqual(custom, ["MSFT", "QQQ", "AAPL"])

    def test_mcd_target_range_stop_and_rrr_sync_from_approved_horizon(self):
        view = resolve_approved_strategy(record(), scenarios(), current_price=248.46)
        self.assertEqual(view["horizon"], "Medium-term")
        self.assertEqual(target_range_text(view["scenario"]), "$284.56–$285.00")
        self.assertEqual(scenario_levels(view["scenario"])["stop"], 228.0)
        expected = ((284.56 - 248.46) / (248.46 - 228), (285 - 248.46) / (248.46 - 228))
        self.assertAlmostEqual(view["analysis"]["rrr_range"][0], expected[0])
        self.assertAlmostEqual(view["analysis"]["rrr_range"][1], expected[1])
        self.assertAlmostEqual(expected[0], 1.7644, places=3)
        self.assertAlmostEqual(expected[1], 1.7859, places=3)

    def test_primary_horizon_default_and_user_selection(self):
        self.assertEqual(resolve_approved_strategy(record(), scenarios())["horizon"], "Medium-term")
        selected = resolve_approved_strategy(record(), scenarios(), active_horizon="Long-term")
        self.assertEqual(selected["horizon"], "Long-term")

    def test_multiple_horizons_without_primary_require_selection(self):
        result = resolve_approved_strategy(record(""), scenarios())
        self.assertTrue(result["requires_selection"])
        result = resolve_approved_strategy(record(""), scenarios(), active_horizon="Swing / short-term")
        self.assertEqual(result["horizon"], "Swing / short-term")

    def test_legacy_strategy_mapping_and_draft_separation(self):
        legacy = {"investment_horizon": "Long", "investment_thesis": "Old approved thesis",
                  "entry_price_low": 100, "entry_price_high": 100, "target_price": 130,
                  "invalidation_price": 90, "draft": {"target_price": 999}}
        view = resolve_approved_strategy(legacy, [], current_price=110)
        self.assertEqual(view["horizon"], "Long-term")
        self.assertEqual(view["analysis"]["rrr_range"], (3, 3))
        self.assertNotEqual(scenario_levels(view["scenario"])["target_low"], 999)

    def test_stale_or_missing_quote_and_adjusted_reference(self):
        self.assertIsNone(resolve_approved_strategy(record(), scenarios(), current_price=250,
                                                   current_quote_stale=True)["analysis"]["remaining_range"])
        self.assertIsNone(resolve_approved_strategy(record(), scenarios())["analysis"]["remaining_range"])
        adjusted = {**record(), "structured_analysis_json": '{"market_price_reference":{"basis":"dividend-adjusted"}}'}
        result = resolve_approved_strategy(adjusted, scenarios(), current_price=250)
        self.assertFalse(result["quote_basis_compatible"])
        self.assertIsNone(result["analysis"]["remaining_range"])

    def test_price_position_and_monitor_alert_thresholds(self):
        scenario = scenarios()[1]["scenario"]
        self.assertIn("Near stop", price_position_status(234, scenario, stop_near_pct=3))
        self.assertIn("Near target", price_position_status(279, scenario, target_near_pct=3))
        self.assertIn("Target reached", price_position_status(286, scenario))
        self.assertIn("Stop breached", price_position_status(227, scenario))
        self.assertEqual(price_position_status(286, scenario, stale=True), ["Stale quote"])
        view = resolve_approved_strategy(record(), scenarios(), current_price=229)
        alerts = monitor_alerts({"thesis_status": "Active"}, view, 229, stale=False,
                                stop_near_pct=3, target_near_pct=3, remaining_rrr_limit=1)
        self.assertIn("Near stop", alerts)
        review = {**view, "thesis_status": "Review Needed"}
        self.assertIn("Thesis review needed", monitor_alerts(
            {}, review, None, stale=True, stop_near_pct=3, target_near_pct=3, remaining_rrr_limit=1))

    def test_horizon_and_alert_settings_persist_additively(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.sqlite"
            initialize_database(db)
            save_portfolio_strategy_horizon("mcd", "Long-term", db)
            save_portfolio_monitor_settings(4, 2, .8, db)
            initialize_database(db)
            self.assertEqual(get_portfolio_strategy_horizon("MCD", db), "Long-term")
            self.assertEqual(get_portfolio_monitor_settings(db), {
                "stop_near_pct": 4, "target_near_pct": 2, "remaining_rrr_below": .8})


if __name__ == "__main__":
    unittest.main()

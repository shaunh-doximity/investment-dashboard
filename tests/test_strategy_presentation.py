import json
import unittest

from dashboard.strategy_presentation import analyze_scenario, investment_logic, strategy_scenarios, summarize_thesis


class StrategyPresentationTests(unittest.TestCase):
    def test_scenario_rows_preserve_independent_horizons_and_target_ranges(self):
        strategy = {"investment_horizon": "Medium", "entry_price_low": 248.46,
                    "entry_price_high": 248.46, "target_price": None,
                    "invalidation_price": 228.0}
        rows = [{"horizon": "Medium-term", "scenario": {
            "entry_price_low": 248.46, "entry_price_high": 248.46,
            "target_price_low": 284.56, "target_price_high": 285.0,
            "stop_price": 228.0, "holding_window": "3-12 months",
        }}]
        result = strategy_scenarios(strategy, rows)
        self.assertEqual(result["medium_term"]["target_price_low"], 284.56)
        self.assertEqual(result["medium_term"]["target_price_high"], 285.0)
        self.assertIsNone(result["short_term"].get("entry_price_low"))
        self.assertIsNone(result["long_term"].get("target_price_low"))

    def test_older_draft_horizon_scenarios_are_read_without_copying_levels(self):
        strategy = {"structured_analysis_json": json.dumps({"horizon_scenarios": [
            {"horizon": "Long-term", "entry_price_low": 90, "entry_price_high": 95,
             "target_price": 130, "invalidation_price": 80, "holding_window": "2 years"},
        ]})}
        result = strategy_scenarios(strategy)
        self.assertEqual(result["long_term"]["target_price_low"], 130)
        self.assertEqual(result["long_term"]["stop_price"], 80)
        self.assertIsNone(result["medium_term"].get("target_price_low"))

    def test_mcd_planned_and_remaining_rrr_are_calculated_for_target_range(self):
        scenario = {"entry_price_low": 248.46, "entry_price_high": 248.46,
                    "target_price_low": 284.56, "target_price_high": 285.0,
                    "stop_price": 228.0}
        result = analyze_scenario(scenario, current_price=248.46)
        self.assertEqual(result["status"], "Actionable")
        self.assertAlmostEqual(result["rrr_range"][0], (284.56 - 248.46) / (248.46 - 228.0))
        self.assertAlmostEqual(result["remaining_range"][1], (285.0 - 248.46) / (248.46 - 228.0))
        self.assertIsNone(result["planned_rrr"])

    def test_missing_levels_name_the_missing_field_and_keep_rrr_null(self):
        result = analyze_scenario({"entry_price_low": 100, "entry_price_high": 100,
                                   "stop_price": 90})
        self.assertEqual(result["status"], "Incomplete")
        self.assertIn("target price or complete target range", result["missing"])
        self.assertIsNone(result["planned_rrr"])

    def test_stale_or_incompatible_quote_suppresses_only_remaining_rrr(self):
        scenario = {"entry_price_low": 100, "entry_price_high": 100,
                    "target_price_low": 120, "target_price_high": 120, "stop_price": 90}
        result = analyze_scenario(scenario, current_price=105, quote_is_stale=True)
        self.assertEqual(result["status"], "Conditional")
        self.assertIsNotNone(result["planned_rrr"])
        self.assertIsNone(result["remaining_rrr"])

    def test_invalid_long_order_and_crossed_stop_are_explicit(self):
        scenario = {"entry_price_low": 100, "entry_price_high": 100,
                    "target_price_low": 120, "target_price_high": 120, "stop_price": 105}
        self.assertEqual(analyze_scenario(scenario)["status"], "Invalidated")
        scenario["stop_price"] = 90
        self.assertEqual(analyze_scenario(scenario, current_price=89)["status"], "Invalidated")

    def test_thesis_preview_is_compact_and_original_text_is_unchanged(self):
        text = "First sentence explains the business. Second sentence describes valuation. Third sentence contains a catalyst."
        summary = summarize_thesis(text)
        self.assertEqual(summary, "First sentence explains the business. Second sentence describes valuation.")
        self.assertEqual(text.count("Third sentence"), 1)

    def test_logic_map_limits_items_and_marks_legacy_imports_unverified(self):
        strategy = {"bullish_scenario": "Pricing power supports margins. Repeat demand remains strong. Third point.",
                    "bearish_scenario": "Costs may compress margins.", "catalysts": "Product rollout.",
                    "stop_invalidation": "Two quarters of margin decline."}
        logic = investment_logic(strategy)
        self.assertEqual(len(logic["bullish_drivers"]), 3)
        self.assertEqual(logic["bullish_drivers"][0]["evidence_status"], "Imported / unverified")
        self.assertEqual(len(logic["invalidation_signals"]), 1)
        self.assertEqual(logic["invalidation_signals"][0]["explanation"], "Two quarters of margin decline.")

    def test_imported_verified_label_is_not_treated_as_independently_verified(self):
        logic = investment_logic({}, {"investment_logic": {"bullish_drivers": [
            {"label": "Demand", "explanation": "Sales may grow.", "evidence_status": "Verified"},
        ]}})
        self.assertEqual(logic["bullish_drivers"][0]["evidence_status"], "Imported / unverified")


if __name__ == "__main__":
    unittest.main()

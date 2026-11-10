import tempfile
import unittest
from pathlib import Path

from dashboard.data_access import (
    create_research_workspace,
    delete_holding,
    get_strategy_record,
    list_research_workspaces,
    list_strategy_revisions,
    save_holding,
    save_strategy_record,
)
from dashboard.database import initialize_database


def example_record(thesis: str) -> dict:
    return {
        "investment_thesis": thesis,
        "investment_horizon": "Long",
        "thesis_status": "Active",
        "catalysts": "New product cycle",
        "risks": "Competition",
        "entry_price_low": 90,
        "entry_price_high": 100,
        "target_price": 130,
        "stop_invalidation": "Margins decline",
        "position_sizing_notes": "Start small",
        "last_review_date": "2025-01-02",
        "valuation_notes": "Manual notes",
        "strategy_type": "Quality growth",
        "expected_holding_window": "6–12 months",
        "invalidation_price": 85,
        "max_planned_dollar_risk": 500,
        "bullish_scenario": "Margins expand",
        "bearish_scenario": "Demand weakens",
        "assumptions": "Revenue growth continues",
        "uncertainties": "No recent filing review",
        "ai_price_estimates": 1,
    }


class StrategyPersistenceTests(unittest.TestCase):
    def test_portfolio_level_edits_share_strategy_record_and_append_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "strategy.db"
            initialize_database(database_path)
            save_strategy_record("ABC", {"target_price": 120, "invalidation_price": 90}, database_path)
            before = list_strategy_revisions("ABC", database_path)
            save_strategy_record("ABC", {"target_price": 130, "invalidation_price": 95}, database_path)
            current = get_strategy_record("ABC", database_path)
            revisions = list_strategy_revisions("ABC", database_path)
            self.assertEqual(current["target_price"], 130)
            self.assertEqual(current["invalidation_price"], 95)
            self.assertEqual(len(revisions), len(before) + 1)
            self.assertIn(120, [row["target_price"] for row in revisions])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "strategy.db"
        initialize_database(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_changed_strategy_appends_revision_and_keeps_previous_version(self):
        create_research_workspace("msft", self.database_path)
        self.assertTrue(save_strategy_record("MSFT", example_record("First thesis"), self.database_path))
        self.assertTrue(save_strategy_record("MSFT", example_record("Revised thesis"), self.database_path))
        self.assertFalse(save_strategy_record("MSFT", example_record("Revised thesis"), self.database_path))

        revisions = list_strategy_revisions("MSFT", self.database_path)
        self.assertEqual(len(revisions), 2)
        self.assertEqual({item["investment_thesis"] for item in revisions}, {"First thesis", "Revised thesis"})
        self.assertEqual(revisions[0]["invalidation_price"], 85)
        self.assertEqual(revisions[0]["bullish_scenario"], "Margins expand")
        self.assertEqual(revisions[0]["assumptions"], "Revenue growth continues")
        self.assertEqual(revisions[0]["ai_price_estimates"], 1)

    def test_workspace_survives_position_removal(self):
        save_holding("MSFT", 1, 100, database_path=self.database_path)
        delete_holding("MSFT", self.database_path)
        self.assertIn("MSFT", list_research_workspaces(self.database_path))


if __name__ == "__main__":
    unittest.main()

import json
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import types

from dashboard.ai_providers import AIProviderError
from dashboard.ai_research import _parse_json, generate_strategy_draft
from dashboard.data_access import (
    delete_research_strategy_draft, get_research_strategy_draft,
    get_strategy_record, list_strategy_revisions, save_research_strategy_draft,
    save_strategy_record,
)
from dashboard.database import initialize_database
from dashboard.research_context import build_research_context


class StrategyDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "drafts.db"
        initialize_database(self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parser_accepts_json_fences_and_short_model_preamble(self):
        result = _parse_json('Draft follows:\n```json\n{"investment_thesis":"Test","target_price":null}\n```\n')
        self.assertEqual(result["investment_thesis"], "Test")
        self.assertIsNone(result["target_price"])

    def test_parser_accepts_one_json_object_with_surrounding_text(self):
        result = _parse_json('Here is the requested object: {"investment_horizon":"Long"} End of response.')
        self.assertEqual(result["investment_horizon"], "Long")

    def test_parser_skips_malformed_reasoning_object_and_finds_strategy_object(self):
        result = _parse_json('Reasoning: {"thought": } Final: {"investment_thesis":"T", "investment_horizon":"Long", "strategy_type":"Quality"}')
        self.assertEqual(result["investment_thesis"], "T")

    def test_parser_repairs_trailing_commas_without_changing_string_contents(self):
        result = _parse_json('Draft: {"note":"comma, } stays in text", "target_price":null,} done')
        self.assertEqual(result["note"], "comma, } stays in text")
        self.assertIsNone(result["target_price"])

    def test_parser_gives_actionable_message_for_truncated_output(self):
        with self.assertRaisesRegex(AIProviderError, "increase Max Output Tokens"):
            _parse_json('{"investment_thesis":"cut off"')

    def test_draft_persists_independently_of_approved_strategy(self):
        approved = {"investment_thesis": "Approved", "investment_horizon": "Long", "target_price": 150}
        save_strategy_record("MCD", approved, self.db)
        draft = {"ticker": "MCD", "investment_thesis": "Proposed", "target_price": None, "provider": "deepseek"}
        save_research_strategy_draft("mcd", draft, database_path=self.db)
        save_research_strategy_draft("MSFT", {"ticker": "MSFT", "investment_thesis": "Separate workspace"}, database_path=self.db)
        self.assertEqual(get_strategy_record("MCD", self.db)["investment_thesis"], "Approved")
        initialize_database(self.db)  # simulates app restart; schema/data survive
        self.assertEqual(get_research_strategy_draft("MCD", self.db), draft)
        self.assertEqual(get_research_strategy_draft("MSFT", self.db)["investment_thesis"], "Separate workspace")
        self.assertEqual(len(list_strategy_revisions("MCD", self.db)), 1)
        self.assertTrue(delete_research_strategy_draft("MCD", self.db))
        self.assertIsNone(get_research_strategy_draft("MCD", self.db))

    def test_explicit_approval_persists_structured_details_in_revision(self):
        save_strategy_record("MCD", {"investment_thesis": "Approved", "investment_horizon": "Long"}, self.db)
        structured = json.dumps({"ticker": "MCD", "fundamental_analysis_notes": "Evidence gap"})
        save_strategy_record("MCD", {"investment_thesis": "New approved", "investment_horizon": "Medium",
                                      "structured_analysis_json": structured}, self.db)
        self.assertEqual(json.loads(list_strategy_revisions("MCD", self.db)[0]["structured_analysis_json"])["ticker"], "MCD")

    def test_strategy_output_preserves_missing_levels_and_calculates_rrr_in_python(self):
        payload = {
            "investment_thesis": "Potential thesis from discussion", "strategy_type": "Quality",
            "investment_horizon": "Long", "expected_holding_window": "12 months",
            "bullish_scenario": "Growth continues", "bearish_scenario": "Margins weaken",
            "catalysts": "Product adoption", "risks": "Valuation", "stop_invalidation": "Thesis deteriorates",
            "entry_price_low": None, "entry_price_high": None, "target_price": None,
            "invalidation_price": None, "assumptions": "User discussion only", "uncertainties": "Fundamentals unavailable",
            "fundamental_analysis_notes": "No verified filings available",
            "horizon_scenarios": [{"horizon": "Long-term", "thesis": "Long view", "target_price": None,
                                    "invalidation_price": None, "entry_price_low": None, "entry_price_high": None}],
        }
        fake = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text=json.dumps(payload))
        conversation = [{"role": "user", "content": "I think MCD can grow, but the valuation worries me."},
                        {"role": "assistant", "content": "Let's examine the assumptions."}]
        with patch("dashboard.ai_research.generate_text", return_value=fake) as request:
            result = generate_strategy_draft("MCD", provider="deepseek", model="deepseek-chat", max_output_tokens=1200,
                                             conversation_context=conversation)
        self.assertIsNone(result["target_price"])
        self.assertIsNone(result["invalidation_price"])
        self.assertEqual(result["calculated_setup"]["status"], "missing_data")
        self.assertEqual(result["horizon_scenarios"][0]["target_price"], None)
        self.assertIn("I think MCD", request.call_args.kwargs["prompt"])

    def test_malformed_schema_is_rejected_without_values(self):
        fake = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text=json.dumps({"investment_horizon": "Soon"}))
        with patch("dashboard.ai_research.generate_text", return_value=fake):
            with self.assertRaises(AIProviderError):
                generate_strategy_draft("MCD", provider="deepseek", model="deepseek-chat", max_output_tokens=800)

    def test_current_rrr_uses_latest_stale_alpha_quote_and_marks_its_date(self):
        payload = {
            "investment_thesis": "Thesis", "strategy_type": "Quality", "investment_horizon": "Medium",
            "expected_holding_window": "6 months", "bullish_scenario": "Upside", "bearish_scenario": "Downside",
            "catalysts": "Catalyst", "risks": "Risk", "stop_invalidation": "Break thesis",
            "entry_price_low": 100, "entry_price_high": 100, "target_price": 130,
            "invalidation_price": 80, "assumptions": "Assumptions", "uncertainties": "Unverified",
        }
        fake = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text=json.dumps(payload))
        with patch("dashboard.ai_research.generate_text", return_value=fake):
            result = generate_strategy_draft("MCD", provider="deepseek", model="deepseek-chat", max_output_tokens=800,
                                             quote={"price": 100, "quote_date": "2000-01-01"})
        self.assertAlmostEqual(result["calculated_setup"]["current_rrr"], 1.5)
        self.assertTrue(result["current_quote_stale"])
        self.assertEqual(result["current_quote_date"], "2000-01-01")


class ResearchPriceProvenanceTests(unittest.TestCase):
    def _bars(self, latest_date):
        rows, current = [], date.fromisoformat(latest_date)
        while len(rows) < 210:
            if current.weekday() < 5:
                rows.append({"trading_date": current.isoformat(), "close": 250 + len(rows) * 0.1})
            current -= timedelta(days=1)
        return list(reversed(rows))

    def _context(self, quote_date, history_date):
        with patch("dashboard.research_context.load_history", side_effect=lambda symbol: self._bars(history_date)), \
             patch("dashboard.research_context.list_portfolio_classification_records", return_value={}), \
             patch("dashboard.research_context.list_upcoming_market_events", return_value=[]), \
             patch("dashboard.research_context.list_watchlist", return_value=[]):
            return build_research_context("MCD", {"price": 300.0, "quote_date": quote_date})

    def test_different_observation_dates_are_normal_freshness(self):
        context = self._context("2026-10-23", "2026-10-22")
        self.assertEqual(context["price_reconciliation"]["status"], "different_observation_dates_normal_freshness")
        self.assertEqual(context["historical_source"]["latest_observation_date"], "2026-10-22")
        self.assertEqual(context["cached_quote"]["provider"], "Alpha Vantage")

    def test_same_date_price_difference_is_not_reported_as_discrepancy(self):
        context = self._context("2026-10-23", "2026-10-23")
        self.assertEqual(context["price_reconciliation"]["status"], "same_date_different_basis_unverified")
        self.assertIn("not compared", context["price_reconciliation"]["interpretation"])
        self.assertEqual(context["price_reconciliation"]["technical_price_basis"], "split_and_dividend_adjusted")

    def test_old_quote_is_marked_stale_and_not_reconciled(self):
        context = self._context("2000-01-01", "2026-10-23")
        self.assertTrue(context["cached_quote"]["stale"])
        self.assertEqual(context["price_reconciliation"]["status"], "different_observation_dates_normal_freshness")


if __name__ == "__main__":
    unittest.main()

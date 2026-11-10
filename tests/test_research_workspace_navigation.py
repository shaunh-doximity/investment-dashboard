from pathlib import Path
from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from dashboard.navigation import RESEARCH_TAB, TAB_LABELS, TAB_STATE_KEY, WORKSPACE_STATE_KEY
from dashboard.strategy_import import json_template


class ResearchWorkspaceNavigationTests(unittest.TestCase):
    def _context(self, symbol, quote=None, strategy=None):
        return {
            "ticker": symbol, "company_name": None, "as_of_utc": "2026-01-01T00:00:00+00:00",
            "cached_quote": None,
            "tiingo_adjusted_daily_history": {"latest_adjusted_close": None, "latest_history_date": None},
            "spy_relative_performance": {}, "sector_rotation": None,
            "upcoming_earnings_events": [], "saved_user_strategy": None,
            "verified_external_evidence": [],
            "external_evidence_status": "No verified fundamentals or news source is configured.",
            "calculation_notes": "Test context.",
        }

    def test_watchlist_open_workspace_displays_the_requested_ticker(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        with patch("dashboard.pages.research.list_research_workspaces", return_value=["NVDA"]), \
             patch("dashboard.pages.research.create_research_workspace"), \
             patch("dashboard.pages.research.list_watchlist", return_value=[]), \
             patch("dashboard.pages.research.list_holdings", return_value=[]), \
             patch("dashboard.pages.research.list_cached_quotes", return_value={}), \
             patch("dashboard.pages.research.get_strategy_record", return_value=None), \
             patch("dashboard.pages.research.list_research_chat_messages", return_value=[]), \
             patch("dashboard.pages.research.list_research_strategy_scenarios", return_value=[]), \
             patch("dashboard.pages.research.build_research_context", side_effect=self._context), \
             patch("dashboard.pages.research.answer_research_question") as chat_call:
            result = AppTest.from_file(str(app_path))
            result.session_state[TAB_STATE_KEY] = RESEARCH_TAB
            result.run()
            self.assertFalse(result.exception)
            result.button(key="research_open_NVDA").click().run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state[WORKSPACE_STATE_KEY], "NVDA")
            self.assertEqual(result.session_state[TAB_STATE_KEY], RESEARCH_TAB)
            self.assertTrue(any("NVDA" in item.value for item in result.subheader))
            chat_call.assert_not_called()

    def test_import_strategy_is_primary_and_workspace_rerun_does_not_call_ai(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        with patch("dashboard.pages.research.list_research_workspaces", return_value=["MCD"]), \
             patch("dashboard.pages.research.list_watchlist", return_value=[{"symbol": "MCD", "company_name": "McDonald's"}]), \
             patch("dashboard.pages.research.list_holdings", return_value=[]), \
             patch("dashboard.pages.research.list_cached_quotes", return_value={}), \
             patch("dashboard.pages.research.get_strategy_record", return_value=None), \
             patch("dashboard.pages.research.list_research_chat_messages", return_value=[]), \
             patch("dashboard.pages.research.list_research_strategy_scenarios", return_value=[]), \
             patch("dashboard.pages.research.list_strategy_revisions", return_value=[]), \
             patch("dashboard.pages.research.get_buy_zone_alert", return_value=None), \
             patch("dashboard.pages.research.save_strategy_record") as persist_approved, \
             patch("dashboard.pages.research.build_research_context", side_effect=self._context), \
             patch("dashboard.pages.research.answer_research_question") as chat:
            app = AppTest.from_file(str(app_path))
            app.session_state[TAB_STATE_KEY] = RESEARCH_TAB
            app.session_state[WORKSPACE_STATE_KEY] = "MCD"
            app.run()
            self.assertFalse(app.exception)
            self.assertIn("Import Strategy", [button.label for button in app.button])
            self.assertNotIn("Generate Strategy from Chat", [button.label for button in app.button])
            chat.assert_not_called()
            persist_approved.assert_not_called()

    def test_compact_scenario_cards_render_saved_target_range_and_quote_without_persisting(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        scenario_rows = [
            {"horizon": "Swing / short-term", "scenario": {"entry_price_low": None, "entry_price_high": None,
             "target_price_low": None, "target_price_high": None, "stop_price": None}},
            {"horizon": "Medium-term", "scenario": {"entry_price_low": 248.46, "entry_price_high": 248.46,
             "target_price_low": 284.56, "target_price_high": 285.0, "stop_price": 228.0,
             "holding_window": "3-12 months"}},
            {"horizon": "Long-term", "scenario": {"entry_price_low": None, "entry_price_high": None,
             "target_price_low": None, "target_price_high": None, "stop_price": None}},
        ]
        detail = {"schema_version": 1, "ticker": "MCD", "research_date": "2026-10-23",
                  "market_price_reference": {"price": 248.46, "date": "2026-10-23",
                    "basis": "NYSE regular-session closing price in USD", "source": "https://example.com/close"},
                  "scenarios": {}}
        strategy = {"investment_thesis": "First sentence. Second sentence. Long detail retained.",
                    "investment_horizon": "Medium", "thesis_status": "Active", "strategy_type": "Value",
                    "expected_holding_window": "3-12 months", "last_review_date": "2026-10-23",
                    "entry_price_low": 248.46, "entry_price_high": 248.46, "target_price": None,
                    "invalidation_price": 228.0, "structured_analysis_json": json.dumps(detail),
                    "catalysts": "Product launch.", "risks": "Competition."}
        quote = {"symbol": "MCD", "price": 248.46, "quote_date": "2026-10-23",
                 "freshness": "End-of-day quote", "fetched_at": "2026-10-23T21:00:00+00:00"}
        with patch("dashboard.pages.research.list_research_workspaces", return_value=["MCD"]), \
             patch("dashboard.pages.research.list_watchlist", return_value=[{"symbol": "MCD", "company_name": "McDonald's"}]), \
             patch("dashboard.pages.research.list_holdings", return_value=[]), \
             patch("dashboard.pages.research.list_cached_quotes", return_value={"MCD": quote}), \
             patch("dashboard.pages.research.get_strategy_record", return_value=strategy), \
             patch("dashboard.pages.research.list_research_chat_messages", return_value=[]), \
             patch("dashboard.pages.research.list_research_strategy_scenarios", side_effect=lambda symbol, history=False: [] if history else scenario_rows), \
             patch("dashboard.pages.research.list_strategy_revisions", return_value=[]), \
             patch("dashboard.pages.research.get_buy_zone_alert", return_value=None), \
             patch("dashboard.pages.research.build_research_context", side_effect=self._context), \
             patch("dashboard.pages.research.save_strategy_record") as save_strategy, \
             patch("dashboard.pages.research.answer_research_question") as chat:
            app = AppTest.from_file(str(app_path))
            app.session_state[TAB_STATE_KEY] = RESEARCH_TAB
            app.session_state[WORKSPACE_STATE_KEY] = "MCD"
            app.run()
            self.assertFalse(app.exception)
            rendered = " ".join([x.value for x in app.markdown] + [x.value for x in app.caption])
            self.assertIn("284.56", rendered)
            self.assertIn("285.00", rendered)
            self.assertIn("Short-term", rendered)
            self.assertIn("Medium-term", rendered)
            self.assertIn("Long-term", rendered)
            self.assertIn("Latest reference: $248.46", rendered)
            self.assertIn("First sentence. Second sentence.", rendered)
            self.assertIn("Long detail retained", rendered)  # still available under Full Research & Sources
            self.assertTrue(any(metric.label == "Planned RRR" and "1.76" in metric.value for metric in app.metric))
            app.number_input(key="sim_target_MCD").set_value(300.0).run()
            self.assertFalse(app.exception)
            save_strategy.assert_not_called()
            chat.assert_not_called()

    def test_strategy_import_preview_is_local_and_does_not_save_until_approval(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        with patch("dashboard.pages.research.list_research_workspaces", return_value=["MCD"]), \
             patch("dashboard.pages.research.list_watchlist", return_value=[]), \
             patch("dashboard.pages.research.list_holdings", return_value=[]), \
             patch("dashboard.pages.research.list_cached_quotes", return_value={}), \
             patch("dashboard.pages.research.get_strategy_record", return_value=None), \
             patch("dashboard.pages.research.list_research_chat_messages", return_value=[]), \
             patch("dashboard.pages.research.list_research_strategy_scenarios", return_value=[]), \
             patch("dashboard.pages.research.list_strategy_revisions", return_value=[]), \
             patch("dashboard.pages.research.get_buy_zone_alert", return_value=None), \
             patch("dashboard.pages.research.build_research_context", side_effect=self._context), \
             patch("dashboard.pages.research.answer_research_question") as chat, \
             patch("dashboard.pages.research.approve_import") as approve:
            app = AppTest.from_file(str(app_path))
            app.session_state[TAB_STATE_KEY] = RESEARCH_TAB
            app.session_state[WORKSPACE_STATE_KEY] = "MCD"
            app.run()
            app.button(key="import_strategy_MCD").click().run()
            template = json.loads(json_template("MCD"))
            template["investment_thesis"] = "Test thesis for import preview"
            app.text_area(key="strategy_import_json_MCD").set_value(json.dumps(template)).run()
            app.button(key="validate_strategy_import_MCD").click().run()
            self.assertFalse(app.exception)
            self.assertIn("Import & Save", [button.label for button in app.button])
            self.assertTrue(app.dataframe)
            chat.assert_not_called()
            approve.assert_not_called()

    def test_market_home_open_workspace_queues_and_displays_selected_ticker(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        with patch("dashboard.pages.home.create_research_workspace"), \
             patch("dashboard.pages.home.list_market_symbols", return_value=["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"]), \
             patch("dashboard.pages.home.list_market_leadership_symbols", return_value=["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"]), \
             patch("dashboard.pages.home.list_holdings", return_value=[]), \
             patch("dashboard.pages.home.list_watchlist", return_value=[]), \
             patch("dashboard.pages.research.list_research_workspaces", return_value=["SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"]), \
             patch("dashboard.pages.research.list_watchlist", return_value=[]), \
             patch("dashboard.pages.research.list_holdings", return_value=[]), \
             patch("dashboard.pages.research.list_cached_quotes", return_value={}), \
             patch("dashboard.pages.research.get_strategy_record", return_value=None), \
             patch("dashboard.pages.research.list_research_chat_messages", return_value=[]), \
             patch("dashboard.pages.research.list_research_strategy_scenarios", return_value=[]), \
             patch("dashboard.pages.research.build_research_context", side_effect=self._context), \
             patch("dashboard.pages.home.load_history", return_value=[]), \
             patch("dashboard.pages.home.TiingoDailyProvider.from_environment", return_value=None), \
             patch("dashboard.pages.home.fred_api_configured", return_value=False), \
             patch("dashboard.pages.home.list_cached_quotes", return_value={}):
            result = AppTest.from_file(str(app_path)).run()
            self.assertFalse(result.exception)
            result.button(key="market_home_open_workspace").click().run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state[TAB_STATE_KEY], RESEARCH_TAB)
            ticker = result.session_state[WORKSPACE_STATE_KEY]
            self.assertIn(ticker, ("SPY", "RSP", "QQQ", "DIA", "IWM", "SMH"))
            self.assertTrue(any(ticker in item.value for item in result.subheader))

    def test_portfolio_holding_opens_its_research_workspace(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        holding = {"id": 9001, "symbol": "COST", "shares": 2.0, "average_cost": 700.0,
                   "investment_horizon": "Long", "thesis_status": "Active"}
        patch_specs = [
            ("dashboard.pages.portfolio.list_holdings", {"return_value": [holding]}),
            ("dashboard.pages.portfolio.ensure_portfolio_classifications", {}),
            ("dashboard.pages.portfolio.list_cached_quotes", {"return_value": {}}),
            ("dashboard.pages.portfolio.get_cash_balance", {"return_value": 1000.0}),
            ("dashboard.pages.portfolio.load_history", {"return_value": []}),
            ("dashboard.pages.portfolio.get_strategy_record", {"return_value": None}),
            ("dashboard.pages.portfolio.list_portfolio_classification_records", {"return_value": {}}),
            ("dashboard.pages.portfolio._render_history_status", {}),
            ("dashboard.pages.portfolio._render_sector_exposure", {}),
            ("dashboard.pages.portfolio._render_rotation_connection", {}),
            ("dashboard.pages.portfolio._render_downside_summary", {}),
            ("dashboard.pages.portfolio._render_sector_classification_editor", {}),
            ("dashboard.pages.portfolio._render_risk_attribution", {}),
            ("dashboard.pages.portfolio.market_risk.render", {}),
            ("dashboard.pages.research.list_research_workspaces", {"return_value": ["COST"]}),
            ("dashboard.pages.research.list_watchlist", {"return_value": []}),
            ("dashboard.pages.research.list_holdings", {"return_value": []}),
            ("dashboard.pages.research.list_cached_quotes", {"return_value": {}}),
            ("dashboard.pages.research.get_strategy_record", {"return_value": None}),
            ("dashboard.pages.research.list_research_chat_messages", {"return_value": []}),
            ("dashboard.pages.research.list_research_strategy_scenarios", {"return_value": []}),
            ("dashboard.pages.research.build_research_context", {"side_effect": self._context}),
        ]
        with ExitStack() as stack:
            for target, kwargs in patch_specs:
                stack.enter_context(patch(target, **kwargs))
            result = AppTest.from_file(str(app_path))
            result.session_state[TAB_STATE_KEY] = TAB_LABELS[1]
            result.run()
            self.assertFalse(result.exception)
            result.button(key="portfolio_open_9001").click().run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state[TAB_STATE_KEY], RESEARCH_TAB)
            self.assertEqual(result.session_state[WORKSPACE_STATE_KEY], "COST")
            self.assertTrue(any("COST" in item.value for item in result.subheader))


if __name__ == "__main__":
    unittest.main()

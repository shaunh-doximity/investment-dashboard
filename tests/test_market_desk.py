import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashboard.database import initialize_database
from dashboard.data_access import (
    list_market_section_chat, save_market_section_chat_message,
)
from dashboard.market_chat import build_market_chat_context
from dashboard.market_chat import parse_premarket_what_if
from dashboard.market_historical_context import (
    completed_month_relative_history, completed_month_seasonality, summarize_vix_stress_history,
)
from dashboard.market_scenarios import (
    evaluate_scenario_conditions, extract_market_symbols,
    list_market_research_notes, list_market_scenario_revisions, list_market_scenarios,
    list_market_user_observations, normalize_observation_time,
    save_approved_market_scenario, save_market_research_note, save_market_user_observation,
    validate_user_observation,
)


def _scenario(**overrides):
    result = {
        "title": "Semiconductor recovery with broad participation",
        "hypothesis": "Technology may recover without an immediate leadership handoff.",
        "market_context": "SPY and sector breadth are the local context.",
        "if_conditions": "SMH/SPY and RSP/SPY both improve over five sessions.",
        "then_interpretation": "Participation may be broadening.",
        "competing_interpretation": "A short covering bounce could explain the move.",
        "indicators_to_monitor": ["SMH/SPY", "RSP/SPY", "VIX"],
        "invalidation_conditions": "Relative strength rolls over and breadth narrows.",
        "review_horizon": "Next 5–20 completed trading sessions",
        "source_observations": [], "source_links": [], "condition_rules": [],
        "status": "Watching", "origin_conversation_id": "market-intelligence-desk-conversation-v1",
        "origin_message_id": 8,
    }
    result.update(overrides)
    return result


def _snapshot():
    return {
        "fingerprint": "snapshot-a", "as_of": "2026-11-04", "schema_version": 1,
        "sections": {
            "relative_strength": {"as_of": "2026-11-04", "source": "Tiingo", "metrics": {
                "rsp_spy": {"value": 1.2, "unit": "pp", "window": "5D", "as_of": "2026-11-04", "status": "available"},
                "iwm_spy": {"value": -0.5, "unit": "pp", "window": "5D", "as_of": "2026-11-04", "status": "available"}}},
            "rates_macro": {"as_of": "2026-11-04", "metrics": {
                "ten_year_change_bp": {"value": -6, "unit": "bp", "window": "5D", "as_of": "2026-11-04", "status": "available"}}},
            "catalysts": {"events": [{"name": "CPI", "date": "2026-11-10", "status": "Confirmed"}]},
        },
        "view_state": {"relative_strength": {"first": "SMH", "second": "SPY", "window": 20}},
    }


class MarketDeskScenarioTests(unittest.TestCase):
    def test_observations_are_user_supplied_and_linked_to_chat_message(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "desk.sqlite"
            initialize_database(db)
            message_id = save_market_section_chat_message(
                section_id="market_wide", snapshot_fingerprint="market-intelligence-desk-conversation-v1",
                request_id="turn-1", role="user", content="SMH is down 2% premarket.", database_path=db)
            observation = validate_user_observation(
                observation_text="SMH is down 2% premarket.",
                observed_at=datetime(2026, 11, 4, 8, 15), session_type="premarket",
                symbols=extract_market_symbols("$SMH is down 2% premarket."))
            saved = save_market_user_observation(observation=observation,
                origin_conversation_id="market-intelligence-desk-conversation-v1",
                origin_message_id=message_id, database_path=db)
            self.assertEqual(saved["verification_status"], "user_supplied_unverified")
            self.assertEqual(saved["symbols"], ["SMH"])
            context = build_market_chat_context(_snapshot(), "market_wide", None, None,
                list_market_section_chat("market_wide", "market-intelligence-desk-conversation-v1", database_path=db),
                "Could this reflect election risk?", user_observations=[saved])
            self.assertIn("user_supplied_observations", context)
            self.assertEqual(context["snapshot_as_of"], "2026-11-04")
            self.assertEqual(context["user_supplied_observations"][0]["verification_status"],
                             "user_supplied_unverified")
            self.assertNotIn("holdings", str(context).lower())

    def test_market_wide_context_is_compact_and_carries_saved_briefing_scenarios_and_history(self):
        context = build_market_chat_context(_snapshot(), "market_wide", None,
            {"generated_at": "2026-11-04T12:00Z", "snapshot": {"as_of": "2026-11-04"},
             "report_text": "saved report", "sources": []},
            [{"role": "user", "content": "Why are yields down?"}], "Follow up", scenario_notes=[{"title": "Watch SPY/RSP"}])
        self.assertEqual(context["section_id"], "market_wide")
        self.assertIsNone(context["saved_briefing"])
        self.assertEqual(context["saved_scenarios"][0]["title"], "Watch SPY/RSP")
        self.assertEqual(context["recent_messages"][-1]["content"], "Why are yields down?")
        self.assertIn("catalysts", context["section_data"])
        self.assertLess(len(str(context)), 30000)

    def test_recent_web_sources_are_reused_as_dated_chat_context(self):
        context = build_market_chat_context(_snapshot(), "market_wide", None, None,
            [{"role": "assistant", "content": "Fed statement discussed a slower pace.",
              "created_at": "2026-11-04T13:00:00+00:00", "source_metadata": [
                  {"title": "Federal Reserve statement", "url": "https://www.federalreserve.gov/example", "date": "2026-11-04"}]}],
            "What changed?")
        source = context["recent_messages"][0]["retrieved_sources"][0]
        self.assertEqual(source["date"], "2026-11-04")
        self.assertTrue(source["url"].startswith("https://"))

    def test_observation_timestamps_normalize_dst_and_reject_nonexistent_local_times(self):
        normalized = normalize_observation_time(datetime(2026, 11, 4, 8, 30))
        self.assertEqual(normalized, "2026-11-04T13:30:00+00:00")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            normalize_observation_time(datetime(2026, 3, 8, 2, 30))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            normalize_observation_time(datetime(2026, 11, 1, 1, 30))

    def test_scenarios_require_explicit_approval_keep_revisions_and_trace_source(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "desk.sqlite"
            initialize_database(db)
            saved = save_approved_market_scenario(_scenario(), database_path=db)
            self.assertEqual(saved["revision_number"], 1)
            self.assertEqual(len(list_market_scenarios(database_path=db)), 1)
            changed = save_approved_market_scenario({**saved, "hypothesis": "Revised after new breadth evidence."},
                scenario_id=saved["scenario_id"], database_path=db)
            revisions = list_market_scenario_revisions(saved["scenario_id"], database_path=db)
            self.assertEqual(changed["revision_number"], 2)
            self.assertEqual(len(revisions), 2)
            self.assertEqual(revisions[-1]["scenario"]["origin_message_id"], 8)

    def test_scenario_condition_evaluation_is_local_and_reports_data_gaps(self):
        scenario = _scenario(condition_rules=[{"metric_ref": "relative_strength.rsp_spy", "operator": ">=", "threshold": 1.0, "unit": "pp"}])
        result = evaluate_scenario_conditions(scenario, _snapshot())
        self.assertEqual(result["status"], "Condition met")
        missing = evaluate_scenario_conditions({**scenario, "condition_rules": [{"metric_ref": "vix.not_here", "operator": ">", "threshold": 1, "unit": "points"}]}, _snapshot())
        self.assertEqual(missing["status"], "Awaiting data")
        self.assertIn("hypotheses remain unproven", result["reason"])

    def test_market_ui_has_one_desk_and_one_render_call_without_auto_analysis(self):
        from dashboard import market_monitor_ui as ui
        import inspect
        source = inspect.getsource(ui._render_overview)
        self.assertEqual(source.count("render_saved_market_briefing("), 1)
        self.assertIn("_render_market_wide_chat", inspect.getsource(ui.render_saved_market_briefing))
        self.assertIn("_render_premarket_what_if", inspect.getsource(ui.render_saved_market_briefing))
        self.assertNotIn("render_market_scenario_notebook", inspect.getsource(ui.render_saved_market_briefing))
        self.assertNotIn("Develop Scenario from this answer", inspect.getsource(ui._render_market_wide_chat))
        self.assertIn("AI Market Intelligence Desk", inspect.getsource(ui.render_saved_market_briefing))
        self.assertGreater(source.find("render_saved_market_briefing("), source.find("_render_vix_monitor"))

    def test_market_desk_uses_responsive_briefing_chat_columns_and_bounded_history(self):
        from dashboard import market_monitor_ui as ui
        import inspect

        self.assertEqual(ui.MARKET_DESK_LAYOUT["column_ratio"], (3, 1))
        self.assertTrue(ui.MARKET_DESK_LAYOUT["responsive_stack"])
        self.assertGreaterEqual(ui.MARKET_DESK_LAYOUT["chat_history_height_px"], 450)
        self.assertLessEqual(ui.MARKET_DESK_LAYOUT["chat_history_height_px"], 650)

        render_source = inspect.getsource(ui.render_saved_market_briefing)
        self.assertIn('st.columns(MARKET_DESK_LAYOUT["column_ratio"]', render_source)
        self.assertLess(render_source.index("with briefing_column:"), render_source.index("with chat_column:"))
        self.assertLess(render_source.index("with chat_column:"), render_source.index("_render_premarket_what_if("))
        self.assertLess(render_source.index("_render_premarket_what_if("), render_source.index('st.expander("Full Research'))

        chat_source = inspect.getsource(ui._render_market_wide_chat)
        history_container = chat_source.index('with st.container(height=MARKET_DESK_LAYOUT["chat_history_height_px"], border=True):')
        question_input = chat_source.index('question = st.text_area("Ask about the market"')
        ask_action = chat_source.index('st.button("Ask AI"')
        self.assertLess(history_container, question_input)
        self.assertLess(question_input, ask_action)
        self.assertIn('st.text_area("Ask about the market", key="market_desk_question", height=68', chat_source)

    def test_market_desk_chat_action_is_explicit_and_premarket_remains_independent(self):
        from dashboard import market_monitor_ui as ui
        import inspect

        chat_source = inspect.getsource(ui._render_market_wide_chat)
        premarket_source = inspect.getsource(ui._render_premarket_what_if)
        self.assertIn('if st.button("Ask AI", key="market_desk_ask_ai"', chat_source)
        self.assertIn('label = "Analyze Premarket Setup" if not messages else "Analyze Premarket Setup / Ask Follow-up"', premarket_source)
        self.assertIn('key="premarket_analyze_setup"', premarket_source)
        self.assertIn("PREMARKET_CHAT_KEY", premarket_source)
        self.assertIn("MARKET_DESK_CHAT_KEY", chat_source)

    def test_daily_refresh_gap_warning_names_sources_and_excludes_stock_symbols(self):
        from dashboard.market_monitor_ui import _daily_market_refresh_gaps
        result = {
            "historical": {
                "diagnostics": [
                    {"symbol": "SPY", "final_freshness_state": "Provider publication pending"},
                    {"symbol": "AMZN", "final_freshness_state": "Unavailable"},
                ],
                "errors": {"SPY": "provider timeout", "AMZN": "provider timeout"},
            },
            "vix": {"errors": {"CBOE": "publication pending"}},
            "treasury": {"errors": {}},
            "calendar": {"errors": {"BLS": "calendar unavailable"}},
        }
        gaps = _daily_market_refresh_gaps(result)
        self.assertTrue(any("ETF SPY" in gap for gap in gaps))
        self.assertTrue(any("VIX CBOE" in gap for gap in gaps))
        self.assertTrue(any("CALENDAR BLS" in gap for gap in gaps))
        self.assertFalse(any("AMZN" in gap for gap in gaps))

    def test_opted_in_post_refresh_analysis_runs_without_second_confirmation(self):
        from dashboard import market_monitor_ui as ui
        import inspect
        source = inspect.getsource(ui._render_gpt_research)
        self.assertIn('daily_refresh_analysis = bool(st.session_state.pop("_daily_market_refresh_run_ai", False))', source)
        self.assertIn("if run_clicked or retry_clicked or force_clicked or daily_refresh_analysis:", source)
        self.assertNotIn("daily_refresh_analyze_stale_data", source)
        self.assertIn('"Deep Research")', source)

    def test_refresh_result_explains_budget_block_without_changing_saved_limits(self):
        from dashboard.pages import home
        import inspect
        source = inspect.getsource(home._render_market_refresh_result)
        self.assertIn('ai_run.get("status") == "Blocked by budget"', source)
        self.assertIn("estimated request exceeds your saved spending limit", source)

    def test_premarket_contract_requires_conditional_paths_without_probability_fields(self):
        import json
        payload = {"setup_meaning": "Technology weakness conflicts with broad participation.",
            "most_important_uncertainty": "Whether weakness persists after the open.",
            "paths": [{"path_id": f"p{i}", "name": f"Path {i}", "if_condition": "SMH remains weak",
                "then_interpretation": "Leadership risk persists", "why_it_might_happen": "Earnings repricing",
                "supporting_or_contradicting_evidence": "QQQ lags SPY", "indicators_to_watch": "SMH/SPY and breadth"}
                for i in range(3)], "session_watch": ["Compare closing breadth with the opening gap"]}
        parsed = parse_premarket_what_if(json.dumps(payload))
        self.assertEqual(len(parsed["paths"]), 3)
        payload["paths"][0]["probability"] = 0.7
        with self.assertRaisesRegex(ValueError, "probability"):
            parse_premarket_what_if(json.dumps(payload))

    def test_historical_context_is_descriptive_and_excludes_current_month(self):
        histories = {"SPY": [
            {"trading_date": "2024-11-01", "close": 100}, {"trading_date": "2024-11-29", "close": 110},
            {"trading_date": "2025-11-03", "close": 100}, {"trading_date": "2025-11-28", "close": 90},
            {"trading_date": "2026-11-02", "close": 100}, {"trading_date": "2026-11-03", "close": 120}]}
        result = completed_month_seasonality(histories, as_of="2026-11-04", symbols=("SPY",))
        sample = result["symbols"]["SPY"]
        self.assertEqual(sample["sample_count"], 2)
        self.assertEqual(sample["positive_months"], 1)
        self.assertAlmostEqual(sample["mean_return_pct"], 0)
        self.assertEqual(sample["sample_years"], [2024, 2025])
        paired = completed_month_relative_history({
            "RSP": [{"trading_date": "2024-11-01", "close": 100}, {"trading_date": "2024-11-29", "close": 110}],
            "SPY": [{"trading_date": "2024-11-01", "close": 100}, {"trading_date": "2024-11-29", "close": 105}],
        }, as_of="2026-11-04", pairs=(("RSP", "SPY"),))
        self.assertEqual(paired["pairs"]["RSP/SPY"]["sample_count"], 1)
        self.assertAlmostEqual(paired["pairs"]["RSP/SPY"]["observations"][0]["relative_return_pct"], 100 * (1.1 / 1.05 - 1))
        vix = summarize_vix_stress_history([
            {"observation_date": "2026-11-03", "close": 32},
            {"observation_date": "2026-11-04", "close": 19}], as_of="2026-11-03")
        self.assertEqual(vix["sessions_at_or_above_threshold"], 1)
        self.assertEqual(vix["as_of"], "2026-11-03")

    def test_research_note_requires_explicit_save_and_keeps_source_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "desk.sqlite"
            initialize_database(db)
            saved = save_market_research_note(conversation_id="premarket", message_id=12,
                note_text="Compare SMH/SPY against breadth.", snapshot_fingerprint="snap-1",
                source_metadata=[{"title": "Fed", "url": "https://www.federalreserve.gov/x", "date": "2026-11-04"},
                                 {"url": "http://bad.example"}], database_path=db)
            again = save_market_research_note(conversation_id="premarket", message_id=12,
                note_text="Changed duplicate should be idempotent.", database_path=db)
            self.assertEqual(saved["note_id"], again["note_id"])
            self.assertEqual(len(list_market_research_notes(database_path=db)), 1)
            self.assertEqual(saved["source_metadata"][0]["date"], "2026-11-04")


if __name__ == "__main__":
    unittest.main()

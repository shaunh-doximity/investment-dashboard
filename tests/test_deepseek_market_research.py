import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dashboard.database import initialize_database
from dashboard.deepseek_market_research import (
    MARKET_ANALYST_PROMPT_VERSION, MarketResearchError, _market_analyst_prompt, extract_responses_text, market_research_cache_key, parse_market_briefing, public_market_snapshot,
    _response_sources, estimate_market_research_cost,
    run_market_research, _stream_response, timeout_error_category, transport_error_category,
    bounded_deepseek_timeouts, market_research_mode_settings, market_research_retry_settings,
    bounded_search_query, market_analysis_model, DEEP_RESEARCH_MODEL,
    MAX_MARKET_RESEARCH_OUTPUT_TOKENS,
    configured_market_research_model,
    run_deepseek_capability_diagnostics,
)
from dashboard.market_snapshot import (SECTION_ORDER, build_market_snapshot, metric_references,
                                       saved_report_is_stale, snapshot_with_recent_catalysts)
from dashboard.data_access import (get_deepseek_market_research_by_key,
    get_latest_completed_deepseek_market_research, list_deepseek_market_research_reports,
    list_market_ai_request_audit, save_deepseek_market_research_report)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.ok = payload, status, status < 400
    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, results=None):
        self.posts = []
        self.results = results if results is not None else [{"title":"Fed source", "url":"https://www.federalreserve.gov/test", "date":"2026-10-30", "snippet":"Official context"}]
    def get(self, url, **kwargs):
        return FakeResponse({"data":[{"id":"deepseek-flash"}, {"id":"deepseek-v4-pro"}]})
    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse({"results":self.results})


class ErrorHttp(FakeHttp):
    def __init__(self, status):
        super().__init__()
        self.status = status
    def post(self, url, **kwargs):
        return FakeResponse({"error":{"message":"redacted"}}, self.status)


class DeepSeekMarketResearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "research.db"
        initialize_database(self.db)
        self.snapshot = public_market_snapshot(comparison={"status":"available", "symbols":["SMH","SPY"], "window":20, "as_of":"2026-10-30", "excess_pp":2.1},
                                               vix={"status":"current", "observation_date":"2026-10-30", "close":20},
                                               missing=["Treasury unavailable"], timeframe=20)
    def tearDown(self):
        self.directory.cleanup()

    def test_snapshot_excludes_personal_portfolio_data(self):
        self.assertNotIn("holdings", self.snapshot)
        self.assertNotIn("cash", self.snapshot)
        self.assertIn("selected_comparison", self.snapshot)

    def test_dashboard_analysis_is_one_model_call_and_never_searches_or_sends_private_data(self):
        sections = {"regime": {"status": "available", "as_of": "2026-10-30", "source": "Tiingo",
            "metrics": {"participation": {"value": 7, "unit": "sectors", "window": "20 sessions",
                "universe": "standard 11", "definition": "above SMA50", "as_of": "2026-10-30"}}}}
        snapshot = build_market_snapshot(sections)
        refs = sorted(metric_references(snapshot))
        payload = {"schema_version": "market-analyst-response-v1",
            "highlights": [{"section_id": section, "headline": "Insufficient evidence",
                "category": "insufficient evidence", "insight": "No supported change is present.",
                "why_it_matters": "No current evidence supports a stronger interpretation.",
                "monitor_next": "Wait for the next observation.",
                "metric_refs": refs if section == "regime" else []} for section in SECTION_ORDER],
            "briefing": {"executive_takeaway": "The available readings are mixed.",
                "risks": [], "supportive": [], "sector_implications": [], "conflicting_evidence": [],
                "catalysts": [], "conditions": [], "metric_refs": {
                    "executive_takeaway": refs, "risks": [], "supportive": [], "sector_implications": [],
                    "conflicting_evidence": [], "catalysts": [], "conditions": []}}}
        fake_http = FakeHttp()
        response = SimpleNamespace(output_text=__import__("json").dumps(payload), status="completed",
            model="deepseek-flash", usage=SimpleNamespace(input_tokens=800, output_tokens=600))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            report = run_market_research(topic="Full market regime and capital rotation", snapshot=snapshot,
                database_path=self.db, http=fake_http, search_mode="dashboard_analysis",
                reasoning_effort="none", output_token_limit=2500)
        self.assertEqual(len(fake_http.posts), 0)
        client.return_value.responses.create.assert_called_once()
        sent = client.return_value.responses.create.call_args.kwargs["input"]
        self.assertIn("Canonical local snapshot", sent)
        self.assertNotIn("holdings", sent.lower())
        self.assertIn("Allowed local metric references (copy these exact strings only)", sent)
        self.assertEqual(report["research_status"], "complete")
        self.assertEqual(report["usage"]["search_mode"], "dashboard_analysis")
        self.assertFalse(report["web_search_succeeded"])
        audit = list_market_ai_request_audit(database_path=self.db, limit=1)[0]
        self.assertEqual(audit["purpose"], "full_market_analysis:fast_briefing")
        self.assertEqual(audit["evidence_mode"], "dashboard_analysis")
        self.assertEqual(audit["requested_model"], "deepseek-flash")
        self.assertEqual(audit["actual_model"], "deepseek-flash")
        self.assertIn("POST /responses", audit["endpoint"])
        self.assertEqual(audit["output_token_allowance"], 2500)
        self.assertEqual(audit["reasoning_effort"], "none")
        self.assertEqual(audit["completion_status"], "completed")

    def test_web_report_fingerprint_includes_catalysts_refreshed_during_its_own_run(self):
        snapshot = build_market_snapshot({"regime":{"status":"available","as_of":"2026-11-01","source":"Tiingo",
            "metrics":{"reference":{"value":1.0,"unit":"%","window":"20D","universe":"SPY",
                        "definition":"sample metric","as_of":"2026-11-01"}}}})
        refs=sorted(metric_references(snapshot))
        cache = {"status":"available", "refreshed_at":"2026-11-02T12:00:00+00:00",
                 "records":[{"event_id":"evt1", "publication_date":"2026-11-01",
                             "development":"Dated earnings guidance changed.",
                             "source_url":"https://example.com/result"}]}
        payload = {"schema_version":"market-analyst-response-v1",
            "highlights":[{"section_id":section,"headline":"No material change",
                "category":"insufficient evidence","insight":"No material supported change.",
                "why_it_matters":"No new evidence changes the picture.","monitor_next":"Watch the next release.",
                "metric_refs":refs if section=="regime" else []} for section in SECTION_ORDER],
            "briefing":{"executive_takeaway":"The available signals are mixed.","risks":[],"supportive":[],
                "sector_implications":[],"conflicting_evidence":[],"catalysts":[],"conditions":[],
                "metric_refs":{"executive_takeaway":refs,"risks":[],"supportive":[],
                    "sector_implications":[],"conflicting_evidence":[],"catalysts":[],"conditions":[]}}}
        response=SimpleNamespace(output_text=__import__("json").dumps(payload),status="completed",
            model="deepseek-v4-pro",usage=SimpleNamespace(input_tokens=1200,output_tokens=500,
            output_tokens_details=SimpleNamespace(reasoning_tokens=100)))
        with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"test-only"}), \
             patch("dashboard.recent_catalysts.save_recent_catalysts",return_value=cache), \
             patch("dashboard.recent_catalysts.load_recent_catalysts",return_value=cache), \
             patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value=response
            report=run_market_research(topic="Full Market Intelligence Snapshot",snapshot=snapshot,
                request_limit_usd=1,daily_limit_usd=5,model="deepseek-v4-pro",search_mode="dashboard_web",
                reasoning_effort="high",output_token_limit=2000,database_path=self.db,http=FakeHttp())
        self.assertEqual(report["research_status"],"complete")
        self.assertEqual(report["snapshot"]["sections"]["catalysts"]["recent_developments"],cache["records"])
        self.assertEqual(report["snapshot"]["fingerprint"],snapshot_with_recent_catalysts(snapshot,cache)["fingerprint"])
        self.assertFalse(saved_report_is_stale(report,report["snapshot"]["fingerprint"],MARKET_ANALYST_PROMPT_VERSION))
        prompt=client.return_value.responses.create.call_args.kwargs["input"]
        self.assertIn("Dated earnings guidance changed",prompt)
        persisted=get_deepseek_market_research_by_key(report["cache_key"],self.db)
        self.assertEqual(persisted["snapshot"]["fingerprint"],report["snapshot"]["fingerprint"])

    def test_reasoning_mode_does_not_silently_switch_flash_to_pro(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = SimpleNamespace(
                output_text="A concise answer", status="completed", model="deepseek-flash",
                usage=SimpleNamespace(input_tokens=100, output_tokens=50))
            result = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                model="deepseek-flash", reasoning_effort="high", output_token_limit=6000,
                search_mode="dashboard_analysis", database_path=self.db, http=FakeHttp())
        self.assertEqual(client.return_value.responses.create.call_args.kwargs["model"], "deepseek-flash")
        self.assertEqual(client.return_value.responses.create.call_args.kwargs["reasoning"], {"effort":"high"})
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertEqual(result["usage"]["actual_model"], "deepseek-flash")

    def test_dashboard_analysis_cost_estimate_has_no_search_charge(self):
        with_search = estimate_market_research_cost(self.snapshot, "deepseek-flash", output_token_limit=2500, search_mode="dashboard_web")
        local_only = estimate_market_research_cost(self.snapshot, "deepseek-flash", output_token_limit=2500, search_mode="dashboard_analysis")
        self.assertAlmostEqual(with_search - local_only, .00075)

    def test_market_research_modes_and_bounded_output_allowance(self):
        self.assertEqual(market_research_mode_settings("Fast Briefing"), ("none", 12000))
        self.assertEqual(market_research_mode_settings("Deep Research"), ("high", 48000))
        self.assertEqual(market_research_retry_settings("Fast Briefing"), ("none", 24000))
        self.assertEqual(market_research_retry_settings("Deep Research"), ("high", 64000))
        with self.assertRaises(ValueError):
            market_research_mode_settings("Deep Research", 8192)
        with self.assertRaises(ValueError):
            market_research_mode_settings("Fast Briefing", 32000)
        self.assertEqual(MAX_MARKET_RESEARCH_OUTPUT_TOKENS, 64000)
        self.assertEqual(market_analysis_model("Fast Briefing", "deepseek-flash"), "deepseek-flash")
        self.assertEqual(market_analysis_model("Deep Research", "deepseek-flash"), "deepseek-v4-pro")
        self.assertEqual(market_analysis_model("Deep Research", "deepseek-v4-pro"), DEEP_RESEARCH_MODEL)
        with self.assertRaises(ValueError):
            market_analysis_model("Fast Briefing", "unsupported-model")
        with patch.dict(os.environ, {"DEEPSEEK_MARKET_RESEARCH_MODEL": "deepseek-v4-pro"}):
            self.assertEqual(configured_market_research_model(), "deepseek-flash")
        estimate = estimate_market_research_cost(self.snapshot, "deepseek-flash", output_token_limit=64000)
        self.assertLess(estimate, .10)
        with self.assertRaises(ValueError):
            estimate_market_research_cost(self.snapshot, "deepseek-flash", output_token_limit=64001)

    def test_deep_research_route_explicitly_requests_v4_pro_and_never_falls_back(self):
        selected_fast_model = "deepseek-flash"
        requested_model = market_analysis_model("Deep Research", selected_fast_model)
        self.assertEqual(requested_model, "deepseek-v4-pro")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = SimpleNamespace(
                output_text="{}", status="completed", model="deepseek-v4-pro",
                usage=SimpleNamespace(input_tokens=100, output_tokens=50))
            # No fallback is performed if the account-level model check omits V4 Pro.
            class FlashOnlyHttp(FakeHttp):
                def get(self, url, **kwargs):
                    return FakeResponse({"data": [{"id": "deepseek-flash"}]})
            with self.assertRaisesRegex(MarketResearchError, "does not list deepseek-v4-pro"):
                run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                    model=requested_model, reasoning_effort="high", output_token_limit=48000,
                    search_mode="dashboard_analysis", request_limit_usd=1.0, daily_limit_usd=5.0,
                    database_path=self.db, http=FlashOnlyHttp())
            client.return_value.responses.create.assert_not_called()

    def test_auto_budget_is_rejected_against_account_model_limits_before_search(self):
        class LimitedModelHttp(FakeHttp):
            def get(self, url, **kwargs):
                return FakeResponse({"data":[{"id":"deepseek-flash", "context_window":8192,
                                               "max_output_tokens":4096}]})
        fake_http = LimitedModelHttp()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            with self.assertRaisesRegex(MarketResearchError, "currently advertises"):
                run_market_research(topic="Full Market Intelligence Snapshot", snapshot=self.snapshot,
                    database_path=self.db, http=fake_http, search_mode="dashboard_web",
                    reasoning_effort="none", output_token_limit=12000)
        self.assertEqual(fake_http.posts, [])
        client.assert_not_called()

    def test_full_market_prompt_includes_broad_equity_participation_questions(self):
        snapshot = build_market_snapshot({"regime":{"status":"available", "as_of":"2026-10-30",
            "metrics":{"broad_rsp_20d_excess_vs_spy":{"value":2.1,"unit":"pp","window":"20 sessions",
                "universe":"RSP/SPY","definition":"equal-weight relative performance","source":"Tiingo",
                "as_of":"2026-10-30"},
                "broad_iwm_20d_excess_vs_spy":{"value":-1.3,"unit":"pp","window":"20 sessions",
                "universe":"IWM/SPY","definition":"small-cap relative performance","source":"Tiingo",
                "as_of":"2026-10-30"}}},
            "sector_rotation": {"status":"available", "as_of":"2026-10-30",
                "risk_appetite":{"methodology_version":"risk-appetite-v4","classification":"Rotation / Mixed",
                    "components":{"equity_participation":{"status":"Broadening participation"}}},
                "regime_transition":{"methodology_version":"market-regime-transition-v2",
                    "transition_status":"Weakening","current_leadership":"Defensive leadership",
                    "summary":"Defensive leadership remains positive but momentum has weakened."},
                "metrics":{"risk_appetite_classification":{"value":"Rotation / Mixed","unit":"descriptive regime"}}}})
        prompt = _market_analyst_prompt("Full market analysis", snapshot, [], "dashboard_analysis")
        for symbol in ("SPY", "RSP", "QQQ", "DIA", "IWM"):
            self.assertIn(symbol, prompt)
        self.assertIn("concentrated or broadening", prompt)
        self.assertIn("broad_rsp_20d_excess_vs_spy", prompt)
        self.assertIn("deterministic risk_appetite, regime_transition", prompt)
        self.assertIn("turning_point_watch signals", prompt)
        self.assertIn("risk-appetite-v4", prompt)
        self.assertIn("credit_confirmation", prompt)
        self.assertIn("Never call absolute breadth 'improving'", prompt)
        self.assertIn("relative resilience during a selloff", prompt)
        self.assertIn("sector-ETF participation proxy", prompt)
        self.assertIn("market-regime-transition-v2", prompt)

    def test_calendar_prompt_prioritizes_catalyst_dates_and_impact_over_verification(self):
        snapshot = build_market_snapshot({"catalysts": {
            "status": "available", "as_of": "2026-11-02", "source": "SQLite calendar",
            "metrics": {"event_count": {"value": 12, "unit": "events"},
                        "event_01_date": {"value": "2026-11-10", "unit": "calendar date/time",
                            "universe": "CPI release · Macro", "definition": "Calendar planning date"}},
            "events": [{"name": "CPI release", "category": "Macro", "event_date": "2026-11-10",
                        "status": "Unconfirmed", "source_verification": "GPT import · Unverified",
                        "confirmation_state": "Unconfirmed", "related_symbols": ["XLK", "XLY"]},
                       {"name": "NVDA earnings", "category": "Earnings", "event_date": "2026-11-12",
                        "status": "Unconfirmed", "source_verification": "GPT import · Unverified",
                        "confirmation_state": "Unconfirmed", "related_symbols": ["NVDA", "SMH"]}]
        }})
        prompt = _market_analyst_prompt("Full market analysis", snapshot, [], "dashboard_analysis")
        self.assertIn("2026-11-10", prompt)
        self.assertIn("CPI release", prompt)
        self.assertIn("catalysts.event_01_date", prompt)
        self.assertIn("macro mechanism or sensitive sectors/assets", prompt)
        self.assertIn("Do not discuss event counts, confirmation status, source verification", prompt)
        # Provenance is preserved in the canonical snapshot, but suppressed in
        # the actual model payload to keep routine verification from dominating.
        model_payload = prompt.split("Canonical local snapshot (public market data only; view controls omitted):\n", 1)[1].split(
            "\nRetrieved external evidence", 1)[0]
        self.assertNotIn("GPT import", model_payload)
        self.assertNotIn("confirmation_state", model_payload)
        self.assertNotIn("event_count", model_payload)

    def test_api_reported_model_is_kept_separate_from_requested_flash(self):
        response = SimpleNamespace(model="deepseek-v4-pro", output_text="Reported model ID is explicit.",
                                   status="completed", usage=None)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            report = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                model="deepseek-flash", reasoning_effort="none", output_token_limit=2500,
                database_path=self.db, http=FakeHttp())
        self.assertEqual(report["model"], "deepseek-flash")
        self.assertEqual(report["usage"]["requested_model"], "deepseek-flash")
        self.assertEqual(report["usage"]["actual_model"], "deepseek-v4-pro")

    def test_provider_advertised_output_cap_is_checked_before_paid_search(self):
        fake_http = FakeHttp()
        fake_http.get = lambda *args, **kwargs: FakeResponse({"data": [
            {"id": "deepseek-flash", "max_output_tokens": 8000, "context_window": 100000}]})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch("openai.OpenAI") as client:
            with self.assertRaisesRegex(MarketResearchError, "advertises a 8,000-token output maximum"):
                run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                    model="deepseek-flash", output_token_limit=16000, database_path=self.db, http=fake_http)
            self.assertEqual(fake_http.posts, [])
            client.assert_not_called()

    def test_briefing_parser_handles_current_and_saved_legacy_reports_without_losing_citations(self):
        legacy = """## Current market regime
The tape is mixed. [S1]

## Accumulating risks
- VIX rose 8% on the observed session. [S2]

## Improving conditions
- Breadth improved over the stated window. [S3]

## Sector rotation
- Semiconductors outperformed the selected benchmark. [S4]

## Upcoming catalysts
- FOMC date listed by the source. [S5]

## Unresolved questions
- The next report is not yet available.
"""
        parsed = parse_market_briefing(legacy)
        self.assertEqual(parsed["recognized"], 6)
        self.assertIn("[S1]", parsed["sections"]["market_regime"][0])
        self.assertIn("[S2]", parsed["sections"]["risks"][0])
        self.assertIn("[S3]", parsed["sections"]["supportive"][0])
        self.assertIn("[S5]", parsed["sections"]["catalysts"][0])
        self.assertIn("next report", parsed["sections"]["conditions"][0])
        self.assertIn("[S1]", legacy)

    def test_unrecognized_briefing_sections_remain_unmapped_not_invented(self):
        parsed = parse_market_briefing("## Unknown section\nOnly saved material.")
        self.assertEqual(parsed["recognized"], 0)
        self.assertEqual(parsed["unmapped"], ["Only saved material."])

    def test_explicit_request_checks_model_searches_and_preserves_sources(self):
        fake_http = FakeHttp()
        response = SimpleNamespace(output_text="## Market regime\nMixed evidence.", status="completed",
                                   usage=SimpleNamespace(input_tokens=1000, output_tokens=300))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), \
             patch("openai.OpenAI") as client_class:
            client_class.return_value.responses.create.return_value = response
            report = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                model="deepseek-v4-pro", request_limit_usd=1, daily_limit_usd=5, database_path=self.db, http=fake_http,
                output_token_limit=2000)
        self.assertTrue(report["web_search_succeeded"])
        self.assertEqual(report["model"], "deepseek-v4-pro")
        self.assertIn("https://www.federalreserve.gov/test", report["sources"][0]["url"])
        self.assertEqual(len(fake_http.posts), 1)
        args, kwargs = client_class.call_args
        self.assertEqual(kwargs["base_url"], "https://api.deepseek.com")
        self.assertEqual(client_class.return_value.responses.create.call_args.kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(client_class.return_value.responses.create.call_args.kwargs["reasoning"], {"effort": "none"})
        search_payload = fake_http.posts[0][1]["json"]
        self.assertLessEqual(len(search_payload["query"]), 128)
        self.assertLessEqual(len(search_payload["ori_question"]), 128)
        self.assertEqual(client_class.return_value.responses.create.call_args.kwargs["max_output_tokens"], 2000)
        prompt = client_class.return_value.responses.create.call_args.kwargs["input"]
        self.assertIn("Top 3 accumulating risks", prompt)
        self.assertIn("maximum about 500 words", prompt)
        self.assertIn("no more than three short bullets", prompt)
        persisted = get_deepseek_market_research_by_key(report["cache_key"], self.db)
        self.assertEqual(persisted["report_text"], report["report_text"])

    def test_bounded_search_query_obeys_documented_query_and_original_question_limits(self):
        request = bounded_search_query(" " + "macro rates " * 20,
                                       original_question="Treasury and Federal Reserve question " * 10)
        self.assertTrue(1 <= len(request["query"]) <= 128)
        self.assertTrue(1 <= len(request["ori_question"]) <= 128)

    def test_empty_search_results_do_not_claim_success_or_save_report(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            with self.assertRaisesRegex(MarketResearchError, "no usable source"):
                run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                    database_path=self.db, http=FakeHttp([]))
            client.assert_not_called()

    def test_tool_and_assistant_items_are_parsed_and_completion_is_reported(self):
        shape = {"output_text":"", "status":"completed", "output":[
            {"type":"web_search_call", "id":"search_1"},
            {"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":"Recovered final briefing."}]},
        ]}
        self.assertEqual(extract_responses_text(shape), "Recovered final briefing.")
        fake_http = FakeHttp()
        response = SimpleNamespace(**shape, usage=SimpleNamespace(input_tokens=300, output_tokens=70))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            report = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                database_path=self.db, http=fake_http)
        self.assertEqual(report["report_text"], "Recovered final briefing.")
        self.assertEqual(report["research_status"], "complete")

    def test_successful_search_empty_synthesis_is_saved_as_explicit_failure_with_sources(self):
        response = SimpleNamespace(output_text="", status="completed", output=[{"type":"web_search_call"}],
                                   usage=SimpleNamespace(input_tokens=300, output_tokens=0))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            result = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot, database_path=self.db, http=FakeHttp())
        self.assertEqual(result["research_status"], "empty_synthesis")
        self.assertTrue(result["web_search_succeeded"])
        self.assertEqual(len(result["sources"]), 1)
        self.assertFalse(result["report_text"])
        self.assertFalse(get_deepseek_market_research_by_key(result["cache_key"], self.db)["report_text"])
        self.assertIn("no assistant message text", result["error_message"])

    def test_incomplete_output_and_cap_block_preserve_search_evidence(self):
        response = SimpleNamespace(output_text="partial", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=SimpleNamespace(input_tokens=400, output_tokens=1200))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            result = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot, database_path=self.db, http=FakeHttp())
        self.assertEqual(result["research_status"], "incomplete")
        self.assertEqual(result["completion_reason"], "max_output_tokens")
        self.assertTrue(result["sources"])
        client.reset_mock()
        with self.assertRaisesRegex(MarketResearchError, "Estimated request ceiling"):
            run_market_research(topic="Full market regime and capital rotation", snapshot={"new":"snapshot"},
                request_limit_usd=.0001, daily_limit_usd=.25, database_path=self.db, http=FakeHttp())
        client.assert_not_called()

    def test_synthesis_retry_reuses_saved_search_results(self):
        empty = SimpleNamespace(output_text="", status="completed", output=[], usage=None)
        good = SimpleNamespace(output_text="Now complete", status="completed", output=[], usage=None)
        fake_http = FakeHttp()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.side_effect = [empty, good]
            first = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot, database_path=self.db, http=fake_http)
            second = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                database_path=self.db, http=FakeHttp(), synthesis_only=True, cached_report_key=first["cache_key"])
        self.assertEqual(first["research_status"], "empty_synthesis")
        self.assertEqual(second["research_status"], "complete")
        self.assertEqual(len(fake_http.posts), 1)
        self.assertTrue(second["sources"])
        self.assertEqual(client.return_value.responses.create.call_args.kwargs["reasoning"], {"effort": "none"})

    def test_retry_with_larger_output_budget_uses_a_new_cache_key_and_reuses_sources(self):
        empty = SimpleNamespace(output_text="", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"), output=[], usage=None)
        good = SimpleNamespace(output_text="Larger allowance completed synthesis.", status="completed", output=[], usage=None)
        fake_http = FakeHttp()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.side_effect = [empty, good]
            first = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                database_path=self.db, http=fake_http, output_token_limit=2500)
            second = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                database_path=self.db, http=FakeHttp(), synthesis_only=True, cached_report_key=first["cache_key"],
                output_token_limit=32000)
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.assertEqual(len(fake_http.posts), 1)
        self.assertEqual(client.return_value.responses.create.call_args.kwargs["max_output_tokens"], 32000)
        self.assertEqual(second["research_status"], "complete")
        self.assertEqual(second["sources"], first["sources"])
        self.assertEqual(get_deepseek_market_research_by_key(second["cache_key"], self.db)["report_text"], "Larger allowance completed synthesis.")

    def test_dashboard_only_retry_reuses_snapshot_without_search_sources(self):
        incomplete = SimpleNamespace(output_text="partial", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"), output=[], usage=None)
        complete = SimpleNamespace(output_text="Completed from the same local snapshot.", status="completed", output=[], usage=None)
        http = FakeHttp()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.side_effect = [incomplete, complete]
            first = run_market_research(topic="Full Market Intelligence Snapshot", snapshot=self.snapshot,
                database_path=self.db, http=http, search_mode="dashboard_analysis", output_token_limit=12000)
            second = run_market_research(topic="Full Market Intelligence Snapshot", snapshot=self.snapshot,
                database_path=self.db, http=http, search_mode="dashboard_analysis", synthesis_only=True,
                cached_report_key=first["cache_key"], output_token_limit=24000)
        self.assertEqual(first["research_status"], "incomplete")
        self.assertEqual(second["research_status"], "complete")
        self.assertEqual(http.posts, [])
        self.assertEqual(client.return_value.responses.create.call_count, 2)
        self.assertEqual(client.return_value.responses.create.call_args.kwargs["max_output_tokens"], 24000)

    def test_synthesis_retry_is_rejected_when_snapshot_changed(self):
        incomplete = SimpleNamespace(output_text="partial", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"), output=[], usage=None)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = incomplete
            first = run_market_research(topic="Full Market Intelligence Snapshot", snapshot={"fingerprint":"old"},
                database_path=self.db, http=FakeHttp(), search_mode="dashboard_analysis", output_token_limit=12000)
            with self.assertRaisesRegex(MarketResearchError, "snapshot changed"):
                run_market_research(topic="Full Market Intelligence Snapshot", snapshot={"fingerprint":"new"},
                    database_path=self.db, http=FakeHttp(), search_mode="dashboard_analysis", synthesis_only=True,
                    cached_report_key=first["cache_key"], output_token_limit=24000)
        self.assertEqual(client.return_value.responses.create.call_count, 1)

    def test_cached_report_avoids_all_provider_requests(self):
        fake_http = FakeHttp()
        response = SimpleNamespace(output_text="Report", status="completed", usage=None)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            first = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                database_path=self.db, http=fake_http)
            second = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                database_path=self.db, http=FakeHttp())
        self.assertTrue(second["cached"])
        self.assertEqual(client.call_count, 1)

    def test_completed_over_cap_output_is_preserved_and_cached(self):
        response = SimpleNamespace(output_text="Completed despite reported over-cap usage.", status="completed",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=2_000_000))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            report = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                request_limit_usd=.05, daily_limit_usd=.25, database_path=self.db, http=FakeHttp())
            self.assertEqual(report["research_status"], "complete_cost_warning")
            self.assertIn("Completed despite", report["report_text"])
            retry = run_market_research(topic="VIX and short-term market stress", snapshot=self.snapshot,
                request_limit_usd=.05, daily_limit_usd=.25, database_path=self.db, http=FakeHttp())
        self.assertTrue(retry["cached"])
        self.assertEqual(client.call_count, 1)

    def test_model_unavailable_does_not_fallback(self):
        fake_http = FakeHttp()
        fake_http.get = lambda *args, **kwargs: FakeResponse({"data":[{"id":"deepseek-chat"}]})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            with self.assertRaisesRegex(MarketResearchError, "does not list deepseek-flash"):
                run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                    database_path=self.db, http=fake_http)
            client.assert_not_called()

    def test_search_balance_and_rate_errors_are_actionable_without_model_call(self):
        for code, expected in ((402, "insufficient"), (429, "rate limit")):
            with self.subTest(code=code), patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
                with self.assertRaisesRegex(MarketResearchError, expected):
                    run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                        database_path=self.db, http=ErrorHttp(code))
                client.assert_not_called()

    def test_cache_key_includes_model_search_mode_reasoning_and_output_limit(self):
        first = market_research_cache_key("topic", self.snapshot, model="deepseek-flash")
        self.assertNotEqual(first, market_research_cache_key("topic", self.snapshot, model="deepseek-v4-pro"))
        self.assertNotEqual(first, market_research_cache_key("topic", self.snapshot, model="deepseek-flash", search_mode="responses_web_search"))
        self.assertNotEqual(first, market_research_cache_key("topic", self.snapshot, model="deepseek-flash", reasoning_effort="high"))
        self.assertNotEqual(first, market_research_cache_key("topic", self.snapshot, model="deepseek-flash", output_token_limit=6000))

    def test_view_selector_does_not_invalidate_full_market_analysis_cache(self):
        base = build_market_snapshot({"regime": {"status": "available", "metrics": {
            "breadth": {"value": 6, "unit": "sectors", "window": "20D", "universe": "11 sectors",
                        "definition": "above SMA50", "as_of": "2026-11-01"}}}},
            view_state={"relative_strength": {"first": "SMH", "second": "SPY", "window": 20}})
        changed_view = {**base, "view_state": {"relative_strength": {"first": "XLY", "second": "XLP", "window": 60}},
                        "view_fingerprint": "different"}
        self.assertEqual(market_research_cache_key("Full Market Intelligence Snapshot", base),
                         market_research_cache_key("Full Market Intelligence Snapshot", changed_view))

    def test_canonical_analyst_prompt_requests_combined_sections_and_all_comparisons(self):
        snap = build_market_snapshot({"relative_strength": {"status": "available",
            "ai_comparison_ids": ["XLY_XLP_20D"], "metrics": {
                "xly_xlp_excess": {"value": 2.0, "unit": "pp", "window": "20 sessions",
                    "universe": "XLY/XLP", "definition": "matched returns", "as_of": "2026-11-01"}}}},
            view_state={"relative_strength": {"first": "PRIVATE_VIEW_ONLY"}})
        prompt = _market_analyst_prompt("Full dashboard", snap, [], "dashboard_analysis")
        self.assertIn("market-analyst-response-v6", prompt)
        self.assertIn("transition_outlook", prompt)
        self.assertIn("turning_point_interpretations", prompt)
        self.assertIn("Never give reversal probabilities", prompt)
        self.assertIn("XLY_XLP_20D", prompt)
        self.assertIn("Rates and Macro Proxies", prompt)
        self.assertIn("competing explanations", prompt.lower())
        self.assertNotIn("PRIVATE_VIEW_ONLY", prompt)

    def test_v4_pro_web_search_request_requires_verified_tool_and_source_urls(self):
        fake_http = FakeHttp()
        response = SimpleNamespace(model="deepseek-v4-pro", status="completed", output_text="",
            output=[{"type":"web_search_call", "status":"completed", "action":{"sources":[
                {"title":"Treasury release", "url":"https://home.treasury.gov/release"}] }},
                {"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":"## Market regime / key takeaway\nRates data remain mixed."}]}],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=220, server_tool_use=SimpleNamespace(tool_search=1)))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            report = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                model="deepseek-v4-pro", search_mode="responses_web_search", database_path=self.db,
                http=fake_http, request_limit_usd=.10, daily_limit_usd=.25)
        self.assertEqual(fake_http.posts, [])
        kwargs = client.return_value.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(kwargs["tools"], [{"type":"web_search"}])
        self.assertEqual(kwargs["reasoning"], {"effort":"none"})
        self.assertEqual(report["usage"]["actual_model"], "deepseek-v4-pro")
        self.assertTrue(report["web_search_succeeded"])
        self.assertEqual(report["sources"][0]["url"], "https://home.treasury.gov/release")

    def test_v4_pro_output_without_web_search_execution_is_not_saved_as_briefing(self):
        response = SimpleNamespace(model="deepseek-v4-pro", status="completed", output_text="Some text",
            output=[{"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":"Some text"}]}], usage=None)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            report = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                model="deepseek-v4-pro", search_mode="responses_web_search", database_path=self.db,
                http=FakeHttp(), request_limit_usd=.10, daily_limit_usd=.25)
        self.assertEqual(report["research_status"], "web_search_not_verified")
        self.assertFalse(report["report_text"])
        self.assertFalse(report["web_search_succeeded"])
        self.assertIn("without verifiable web_search execution", report["error_message"])

    def test_unsupported_v4_pro_web_search_returns_actionable_error(self):
        error = RuntimeError("unsupported tool")
        error.status_code = 400
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.side_effect = error
            result = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                model="deepseek-v4-pro", search_mode="responses_web_search", database_path=self.db,
                http=FakeHttp(), request_limit_usd=.10, daily_limit_usd=.25)
        self.assertEqual(result["research_status"], "synthesis_error")
        self.assertIn("switch explicitly to Search API", result["error_message"])

    def test_search_mode_cost_estimate_respects_higher_search_allowance_and_caps(self):
        api_cost = estimate_market_research_cost(self.snapshot, "deepseek-v4-pro", output_token_limit=2500, search_mode="search_api")
        native_cost = estimate_market_research_cost(self.snapshot, "deepseek-v4-pro", output_token_limit=2500, search_mode="responses_web_search")
        self.assertGreater(native_cost, api_cost)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            with self.assertRaisesRegex(MarketResearchError, "exceeds your per-request limit"):
                run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                    model="deepseek-v4-pro", search_mode="responses_web_search", request_limit_usd=.01,
                    daily_limit_usd=.25, http=FakeHttp(), database_path=self.db)
            client.assert_not_called()

    def test_web_search_source_extraction_keeps_clickable_citation_metadata(self):
        response = {"output":[{"type":"message", "content":[{"type":"output_text", "text":"Fact", "annotations":[
            {"type":"url_citation", "title":"Fed page", "url":"https://www.federalreserve.gov/page"}]}]}]}
        self.assertEqual(_response_sources(response), [{"url":"https://www.federalreserve.gov/page", "title":"Fed page"}])

    def test_streaming_reports_progress_and_never_exposes_reasoning_text(self):
        events = [
            {"type":"response.created"},
            {"type":"response.reasoning_text.delta", "delta":"private chain of thought"},
            {"type":"response.output_text.delta", "delta":"Visible "},
            {"type":"response.output_text.delta", "delta":"answer."},
            {"type":"response.completed", "response":{"status":"completed", "output_text":"", "output":[], "usage":None}},
        ]
        class FakeClient:
            class responses:
                @staticmethod
                def create(**kwargs):
                    self.assertTrue(kwargs["stream"])
                    return iter(events)
        progress = []
        response, event_types, visible = _stream_response(FakeClient(), {"model":"deepseek-flash"}, progress_callback=progress.append)
        self.assertEqual(visible, "Visible answer.")
        self.assertEqual(response["output_text"], "Visible answer.")
        self.assertNotIn("private chain of thought", visible)
        self.assertIn("Completed answer", progress)
        self.assertIn("response.reasoning_text.delta", event_types)

    def test_reasoning_only_incomplete_response_is_not_saved_as_briefing(self):
        incomplete = SimpleNamespace(output_text="", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output=[{"type":"reasoning"}], usage=SimpleNamespace(input_tokens=20, output_tokens=900,
                output_tokens_details=SimpleNamespace(reasoning_tokens=900)))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = incomplete
            result = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                reasoning_effort="high", model="deepseek-flash", output_token_limit=6000,
                database_path=self.db, http=FakeHttp())
        self.assertEqual(result["research_status"], "incomplete")
        self.assertFalse(result["report_text"])
        self.assertEqual(result["completion_reason"], "max_output_tokens")
        self.assertEqual(result["usage"]["reasoning_tokens"], 900)
        self.assertEqual(result["usage"]["visible_output_tokens"], 0)
        self.assertIn("no visible answer", result["error_message"])

    def test_invalid_structured_response_preserves_previous_success_and_diagnostic_text(self):
        self.snapshot["schema_version"] = "market-intelligence-snapshot-v3"
        cache_key = market_research_cache_key("Full market regime and capital rotation", self.snapshot,
            model="deepseek-flash", search_mode="dashboard_analysis", reasoning_effort="none", output_token_limit=2500)
        prior = {"cache_key":cache_key, "topic":"Full market regime and capital rotation",
            "snapshot":self.snapshot, "generated_at":"2026-11-01T00:00:00Z", "model":"deepseek-flash",
            "report_text":"Previously saved valid briefing", "sources":[], "usage":{},
            "web_search_succeeded":False, "research_status":"complete"}
        save_deepseek_market_research_report(prior, self.db)
        self.assertEqual(get_deepseek_market_research_by_key(cache_key, self.db)["research_status"], "complete")
        bad_visible_text = '{"schema_version":"market-analyst-response-v3","highlights":[]}'
        response = SimpleNamespace(output_text=bad_visible_text, status="completed", model="deepseek-flash",
                                   usage=SimpleNamespace(input_tokens=700, output_tokens=100))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), patch("openai.OpenAI") as client:
            client.return_value.responses.create.return_value = response
            failed = run_market_research(topic="Full market regime and capital rotation", snapshot=self.snapshot,
                database_path=self.db, http=FakeHttp(), model="deepseek-flash",
                search_mode="dashboard_analysis", force=True,
                reasoning_effort="none", output_token_limit=2500)
        preserved = get_deepseek_market_research_by_key(cache_key, self.db)
        self.assertNotEqual(failed["cache_key"], cache_key)
        self.assertEqual(preserved["report_text"], prior["report_text"])
        self.assertEqual(failed["research_status"], "invalid_analysis_contract")
        self.assertFalse(failed["report_text"])
        self.assertTrue(failed["usage"]["preserved_prior_success"])
        self.assertEqual(failed["usage"]["diagnostic_response_text"], bad_visible_text)
        attempts = list_deepseek_market_research_reports(database_path=self.db, limit=10)
        self.assertTrue(any(row["cache_key"] != cache_key and row["research_status"] == "invalid_analysis_contract"
                            for row in attempts))

    def test_timeout_categories_distinguish_connect_from_read(self):
        self.assertEqual(timeout_error_category(type("ReadTimeout", (Exception,), {})()), "read_timeout")
        self.assertEqual(timeout_error_category(type("ConnectTimeout", (Exception,), {})()), "connect_timeout")
        self.assertIsNone(timeout_error_category(RuntimeError("bad response")))
        wrapped = RuntimeError("sdk wrapper")
        wrapped.__cause__ = type("ReadTimeout", (Exception,), {})()
        self.assertEqual(timeout_error_category(wrapped), "read_timeout")
        self.assertEqual(transport_error_category(wrapped), "read_timeout")
        dns = RuntimeError("wrapped connection")
        dns.__cause__ = type("gaierror", (Exception,), {})()
        self.assertEqual(transport_error_category(dns), "dns_failure")

    def test_timeout_bounds_are_separate_and_clamped(self):
        self.assertEqual(bounded_deepseek_timeouts(100, 999), (30, 300))
        self.assertEqual(bounded_deepseek_timeouts(1, 1), (3, 15))

    def test_diagnostics_use_cached_sources_and_gate_native_search_after_a_and_b(self):
        completed = {"result":"success", "status":"completed", "answer_present":True,
                     "model":"deepseek-v4-pro", "actual_model":"deepseek-v4-pro",
                     "input_tokens":100, "output_tokens":30, "search_requests":0,
                     "output_item_types":["message"]}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), \
             patch("dashboard.data_access.get_latest_deepseek_market_research_with_sources", return_value={
                 "topic":"test", "snapshot":{"as_of":"2026-10-30", "holdings":["must not transmit"]},
                 "sources":[{"title":"Treasury", "url":"https://home.treasury.gov/test", "snippet":"public"}]}), \
             patch("dashboard.deepseek_market_research.deepseek_market_research_daily_spend", return_value=0), \
             patch("dashboard.deepseek_market_research._diagnostic_request", side_effect=[dict(completed), dict(completed),
                 {**completed, "search_requests":1, "sources":[{"url":"https://www.federalreserve.gov/test"}]},
                 dict(completed), {**completed, "model":"deepseek-flash", "actual_model":"deepseek-flash"}]) as request, \
             patch("dashboard.deepseek_market_research.record_deepseek_market_research_usage"):
            results = run_deepseek_capability_diagnostics(request_limit_usd=.05, daily_limit_usd=.25,
                                                           database_path=self.db)
        self.assertEqual([row["test"] for row in results], [
            "A · Tiny V4 Pro request", "B · V4 Pro cached-source synthesis", "C · V4 Pro native web search",
            "V4 Pro · deeper reasoning", "Flash · deeper reasoning"])
        self.assertEqual(request.call_count, 5)
        prompts = [call.kwargs["prompt"] for call in request.call_args_list]
        self.assertIn("https://home.treasury.gov/test", prompts[1])
        self.assertNotIn("must not transmit", prompts[1])
        self.assertEqual(request.call_args_list[2].kwargs["tools"], [{"type":"web_search"}])
        self.assertEqual(request.call_args_list[1].kwargs["tools"], None)

    def test_failed_v4_prerequisites_skip_native_search(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), \
             patch("dashboard.data_access.get_latest_deepseek_market_research_with_sources", return_value=None), \
             patch("dashboard.deepseek_market_research.deepseek_market_research_daily_spend", return_value=0), \
             patch("dashboard.deepseek_market_research._diagnostic_request", return_value={"result":"failed"}), \
             patch("dashboard.deepseek_market_research.record_deepseek_market_research_usage"):
            results = run_deepseek_capability_diagnostics(database_path=self.db)
        web = next(row for row in results if row["test"].startswith("C ·"))
        self.assertEqual(web["result"], "skipped_A_or_B_failed")

    def test_preconnection_dns_failure_does_not_count_as_provider_spend(self):
        dns = RuntimeError("wrapped connection")
        dns.__cause__ = type("gaierror", (Exception,), {})()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), \
             patch("dashboard.data_access.get_latest_deepseek_market_research_with_sources", return_value=None), \
             patch("dashboard.deepseek_market_research.deepseek_market_research_daily_spend", return_value=0), \
             patch("dashboard.deepseek_market_research._diagnostic_request", side_effect=[dns, dns]), \
             patch("dashboard.deepseek_market_research.record_deepseek_market_research_usage") as record:
            results = run_deepseek_capability_diagnostics(database_path=self.db)
        self.assertEqual(results[0]["error_category"], "dns_failure")
        self.assertEqual(results[0]["estimated_cost_usd"], 0.0)
        self.assertEqual(record.call_args_list[0].kwargs["estimated_cost_usd"], 0.0)

    def test_cap_blocks_each_diagnostic_before_provider_call(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-only"}), \
             patch("dashboard.data_access.get_latest_deepseek_market_research_with_sources", return_value=None), \
             patch("dashboard.deepseek_market_research.deepseek_market_research_daily_spend", return_value=0), \
             patch("dashboard.deepseek_market_research._diagnostic_request") as request, \
             patch("dashboard.deepseek_market_research.record_deepseek_market_research_usage"):
            results = run_deepseek_capability_diagnostics(request_limit_usd=.001, daily_limit_usd=.001,
                                                           database_path=self.db)
        request.assert_not_called()
        self.assertEqual(results[0]["result"], "spending_cap")
        self.assertEqual(next(row for row in results if row["test"].startswith("C ·"))["result"],
                         "skipped_A_or_B_failed")

    def test_latest_success_remains_available_after_later_failed_attempt(self):
        good = {"cache_key":"a"*64, "topic":"VIX and short-term market stress", "snapshot":self.snapshot,
                "generated_at":"2026-10-30T12:00:00+00:00", "model":"deepseek-flash",
                "report_text":"Saved complete briefing", "sources":[], "usage":{},
                "research_status":"complete", "web_search_succeeded":False}
        failed = {**good, "cache_key":"b"*64, "generated_at":"2026-10-31T12:00:00+00:00",
                  "report_text":"", "research_status":"incomplete", "completion_reason":"max_output_tokens"}
        save_deepseek_market_research_report(good, self.db)
        save_deepseek_market_research_report(failed, self.db)
        latest_success = get_latest_completed_deepseek_market_research("VIX and short-term market stress", self.db)
        self.assertEqual(latest_success["report_text"], "Saved complete briefing")
        self.assertEqual(latest_success["generated_at"], good["generated_at"])


if __name__ == "__main__":
    unittest.main()

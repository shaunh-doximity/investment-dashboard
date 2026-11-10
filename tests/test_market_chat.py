import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dashboard.data_access import (
    clear_market_section_chat, clear_market_section_chat_groups,
    list_market_section_chat, list_market_section_chat_groups, list_market_ai_request_audit,
    save_market_section_chat_message,
)
from dashboard.database import initialize_database
from dashboard.market_chat import (
    answer_market_section_question, build_market_chat_context,
    estimate_market_chat_cost, market_chat_preflight,
)
from dashboard.market_snapshot import build_market_snapshot


def sample_snapshot():
    return build_market_snapshot({
        "relative_strength": {"as_of":"2026-10-30", "source":"Tiingo", "status":"available",
            "metrics":{"xly_xlp":{"value":1.3,"unit":"pp","window":"20D","universe":"XLY/XLP",
                         "definition":"relative adjusted return","as_of":"2026-10-30"}}},
        "rates_macro": {"as_of":"2026-10-30", "source":"Treasury", "status":"available",
            "metrics":{"ten_year":{"value":4.1,"unit":"%","window":"latest","universe":"Treasury",
                         "definition":"par yield","as_of":"2026-10-30"}}},
    })


class MarketChatTests(unittest.TestCase):
    def test_context_is_snapshot_bound_and_contains_relevant_other_sections(self):
        snap = sample_snapshot()
        history = [{"role":"user", "content":f"Q{i}"} for i in range(12)]
        context = build_market_chat_context(snap, "relative_strength",
            {"headline":"XLY/XLP improved"}, {"id":5,"generated_at":"now","report_text":"briefing",
                                                "sources":[{"url":"https://example.com"}]}, history,
            "Could yields explain this?")
        self.assertEqual(context["snapshot_fingerprint"], snap["fingerprint"])
        self.assertIn("rates_macro", context["section_data"])
        self.assertNotIn("rates_macro", context["other_dashboard_sections"])
        self.assertIn("rates_macro", context["section_data"])
        self.assertEqual(context["section_id"], "relative_strength")
        self.assertEqual(len(context["recent_messages"]), 8)
        self.assertEqual(context["saved_research"]["highlight"]["headline"], "XLY/XLP improved")
        self.assertEqual(context["saved_sources"][0]["url"], "https://example.com")
        self.assertNotIn("portfolio", str(context).lower())

    def test_context_keeps_compact_dated_metrics_without_chart_history(self):
        snap = sample_snapshot()
        snap["sections"]["rates_macro"]["daily_history"] = [{"date": "2026-10-30", "value": i}
                                                               for i in range(1000)]
        context = build_market_chat_context(snap, "rates_macro", None, None, [], "Why could yields fall?")
        encoded = __import__("json").dumps(context)
        self.assertLess(len(encoded), 6000)
        self.assertIn("ten_year", context["section_data"]["rates_macro"]["metrics"])
        self.assertNotIn("daily_history", encoded)
        metric = context["section_data"]["rates_macro"]["metrics"]["ten_year"]
        self.assertEqual(metric["as_of"], "2026-10-30")
        self.assertEqual(metric["unit"], "%")

    def test_budget_estimate_includes_optional_web_search(self):
        context = build_market_chat_context(sample_snapshot(), "relative_strength", None, None, [], "Question")
        local = estimate_market_chat_cost(context, search_mode="dashboard_analysis")
        web = estimate_market_chat_cost(context, search_mode="dashboard_web")
        self.assertGreater(web, local)

    def test_chat_messages_persist_by_section_and_snapshot_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "market.db"
            initialize_database(db)
            for role, request, content in (("user", "r1", "Question"), ("assistant", "r1", "Answer")):
                save_market_section_chat_message(section_id="vix", snapshot_fingerprint="fp-a",
                    request_id=request, role=role, content=content, model="deepseek-flash", database_path=db)
            save_market_section_chat_message(section_id="vix", snapshot_fingerprint="fp-b",
                request_id="r2", role="user", content="Other snapshot", database_path=db)
            self.assertEqual([row["content"] for row in list_market_section_chat("vix", "fp-a", database_path=db)],
                             ["Question", "Answer"])
            self.assertEqual(list_market_section_chat("vix", "fp-b", database_path=db)[0]["content"], "Other snapshot")
            self.assertEqual(clear_market_section_chat("vix", "fp-a", db), 2)
            self.assertEqual(list_market_section_chat("vix", "fp-a", database_path=db), [])

    def test_history_manager_lists_selectable_groups_and_clears_only_selected_or_all(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "chat.db"
            initialize_database(db)
            rows = [
                ("regime", "fp-a", "Question A", "deepseek-flash"),
                ("regime", "fp-a", "Answer A", "deepseek-flash"),
                ("vix", "fp-b", "Question B", "deepseek-v4-pro"),
                ("vix", "fp-b", "Answer B", "deepseek-v4-pro"),
                ("rates_macro", "fp-c", "Keep this", "deepseek-flash"),
            ]
            for index, (section, fingerprint, message, model) in enumerate(rows):
                save_market_section_chat_message(section_id=section, snapshot_fingerprint=fingerprint,
                    request_id=f"r{index}", role="user" if index % 2 == 0 else "assistant",
                    content=message, model=model, database_path=db)
            groups = list_market_section_chat_groups(database_path=db)
            self.assertEqual({(g["section_id"], g["snapshot_fingerprint"]) for g in groups},
                             {("regime", "fp-a"), ("vix", "fp-b"), ("rates_macro", "fp-c")})
            self.assertEqual(next(g for g in groups if g["section_id"] == "vix")["models"], "deepseek-v4-pro")
            deleted = clear_market_section_chat_groups([("regime", "fp-a"), ("vix", "fp-b")], database_path=db)
            self.assertEqual(deleted, 4)
            self.assertEqual([g["section_id"] for g in list_market_section_chat_groups(database_path=db)], ["rates_macro"])
            deleted_all = clear_market_section_chat_groups(
                [(g["section_id"], g["snapshot_fingerprint"]) for g in list_market_section_chat_groups(database_path=db)],
                database_path=db)
            self.assertEqual(deleted_all, 1)
            self.assertEqual(list_market_section_chat_groups(database_path=db), [])

    def test_retention_is_bounded_and_other_sections_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "market.db"
            initialize_database(db)
            for index in range(70):
                save_market_section_chat_message(section_id="regime", snapshot_fingerprint="fp",
                    request_id=f"r{index}", role="user", content=f"message {index}", retention=60,
                    database_path=db)
            self.assertEqual(len(list_market_section_chat("regime", "fp", limit=100, database_path=db)), 60)
            self.assertEqual(list_market_section_chat("vix", "fp", database_path=db), [])

    def test_budget_block_happens_before_provider_request(self):
        context = build_market_chat_context(sample_snapshot(), "regime", None, None, [], "Question")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"placeholder"}), \
             patch("dashboard.market_chat.deepseek_market_research_daily_spend", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "per-request cap"):
                answer_market_section_question(context, search_mode="dashboard_analysis",
                    request_limit_usd=.000001, daily_limit_usd=5)

    def test_preflight_explains_empty_question_and_budget_but_allows_new_chat_without_highlight(self):
        snap = sample_snapshot()
        empty = market_chat_preflight(snap, "", search_mode="dashboard_analysis",
            request_limit_usd=1, daily_limit_usd=5, daily_spend_usd=0)
        self.assertFalse(empty["enabled"])
        self.assertIn("Enter a question", " ".join(empty["reasons"]))
        estimate = estimate_market_chat_cost({"current_question":"Hello", "snapshot":snap}, search_mode="dashboard_analysis")
        allowed = market_chat_preflight(snap, "Hello", search_mode="dashboard_analysis",
            request_limit_usd=1, daily_limit_usd=5, daily_spend_usd=0, api_configured=True)
        self.assertTrue(allowed["enabled"])
        blocked = market_chat_preflight(snap, "Hello", search_mode="dashboard_analysis",
            request_limit_usd=estimate/2, daily_limit_usd=5, daily_spend_usd=0)
        self.assertFalse(blocked["enabled"])
        self.assertIn("per-request cap", " ".join(blocked["reasons"]))

    def test_mocked_end_to_end_question_gets_answer_and_can_be_saved(self):
        import os
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "chat.db"
            initialize_database(db)
            context = build_market_chat_context(sample_snapshot(), "relative_strength", None, None, [],
                                                "Could yields explain XLY/XLP?")
            fake_response = SimpleNamespace(status="completed", output_text="Rates can fall after a hike if it was priced in or the forward path softened.",
                                            model="deepseek-flash", usage=SimpleNamespace(input_tokens=1200, output_tokens=30,
                                            output_tokens_details=SimpleNamespace(reasoning_tokens=0)))
            calls=[]
            def create(**kwargs):
                calls.append(kwargs)
                return fake_response
            fake_client = SimpleNamespace(responses=SimpleNamespace(create=create))
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"placeholder"}), \
                 patch("openai.OpenAI", return_value=fake_client), \
                 patch("dashboard.market_chat.deepseek_market_research_daily_spend", return_value=0):
                answer = answer_market_section_question(context, search_mode="dashboard_analysis",
                    request_limit_usd=1, daily_limit_usd=5, database_path=db)
            self.assertTrue(answer["text"].startswith("Rates can fall"))
            self.assertIn("two or three competing hypotheses", calls[0]["input"])
            self.assertIn("general mechanisms", calls[0]["input"])
            self.assertIn("event-specific facts remain unverified", calls[0]["input"])
            self.assertEqual(calls[0]["model"], "deepseek-flash")
            self.assertEqual(answer["requested_model"], "deepseek-flash")
            self.assertEqual(answer["actual_model"], "deepseek-flash")
            self.assertEqual(answer["reasoning_effort"], "none")
            self.assertEqual(answer["output_token_allowance"], 1200)
            save_market_section_chat_message(section_id="relative_strength", snapshot_fingerprint=sample_snapshot()["fingerprint"],
                request_id="request-1", role="assistant", content=answer["text"], model=answer["model"], database_path=db)
            saved = list_market_section_chat("relative_strength", sample_snapshot()["fingerprint"], database_path=db)
            self.assertEqual(saved[-1]["content"], answer["text"])
            audit = list_market_ai_request_audit(database_path=db, limit=1)[0]
            self.assertEqual(audit["purpose"], "section_chat:relative_strength")
            self.assertEqual(audit["requested_model"], "deepseek-flash")
            self.assertEqual(audit["actual_model"], "deepseek-flash")
            self.assertIn("POST /responses", audit["endpoint"])
            self.assertEqual(audit["reasoning_tokens"], 0)

    def test_chat_does_not_infer_actual_model_if_provider_omits_it(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "chat.db"
            initialize_database(db)
            context=build_market_chat_context(sample_snapshot(), "regime", None, None, [], "Question")
            response=SimpleNamespace(status="completed", output_text="Answer", model=None,
                usage=SimpleNamespace(input_tokens=50, output_tokens=20))
            fake_client=SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs:response))
            with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"placeholder"}), \
                 patch("openai.OpenAI",return_value=fake_client), \
                 patch("dashboard.market_chat.deepseek_market_research_daily_spend",return_value=0):
                answer=answer_market_section_question(context,search_mode="dashboard_analysis",
                    request_limit_usd=1,daily_limit_usd=5,database_path=db)
            self.assertIsNone(answer["actual_model"])
            self.assertEqual(answer["model"],"Actual model not reported")
            audit=list_market_ai_request_audit(database_path=db,limit=1)[0]
            self.assertIsNone(audit["actual_model"])

    def test_failed_chat_request_is_audited_without_prompt_or_question_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/"chat.db"; initialize_database(db)
            context=build_market_chat_context(sample_snapshot(),"rates_macro",None,None,[],"Why could yields fall after a hike?")
            with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"placeholder"}), \
                 patch("openai.OpenAI",side_effect=__import__("requests").ConnectionError("connection failed")), \
                 patch("dashboard.market_chat.deepseek_market_research_daily_spend",return_value=0):
                with self.assertRaisesRegex(RuntimeError,"network_connection_failure"):
                    answer_market_section_question(context,search_mode="dashboard_analysis",
                        request_limit_usd=1,daily_limit_usd=5,database_path=db)
            audit=list_market_ai_request_audit(database_path=db,limit=1)[0]
            self.assertEqual(audit["requested_model"],"deepseek-flash")
            self.assertIsNone(audit["actual_model"])
            self.assertEqual(audit["completion_status"],"provider_error")
            self.assertEqual(audit["error_category"],"network_connection_failure")
            self.assertNotIn("Why could yields fall",str(audit))

    def test_web_mode_retrieves_dated_source_and_keeps_model_citations_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/"chat.db"; initialize_database(db)
            context=build_market_chat_context(sample_snapshot(),"rates_macro",None,None,[],"Why did long yields decline?")
            class SearchResponse:
                ok=True
                status_code=200
                def json(self):
                    return {"results":[{"title":"Treasury release","url":"https://home.treasury.gov/test",
                        "date":"2026-10-30","snippet":"Dated official evidence."}]}
            class SearchClient:
                calls=[]
                payloads=[]
                @classmethod
                def post(cls,url,**kwargs):
                    cls.calls.append(url)
                    cls.payloads.append(kwargs.get("json", {}))
                    return SearchResponse()
            response=SimpleNamespace(status="completed",output_text="A dated answer [source].",model="deepseek-v4-pro",
                usage=SimpleNamespace(input_tokens=500,output_tokens=100,output_tokens_details=SimpleNamespace(reasoning_tokens=0)))
            calls=[]
            def create(**kwargs):
                calls.append(kwargs)
                return response
            fake=SimpleNamespace(responses=SimpleNamespace(create=create))
            with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"placeholder"}), \
                 patch("openai.OpenAI",return_value=fake), \
                 patch("dashboard.market_chat.deepseek_market_research_daily_spend",return_value=0):
                answer=answer_market_section_question(context,search_mode="dashboard_web",
                    request_limit_usd=1,daily_limit_usd=5,database_path=db,http=SearchClient,
                    model="deepseek-v4-pro")
            self.assertEqual(SearchClient.calls,["https://api.deepseek.com/v1/tools/search"])
            self.assertLessEqual(len(SearchClient.payloads[0]["query"]),128)
            self.assertLessEqual(len(SearchClient.payloads[0]["ori_question"]),128)
            self.assertEqual(answer["sources"][0]["date"],"2026-10-30")
            self.assertEqual(answer["requested_model"],"deepseek-v4-pro")
            self.assertEqual(answer["actual_model"],"deepseek-v4-pro")
            self.assertTrue(answer["search_succeeded"])
            self.assertIn("cite important factual claims with source URLs and dates",calls[0]["input"])
            audit=list_market_ai_request_audit(database_path=db,limit=1)[0]
            self.assertEqual(audit["search_requests"],1)
            self.assertIn("POST /v1/tools/search",audit["endpoint"])
            self.assertIn("POST /responses",audit["endpoint"])
            self.assertEqual(audit["requested_model"],"deepseek-v4-pro")

    def test_explicit_premarket_request_returns_valid_conditional_paths(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "chat.db"
            initialize_database(db)
            context = build_market_chat_context(sample_snapshot(), "market_premarket", None, None,
                [{"role": "user", "content": "SMH is down 2% premarket."}],
                "SMH is down 2% premarket while breadth is firm.",
                user_observations=[{"observation_text": "SMH is down 2% premarket.",
                    "observed_at": "2026-11-05T13:00:00+00:00", "session_type": "premarket",
                    "verification_status": "user_supplied_unverified", "symbols": ["SMH"]}])
            context["conversation_mode"] = "premarket_what_if"
            payload = {"setup_meaning": "A technology-specific move could coexist with broad participation.",
                "most_important_uncertainty": "Whether breadth persists through the regular session.",
                "paths": [{"path_id": f"case-{i}", "name": f"Conditional path {i}",
                    "if_condition": "SMH lags SPY", "then_interpretation": "Leadership remains concentrated",
                    "why_it_might_happen": "Earnings repricing or positioning", "supporting_or_contradicting_evidence": "RSP/SPY is positive",
                    "indicators_to_watch": "SMH/SPY, QQQ/SPY and breadth"} for i in range(1, 4)],
                "session_watch": ["Check whether SPY holds above its opening level"]}
            answer_json = json.dumps(payload)
            response = SimpleNamespace(status="completed", output_text=answer_json,
                model="deepseek-flash", usage=SimpleNamespace(input_tokens=800, output_tokens=500,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0)))
            request_payloads = []
            def create(**kwargs):
                request_payloads.append(kwargs)
                return response
            fake_client = SimpleNamespace(responses=SimpleNamespace(create=create))
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "placeholder"}), \
                 patch("openai.OpenAI", return_value=fake_client), \
                 patch("dashboard.market_chat.deepseek_market_research_daily_spend", return_value=0):
                answer = answer_market_section_question(context, search_mode="dashboard_analysis",
                    request_limit_usd=1, daily_limit_usd=5, output_tokens=2800, model="deepseek-flash",
                    database_path=db)
            self.assertEqual(len(json.loads(answer["text"])["paths"]), 3)
            self.assertIn("Premarket What-If analysis", request_payloads[0]["input"])
            self.assertIn("user_supplied_unverified", request_payloads[0]["input"])
            audit = list_market_ai_request_audit(database_path=db, limit=1)[0]
            self.assertEqual(audit["purpose"], "section_chat:market_premarket")


if __name__ == "__main__":
    unittest.main()

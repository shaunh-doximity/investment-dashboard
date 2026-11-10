import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from dashboard.ai_providers import AIProviderError, generate_text
from dashboard.ai_research import answer_research_question, generate_strategy_draft


class ProviderMocks(unittest.TestCase):
    def test_deepseek_is_the_default_provider(self):
        from dashboard.ai_providers import DEFAULT_PROVIDER, MODEL_CHOICES, default_model
        self.assertEqual(DEFAULT_PROVIDER, "deepseek")
        self.assertEqual(MODEL_CHOICES["deepseek"], ("deepseek-chat",))
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-chat"}):
            self.assertEqual(default_model("deepseek"), "deepseek-chat")
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-v4-pro"}):
            self.assertEqual(default_model("deepseek"), "deepseek-chat")

    def test_dashboard_rejects_unavailable_deepseek_model(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
            with self.assertRaisesRegex(AIProviderError, "deepseek-chat model only"):
                generate_text(prompt="hello", provider="deepseek", model="deepseek-v4-pro")

    def test_missing_key_is_actionable_and_does_not_create_a_client(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaisesRegex(AIProviderError, "Add DEEPSEEK_API_KEY"):
                generate_text(prompt="hello", provider="deepseek")

    def test_deepseek_uses_openai_sdk_compatible_base_and_chat_completions(self):
        calls = {}

        class FakeClient:
            def __init__(self, **kwargs):
                calls["client"] = kwargs
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

            def create(self, **kwargs):
                calls["request"] = kwargs
                return {"model": "deepseek-chat", "choices": [{"message": {"content": "Draft"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-deepseek", "DEEPSEEK_MODEL": "deepseek-chat"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            result = generate_text(prompt="hello", provider="deepseek", max_output_tokens=512)
        self.assertEqual(calls["client"]["base_url"], "https://api.deepseek.com")
        self.assertEqual(calls["request"]["model"], "deepseek-chat")
        self.assertEqual(calls["request"]["max_tokens"], 512)
        self.assertEqual(result.text, "Draft")
        self.assertEqual((result.input_tokens, result.output_tokens), (10, 20))

    def test_deepseek_json_mode_uses_supported_chat_completions_parameter(self):
        calls = {}
        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
            def create(self, **kwargs):
                calls.update(kwargs)
                return {"model": "deepseek-chat", "choices": [{"message": {"content": "{}"}}]}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            generate_text(prompt="return json", provider="deepseek", model="deepseek-chat", json_mode=True)
        self.assertEqual(calls["response_format"], {"type": "json_object"})

    def test_deepseek_json_mode_reports_output_truncation_actionably(self):
        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: {
                    "choices": [{"finish_reason": "length", "message": {"content": '{"investment_thesis":'}}],
                }))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            with self.assertRaisesRegex(AIProviderError, "reached the output-token limit"):
                generate_text(prompt="return json", provider="deepseek", model="deepseek-chat", json_mode=True)

    def test_switching_provider_changes_actual_client_configuration(self):
        client_configs = []

        class FakeClient:
            def __init__(self, **kwargs):
                client_configs.append(kwargs)
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: {
                    "model": kw["model"], "choices": [{"message": {"content": "DeepSeek response"}}],
                }))
                self.responses = types.SimpleNamespace(create=lambda **kw: {
                    "model": kw["model"], "output_text": "OpenAI response",
                })

        env = {"DEEPSEEK_API_KEY": "deepseek-test", "OPENAI_API_KEY": "openai-test"}
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            deepseek = generate_text(prompt="p", provider="deepseek", model="deepseek-chat")
            openai = generate_text(prompt="p", provider="openai", model="gpt-5-nano")
        self.assertEqual(deepseek.provider, "deepseek")
        self.assertEqual(openai.provider, "openai")
        self.assertEqual(client_configs[0]["base_url"], "https://api.deepseek.com")
        self.assertNotIn("base_url", client_configs[1])

    def test_openai_responses_api_result_is_normalized(self):
        class FakeClient:
            def __init__(self, **kwargs):
                self.responses = types.SimpleNamespace(create=lambda **kw: {
                    "model": kw["model"], "output_text": "ok",
                    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                })
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            result = generate_text(prompt="hello", provider="openai", model="gpt-5-nano")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.total_tokens, 5)

    def test_openai_json_mode_uses_responses_text_format(self):
        calls = {}
        class FakeClient:
            def __init__(self, **kwargs):
                self.responses = types.SimpleNamespace(create=self.create)
            def create(self, **kwargs):
                calls.update(kwargs)
                return {"model": kwargs["model"], "output_text": "{}"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            generate_text(prompt="return json", provider="openai", model="gpt-5-nano", json_mode=True)
        self.assertEqual(calls["text"], {"format": {"type": "json_object"}})

    def test_empty_or_malformed_provider_responses_are_actionable(self):
        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: {
                    "choices": [{"message": {"content": "  "}}],
                }))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            with self.assertRaisesRegex(AIProviderError, "empty response"):
                generate_text(prompt="hello", provider="deepseek")

        class MalformedClient:
            def __init__(self, **kwargs):
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: {"choices": []}))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=MalformedClient)}):
            with self.assertRaisesRegex(AIProviderError, "empty response"):
                generate_text(prompt="hello", provider="deepseek")

    def test_provider_errors_are_translated_without_echoing_private_details(self):
        class FakeRateLimitError(Exception):
            status_code = 429
            def __str__(self):
                return "secret-bearing request detail"

        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(FakeRateLimitError())))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeClient)}):
            with self.assertRaisesRegex(AIProviderError, "rate limit or quota") as caught:
                generate_text(prompt="private prompt", provider="deepseek")
        self.assertNotIn("secret-bearing", str(caught.exception))

    def test_deepseek_does_not_accept_openai_web_search_as_a_silent_fallback(self):
        with self.assertRaisesRegex(AIProviderError, "only with the OpenAI"):
            generate_text(prompt="hello", provider="deepseek", web_search=True)

    def test_strategy_draft_prices_are_estimates_and_rrr_is_calculated_in_python(self):
        payload = {
            "investment_thesis": "Example thesis", "strategy_type": "Growth", "investment_horizon": "Medium",
            "expected_holding_window": "6 months", "bullish_scenario": "Growth continues",
            "bearish_scenario": "Growth slows", "catalysts": ["Product release"], "risks": ["Execution"],
            "entry_price_low": 90, "entry_price_high": 100, "target_price": 130,
            "invalidation_price": 80, "stop_invalidation": "Thesis breaks",
            "assumptions": ["Illustrative figures"], "uncertainties": ["No verified current data"],
        }
        fake_result = types.SimpleNamespace(provider="deepseek", model="deepseek-chat",
                                            text=json.dumps(payload), source_metadata=())
        with patch("dashboard.ai_research.generate_text", return_value=fake_result) as call:
            draft = generate_strategy_draft("TEST", provider="deepseek", model="deepseek-chat",
                                            max_output_tokens=1024)
        self.assertTrue(draft["ai_price_estimates"])
        self.assertEqual(draft["calculated_setup"]["status"], "valid")
        scenarios = draft["calculated_setup"]["planned_scenarios"]
        self.assertAlmostEqual(scenarios[0]["planned_rrr"], 4.0)
        self.assertAlmostEqual(scenarios[1]["planned_rrr"], 1.5)
        self.assertFalse(call.call_args.kwargs["web_search"])
        self.assertTrue(call.call_args.kwargs["json_mode"])

    def test_horizon_scenario_rrr_is_calculated_by_python(self):
        payload = {
            "investment_thesis": "Test thesis", "strategy_type": "Quality", "investment_horizon": "Medium",
            "expected_holding_window": "Six months", "bullish_scenario": "Growth persists",
            "bearish_scenario": "Margins contract", "catalysts": "Product launch", "risks": "Competition",
            "entry_price_low": 90, "entry_price_high": 100, "target_price": 130,
            "invalidation_price": 80, "stop_invalidation": "Thesis breaks",
            "assumptions": "Assumes stable demand", "uncertainties": "No verified news",
            "horizon_scenarios": [{
                "horizon": "Swing / short-term", "holding_window": "2-8 weeks", "thesis": "Momentum setup",
                "entry_price_low": 100, "entry_price_high": 100, "target_price": 120,
                "invalidation_price": 90, "entry_conditions": "Hold above support",
                "evidence_for_levels": "User supplied levels",
            }],
        }
        fake_result = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text=json.dumps(payload))
        with patch("dashboard.ai_research.generate_text", return_value=fake_result):
            draft = generate_strategy_draft("TEST", provider="deepseek", model="deepseek-chat", max_output_tokens=900)
        scenario = draft["horizon_scenarios"][0]
        self.assertEqual(scenario["calculated_setup"]["planned_rrr"], 2.0)
        self.assertTrue(scenario["ai_price_estimates"])

    def test_malformed_ai_strategy_response_never_yields_a_draft_for_saving(self):
        fake_result = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text="not json")
        with patch("dashboard.ai_research.generate_text", return_value=fake_result):
            with self.assertRaisesRegex(AIProviderError, "not valid JSON"):
                generate_strategy_draft("TEST", provider="deepseek", model="deepseek-chat", max_output_tokens=900)

    def test_research_chat_sends_saved_strategy_without_portfolio_positions(self):
        fake_result = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text="Answer")
        strategy = {"investment_thesis": "private saved thesis", "target_price": 150}
        with patch("dashboard.ai_research.generate_text", return_value=fake_result) as call:
            answer_research_question("TEST", "What changed?", [], provider="deepseek", model="deepseek-chat",
                                     max_output_tokens=800, strategy=strategy, portfolio_context=None)
            self.assertIn("private saved thesis", call.call_args.kwargs["prompt"])
            self.assertNotIn("optional_portfolio_context", call.call_args.kwargs["prompt"])
            answer_research_question("TEST", "What changed?", [], provider="deepseek", model="deepseek-chat",
                                     max_output_tokens=800, strategy=strategy, portfolio_context=[{"symbol": "TEST"}])
            self.assertIn("private saved thesis", call.call_args.kwargs["prompt"])
            self.assertIn("optional_portfolio_context", call.call_args.kwargs["prompt"])

    def test_research_system_prompt_can_be_configured_locally(self):
        fake_result = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text="Answer")
        with patch.dict(os.environ, {"RESEARCH_STRATEGIST_PROMPT": "CUSTOM SKEPTICAL PROMPT"}), \
             patch("dashboard.ai_research.generate_text", return_value=fake_result) as call:
            answer_research_question("TEST", "Question", [], provider="deepseek", model="deepseek-chat",
                                     max_output_tokens=500, strategy={})
        self.assertIn("CUSTOM SKEPTICAL PROMPT", call.call_args.kwargs["instructions"])


if __name__ == "__main__":
    unittest.main()

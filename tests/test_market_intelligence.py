import os
import ast
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dashboard.ai_market_analyst import (
    AssessmentValidationError, _aligned_relative_performance, build_market_snapshot,
    generate_market_briefing, validate_market_assessment,
)
from dashboard.data_access import get_latest_market_briefing, save_market_briefing
from dashboard.database import initialize_database
from dashboard.deepseek_market_research import MarketResearchError
from dashboard.market_history import SECTOR_SYMBOLS
from dashboard.market_visuals import (
    breadth_proxy_summary, breadth_rows, relative_leadership_rows,
    leadership_calculation_symbols, market_leadership_observation, market_leadership_series,
    relative_strength_label, rotation_quadrant, sector_heatmap_grid,
    sector_return_rows, sector_rotation_points, sector_rotation_pulse, signed_return_color,
)
from dashboard.navigation import TAB_LABELS, TAB_STATE_KEY
from dashboard.theme import PLOTLY_FONT_SIZE, apply_plotly_theme

BROAD_TEST_SYMBOLS = ("SPY", "RSP", "QQQ", "DIA", "IWM", "SMH")


def bars(closes, start=date(2025, 1, 1)):
    result, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        result.append({"trading_date": day.isoformat(), "close": close})
        day += timedelta(days=1)
    return result


class MarketVisualizationTests(unittest.TestCase):
    def test_shared_plotly_theme_sets_readable_fonts_and_requested_height(self):
        import plotly.graph_objects as go

        figure = apply_plotly_theme(go.Figure(), height=420)
        self.assertEqual(figure.layout.height, 420)
        self.assertEqual(figure.layout.font.size, PLOTLY_FONT_SIZE)
        self.assertEqual(figure.layout.legend.font.size, PLOTLY_FONT_SIZE)
        self.assertEqual(figure.layout.hoverlabel.font.size, PLOTLY_FONT_SIZE)

    def test_sector_performance_and_breadth_transform(self):
        histories = {"SPY": bars(range(100, 131)), "XLK": bars(range(100, 161)), "XLF": bars(range(100, 91, -1))}
        rows = sector_return_rows(histories, {"XLK": "Technology", "XLF": "Financials"}, 5)
        self.assertEqual(rows[0]["symbol"], "XLK")
        self.assertEqual(rows[0]["name"], "Technology")
        rows_60d = sector_return_rows({"XLK": bars(range(100, 171))}, {"XLK": "Technology"}, 60)
        self.assertEqual(rows_60d[0]["symbol"], "XLK")
        self.assertIsNotNone(rows_60d[0]["return"])
        breadth = breadth_rows(histories, ("SPY", "XLK"))
        self.assertAlmostEqual(breadth[1]["excess_5d"], breadth[1]["return_5d"] - breadth[0]["return_5d"])

    def test_global_timeframes_and_relative_leadership_chart_rows(self):
        spy = bars([100 + i for i in range(61)])
        qqq = bars([100 + 1.5 * i for i in range(61)])
        rsp = bars([100 + .5 * i for i in range(61)])
        histories = {"SPY": spy, "QQQ": qqq, "RSP": rsp, "DIA": bars([100, 101])}
        for window in (5, 20, 60):
            rows = relative_leadership_rows(histories, ("SPY", "RSP", "QQQ", "IWM"), window)
            qqq_row = next(row for row in rows if row["symbol"] == "QQQ")
            rsp_row = next(row for row in rows if row["symbol"] == "RSP")
            spy_row = next(row for row in rows if row["symbol"] == "SPY")
            self.assertAlmostEqual(qqq_row["excess"], qqq_row["return"] - qqq_row["benchmark_return"])
            self.assertEqual(spy_row["excess"], 0.0)
            self.assertIsNone(next(row for row in rows if row["symbol"] == "IWM")["excess"])
            self.assertLessEqual(rows.index(qqq_row), rows.index(rsp_row))
        with self.assertRaises(ValueError):
            relative_leadership_rows(histories, ("SPY",), 10)

    def test_cumulative_and_relative_series_use_n_plus_one_aligned_observations(self):
        symbols = ("SPY", "RSP", "QQQ")
        histories = {
            "SPY": bars([100, 110, 105, 120]),
            "RSP": bars([100, 105, 110, 115]),
            "QQQ": bars([100, 120, 115, 130]),
        }
        series = market_leadership_series(histories, symbols, 3)
        self.assertEqual(series["status"], "available")
        self.assertEqual(len(series["dates"]), 4)
        self.assertEqual(series["absolute_returns"]["SPY"][0], 0.0)
        self.assertEqual(series["relative_returns"]["RSP"][0], 0.0)
        self.assertEqual(series["relative_returns"]["SPY"], [0.0] * 4)
        self.assertAlmostEqual(series["absolute_returns"]["SPY"][-1], 20.0)
        self.assertAlmostEqual(series["relative_returns"]["RSP"][-1], -5.0)
        self.assertIn("RSP", market_leadership_observation(series))
        self.assertEqual(series["latest_date"], series["dates"][-1])

    def test_hidden_spy_remains_in_the_leadership_calculation_universe(self):
        visible = ("IGV", "MAGS")
        calculated = leadership_calculation_symbols(visible)
        self.assertEqual(calculated, ("SPY", "IGV", "MAGS"))
        self.assertEqual(leadership_calculation_symbols(("SPY", "IGV")), ("SPY", "IGV"))
        histories = {symbol: bars([100 + index for index in range(8)]) for symbol in calculated}
        series = market_leadership_series(histories, calculated, 5)
        self.assertEqual(series["status"], "available")
        self.assertIn("SPY", series["relative_returns"])
        self.assertEqual(series["relative_returns"]["SPY"], [0.0] * 6)

    def test_visible_sector_selection_does_not_change_standard_pulse_universe(self):
        histories = {"SPY": bars([100 + i for i in range(80)])}
        for index, symbol in enumerate(SECTOR_SYMBOLS):
            histories[symbol] = bars([100 + i * (1 + index / 100) for i in range(80)])
        # The UI's visible list is a display preference; this pulse is deliberately
        # calculated against the immutable standard-sector mapping.
        visible_custom_selection = {"IGV": "Custom ETF · IGV"}
        self.assertEqual(list(visible_custom_selection), ["IGV"])
        pulse = sector_rotation_pulse(histories, SECTOR_SYMBOLS, 5)
        self.assertEqual(pulse["sector_count"], 11)
        self.assertEqual(pulse["available_sector_count"], 11)

    def test_leadership_aligns_common_dates_and_marks_insufficient_history(self):
        symbols = ("SPY", "RSP", "QQQ")
        histories = {symbol: bars([100, 101, 102, 103, 104]) for symbol in symbols}
        histories["RSP"].pop(2)
        aligned = market_leadership_series(histories, symbols, 2)
        expected_dates = sorted(set(row["trading_date"] for row in histories["SPY"])
                                & set(row["trading_date"] for row in histories["RSP"])
                                & set(row["trading_date"] for row in histories["QQQ"]))
        self.assertEqual(aligned["dates"], expected_dates[-3:])
        self.assertNotIn(histories["SPY"][2]["trading_date"], aligned["dates"])
        short = market_leadership_series(histories, symbols, 5)
        self.assertEqual(short["status"], "insufficient")
        self.assertEqual(short["required_observations"], 6)

    def test_sector_heatmap_grid_sorts_tiles_and_omits_missing_values(self):
        grid = sector_heatmap_grid([
            {"symbol": "XLF", "name": "Financials", "return": -.02},
            {"symbol": "XLK", "name": "Technology", "return": .04},
            {"symbol": "XLE", "name": "Energy", "return": None},
            {"symbol": "XLV", "name": "Health Care", "return": .01},
        ], columns=2)
        self.assertEqual([row["symbol"] for row in grid["rows"]], ["XLK", "XLV", "XLF"])
        self.assertEqual(grid["height"], 2)
        self.assertEqual(grid["values"][0], [4.0, 1.0])
        self.assertLess(grid["values"][1][0], 0)
        self.assertEqual(grid["color_bound"], 4.0)

    def test_rotation_quadrant_classification(self):
        self.assertEqual(rotation_quadrant(.01, .02), "Leading")
        self.assertEqual(rotation_quadrant(.01, -.02), "Weakening")
        self.assertEqual(rotation_quadrant(-.01, -.02), "Lagging")
        self.assertEqual(rotation_quadrant(-.01, .02), "Improving")

    def test_sector_rotation_points_use_change_in_rolling_relative_strength(self):
        spy = bars([100.0] * 26)
        weakening_closes = [100.0 + 2.0 * i for i in range(21)] + [138.0, 136.0, 135.0, 134.0, 135.0]
        leading_closes = [100.0 + 1.5 * i for i in range(21)] + [132.0, 134.0, 136.0, 138.0, 140.0]
        points = sector_rotation_points(
            {"SPY": spy, "XLW": bars(weakening_closes), "XLD": bars(leading_closes)},
            {"XLW": "Weakening Test", "XLD": "Leading Test"},
            strength_window=20, momentum_window=5,
        )
        by_symbol = {row["symbol"]: row for row in points}
        self.assertEqual(by_symbol["XLW"]["quadrant"], "Weakening")
        self.assertGreater(by_symbol["XLW"]["relative_strength"], 0)
        self.assertLess(by_symbol["XLW"]["relative_momentum"], 0)
        self.assertEqual(by_symbol["XLD"]["quadrant"], "Leading")
        self.assertGreater(by_symbol["XLD"]["relative_momentum"], 0)

    def test_sector_pulse_counts_spread_equal_weight_baskets_and_participation_change(self):
        current_returns = {
            "XLV": .03, "XLP": .02, "XLU": .01,
            "XLY": .005, "XLI": 0.0, "XLB": -.01, "XLE": -.02,
            "XLF": .005, "XLK": -.015, "XLC": 0.0, "XLRE": .002,
        }
        histories = {}
        for symbol in SECTOR_SYMBOLS:
            previous_end = 98.0
            current_end = previous_end * (1 + current_returns[symbol])
            closes = [100 + (previous_end - 100) * index / 5 for index in range(6)]
            closes.extend(previous_end + (current_end - previous_end) * index / 5 for index in range(1, 6))
            histories[symbol] = bars(closes)

        pulse = sector_rotation_pulse(histories, SECTOR_SYMBOLS, 5)
        self.assertEqual(pulse["positive_count"], 6)
        self.assertEqual(pulse["available_sector_count"], 11)
        self.assertEqual(pulse["strongest"]["symbol"], "XLV")
        self.assertEqual(pulse["weakest"]["symbol"], "XLE")
        self.assertAlmostEqual(pulse["leadership_spread_pp"], 5.0)
        expected_defensive = sum(current_returns[symbol] for symbol in ("XLV", "XLP", "XLU")) / 3
        expected_cyclical = sum(current_returns[symbol] for symbol in ("XLY", "XLI", "XLB", "XLE", "XLF", "XLK", "XLC", "XLRE")) / 8
        self.assertAlmostEqual(pulse["defensive_return"], expected_defensive)
        self.assertAlmostEqual(pulse["cyclical_return"], expected_cyclical)
        self.assertGreater(pulse["defensive_vs_cyclical_pp"], 0)
        self.assertEqual(pulse["participation"], "broader")
        self.assertEqual(pulse["previous_positive_count"], 0)
        self.assertEqual(pulse["current_positive_count"], 6)

    def test_sector_pulse_reports_incomplete_basket_data(self):
        histories = {symbol: bars([100 + index for index in range(12)]) for symbol in SECTOR_SYMBOLS}
        histories.pop("XLU")
        pulse = sector_rotation_pulse(histories, SECTOR_SYMBOLS, 5)
        self.assertEqual(pulse["available_sector_count"], 10)
        self.assertIsNone(pulse["defensive_vs_cyclical_pp"])
        self.assertEqual(pulse["participation"], "unchanged")

    def test_absolute_return_color_and_relative_benchmark_label(self):
        self.assertEqual(signed_return_color(0.01), "green")
        self.assertEqual(signed_return_color(-0.01), "red")
        self.assertEqual(signed_return_color(None), "gray")
        self.assertEqual(relative_strength_label({"symbol": "SPY", "excess_5d": 0}), "Benchmark")
        self.assertEqual(relative_strength_label({"symbol": "QQQ", "excess_5d": 0.0251}), "+2.51 pp vs SPY")

    def test_rsp_breadth_proxy_uses_return_difference_and_cautious_interpretation(self):
        rows = [
            {"symbol": "SPY", "return_5d": 0.01, "return_20d": 0.03, "as_of": "2025-01-31"},
            {"symbol": "RSP", "return_5d": 0.0351, "return_20d": 0.02, "as_of": "2025-01-31"},
        ]
        positive = breadth_proxy_summary(rows, 5, today=date(2025, 2, 1))
        self.assertEqual(positive["status"], "available")
        self.assertAlmostEqual(positive["difference_pp"], 2.51)
        self.assertIn("both gained", positive["interpretation"])
        negative = breadth_proxy_summary(rows, 20, today=date(2025, 2, 1))
        self.assertAlmostEqual(negative["difference_pp"], -1.0)
        self.assertIn("does not establish narrowing absolute breadth", negative["interpretation"])
        self.assertEqual(negative["window"], 20)

    def test_rsp_outperformance_during_selloff_is_relative_resilience_not_broadening(self):
        rows = [
            {"symbol": "SPY", "return_5d": -0.05, "as_of": "2025-01-31"},
            {"symbol": "RSP", "return_5d": -0.025, "as_of": "2025-01-31"},
        ]
        result = breadth_proxy_summary(rows, 5, today=date(2025, 2, 1))
        self.assertAlmostEqual(result["rsp_return_pct"], -2.5)
        self.assertAlmostEqual(result["spy_return_pct"], -5.0)
        self.assertIn("Relative resilience during a selloff", result["interpretation"])
        self.assertNotIn("healthy broadening", result["interpretation"].lower())

    def test_breadth_proxy_missing_misaligned_or_stale_data_is_labeled(self):
        rows = [
            {"symbol": "SPY", "return_5d": 0.01, "as_of": "2025-01-31"},
            {"symbol": "RSP", "return_5d": 0.02, "as_of": "2025-01-31"},
        ]
        stale = breadth_proxy_summary(rows, today=date(2025, 2, 10))
        self.assertEqual(stale["status"], "stale")
        self.assertIn("Stale cache", stale["interpretation"])
        misaligned = [dict(rows[0]), {**rows[1], "as_of": "2025-01-30"}]
        self.assertEqual(breadth_proxy_summary(misaligned)["status"], "unavailable")
        self.assertEqual(breadth_proxy_summary(rows[:1])["status"], "unavailable")

    def test_sidebar_is_native_collapsed_and_ai_controls_remain_singleton(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        module = ast.parse(app_path.read_text())
        calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "set_page_config"]
        self.assertEqual(len(calls), 1)
        kwargs = {item.arg: item.value for item in calls[0].keywords}
        self.assertEqual(ast.literal_eval(kwargs["initial_sidebar_state"]), "collapsed")
        self.assertEqual(Path(__file__).resolve().parents[1].joinpath("dashboard/ai_settings.py").read_text().count('with st.sidebar.expander("AI settings"'), 1)

    def test_rotation_uses_intersected_adjusted_close_dates_and_trails(self):
        spy = bars([100 + i for i in range(30)])
        sector = bars([100 + 2 * i for i in range(30)])
        sector.pop(-1)  # last point must use the last overlapping session
        points = sector_rotation_points({"SPY": spy, "XLK": sector}, {"XLK": "Technology"})
        self.assertEqual(points[0]["trading_date"], sector[-1]["trading_date"])
        self.assertGreater(points[0]["relative_strength"], 0)
        self.assertEqual(points[0]["trail"][-1]["trading_date"], points[0]["trading_date"])


class MarketBriefingTests(unittest.TestCase):
    def test_snapshot_has_cached_source_dates_and_no_personal_data_by_default(self):
        histories = {symbol: bars([100 + i for i in range(25)]) for symbol in ("SPY", "XLK", "TLT")}
        snapshot = build_market_snapshot(histories)
        self.assertIn("SPY", snapshot["as_of_dates"])
        self.assertEqual(snapshot["source"], "Local SQLite cache; Tiingo daily split-and-dividend-adjusted close")
        self.assertNotIn("optional_portfolio_context", snapshot)

    def test_ai_snapshot_preserves_event_status_and_gates_earnings_symbols(self):
        event_records = [
            {"name": "FOMC meeting", "category": "Fed", "status": "Tentative", "start_utc": "2026-11-05T18:00:00Z", "related_symbols": [], "source_url": "https://federalreserve.gov", "provider": "fed", "last_verified_at": "2026-10-25"},
            {"name": "NVDA earnings", "category": "Earnings", "status": "Confirmed", "start_utc": "2026-11-10T13:30:00Z", "related_symbols": ["NVDA"], "source_url": "https://investor.nvidia.com", "provider": "manual_earnings", "last_verified_at": "2026-10-25"},
        ]
        histories = {"SPY": bars([100, 101])}
        without_portfolio = build_market_snapshot(histories, upcoming_events=event_records)
        self.assertEqual([event["category"] for event in without_portfolio["upcoming_market_events"]], ["Fed"])
        self.assertEqual(without_portfolio["upcoming_market_events"][0]["status"], "Tentative")
        with_portfolio = build_market_snapshot(histories, portfolio_context=[], upcoming_events=event_records)
        self.assertEqual([event["category"] for event in with_portfolio["upcoming_market_events"]], ["Fed", "Earnings"])

    def test_openai_responses_are_mocked_and_citations_usage_are_captured(self):
        class FakeResponses:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return types.SimpleNamespace(
                    output_text="""## Market Character
Mixed observations.
## Top 3 Market Signals
No supplied data.
## What Changed Since the Previous Briefing
No prior snapshot.
## Volatility & Market Risk
Unavailable.
## Portfolio and RRR Implications
Not included.
## What to Watch Next
Observe cached updates.""", model="gpt-6-luna",
                    usage=types.SimpleNamespace(input_tokens=100, output_tokens=50, total_tokens=150),
                    output=[types.SimpleNamespace(type="message", content=[types.SimpleNamespace(annotations=[
                        types.SimpleNamespace(type="url_citation", url_citation=types.SimpleNamespace(url="https://example.test/news", title="Example"))
                    ])])],
                )
        fake_responses = FakeResponses()
        fake_module = types.SimpleNamespace(OpenAI=lambda **kwargs: types.SimpleNamespace(responses=fake_responses))
        with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "gpt-6-luna"}), \
             patch.dict(sys.modules, {"openai": fake_module}):
            result = generate_market_briefing({"source": "local-cache"}, web_search=True, provider="openai")
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["total_tokens"], 150)
        self.assertEqual(result["source_metadata"][0]["url"], "https://example.test/news")
        self.assertEqual(fake_responses.kwargs["tools"][0]["type"], "web_search")
        self.assertEqual(fake_responses.kwargs["tool_choice"], "required")
        self.assertFalse(fake_responses.kwargs["store"])

    def test_sector_relative_acceleration_uses_adjacent_20_session_ratio_windows(self):
        dates = [row["trading_date"] for row in bars([100] * 42)]
        spy = [{"trading_date": day, "close": 100 + i} for i, day in enumerate(dates)]
        sector = [{"trading_date": day, "close": (100 + i) * (1 + (0.001 * i))} for i, day in enumerate(dates)]
        metric = _aligned_relative_performance({"SPY": spy, "XLK": sector}, ("XLK",))["XLK"]
        self.assertIsNotNone(metric["relative_momentum_acceleration_20d_pp"])
        self.assertIn("preceding 20-session", metric["acceleration_method"])
        missing = _aligned_relative_performance({"SPY": spy, "XLK": sector[:10]}, ("XLK",))["XLK"]
        self.assertIsNone(missing["relative_momentum_acceleration_20d_pp"])

    def test_assessment_response_requires_all_six_sections(self):
        good = "\n".join(f"## {heading}\nText." for heading in (
            "Market Character", "Top 3 Market Signals", "What Changed Since the Previous Briefing",
            "Volatility & Market Risk", "Portfolio and RRR Implications", "What to Watch Next"))
        self.assertEqual(validate_market_assessment(good), good)
        with self.assertRaises(AssessmentValidationError):
            validate_market_assessment("## Market Character\nOnly this.")

    def test_incomplete_ai_assessment_is_rejected_before_persistence_payload(self):
        fake_result = types.SimpleNamespace(provider="deepseek", model="deepseek-chat", text="Not a complete report",
                                            input_tokens=1, output_tokens=1, total_tokens=2, source_metadata=())
        with patch("dashboard.ai_market_analyst.generate_text", return_value=fake_result):
            with self.assertRaises(AssessmentValidationError):
                generate_market_briefing({"source": "cache"}, provider="deepseek")

    def test_briefing_persists_snapshot_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "briefing.db"
            initialize_database(path)
            original = {"generated_at": "2025-01-01T00:00:00+00:00", "provider": "deepseek", "model": "unit-test",
                        "input_snapshot": {"source": "local"}, "report_text": "Assessment",
                        "source_metadata": [], "input_tokens": 2, "output_tokens": 3,
                        "total_tokens": 5, "estimated_cost_usd": 0.0001}
            save_market_briefing(original, path)
            saved = get_latest_market_briefing(path)
            self.assertEqual(saved["input_snapshot"], original["input_snapshot"])
            self.assertEqual(saved["report_text"], "Assessment")
            self.assertEqual(saved["total_tokens"], 5)
            self.assertEqual(saved["provider"], "deepseek")

    @patch("dashboard.market_monitor_ui.save_market_regime_transition_record")
    @patch("dashboard.market_monitor_ui.get_app_preference", side_effect=lambda key, default=None: {
        "market_ai_spending_limits_v1": {"per_request_usd": 0.5, "daily_usd": 2.0},
        "market_ai_model_v1": "deepseek-flash",
    }.get(key, default))
    def test_normal_market_home_rerun_does_not_call_openai(self, _get_preference, save_transition_record):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        with patch("dashboard.pages.home.generate_market_briefing") as generate, \
             patch("dashboard.pages.home.refresh_historical_data") as refresh, \
             patch("dashboard.pages.home.refresh_cboe_volatility") as refresh_vix, \
             patch("dashboard.pages.home.load_history", side_effect=lambda symbol, *_args: bars([100 + i for i in range(180)]) if symbol in BROAD_TEST_SYMBOLS + tuple(SECTOR_SYMBOLS) else []), \
             patch("dashboard.cboe_volatility.requests.get") as cboe_request, \
             patch("dashboard.treasury_yields.requests.get") as treasury_request, \
             patch("dashboard.vix_sources.fetch_yfinance_vix") as yfinance_request, \
             patch("dashboard.fred_volatility.requests.get") as fred_request, \
             patch("dashboard.fred_macro.requests.get") as fred_macro_request, \
             patch("dashboard.event_calendar.requests.get") as event_request, \
             patch("dashboard.event_calendar_ui.list_market_events", return_value=[{
                 "event_id": "test", "name": "Test CPI Release", "category": "Macro",
                 "start_utc": (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                 "source_timezone": "America/New_York", "source_url": "https://www.bls.gov/schedule/",
                 "provider": "bls_ical", "status": "Tentative", "last_verified_at": datetime.now(timezone.utc).isoformat(),
                 "related_symbols": [], "all_day": False,
             }]), \
             patch("dashboard.event_calendar_ui.save_quick_earnings_events", return_value={
                 "added": 2, "updated": 0, "skipped": 0, "rejected": 0, "details": [],
             }) as save_earnings, \
             patch("dashboard.market_monitor_ui.answer_market_section_question") as market_chat_answer, \
             patch("dashboard.pages.home.TiingoDailyProvider.from_environment", return_value=None), \
             patch("dashboard.pages.home.TiingoDailyProvider.fetch_daily") as fetch_daily, \
             patch("dashboard.pages.research.answer_research_question") as chat, \
             patch("dashboard.deepseek_market_research.requests.get") as deepseek_models, \
             patch("dashboard.deepseek_market_research.requests.post") as deepseek_search, \
             patch("dashboard.market_monitor_ui.run_market_research", side_effect=MarketResearchError("DeepSeek model check failed (dns_failure; ConnectionError). Check DNS resolution, TLS certificates, proxy/VPN settings, then retry.")) as full_market_run, \
             patch("openai.OpenAI") as deepseek_client:
            result = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
            self.assertFalse(result.exception)
            save_transition_record.assert_called()
            self.assertTrue(any("AI Market Intelligence Desk" in str(item.value) for item in result.markdown))
            rendered_markdown = "\n".join(str(item.value) for item in result.markdown)
            rendered_copy = rendered_markdown + "\n" + "\n".join(str(item.value) for item in result.caption)
            self.assertIn("Market Ask AI", rendered_markdown)
            self.assertIn("Premarket What-If with AI", rendered_markdown)
            self.assertGreaterEqual(rendered_markdown.count("Ask AI about this section"), 6)
            self.assertNotIn("Section-specific AI Highlight and saved chat", "\n".join(str(item.label) for item in result.expander))
            self.assertIn("Saved research notes and prior scenario records", "\n".join(str(item.label) for item in result.expander))
            self.assertIn("Quantitative regime evidence, methods and feature export", "\n".join(str(item.label) for item in result.expander))
            self.assertNotIn("PYTHON OBSERVED REGIME", rendered_copy)
            self.assertNotIn("Turning-Point Signals", rendered_markdown)
            self.assertEqual(rendered_markdown.count("#### AI Market Intelligence Desk"), 1)
            self.assertNotIn("Risk Appetite & Market Regime Outlook", rendered_markdown)
            self.assertNotIn("Market Regime Transition Outlook", rendered_markdown)
            self.assertGreater(rendered_markdown.find("#### AI Market Intelligence Desk"),
                               rendered_markdown.find("##### Volatility Risk Monitor"))
            model_selector = next(widget for widget in result.selectbox if widget.label == "Fast Briefing / section-chat model")
            self.assertEqual(set(model_selector.options), {"deepseek-flash", "deepseek-v4-pro"})
            self.assertIn(result.session_state.get("market_ai_model"), model_selector.options)
            self.assertTrue(any(widget.label.startswith("Clear chat history") for widget in result.button))
            self.assertFalse(any(button.label == "Refresh WTI spot" for button in result.button))
            self.assertTrue(any("USO is a futures-based oil ETF proxy" in item.value
                                for item in result.caption if isinstance(item.value, str)))
            self.assertEqual(result.session_state["market_analysis_timeframe"], 20)
            self.assertEqual(result.session_state["deepseek_market_mode"], "Deep Research")
            self.assertIn("broad_index_window", result.session_state)
            self.assertIn("broad_index_mode", result.session_state)
            self.assertEqual(result.session_state["broad_index_visible"], ["SPY", "RSP", "QQQ", "DIA", "IWM"])
            self.assertNotIn("deepseek_market_deep_output_tokens", result.session_state)
            result.selectbox(key="broad_index_window").set_value(60).run()
            self.assertFalse(result.exception)
            result.selectbox(key="broad_index_mode").set_value("Relative cumulative performance vs SPY").run()
            self.assertFalse(result.exception)
            generate.assert_not_called()
            market_chat_answer.assert_not_called()
            full_market_run.assert_not_called()
            fred_macro_request.assert_not_called()
            treasury_request.assert_not_called()
            yfinance_request.assert_not_called()
            result.radio(key="market_analysis_timeframe").set_value(60).run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state["market_analysis_timeframe"], 60)
            result.selectbox(key="rs_pair_preset").set_value("XLY/XLP").run()
            self.assertFalse(result.exception)
            result.selectbox(key="rotation_strength_window").set_value(60).run()
            self.assertFalse(result.exception)
            result.checkbox(key="rotation_trails").set_value(True).run()
            self.assertFalse(result.exception)
            result.button(key="copy_compact_catalyst_prompt").click().run()
            self.assertFalse(result.exception)
            next_event_date = (datetime.now(timezone.utc).date() + timedelta(days=3)).strftime("%m/%d")
            result.text_area(key="compact_event_import_text").set_value(f"AAPL {next_event_date} AMC").run()
            result.button(key="parse_compact_event_import").click().run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state["compact_event_import_parsed"]["events"][0]["ticker"], "AAPL")
            result.text_area(key="quick_earnings_input").set_value("HOOD 10/27 AMC, AAPL 10/28 BMO").run()
            result.button(key="quick_earnings_parse").click().run()
            self.assertFalse(result.exception)
            self.assertEqual(
                [row["ticker"] for row in result.session_state["quick_earnings_parsed"]["events"]],
                ["HOOD", "AAPL"],
            )
            result.button(key="quick_earnings_save_1").click().run()
            self.assertFalse(result.exception)
            save_earnings.assert_called_once()
            saved_rows = save_earnings.call_args.args[0]
            self.assertEqual([row["ticker"] for row in saved_rows], ["HOOD", "AAPL"])
            self.assertEqual([row["session"] for row in saved_rows], ["AMC", "BMO"])
            refresh.assert_not_called()
            refresh_vix.assert_not_called()
            cboe_request.assert_not_called()
            fred_request.assert_not_called()
            event_request.assert_not_called()
            fetch_daily.assert_not_called()
            # Market uses one connected DeepSeek configuration, no topic/model
            # selector, and persisted spending defaults.
            for key in ("deepseek_market_analysis_mode", "deepseek_market_mode",
                        "market_ai_request_cap", "market_ai_daily_cap"):
                self.assertIn(key, result.session_state)
            self.assertEqual(result.session_state["market_ai_request_cap"], 0.5)
            self.assertEqual(result.session_state["market_ai_daily_cap"], 2.0)
            self.assertNotIn("ai_provider", result.session_state)
            self.assertNotIn("deepseek_market_topic", result.session_state)
            self.assertNotIn("market_ai_highlight_section", result.session_state)
            self.assertNotIn("deepseek_market_deep_output_tokens", result.session_state)
            result.selectbox(key="scenario_vix").set_value("Eases").run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state["scenario_vix"], "Eases")
            full_market_run.assert_not_called()
            result.button(key="regime_scenario_reset").click().run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state["scenario_vix"], "Unchanged · use current reading")
            result.radio(key="deepseek_market_mode").set_value("Deep Research").run()
            self.assertFalse(result.exception)
            self.assertTrue(any("Deep Research synthesis model: deepseek-v4-pro" in str(item.value)
                                for item in result.caption))
            result.button(key="deepseek_market_research").click().run()
            self.assertFalse(result.exception)
            self.assertEqual(result.session_state["market_full_analysis_run"]["error_category"], "dns_failure")
            self.assertIn("dns_failure", result.session_state["market_full_analysis_run"]["error"])
            full_market_run.assert_called_once()
            self.assertEqual(full_market_run.call_args.kwargs["model"], "deepseek-v4-pro")
            for label in TAB_LABELS[1:]:
                result.session_state[TAB_STATE_KEY] = label
                result.run()
                self.assertFalse(result.exception, f"Streamlit tab failed to render: {label}")
            for key in ("ai_provider", "ai_model", "ai_max_output_tokens", "ai_include_portfolio"):
                self.assertIn(key, result.session_state)
            generate.assert_not_called()
            chat.assert_not_called()
            full_market_run.assert_called_once()
            market_chat_answer.assert_not_called()
            deepseek_models.assert_not_called()
            deepseek_search.assert_not_called()
            deepseek_client.assert_not_called()

    def test_research_workspace_rerun_does_not_call_ai(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        with patch("dashboard.pages.home.generate_market_briefing") as briefing, \
             patch("dashboard.pages.research.answer_research_question") as chat, \
             patch("dashboard.pages.research.list_research_workspaces", return_value=["TEST"]), \
             patch("dashboard.pages.research.get_strategy_record", return_value=None), \
             patch("dashboard.pages.research.list_holdings", return_value=[]), \
             patch("dashboard.pages.research.list_cached_quotes", return_value={}), \
             patch("dashboard.pages.research.list_research_chat_messages", return_value=[
                 {"role": "user", "content": "Review my thesis."},
                 {"role": "assistant", "content": "Let's test the assumptions."},
             ]), \
             patch("dashboard.pages.research.get_buy_zone_alert", return_value=None), \
             patch("dashboard.pages.research.list_strategy_revisions", return_value=[]):
            result = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
            result.session_state[TAB_STATE_KEY] = "Research & Watchlist"
            result.session_state["research_workspace_symbol"] = "TEST"
            result.run()
            self.assertFalse(result.exception)
            briefing.assert_not_called()
            chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()

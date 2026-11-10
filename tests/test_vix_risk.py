import unittest
from datetime import date

from dashboard.vix_risk import (build_vix_risk_monitor, latest_completed_weekday,
    vix_candlestick_data, vix_five_session_summary, vix_selected_window_expansion)


def rows(closes, *, ohlc=None, start="2026-10-01"):
    from datetime import timedelta
    day = date.fromisoformat(start)
    result = []
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        row = {"observation_date": day.isoformat(), "close": close, "provider": "cboe_official_csv"}
        if ohlc and index == len(closes)-1:
            row.update(ohlc)
        result.append(row)
        day += timedelta(days=1)
    return result


class VixRiskMonitorTests(unittest.TestCase):
    def test_completed_session_rule_handles_weekend_and_before_close(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        self.assertEqual(latest_completed_weekday(datetime(2026, 11, 1, 12, tzinfo=et)), "2026-10-30")
        self.assertEqual(latest_completed_weekday(datetime(2026, 11, 2, 15, tzinfo=et)), "2026-10-30")
        self.assertEqual(latest_completed_weekday(datetime(2026, 11, 2, 17, tzinfo=et)), "2026-11-02")

    def test_ohlc_changes_and_reversal_classification(self):
        history = rows([16, 18, 24, 35, 20], ohlc={"open": 25, "high": 35, "low": 18, "close": 20})
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date(2026, 10, 8))
        self.assertEqual(result["close"], 20)
        self.assertEqual(result["prior_close"], 35)
        self.assertEqual(result["high_to_close_points"], 15)
        self.assertLess(result["closing_location_percent"], 55)
        self.assertIn("Intraday spike partially reversed", result["classifications"])
        self.assertEqual(result["change_3_sessions"]["points"], 2)

    def test_daily_three_and_five_session_cumulative_change_references(self):
        history = rows([10, 11, 12, 13, 14, 15, 16])
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertAlmostEqual(result["daily_change_percent"], (16/15-1)*100)
        self.assertEqual(result["prior_close_date"], history[-2]["observation_date"])
        self.assertAlmostEqual(result["change_3_sessions"]["percent"], (16/13-1)*100)
        self.assertEqual(result["change_3_sessions"]["reference_date"], history[-4]["observation_date"])
        self.assertEqual(result["change_3_sessions"]["reference_close"], 13)
        self.assertAlmostEqual(result["change_5_sessions"]["percent"], (16/11-1)*100)
        self.assertEqual(result["change_5_sessions"]["reference_date"], history[-6]["observation_date"])
        self.assertEqual(result["change_5_sessions"]["reference_close"], 11)

    def test_cumulative_changes_are_unavailable_without_n_plus_one_closes(self):
        history = rows([18, 19, 20, 21])
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertIsNotNone(result["change_3_sessions"])
        self.assertIsNone(result["change_5_sessions"])

    def test_user_reported_1942_example_uses_distinct_reference_closes(self):
        history = rows([18.0, 19.45, 17.8, 17.52, 18.0, 17.20, 19.42])
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertAlmostEqual(result["daily_change_percent"], 12.9069767, places=5)
        self.assertEqual(result["prior_close"], 17.20)
        self.assertAlmostEqual(result["change_3_sessions"]["percent"], 10.8447489, places=5)
        self.assertEqual(result["change_3_sessions"]["reference_close"], 17.52)
        self.assertAlmostEqual(result["change_5_sessions"]["percent"], -0.1542416, places=5)
        self.assertEqual(result["change_5_sessions"]["reference_close"], 19.45)

    def test_closing_near_high_is_not_classified_as_reversal(self):
        history = rows([16, 17, 19, 22, 24], ohlc={"open": 21, "high": 25, "low": 20, "close": 24})
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date(2026, 10, 8))
        self.assertNotIn("Intraday spike partially reversed", result["classifications"])
        self.assertIn("Volatility accelerating", result["classifications"])

    def test_78th_percentile_moderate_level_with_spike_reversal_and_daily_rise(self):
        values = [15.0] * 196 + [21.0] * 55 + [17.20, 19.42]
        history = rows(values, start="2025-10-01")
        history[-1].update(open=20.0, high=23.93, low=18.03, close=19.42)
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        # Current rank depends on supplied history; intraday and day changes are direct measurements.
        self.assertAlmostEqual(result["daily_change_points"], 2.22)
        self.assertAlmostEqual(result["high_to_close_points"], 4.51)
        self.assertAlmostEqual(result["closing_location_percent"], (19.42-18.03)/(23.93-18.03)*100)
        self.assertIn("Intraday spike partially reversed", result["classifications"])
        self.assertNotIn("Volatility extreme", result["classifications"])

    def test_intraday_vix_percentages_use_full_precision(self):
        history = rows([17.20, 19.42], ohlc={"open":18.43, "high":23.93, "low":18.03, "close":19.42})
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertAlmostEqual(result["open_to_high_percent"], (23.93/18.43-1)*100, places=10)
        self.assertAlmostEqual(result["high_to_close_percent"], (19.42/23.93-1)*100, places=10)
        self.assertAlmostEqual(result["open_to_close_percent"], (19.42/18.43-1)*100, places=10)
        self.assertAlmostEqual(result["high_to_close_range_percent"], (23.93-19.42)/(23.93-18.03)*100, places=10)
        self.assertAlmostEqual(result["open_to_high_percent"], 29.84265, places=4)
        self.assertAlmostEqual(result["high_to_close_percent"], -18.84664, places=4)
        self.assertAlmostEqual(result["open_to_close_percent"], 5.37167, places=4)
        self.assertAlmostEqual(result["high_to_close_range_percent"], 76.44068, places=4)

    def test_peak_surge_unwound_retained_and_net_change_use_previous_close(self):
        history = rows([17.20, 19.42], ohlc={"open":18.43, "high":23.93, "low":18.03, "close":19.42})
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertAlmostEqual(result["peak_surge_percent"], 39.1279069767, places=6)
        self.assertAlmostEqual(result["peak_unwound_percent"], 67.013373, places=5)
        self.assertAlmostEqual(result["peak_retained_percent"], 32.986627, places=5)
        self.assertAlmostEqual(result["daily_change_percent"], 12.9069767, places=5)
        self.assertEqual(result["spike_status"], "partially_reversed")

    def test_spike_can_be_fully_reversed_below_previous_close_without_clamping(self):
        history = rows([17.20, 16.0], ohlc={"open":18.0, "high":20.0, "low":15.0, "close":16.0})
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertEqual(result["spike_status"], "fully_reversed_below_prior")
        self.assertGreater(result["peak_unwound_percent"], 100)
        self.assertLess(result["peak_retained_percent"], 0)

    def test_no_upward_spike_has_no_peak_percentages(self):
        history = rows([17.20, 17.0], ohlc={"open":17.1, "high":17.2, "low":16.5, "close":17.0})
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertEqual(result["spike_status"], "no_upward_spike")
        self.assertIsNone(result["peak_surge_percent"])
        self.assertIsNone(result["peak_unwound_percent"])
        self.assertIsNone(result["peak_retained_percent"])

    def test_five_session_range_requires_five_complete_ohlc_rows(self):
        history = rows([17,18,19,20,21,22], start="2026-10-01")
        for row in history[-5:]:
            close = float(row["close"])
            row.update(open=close-.5, high=close+1, low=close-1, close=close)
        summary = vix_five_session_summary(history)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["observations"], 5)
        self.assertEqual(summary["lowest_vix"], 17.0)
        self.assertEqual(summary["highest_vix"], 23.0)
        self.assertAlmostEqual(summary["range_expansion_percent"], (23/17-1)*100)
        history[-3].pop("high")
        self.assertEqual(vix_five_session_summary(history)["status"], "incomplete_ohlc")

    def test_five_session_peak_unwind_uses_pre_window_close_and_preserves_over_100_percent(self):
        history = rows([17.20, 18, 19, 20, 21, 16], start="2026-10-01")
        for row in history[-5:]:
            close = float(row["close"])
            row.update(open=close, high=close + 1, low=close - .5, close=close)
        history[-3]["high"] = 23.93
        summary = vix_five_session_summary(history)
        metric = summary["peak_surge_unwound"]
        self.assertEqual(metric["reference_close"], 17.20)
        self.assertEqual(metric["reference_date"], history[0]["observation_date"])
        self.assertEqual(metric["peak_high"], 23.93)
        self.assertEqual(metric["latest_close"], 16)
        self.assertEqual(metric["status"], "fully_reversed_below_reference")
        self.assertGreater(metric["peak_surge_unwound_percent"], 100)
        self.assertLess(metric["peak_surge_retained_percent"], 0)

    def test_five_session_peak_unwind_reports_no_upward_spike(self):
        history = rows([20, 19, 18, 19, 18, 17], start="2026-10-01")
        for row in history[-5:]:
            close = float(row["close"])
            row.update(open=close, high=close + .2, low=close - .2, close=close)
        summary = vix_five_session_summary(history)
        self.assertEqual(summary["peak_surge_unwound"]["status"], "no_upward_spike")

    def test_five_session_window_not_claimed_with_only_four_rows(self):
        summary = vix_five_session_summary(rows([17,18,19,20]))
        self.assertEqual(summary["status"], "incomplete")
        self.assertNotIn("range_expansion_percent", summary)

    def test_selected_window_expansion_uses_exact_5_10_15_and_20_session_windows(self):
        history = rows([20 + i * .1 for i in range(25)], start="2026-08-03")
        for index, row in enumerate(history):
            close = float(row["close"])
            row.update(open=close, high=close + (index + 1) * .1, low=close - .5, close=close)
        for sessions in (5, 10, 15, 20):
            result = vix_selected_window_expansion(history, sessions=sessions)
            chart = vix_candlestick_data(history, sessions=sessions)
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["observations"], sessions)
            self.assertEqual(result["start_date"], history[-sessions]["observation_date"])
            self.assertEqual(result["end_date"], history[-1]["observation_date"])
            self.assertEqual(result["start_date"], chart["dates"][0])
            self.assertEqual(result["end_date"], chart["dates"][-1])
            selected = history[-sessions:]
            expected_high = max(float(row["high"]) for row in selected)
            expected_low = min(float(row["low"]) for row in selected)
            self.assertAlmostEqual(result["expansion_percent"], (expected_high / expected_low - 1) * 100)

    def test_selected_window_expansion_rejects_insufficient_or_incomplete_ohlc(self):
        history = rows([20, 21, 22, 23, 24], start="2026-08-03")
        for row in history:
            close = float(row["close"])
            row.update(open=close, high=close + 1, low=close - 1)
        self.assertEqual(vix_selected_window_expansion(history, sessions=10)["status"], "insufficient_history")
        full = rows([20 + i for i in range(10)], start="2026-08-03")
        for row in full:
            close = float(row["close"])
            row.update(open=close, high=close + 1, low=close - 1)
        full[-2].pop("high")
        result = vix_selected_window_expansion(full, sessions=10)
        self.assertEqual(result["status"], "incomplete_ohlc")
        self.assertNotIn("expansion_percent", result)

    def test_intraday_percentages_handle_zero_denominators_and_zero_range(self):
        history = rows([19.42], ohlc={"open":19.42,"high":19.42,"low":19.42,"close":19.42})
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertEqual(result["open_to_high_percent"], 0)
        self.assertEqual(result["high_to_close_percent"], 0)
        self.assertEqual(result["open_to_close_percent"], 0)
        self.assertIsNone(result["high_to_close_range_percent"])

    def test_stale_session_does_not_show_as_current_and_ohlc_is_not_fabricated(self):
        history = rows([16, 18], start="2026-10-01")
        result = build_vix_risk_monitor(history, completed_session="2026-10-05", today=date(2026, 10, 6))
        self.assertFalse(result["current_available"])
        self.assertIsNone(result["close"])
        self.assertIsNone(result["open"])
        self.assertFalse(result["ohlc_available"])

    def test_full_rolling_percentile_minimums(self):
        history = rows([float(10+i%20) for i in range(1260)])
        result = build_vix_risk_monitor(history, completed_session=history[-1]["observation_date"], today=date.fromisoformat(history[-1]["observation_date"]))
        self.assertIsNotNone(result["percentile_1y"])
        self.assertIsNotNone(result["percentile_5y"])

    def test_candlestick_data_contains_only_real_complete_ohlc(self):
        history = rows([16, 17, 18, 19, 20, 21, 22], start="2026-10-01")
        history[-1].update(open=21, high=23, low=20, close=22)
        history[-2].update(open=20, high=22, low=19, close=21)
        chart = vix_candlestick_data(history, sessions=5, show_average=True)
        self.assertEqual(len(chart["candles"]), 2)
        self.assertEqual(len(chart["missing_ohlc_dates"]), 3)
        self.assertEqual(chart["candles"][-1]["high"], 23)
        self.assertEqual(chart["latest_close"], 22)
        self.assertEqual(chart["previous_close"], 21)
        self.assertEqual(chart["five_session_average"][-1]["value"], 20)

    def test_candlestick_rejects_invalid_window_and_never_infers_ohlc(self):
        history = rows([16, 17, 18, 19, 20, 21])
        chart = vix_candlestick_data(history, sessions=5, show_average=False)
        self.assertEqual(chart["candles"], [])
        self.assertEqual(len(chart["missing_ohlc_dates"]), 5)
        with self.assertRaises(ValueError):
            vix_candlestick_data(history, sessions=7)


if __name__ == "__main__":
    unittest.main()

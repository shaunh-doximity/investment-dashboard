import unittest
from datetime import date, timedelta
from statistics import NormalDist

from dashboard.vix_statistics import (
    equity_confirmation,
    vix_return_variability,
    vix_reversal_frequency,
    vix_z_score,
    summarize_vix_statistical_signals,
)


def sessions(n, *, start=date(2025, 1, 2)):
    day, output = start, []
    while len(output) < n:
        if day.weekday() < 5:
            output.append(day.isoformat())
        day += timedelta(days=1)
    return output


def vix_rows(closes, days=None):
    days = days or sessions(len(closes))
    return [{"observation_date": day, "open": close, "high": close * 1.01,
             "low": close * .99, "close": close} for day, close in zip(days, closes)]


class VixStatisticsTests(unittest.TestCase):
    def test_normal_equivalent_percentile_for_example_z_score_is_secondary_math(self):
        self.assertAlmostEqual(NormalDist().cdf(2.47) * 100, 99.32, delta=.03)

    def test_zscore_uses_trailing_window_and_guards_zero_variance(self):
        data = vix_rows([20 + (i % 5) for i in range(59)] + [30])
        result = vix_z_score(data, window=60)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["observations"], 60)
        expected_mean = sum(row["close"] for row in data) / 60
        self.assertAlmostEqual(result["mean"], expected_mean)
        latest = data[-1]["close"]
        values = [row["close"] for row in data]
        midrank = (sum(value < latest for value in values) + .5 * sum(value == latest for value in values)) / 60 * 100
        self.assertAlmostEqual(result["empirical_percentile"], midrank)
        self.assertAlmostEqual(result["normal_equivalent_percentile"], NormalDist().cdf(result["value"]) * 100)
        self.assertIn("inclusive trailing sample", result["ranking_method"])
        constant = vix_z_score(vix_rows([20.0] * 60), window=60)
        self.assertEqual(constant["status"], "zero_standard_deviation")
        self.assertEqual(constant["empirical_percentile"], 50.0)
        self.assertIsNone(constant["normal_equivalent_percentile"])
        near_constant = vix_z_score(vix_rows([20.0 + (1e-14 if i % 2 else 0.0) for i in range(60)]), window=60)
        self.assertEqual(near_constant["status"], "zero_standard_deviation")

    def test_zscore_is_point_in_time_and_requires_full_history(self):
        rows = vix_rows([15 + (i % 7) for i in range(75)])
        cutoff = rows[:65]
        before = vix_z_score(cutoff, window=60)
        with_future = vix_z_score(rows, window=60)
        self.assertNotEqual(before["as_of"], with_future["as_of"])
        trailing = [row["close"] for row in cutoff[-60:]]
        mean = sum(trailing) / len(trailing)
        variance = sum((value - mean) ** 2 for value in trailing) / len(trailing)
        self.assertAlmostEqual(before["value"], (trailing[-1] - mean) / variance ** .5)
        self.assertEqual(before["as_of"], cutoff[-1]["observation_date"])
        self.assertEqual(before["observations"], 60)
        self.assertEqual(vix_z_score(rows[:20], window=60)["status"], "insufficient_history")

    def test_variability_is_daily_log_return_std_and_compares_to_prior_windows(self):
        closes = [18 + (i % 3) * .15 for i in range(300)]
        closes[-1] *= 1.20
        result = vix_return_variability(vix_rows(closes), return_window=20, history_window=252)
        self.assertEqual(result["status"], "available")
        self.assertAlmostEqual(result["daily_log_return_std_percent"], result["daily_log_return_std"] * 100)
        self.assertLessEqual(result["prior_windows"], 252)
        self.assertIsNotNone(result["historical_percentile"])

    def test_variability_rank_is_unavailable_without_minimum_comparison_history(self):
        result = vix_return_variability(vix_rows([18 + i * .05 for i in range(35)]),
                                        return_window=20, history_window=252)
        self.assertEqual(result["status"], "available")
        self.assertIsNone(result["historical_percentile"])
        self.assertEqual(result["percentile_status"], "insufficient_comparison_history")

    def test_reversal_frequency_separates_qualifying_spikes_from_wide_ranges(self):
        rows = vix_rows([20.0] * 21)
        # A peak 12% over prior close with half of that surge unwound.
        rows[-1].update(high=22.4, low=19.9, close=21.2)
        # A wide range without a qualifying 10% peak surge is not a reversal.
        rows[-2].update(high=21.2, low=18.8, close=20.1)
        result = vix_reversal_frequency(rows, sessions=20)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["eligible_spike_sessions"], 1)
        self.assertEqual(result["significant_reversals"], 1)
        self.assertEqual(result["large_intraday_ranges"], 2)
        self.assertEqual(result["reversal_fraction"], 1.0)

    def test_reversal_window_does_not_fill_missing_ohlc_or_dates(self):
        rows = vix_rows([20.0] * 21)
        rows[-3].pop("high")
        self.assertEqual(vix_reversal_frequency(rows, sessions=20)["status"], "incomplete_ohlc")
        rows = vix_rows([20.0] * 21)
        rows[-1]["observation_date"] = (date.fromisoformat(rows[-2]["observation_date"]) + timedelta(days=7)).isoformat()
        self.assertEqual(vix_reversal_frequency(rows, sessions=20)["status"], "incomplete_dates")
        self.assertEqual(vix_reversal_frequency(rows[:10], sessions=20)["status"], "insufficient_history")

    def test_equity_confirmation_is_cut_off_at_vix_observation_date(self):
        days = sessions(210)
        symbols = ("SPY", "XLK", "XLF")
        base = {symbol: [{"trading_date": day, "close": 100 + i * .1} for i, day in enumerate(days)]
                for symbol in symbols}
        cutoff = days[204]
        result = equity_confirmation(base, as_of=cutoff, sectors=symbols[1:])
        extended = {symbol: list(rows) + [{"trading_date": "2026-12-31", "close": 1000}]
                    for symbol, rows in base.items()}
        with_future = equity_confirmation(extended, as_of=cutoff, sectors=symbols[1:])
        self.assertEqual(result, with_future)
        self.assertEqual(result["as_of"], cutoff)
        self.assertTrue(result["as_of_match"])
        self.assertEqual(result["sector_sma50_coverage"], 2)

    def test_statistical_summary_uses_existing_metrics_and_calls_out_equity_confirmation(self):
        signals = {"status":"available", "as_of":"2026-10-30",
            "z_score":{"value":2.3,"window":60,"empirical_percentile":95.0,"normal_equivalent_percentile":98.9},
            "return_variability":{"historical_percentile":94,"unusually_turbulent":True},
            "reversal_frequency":{"status":"available","significant_reversals":2,
                "eligible_spike_sessions":3,"sessions":20},
            "equity_confirmation":{"status":"available","as_of":"2026-10-30",
                "spy_above_sma50":False,"spy_above_sma200":False,
                "sector_above_sma50":3,"sector_sma50_coverage":11}}
        monitor = {"percentile_1y":82,"change_5_sessions":{"percent":1.2}}
        summary = summarize_vix_statistical_signals(signals, monitor, {})
        self.assertIn("unusually elevated", summary["historical"])
        self.assertIn("empirical midrank", summary["historical"])
        self.assertIn("Gaussian distribution", summary["historical"])
        self.assertIn("erratic", summary["headline"])
        self.assertIn("broadly confirm caution", summary["confirmation"])
        self.assertNotIn("probability", summary["implication"].lower())

    def test_statistical_summary_handles_missing_inputs_without_forecast(self):
        result = summarize_vix_statistical_signals({"status":"unavailable"}, {}, None)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("No statistical interpretation", result["implication"])


if __name__ == "__main__":
    unittest.main()

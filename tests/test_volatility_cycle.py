import unittest
from datetime import date, timedelta

from dashboard.volatility_cycle import absolute_vix_regime, analyze_volatility_cycle, daily_vix_momentum


def rows_from_closes(closes, *, last_high=None, last_low=None):
    result = []
    day = date(2026, 1, 1)
    for i, close in enumerate(closes):
        result.append({"observation_date": day.isoformat(), "close": close,
                       "open": close - .1, "high": close + .2, "low": close - .3})
        day += timedelta(days=1)
    if last_high is not None:
        result[-1].update(high=last_high, low=last_low)
    return result


class VolatilityCycleTests(unittest.TestCase):
    def test_absolute_regime_boundaries_and_custom_thresholds(self):
        self.assertEqual(absolute_vix_regime(14.99), "Subdued")
        self.assertEqual(absolute_vix_regime(15), "Normal")
        self.assertEqual(absolute_vix_regime(20), "Elevated")
        self.assertEqual(absolute_vix_regime(30), "Extreme")
        self.assertEqual(absolute_vix_regime(12, subdued_max=10, normal_max=18, elevated_max=25), "Normal")
        with self.assertRaises(ValueError):
            absolute_vix_regime(20, subdued_max=20, normal_max=15, elevated_max=30)

    def test_daily_stress_threshold_is_independent(self):
        self.assertEqual(daily_vix_momentum(6), "Stress increasing")
        self.assertEqual(daily_vix_momentum(-6), "Stress easing")
        self.assertEqual(daily_vix_momentum(2), "Neutral daily move")

    def test_calm_stress_panic_and_normalizing_states(self):
        self.assertEqual(analyze_volatility_cycle(rows_from_closes([12] * 21), window=20)["state"], "Calm")
        rising = [14.5] * 10 + [15, 15.2, 15.4, 15.6, 15.8, 16, 16.3, 16.7, 17.3, 18.3]
        self.assertEqual(analyze_volatility_cycle(rows_from_closes(rising), window=20)["state"], "Stress building")
        panic = [24] * 15 + [25, 26, 27, 28, 29, 31]
        self.assertEqual(analyze_volatility_cycle(rows_from_closes(panic), window=20)["state"], "Panic/disorder")
        normalizing = [25, 24, 23, 22, 21, 20, 19]
        self.assertEqual(analyze_volatility_cycle(rows_from_closes(normalizing), window=5)["state"], "Normalizing")

    def test_failed_high_and_weakening_momentum_is_turbulent_stabilization(self):
        values = [19] * 10 + [19.5, 20, 20.4, 20.4, 20.4, 20.4, 20.4, 21, 22, 22.8, 23.5, 22.8]
        rows = rows_from_closes(values, last_high=24, last_low=22.5)
        result = analyze_volatility_cycle(rows, window=20)
        self.assertEqual(result["state"], "Turbulent stabilization")
        self.assertGreaterEqual(result["failed_highs"], 1)
        self.assertGreaterEqual(result["daily_high_close_reversals"], 1)

    def test_mixed_signals_and_missing_data_remain_explicit(self):
        self.assertEqual(analyze_volatility_cycle([], window=10)["state"], "Insufficient data")
        result = analyze_volatility_cycle(rows_from_closes([18, 17, 19, 18, 19, 18]), window=5)
        self.assertEqual(result["state"], "Mixed signals")
        self.assertEqual(result["observations"], 5)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import date, timedelta
import unittest

from dashboard.market_snapshot import build_market_snapshot
from dashboard.risk_appetite import calculate_risk_appetite


SECTORS = ("XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC")


def _weekday_dates(count=80):
    days, cursor = [], date(2026, 7, 1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _histories(scenario="risk_on"):
    days = _weekday_dates()
    ends = {
        "SPY": -5.0 if scenario == "risk_off" else 10.0,
        "RSP": 12.0 if scenario != "risk_off" else -5.0,
        "IWM": 15.0 if scenario != "risk_off" else -8.0,
        "HYG": 5.0 if scenario != "risk_off" else -5.0,
        "LQD": 2.0,
        "IEF": 0.0 if scenario != "risk_off" else 3.0,
    }
    for symbol in SECTORS:
        if scenario == "risk_off":
            ends[symbol] = 12.0 if symbol in {"XLV", "XLP", "XLU"} else -24.0
        elif scenario == "defensive_mixed":
            ends[symbol] = (30.0 if symbol in {"XLV", "XLP", "XLU"} else
                            12.0 if symbol in {"XLY", "XLI", "XLE", "XLF", "XLK", "XLB"} else -6.0)
        else:
            ends[symbol] = 20.0
    output = {}
    for symbol, total_pct in ends.items():
        output[symbol] = [{"trading_date": day, "close": 100 * (1 + total_pct / 100 * i / (len(days) - 1))}
                          for i, day in enumerate(days)]
    return output, days


def _vix(days, mode="calm"):
    if mode == "stress":
        closes = [16.0 + 14.0 * i / (len(days) - 1) for i in range(len(days))]
    elif mode == "recovery":
        closes = [16.0] * len(days)
        for i in range(20, 42):
            closes[i] = 27.0
        for i in range(42, len(days)):
            closes[i] = 27 - 12 * (i - 42) / (len(days) - 43)
    else:
        closes = [15.0] * len(days)
    return [{"observation_date": day, "close": close, "high": close + .5,
             "low": close - .5, "open": close} for day, close in zip(days, closes)]


class RiskAppetiteTests(unittest.TestCase):
    def evaluate(self, scenario="risk_on", vix_mode="calm", **kwargs):
        histories, days = _histories(scenario)
        result = calculate_risk_appetite(histories, _vix(days, vix_mode), timeframe=20,
                                         expected_session=days[-1], **kwargs)
        return result, histories, days

    def test_risk_on_requires_broad_participation_credit_and_contained_vix(self):
        result, *_ = self.evaluate()
        self.assertEqual(result["classification"], "Risk-on")
        self.assertEqual(result["components"]["equity_participation"]["direction"], 1)
        self.assertEqual(result["components"]["credit_risk_appetite"]["direction"], 1)
        self.assertEqual(result["components"]["volatility_pressure"]["direction"], 0)

    def test_risk_off_requires_confirming_equity_credit_and_vix_deterioration(self):
        result, *_ = self.evaluate("risk_off", "stress")
        self.assertEqual(result["classification"], "Risk-off")
        self.assertEqual(result["components"]["equity_participation"]["direction"], -1)
        self.assertEqual(result["components"]["credit_risk_appetite"]["direction"], -1)
        self.assertEqual(result["components"]["volatility_pressure"]["direction"], -1)

    def test_defensive_strength_with_healthy_participation_is_mixed_not_risk_off(self):
        result, *_ = self.evaluate("defensive_mixed", "calm")
        self.assertEqual(result["components"]["defensive_rotation"]["direction"], -1)
        self.assertEqual(result["components"]["equity_participation"]["direction"], 1)
        self.assertEqual(result["classification"], "Rotation / Mixed")

    def test_stress_recovery_requires_prior_stress_and_current_confirmation(self):
        recovered, *_ = self.evaluate("risk_on", "recovery")
        no_prior, *_ = self.evaluate("risk_on", "calm")
        self.assertTrue(recovered["prior_stress_observed"])
        self.assertEqual(recovered["classification"], "Risk-on")
        self.assertTrue(recovered["transition_characteristics"]["recovery_from_stress_confirmed"])
        self.assertFalse(no_prior["prior_stress_observed"])
        self.assertFalse(no_prior["transition_characteristics"]["recovery_from_stress_confirmed"])
        self.assertIn(no_prior["classification"], {"Risk-on", "Rotation / Mixed", "Risk-off"})

    def test_insufficient_evidence_when_two_groups_are_missing(self):
        result, histories, days = self.evaluate()
        histories.pop("HYG")
        histories.pop("LQD")
        histories.pop("IEF")
        result = calculate_risk_appetite(histories, [], timeframe=20, expected_session=days[-1])
        self.assertEqual(result["classification"], "Insufficient Evidence")
        self.assertIn("credit_risk_appetite", result["missing_or_stale"])
        self.assertIn("volatility_pressure", result["missing_or_stale"])

    def test_missing_credit_is_unavailable_and_not_silently_substituted(self):
        result, histories, days = self.evaluate()
        for ticker in ("HYG", "LQD", "IEF"):
            histories.pop(ticker)
        result = calculate_risk_appetite(histories, _vix(days), timeframe=20,
                                         expected_session=days[-1])
        self.assertIsNone(result["components"]["credit_risk_appetite"]["direction"])
        self.assertEqual(result["components"]["credit_risk_appetite"]["status"], "unavailable")

    def test_stale_vix_is_not_counted_as_current_confirmation(self):
        result, *_ = self.evaluate(vix_current_available=False)
        self.assertIsNone(result["components"]["volatility_pressure"]["direction"])
        self.assertEqual(result["components"]["volatility_pressure"]["status"], "Stale observations")

    def test_technology_led_weakness_with_healthy_breadth_is_mixed(self):
        histories, days = _histories("risk_on")
        # Make tech lag while the remaining sectors and equal-weight index retain strength.
        histories["XLK"][-1]["close"] = histories["XLK"][0]["close"] * .97
        result = calculate_risk_appetite(histories, _vix(days), timeframe=20,
                                         expected_session=days[-1])
        self.assertEqual(result["components"]["equity_participation"]["direction"], 1)
        self.assertNotEqual(result["classification"], "Risk-off")

    def test_relative_sector_outperformance_in_selloff_does_not_make_equity_breadth_constructive(self):
        histories, days = _histories("risk_on")
        targets = {"SPY": -5.0, "RSP": -2.5, "IWM": -3.0}
        targets.update({symbol: (-2.0 if index < 9 else -6.0)
                        for index, symbol in enumerate(SECTORS)})
        for symbol, total in targets.items():
            rows = histories[symbol]
            base = float(rows[-21]["close"])
            for offset, row in enumerate(rows[-20:], start=1):
                row["close"] = base * (1 + total * offset / 20 / 100)
        result = calculate_risk_appetite(histories, _vix(days), timeframe=20,
                                         expected_session=days[-1])
        equity = result["components"]["equity_participation"]
        self.assertEqual(equity["direction"], 0)
        self.assertEqual(equity["status"], "Relative resilience during selloff")
        self.assertAlmostEqual(equity["value"]["spy_return_pct"], -5.0)
        self.assertAlmostEqual(equity["value"]["rsp_return_pct"], -2.5)
        self.assertEqual(equity["value"]["sectors_positive_absolute"], 0)
        self.assertEqual(equity["value"]["sectors_outperforming_spy"], 9)

    def test_mismatched_observation_dates_are_not_combined(self):
        histories, days = _histories()
        histories["IWM"] = histories["IWM"][:-1]
        result = calculate_risk_appetite(histories, _vix(days), timeframe=20,
                                         expected_session=days[-1])
        self.assertIsNone(result["components"]["equity_participation"]["direction"])
        self.assertEqual(result["components"]["equity_participation"]["status"], "Stale observations")

    def test_timeframe_selection_changes_measurement_window(self):
        histories, days = _histories()
        five = calculate_risk_appetite(histories, _vix(days), timeframe=5, expected_session=days[-1])
        twenty = calculate_risk_appetite(histories, _vix(days), timeframe=20, expected_session=days[-1])
        self.assertEqual(five["timeframe_sessions"], 5)
        self.assertEqual(twenty["timeframe_sessions"], 20)
        self.assertEqual(five["as_of"], twenty["as_of"])

    def test_snapshot_serializes_monitor_and_fingerprints_rule_changes(self):
        monitor, *_ = self.evaluate()
        first = build_market_snapshot({"sector_rotation": {"metrics": {}, "risk_appetite": monitor}})
        second = build_market_snapshot({"sector_rotation": {"metrics": {},
            "risk_appetite": {**monitor, "classification": "Rotation / Mixed"}}})
        self.assertEqual(first["sections"]["sector_rotation"]["risk_appetite"]["methodology_version"], "risk-appetite-v5")
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])


if __name__ == "__main__":
    unittest.main()

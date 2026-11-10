import tempfile
import types
import unittest
import requests
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from dashboard.ai_market_analyst import build_market_snapshot, compare_market_snapshots, assess_snapshot_completeness
from dashboard.cboe_volatility import (
    CboeDataError, parse_cboe_history, refresh_cboe_volatility, volatility_summary,
)
from dashboard.data_access import (
    get_latest_market_briefing, list_volatility_observations,
    save_market_briefing, upsert_volatility_observations,
)
from dashboard.database import initialize_database
from dashboard.market_regime import (
    build_regime_features, classify_regime, historical_analog_distribution,
    percentile_rank,
)
from dashboard.market_history import SECTOR_SYMBOLS
from dashboard.fred_volatility import (
    FredVolatilityError, PROVIDER as FRED_PROVIDER, load_fred_vix,
    parse_fred_observations, refresh_fred_vix,
)


def bars(closes, start=date(2024, 1, 2)):
    result, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        result.append({"trading_date": day.isoformat(), "close": float(close)})
        day += timedelta(days=1)
    return result


class CboeVolatilityTests(unittest.TestCase):
    def test_parse_official_csv_date_close_and_reject_malformed(self):
        rows = parse_cboe_history("DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2025,18,20,17,19.5\n01/03/2025,,,,\n", "VIX", "now")
        self.assertEqual(rows, [{"symbol": "VIX", "observation_date": "2025-01-02", "close": 19.5,
                                 "provider": "cboe_official_csv", "retrieved_at": "now",
                                 "open": 18.0, "high": 20.0, "low": 17.0}])
        with self.assertRaises(CboeDataError):
            parse_cboe_history("DATE,OPEN\n01/02/2025,18", "VIX")

    def test_volatility_metrics_n_plus_one_percentile_and_common_date_ratios(self):
        vix = [{"observation_date": f"2025-01-{i:02d}", "close": float(i), "retrieved_at": "now"} for i in range(1, 27)]
        vix9d = [{"observation_date": "2025-01-25", "close": 30.0}, {"observation_date": "2025-01-26", "close": 52.0}]
        summary = volatility_summary({"VIX": vix, "VIX9D": vix9d})
        self.assertEqual(summary["close"], 26.0)
        self.assertEqual(summary["change_5d_points"], 5.0)
        self.assertEqual(summary["change_20d_points"], 20.0)
        self.assertAlmostEqual(summary["sma20"], 16.5)
        self.assertAlmostEqual(summary["percentile_252"], 100.0)
        self.assertEqual(summary["vix9d_ratio"], 2.0)
        self.assertIsNone(summary["vix3m_ratio"])

    def test_upsert_preserves_unique_observation_and_provider_errors_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vol.db"
            initialize_database(path)
            row = {"symbol": "VIX", "observation_date": "2025-01-02", "close": 18.0,
                   "provider": "cboe_official_csv", "retrieved_at": "first"}
            upsert_volatility_observations([row], path)
            upsert_volatility_observations([{**row, "close": 19.0, "retrieved_at": "second"}], path)
            stored = list_volatility_observations(database_path=path)["VIX"]
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["close"], 19.0)
            with patch("dashboard.cboe_volatility.requests.get", side_effect=requests.Timeout):
                result = refresh_cboe_volatility(database_path=path)
            self.assertIn("VIX", result["errors"])
            self.assertEqual(list_volatility_observations(database_path=path)["VIX"][0]["close"], 19.0)


class FredVolatilityTests(unittest.TestCase):
    def test_parse_fred_skips_non_trading_missing_observations(self):
        parsed = parse_fred_observations({"observations": [
            {"date": "2025-01-02", "value": "18.25"},
            {"date": "2025-01-03", "value": "."},
        ]}, "test-time")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["provider"], FRED_PROVIDER)
        self.assertEqual(parsed[0]["close"], 18.25)
        with self.assertRaises(FredVolatilityError):
            parse_fred_observations({"observations": [{"date": "2025-01-03", "value": "."}]})

    def test_incremental_request_and_persistence_are_manual_and_provider_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fred.db"
            initialize_database(path)
            upsert_volatility_observations([{"symbol": "VIX", "observation_date": "2025-01-10", "close": 18,
                                             "provider": FRED_PROVIDER, "retrieved_at": "old"}], path)
            response = types.SimpleNamespace(status_code=200, json=lambda: {"observations": [
                {"date": "2025-01-06", "value": "17"}, {"date": "2025-01-10", "value": "19"},
            ]}, raise_for_status=lambda: None)
            with patch.dict("os.environ", {"FRED_API_KEY": "secret-test-key"}), \
                 patch("dashboard.fred_volatility.requests.get", return_value=response) as request:
                result = refresh_fred_vix(database_path=path, today=date(2025, 1, 12))
            params = request.call_args.kwargs["params"]
            self.assertEqual(params["series_id"], "VIXCLS")
            self.assertEqual(params["observation_start"], "2025-01-03")
            self.assertEqual(params["api_key"], "secret-test-key")
            self.assertEqual(result["updated"], 2)
            saved = load_fred_vix(path)
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[-1]["close"], 19)
            self.assertEqual(list_volatility_observations(("VIX",), "cboe_official_csv", path)["VIX"], [])

    def test_provider_failure_keeps_cached_values_and_does_not_echo_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fred.db"
            initialize_database(path)
            upsert_volatility_observations([{"symbol": "VIX", "observation_date": "2025-01-10", "close": 18,
                                             "provider": FRED_PROVIDER, "retrieved_at": "old"}], path)
            with patch.dict("os.environ", {"FRED_API_KEY": "secret-test-key"}), \
                 patch("dashboard.fred_volatility.requests.get", side_effect=requests.Timeout):
                result = refresh_fred_vix(database_path=path)
            self.assertIn("timed out", result["errors"]["FRED"])
            self.assertNotIn("secret-test-key", str(result))
            self.assertEqual(load_fred_vix(path)[0]["close"], 18)

    def test_vix_stale_and_missing_are_not_current_or_imputed(self):
        from dashboard.cboe_volatility import volatility_summary
        self.assertEqual(volatility_summary({"VIX": []})["status"], "unavailable")
        old = [{"observation_date": "2020-01-02", "close": 15, "provider": FRED_PROVIDER}]
        self.assertEqual(volatility_summary({"VIX": old})["status"], "stale")


class RegimeFeatureTests(unittest.TestCase):
    def test_percentile_and_rule_regime_are_deterministic_and_missing_is_honest(self):
        self.assertEqual(percentile_rank(3.0, [1.0, 2.0, 3.0], minimum=3), 100.0)
        self.assertEqual(classify_regime({"spy": {"above_sma50": None}, "volatility": {"close": 15}}), "Insufficient data")
        self.assertEqual(classify_regime({"spy": {"above_sma50": False, "above_sma200": False, "drawdown_252": -.05}, "volatility": {"close": 18}}), "Risk-off conditions")
        self.assertEqual(classify_regime({"spy": {"above_sma50": True, "above_sma200": True, "drawdown_252": -.03}, "volatility": {"close": 16}}), "Risk-on conditions")
        self.assertEqual(classify_regime({"spy": {"above_sma50": True, "above_sma200": True, "drawdown_252": -.03, "status": "stale"}, "volatility": {"close": 16, "status": "available"}}), "Stale input data")

    def test_regime_features_use_cached_adjusted_closes_and_report_gaps(self):
        histories = {"SPY": bars([100 + i * .2 for i in range(300)]),
                     "QQQ": bars([100 + i * .3 for i in range(300)]),
                     "RSP": bars([100 + i * .15 for i in range(300)])}
        for index, symbol in enumerate(SECTOR_SYMBOLS):
            histories[symbol] = bars([100 + i * (0.15 + index * .005) for i in range(300)])
        result = build_regime_features(histories, {"close": 17.0, "percentile_252": 45.0, "status": "available"},
                                       today=date.fromisoformat(histories["SPY"][-1]["trading_date"]))
        self.assertEqual(result["spy"]["above_sma50"], True)
        self.assertEqual(result["spy"]["above_sma200"], True)
        self.assertIn(result["regime_classification"], ("Risk-on conditions", "Mixed conditions"))
        self.assertEqual(result["broad_etf_20d_proxy"]["available"], 3)
        self.assertEqual(result["sector_dispersion_20d"]["available"], 11)
        self.assertIn("not constituent", result["methodology"]["breadth"])
        missing = build_regime_features({"SPY": bars([100, 101])}, {"status": "unavailable"}, today=date(2024, 1, 4))
        self.assertIsNone(missing["spy"]["above_sma50"])
        self.assertEqual(missing["regime_classification"], "Insufficient data")

    def test_historical_analog_reports_sample_size_and_avoids_unobserved_forward_windows(self):
        vix = [{"observation_date": row["trading_date"], "close": 15 + (i % 10)} for i, row in enumerate(bars([1] * 600))]
        spy = bars([100 + i * .1 for i in range(600)])
        result = historical_analog_distribution(vix, spy, horizon=20, tolerance_percentile_points=100)
        self.assertEqual(result["status"], "available")
        self.assertGreater(result["sample_size"], 0)
        self.assertLessEqual(result["sample_size"], 600 - 20)
        self.assertIn("association", result["caveat"])


class AssessmentPersistenceTests(unittest.TestCase):
    def test_snapshot_and_assessment_history_keep_regime_and_as_of(self):
        histories = {"SPY": bars([100 + i * .2 for i in range(300)]), "QQQ": bars([100 + i * .3 for i in range(300)]), "RSP": bars([100 + i * .1 for i in range(300)])}
        snapshot = build_market_snapshot(histories, volatility_history={"VIX": []})
        self.assertIn("regime_features", snapshot)
        self.assertEqual(snapshot["market_data_as_of"], histories["SPY"][-1]["trading_date"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reports.db"
            initialize_database(path)
            save_market_briefing({"generated_at": "2025-01-01T00:00:00+00:00", "provider": "deepseek", "model": "test",
                                  "input_snapshot": snapshot, "indicator_snapshot": snapshot["regime_features"],
                                  "market_data_as_of": snapshot["market_data_as_of"],
                                  "market_data_as_of_timestamp": "2025-01-01T22:00:00+00:00",
                                  "regime_classification": snapshot["regime_features"]["regime_classification"],
                                  "report_text": "Executive summary", "source_metadata": []}, path)
            saved = get_latest_market_briefing(path)
            self.assertEqual(saved["market_data_as_of"], snapshot["market_data_as_of"])
            self.assertEqual(saved["market_data_as_of_timestamp"], "2025-01-01T22:00:00+00:00")
            self.assertEqual(saved["indicator_snapshot"]["regime_classification"], snapshot["regime_features"]["regime_classification"])

    def test_comparison_uses_actual_prior_snapshot_and_reports_no_prior(self):
        current = {"relative_performance": {"QQQ": {"20": {"excess_return_pp": 3}}},
                   "sector_rotation": {"symbols": {"XLK": {"quadrant": "Leading"}}},
                   "risk_volatility": {"close": 22}}
        self.assertEqual(compare_market_snapshots(current, None)["status"], "no_previous_snapshot")
        previous = {"generated_at": "yesterday", "input_snapshot": {
            "relative_performance": {"QQQ": {"20": {"excess_return_pp": 1}}},
            "sector_rotation": {"symbols": {"XLK": {"quadrant": "Improving"}}},
            "risk_volatility": {"close": 20}, "market_data_as_of": "2025-01-01"}}
        compared = compare_market_snapshots(current, previous)
        self.assertEqual(compared["relative_performance_changes"][0]["direction"], "strengthening")
        self.assertEqual(compared["sector_quadrant_transitions"][0]["to"], "Leading")
        self.assertEqual(compared["vix_level_change_points"], 2)

    def test_completeness_reports_missing_vix(self):
        self.assertIn("VIX (FRED VIXCLS)", assess_snapshot_completeness({"as_of_dates": {}, "freshness_by_symbol": {}, "risk_volatility": {"status": "unavailable"}})["missing_symbols"])


if __name__ == "__main__":
    unittest.main()

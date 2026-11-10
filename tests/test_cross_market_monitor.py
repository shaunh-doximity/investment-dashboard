import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard.cross_market import (
    classify_breadth_condition, daily_returns_by_session, lead_lag_correlations, macro_changes_by_session,
    lead_lag_sample_diagnostics, normalized_performance, observation_freshness, relative_ratio_return, rolling_correlations,
    sector_participation_metrics,
)
from dashboard.data_access import (
    list_macro_series_observations, macro_series_coverage, upsert_macro_series_observations,
)
from dashboard.database import initialize_database
from dashboard.fred_macro import (
    PROVIDER, FredMacroError, _fred_credentials, diagnose_fred_connection,
    parse_fred_macro_observations, refresh_fred_macro,
)
from dashboard.market_history import SECTOR_SYMBOLS


def make_bars(symbol, dates, values):
    return [{"symbol": symbol, "trading_date": day.isoformat(), "close": value}
            for day, value in zip(dates, values)]


class CrossMarketCalculationTests(unittest.TestCase):
    def setUp(self):
        self.dates = [date(2024, 1, 2) + timedelta(days=i) for i in range(70)]
        self.dates = [day for day in self.dates if day.weekday() < 5]
        spy = [100 + i for i in range(len(self.dates))]
        rsp = [100 + i * 1.1 for i in range(len(self.dates))]
        self.histories = {"SPY": make_bars("SPY", self.dates, spy),
                          "RSP": make_bars("RSP", self.dates, rsp)}

    def test_normalized_series_align_and_rebase(self):
        result = normalized_performance(self.histories, ["SPY", "RSP"], 5)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["series"]["SPY"][0], 100)
        self.assertEqual(result["series"]["RSP"][0], 100)
        relative = normalized_performance(self.histories, ["RSP"], 5, benchmark="SPY")
        self.assertEqual(relative["status"], "available")
        self.assertEqual(relative["series"]["RSP"][0], 100)

    def test_missing_observation_is_not_filled(self):
        incomplete = {**self.histories, "RSP": self.histories["RSP"][:-1]}
        result = normalized_performance(incomplete, ["SPY", "RSP"], 5)
        self.assertEqual(result["status"], "missing_observations")
        returns = daily_returns_by_session(incomplete["RSP"], [d.isoformat() for d in self.dates])
        self.assertNotIn(self.dates[-1].isoformat(), returns)

    def test_macro_changes_are_lagged_and_percent_converts_to_basis_points(self):
        sessions = ["2024-01-08", "2024-01-09", "2024-01-10"]
        rows = [{"observation_date": "2024-01-08", "value": 4.0},
                {"observation_date": "2024-01-09", "value": 4.1}]
        changes = macro_changes_by_session(rows, sessions)
        self.assertNotIn("2024-01-09", changes)  # same-date observation not assumed known
        self.assertAlmostEqual(changes["2024-01-10"]["change"], 10.0)
        self.assertEqual(changes["2024-01-10"]["units"], "basis_points")

    def test_rolling_correlations_require_complete_windows(self):
        sessions = [str(i) for i in range(6)]
        x = {str(i): i for i in range(6)}
        y = {str(i): 2 * i for i in range(6)}
        del y["3"]
        rows = rolling_correlations(x, y, sessions, 3)
        self.assertEqual([row["date"] for row in rows], ["2"])
        self.assertEqual(rows[0]["correlation"], 1)

    def test_lead_lag_and_ratio_metrics_are_deterministic(self):
        sessions = [str(i) for i in range(30)]
        values = [((i * 17) % 23) - 11 for i in range(30)]
        x = {str(i): float(values[i]) for i in range(30)}
        y = {str(i): float(values[i - 1]) for i in range(1, 30)}
        rows = lead_lag_correlations(x, y, sessions, max_lag=2, minimum_observations=10)
        self.assertEqual(max(rows, key=lambda row: row["correlation"])["lag_sessions"], 1)
        result = relative_ratio_return(self.histories, "RSP", "SPY", 5)
        self.assertEqual(result["status"], "available")
        self.assertGreater(result["return"], 0)

    def test_lead_lag_diagnostics_separate_no_overlap_from_lag_edge_loss(self):
        sessions = [str(index) for index in range(30)]
        x = {str(i): float(i) for i in range(30)}
        y = {str(i): float(i*i) for i in range(30)}
        diagnostics = lead_lag_sample_diagnostics(x, y, sessions, max_lag=5, minimum_observations=20)
        self.assertEqual(diagnostics["status"], "available")
        self.assertEqual(diagnostics["matched_observations_zero_lag"], 30)
        self.assertEqual(diagnostics["overlap_start"], "0")
        self.assertEqual(diagnostics["requested_lags"], (-5, 5))
        no_match = lead_lag_sample_diagnostics({"0":1}, {"1":2}, sessions, max_lag=5)
        self.assertEqual(no_match["status"], "unavailable")
        self.assertIn("no matched observations", no_match["reason"])

    def test_participation_uses_fixed_standard_sector_baseline(self):
        dates = self.dates[-60:]
        histories = {"SPY": make_bars("SPY", dates, [100 + i for i in range(60)])}
        for index, symbol in enumerate(SECTOR_SYMBOLS):
            histories[symbol] = make_bars(symbol, dates, [100 + i * (1 + index / 100) for i in range(60)])
        # A custom ETF cannot alter the denominator or the fixed sector basket.
        histories["MAGS"] = make_bars("MAGS", dates, [100 + i * 5 for i in range(60)])
        result = sector_participation_metrics(histories, timeframe=5)
        self.assertEqual(result["universe"], 11)
        self.assertEqual(result["above_sma50"]["universe"], 11)
        self.assertEqual(result["outperformers"][5]["universe"], 11)

    def test_absolute_relative_breadth_and_window_changes_are_separate(self):
        dates = self.dates[-65:]
        histories = {"SPY": make_bars("SPY", dates, [100 + i for i in range(len(dates))]),
                     "RSP": make_bars("RSP", dates, [100 + i * 1.2 for i in range(len(dates))])}
        for index, symbol in enumerate(SECTOR_SYMBOLS):
            histories[symbol] = make_bars(symbol, dates, [100 + i * (1.01 + index / 100) for i in range(len(dates))])
        result = sector_participation_metrics(histories, timeframe=20)
        self.assertIn(20, result["absolute_breadth"])
        self.assertEqual(result["absolute_breadth"][20]["positive_count"], 11)
        self.assertEqual(result["absolute_breadth"][20]["outperforming_spy_count"], 11)
        self.assertEqual(result["rsp_spy_returns"][20]["status"], "available")
        self.assertIsNotNone(result["participation_change"][5]["positive_count_change"])
        self.assertIsNotNone(result["participation_change"][10]["relative_count_change"])
        self.assertIsNotNone(result["participation_change"][20]["current_start_date"])

    def test_participation_change_requires_both_aligned_windows(self):
        short_dates = self.dates[-30:]
        histories = {"SPY": make_bars("SPY", short_dates, [100 + i for i in range(30)])}
        for symbol in SECTOR_SYMBOLS:
            histories[symbol] = make_bars(symbol, short_dates, [100 + i for i in range(30)])
        result = sector_participation_metrics(histories, timeframe=20)
        self.assertEqual(result["participation_change"][20]["status"], "insufficient_history")
        self.assertIsNone(result["participation_change"][20]["positive_count_change"])

    def test_selloff_relative_resilience_is_not_healthy_broadening(self):
        dates = self.dates[-45:]
        symbols = ("SPY", "RSP", "IWM", *SECTOR_SYMBOLS)
        targets = {"SPY": 95.0, "RSP": 97.5, "IWM": 97.0}
        for index, symbol in enumerate(SECTOR_SYMBOLS):
            targets[symbol] = 98.0 if index < 9 else 94.0
        histories = {}
        for symbol in symbols:
            end = targets[symbol]
            values = [100.0] * (len(dates) - 21) + [100 + (end - 100) * step / 20 for step in range(21)]
            histories[symbol] = make_bars(symbol, dates, values)
        metrics = sector_participation_metrics(histories, timeframe=20)
        condition = classify_breadth_condition(metrics, window=20)
        self.assertAlmostEqual(metrics["rsp_spy_returns"][20]["spy_return"] * 100, -5.0)
        self.assertAlmostEqual(metrics["rsp_spy_returns"][20]["rsp_return"] * 100, -2.5)
        self.assertEqual(metrics["absolute_breadth"][20]["positive_count"], 0)
        self.assertEqual(metrics["absolute_breadth"][20]["outperforming_spy_count"], 9)
        self.assertEqual(condition["status"], "Relative resilience during a selloff")
        self.assertNotEqual(condition["status"], "Healthy broadening")

    def test_freshness_labels_stale_and_future_values(self):
        self.assertEqual(observation_freshness("2024-01-01", today=date(2024, 1, 10))["status"], "stale")
        self.assertEqual(observation_freshness("2024-01-11", today=date(2024, 1, 10))["status"], "future_date")


class FredMacroStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "macro.db"
        initialize_database(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parser_skips_missing_future_and_invalid_observations(self):
        rows = parse_fred_macro_observations("DGS2", {"observations": [
            {"date": "2024-01-01", "value": "4.2"},
            {"date": "2024-01-02", "value": "."},
            {"date": "2024-01-03", "value": "bad"},
            {"date": "2024-01-04", "value": "4.4"},
        ]}, today=date(2024, 1, 3))
        self.assertEqual([row["value"] for row in rows], [4.2])
        self.assertTrue(rows[0]["initial_release_only"])
        with self.assertRaises(FredMacroError):
            parse_fred_macro_observations("UNKNOWN", {"observations": []})

    def test_credential_priority_ignores_empty_environment_value(self):
        with patch.dict("os.environ", {"FRED_API_KEY": ""}, clear=True), \
             patch("dashboard.fred_macro.dotenv_values", return_value={"FRED_API_KEY": "dotenv-key"}):
            self.assertEqual(_fred_credentials(), ("dotenv-key", ".env"))
        with patch.dict("os.environ", {"FRED_API_KEY": "environment-key"}, clear=True):
            self.assertEqual(_fred_credentials(), ("environment-key", "environment"))

    @patch("dashboard.fred_macro.dotenv_values", return_value={"FRED_API_KEY": "unit-test-secret"})
    @patch("dashboard.fred_macro.requests.get")
    def test_dgs10_diagnostic_success_and_exact_request_configuration(self, get, _dotenv):
        response = Mock(status_code=200)
        response.json.return_value = {"observations": [{"date": "2024-01-02", "value": "4.25"}]}
        get.return_value = response
        with patch.dict("os.environ", {"FRED_API_KEY": ""}):
            result = diagnose_fred_connection("DGS10", today=date(2024, 1, 3))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["credential_source"], ".env")
        self.assertEqual(result["latest_observation"], "2024-01-02")
        self.assertNotIn("unit-test-secret", repr(result))
        self.assertTrue(get.call_args.args[0].endswith("/fred/series/observations"))
        self.assertEqual(get.call_args.kwargs["params"]["file_type"], "json")
        self.assertEqual(get.call_args.kwargs["params"]["series_id"], "DGS10")
        self.assertEqual(get.call_args.kwargs["params"]["output_type"], 4)

    @patch("dashboard.fred_macro.dotenv_values", return_value={"FRED_API_KEY": "unit-test-secret"})
    @patch("dashboard.fred_macro.requests.get")
    def test_diagnostic_classifies_auth_rate_limit_timeout_and_dns(self, get, _dotenv):
        response = Mock(status_code=403)
        response.json.return_value = {"error_code": 400, "error_message": "Invalid API key"}
        get.return_value = response
        self.assertEqual(diagnose_fred_connection()["status"], "authentication_error")
        response.status_code = 429
        self.assertEqual(diagnose_fred_connection()["status"], "rate_limit")
        from requests import ConnectionError, Timeout
        get.side_effect = Timeout("late")
        self.assertEqual(diagnose_fred_connection()["status"], "timeout")
        get.side_effect = ConnectionError("nodename nor servname provided")
        result = diagnose_fred_connection()
        self.assertEqual(result["status"], "network_dns_proxy_failure")
        self.assertEqual(result["error_category"], "dns_failure")
        self.assertNotIn("unit-test-secret", repr(result))

    @patch("dashboard.fred_macro.dotenv_values", return_value={})
    @patch("dashboard.fred_macro.requests.get", side_effect=AssertionError("invalid symbols must not request"))
    def test_diagnostic_invalid_series_and_missing_credentials(self, get, _dotenv):
        with patch.dict("os.environ", {}, clear=True):
            missing = diagnose_fred_connection("DGS10")
        self.assertEqual(missing["status"], "missing_credentials")
        invalid = diagnose_fred_connection("NOT_A_SERIES")
        self.assertEqual(invalid["status"], "invalid_series")
        get.assert_not_called()

    @patch("dashboard.fred_macro.dotenv_values", return_value={"FRED_API_KEY": "unit-test-secret"})
    @patch("dashboard.fred_macro.requests.get")
    def test_diagnostic_valid_response_without_observations(self, get, _dotenv):
        response = Mock(status_code=200)
        response.json.return_value = {"observations": [{"date": "2024-01-02", "value": "."}]}
        get.return_value = response
        self.assertEqual(diagnose_fred_connection(today=date(2024, 1, 3))["status"], "valid_response_no_observations")

    @patch("dashboard.fred_macro.requests.get")
    def test_manual_refresh_uses_initial_release_and_persists_cache(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"observations": [{"date": "2024-01-02", "value": "4.25"}]}
        get.return_value = response
        with patch.dict("os.environ", {"FRED_API_KEY": "unit-test-secret"}):
            result = refresh_fred_macro(database_path=self.database_path, today=date(2024, 1, 3))
        self.assertEqual(get.call_count, 5)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["output_type"], 4)
        self.assertNotIn("unit-test-secret", repr(result))
        saved = list_macro_series_observations(["DGS2"], PROVIDER, self.database_path)
        self.assertEqual(saved["DGS2"][0]["value"], 4.25)
        self.assertEqual(macro_series_coverage(["DGS2"], PROVIDER, self.database_path)["DGS2"]["observations"], 1)

    @patch("dashboard.fred_macro.dotenv_values", return_value={})
    @patch("dashboard.fred_macro.requests.get", side_effect=AssertionError("unexpected request"))
    def test_missing_api_key_does_not_request_and_existing_records_survive(self, get, _dotenv):
        upsert_macro_series_observations([{"series_id": "DGS2", "observation_date": "2024-01-02",
                                           "value": 4.25, "provider": PROVIDER, "units": "percent",
                                           "initial_release_only": True, "retrieved_at": "2024-01-03T00:00:00+00:00"}],
                                         self.database_path)
        with patch.dict("os.environ", {}, clear=True):
            result = refresh_fred_macro(database_path=self.database_path, today=date(2024, 1, 3))
        self.assertIn("Setup", result["errors"])
        get.assert_not_called()
        self.assertEqual(list_macro_series_observations(["DGS2"], PROVIDER, self.database_path)["DGS2"][0]["value"], 4.25)

    @patch("dashboard.fred_macro.dotenv_values", return_value={})
    @patch("dashboard.fred_macro.requests.get")
    def test_provider_failure_keeps_cached_observations(self, get, _dotenv):
        upsert_macro_series_observations([{"series_id": "DGS2", "observation_date": "2024-01-02",
                                           "value": 4.25, "provider": PROVIDER, "units": "percent",
                                           "initial_release_only": True, "retrieved_at": "2024-01-03T00:00:00+00:00"}],
                                         self.database_path)
        get.side_effect = __import__("requests").RequestException("offline")
        with patch.dict("os.environ", {"FRED_API_KEY": "unit-test-secret"}):
            result = refresh_fred_macro(database_path=self.database_path, today=date(2024, 1, 3))
        self.assertTrue(result["errors"])
        self.assertEqual(list_macro_series_observations(["DGS2"], PROVIDER, self.database_path)["DGS2"][0]["value"], 4.25)


if __name__ == "__main__":
    unittest.main()

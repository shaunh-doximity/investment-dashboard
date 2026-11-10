import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard.data_access import (
    add_chart_symbol, add_watchlist_item, list_daily_price_coverage, list_daily_prices,
    reserve_historical_request, save_holding, upsert_daily_prices,
)
from dashboard.database import initialize_database
from dashboard.history_providers import HistoricalDataError, TiingoDailyProvider
from dashboard.market_history import market_universe, refresh_historical_data
from dashboard.market_history import _refresh_ranges


def bar(day, close=100, provider="tiingo", basis="split_and_dividend_adjusted"):
    return {
        "symbol": "SPY", "trading_date": day, "provider": provider,
        "adjustment_basis": basis, "open": close, "high": close,
        "low": close, "close": close, "volume": 1000,
        "retrieved_at": "2024-01-01T00:00:00+00:00",
    }


class FakeProvider:
    name = "fake"
    adjustment_basis = "split_adjusted"
    max_requests_per_hour = 50
    max_requests_per_day = 1000
    max_symbols_per_month = 500

    def __init__(self, return_bars=True):
        self.calls = []
        self.return_bars = return_bars

    def fetch_daily(self, symbol, start_date, end_date):
        self.calls.append((symbol, start_date, end_date))
        if not self.return_bars:
            return []
        output = []
        day, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        while day <= end:
            if day.weekday() < 5:
                output.append({**bar(day.isoformat()), "symbol": symbol,
                               "provider": self.name, "adjustment_basis": self.adjustment_basis})
            day += timedelta(days=1)
        return output


class MarketHistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "history.db"
        initialize_database(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def seed_weekday_history(self, through: date, retrieved_at="2026-11-03T14:00:00+00:00"):
        day = date(2024, 11, 2)
        rows = []
        while day <= through:
            if day.weekday() < 5:
                rows.append({**bar(day.isoformat()), "provider": "fake",
                             "adjustment_basis": "split_adjusted", "retrieved_at": retrieved_at})
            day += timedelta(days=1)
        upsert_daily_prices(rows, self.database_path)

    def test_upsert_key_keeps_provider_and_adjustment_bases_separate(self):
        first = bar("2024-01-02")
        upsert_daily_prices([first, first], self.database_path)
        self.assertEqual(len(list_daily_prices("SPY", "tiingo", "split_and_dividend_adjusted", database_path=self.database_path)), 1)
        upsert_daily_prices([bar("2024-01-02", close=105)], self.database_path)
        upsert_daily_prices([bar("2024-01-02", provider="another", basis="raw")], self.database_path)
        coverage = list_daily_price_coverage(["SPY"], "tiingo", "split_and_dividend_adjusted", self.database_path)
        self.assertEqual(coverage["SPY"]["observations"], 1)
        prices = list_daily_prices("SPY", "tiingo", "split_and_dividend_adjusted", database_path=self.database_path)
        self.assertEqual(prices[0]["close"], 105)
        self.assertEqual(len(list_daily_prices("SPY", "another", "raw", database_path=self.database_path)), 1)

    def test_manual_refresh_caches_success_and_skips_fresh_history(self):
        provider = FakeProvider()
        as_of = date(2024, 7, 10)
        first = refresh_historical_data(["spy"], provider, as_of=as_of, database_path=self.database_path)
        second = refresh_historical_data(["SPY"], provider, as_of=as_of, database_path=self.database_path)
        self.assertGreater(first["updated"]["SPY"], 1)
        self.assertEqual(second["cached"], ["SPY"])
        self.assertEqual(len(provider.calls), 1)

    def test_stale_update_requests_only_a_recent_overlap(self):
        provider = FakeProvider()
        first_day = date(2024, 7, 1)
        refresh_historical_data(["SPY"], provider, as_of=first_day, database_path=self.database_path)
        refresh_historical_data(["SPY"], provider, as_of=date(2024, 7, 10), database_path=self.database_path)
        self.assertEqual(len(provider.calls), 2)
        second_start = date.fromisoformat(provider.calls[1][1])
        self.assertEqual(second_start, date(2024, 7, 1) - timedelta(days=7))

    def test_refresh_backfills_detected_internal_gaps_even_when_latest_is_recent(self):
        provider = FakeProvider()
        upsert_daily_prices([
            {**bar("2024-07-01"), "provider": provider.name, "adjustment_basis": provider.adjustment_basis},
            {**bar("2024-07-10"), "provider": provider.name, "adjustment_basis": provider.adjustment_basis},
        ], self.database_path)
        result = refresh_historical_data(["SPY"], provider, as_of=date(2024, 7, 10), database_path=self.database_path)
        self.assertEqual(len(provider.calls), 2)  # missing history prefix and internal gap
        self.assertEqual(provider.calls[0][1], "2022-07-10")
        self.assertEqual(provider.calls[1][1:], ("2024-07-02", "2024-07-09"))
        self.assertGreater(result["updated"]["SPY"], 1)

    def test_empty_provider_response_keeps_missing_data_explicit(self):
        result = refresh_historical_data(["SPY"], FakeProvider(return_bars=False), as_of=date(2024, 7, 10), database_path=self.database_path)
        self.assertIn("SPY", result["errors"])
        self.assertNotIn("SPY", result["coverage"])
        self.assertEqual(result["diagnostics"][0]["final_freshness_state"], "Provider publication pending")

    def test_october_30_cache_is_stale_when_november_2_is_expected_session(self):
        provider = FakeProvider()
        self.seed_weekday_history(date(2026, 10, 30))
        result = refresh_historical_data(["SPY"], provider, as_of=date(2026, 11, 2),
                                         database_path=self.database_path)
        self.assertTrue(provider.calls)
        self.assertEqual(result["expected_session"], "2026-11-02")
        diag = result["diagnostics"][0]
        self.assertEqual(diag["previous_cached_date"], "2026-10-30")
        self.assertTrue(diag["refresh_attempted"])
        self.assertEqual(diag["new_cached_date"], "2026-11-02")
        self.assertEqual(diag["final_freshness_state"], "Updated")

    def test_same_session_cache_is_current_even_when_request_timestamp_is_old(self):
        provider = FakeProvider()
        self.seed_weekday_history(date(2026, 11, 2), retrieved_at="2026-11-02T23:00:00+00:00")
        result = refresh_historical_data(["SPY"], provider, as_of=date(2026, 11, 2),
                                         database_path=self.database_path)
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["diagnostics"][0]["final_freshness_state"], "Already current")

    def test_recent_request_timestamp_does_not_block_provider_publication_retry(self):
        provider = FakeProvider(return_bars=False)
        self.seed_weekday_history(date(2026, 10, 30))
        result = refresh_historical_data(["SPY"], provider, as_of=date(2026, 11, 2),
                                         database_path=self.database_path)
        self.assertEqual(len(provider.calls), 1)
        diag = result["diagnostics"][0]
        self.assertEqual(diag["provider_request_status"], "no_observations")
        self.assertEqual(diag["final_freshness_state"], "Provider publication pending")

    def test_recent_market_refresh_ranges_are_inclusive_of_expected_session(self):
        self.assertEqual(_refresh_ranges([bar("2026-10-30")], date(2024, 11, 2),
            date(2026, 11, 2), date(2026, 10, 30), None, 7)[-1][1], date(2026, 11, 2))

    def test_request_quota_is_persisted_and_enforced(self):
        allowed, _ = reserve_historical_request("fake", "SPY", hourly_limit=1, daily_limit=10, monthly_symbol_limit=10, database_path=self.database_path)
        blocked, reason = reserve_historical_request("fake", "QQQ", hourly_limit=1, daily_limit=10, monthly_symbol_limit=10, database_path=self.database_path)
        self.assertTrue(allowed)
        self.assertFalse(blocked)
        self.assertIn("hourly", reason)

    def test_universe_includes_benchmarks_sectors_macro_and_user_symbols(self):
        save_holding("CUSTOM", 2, 10, database_path=self.database_path)
        add_watchlist_item("WATCH", database_path=self.database_path)
        symbols = market_universe(self.database_path)
        for symbol in ("SPY", "SMH", "XLK", "XLC", "TLT", "GLD", "USO", "CUSTOM", "WATCH"):
            self.assertIn(symbol, symbols)

    def test_custom_chart_symbols_are_in_refresh_universe_and_missing_history_is_explicit(self):
        add_chart_symbol("leadership", "IGV", self.database_path)
        add_chart_symbol("sector_display", "MAGS", self.database_path)
        symbols = market_universe(self.database_path)
        self.assertIn("IGV", symbols)
        self.assertIn("MAGS", symbols)
        coverage = list_daily_price_coverage(["IGV"], "tiingo", "split_and_dividend_adjusted", self.database_path)
        self.assertNotIn("IGV", coverage)  # UI renders this as “Historical data needed.”

    def test_a_custom_ticker_is_fetched_only_when_explicitly_requested(self):
        provider = FakeProvider()
        # Saving a preference is local-only; no provider object is involved.
        add_chart_symbol("leadership", "IGV", self.database_path)
        self.assertEqual(provider.calls, [])
        refresh_historical_data(["IGV"], provider, as_of=date(2024, 7, 10), database_path=self.database_path)
        self.assertEqual(len(provider.calls), 1)

    @patch("dashboard.history_providers.requests.get")
    def test_tiingo_provider_uses_adjusted_fields_and_safe_auth_header(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = [{
            "date": "2024-01-02T00:00:00+00:00", "open": 100, "high": 110,
            "low": 90, "close": 105, "volume": 1000,
            "adjOpen": 50, "adjHigh": 55, "adjLow": 45,
            "adjClose": 52.5, "adjVolume": 2000,
        }]
        get.return_value = response
        provider = TiingoDailyProvider("secret-test-token")
        rows = provider.fetch_daily("spy", "2024-01-01", "2024-01-03")
        self.assertEqual(rows[0]["close"], 52.5)
        self.assertEqual(rows[0]["volume"], 2000)
        self.assertEqual(rows[0]["adjustment_basis"], "split_and_dividend_adjusted")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Token secret-test-token")

    @patch("dashboard.history_providers.requests.get")
    def test_tiingo_provider_does_not_return_provider_error_payload(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"detail": "invalid secret-test-token"}
        get.return_value = response
        with self.assertRaises(HistoricalDataError) as error:
            TiingoDailyProvider("secret-test-token").fetch_daily("SPY", "2024-01-01", "2024-01-03")
        self.assertNotIn("secret-test-token", str(error.exception))

    @patch("dashboard.history_providers.requests.get")
    def test_tiingo_account_quota_payload_is_classified_without_echoing_payload(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"detail": "monthly symbol quota limit reached; secret-test-token"}
        get.return_value = response
        with self.assertRaises(HistoricalDataError) as error:
            TiingoDailyProvider("secret-test-token").fetch_daily("SPY", "2024-01-01", "2024-01-03")
        self.assertIn("limit response", str(error.exception))
        self.assertNotIn("secret-test-token", str(error.exception))


if __name__ == "__main__":
    unittest.main()

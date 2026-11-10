from datetime import date, timedelta
import unittest
from unittest.mock import patch

from dashboard.research_context import build_research_context


def history(seed):
    rows = []
    day = date.today()
    while len(rows) < 100:
        if day.weekday() < 5:
            rows.append({"trading_date": day.isoformat(), "close": seed + len(rows) * 0.7})
        day -= timedelta(days=1)
    return list(reversed(rows))


class ResearchContextTests(unittest.TestCase):
    def test_context_uses_cached_data_and_verified_sector_only(self):
        with patch("dashboard.research_context.load_history", side_effect=lambda symbol: history(100 if symbol == "SPY" else 70)), \
             patch("dashboard.research_context.list_portfolio_classification_records", return_value={
                 "TEST": {"verification_status": "Verified", "sector_benchmark": "XLK"},
             }), \
             patch("dashboard.research_context.list_upcoming_market_events", return_value=[{
                 "category": "Earnings", "related_symbols": ["TEST"], "name": "TEST earnings",
                 "event_date": date.today().isoformat(), "status": "Confirmed",
                 "source_verification": "Official source", "provider": "official", "source_url": "https://example.test",
             }]), \
             patch("dashboard.research_context.list_watchlist", return_value=[{"symbol": "TEST", "company_name": "Test Co."}]) as watchlist:
            context = build_research_context("test", {"price": 72, "quote_date": date.today().isoformat()}, {"investment_thesis": "User thesis"})

        self.assertEqual(context["ticker"], "TEST")
        self.assertEqual(context["company_name"], "Test Co.")
        self.assertIn("return_20d", context["tiingo_adjusted_daily_history"])
        self.assertIsNotNone(context["spy_relative_performance"]["excess_return_20d"])
        self.assertEqual(context["sector_rotation"]["symbol"], "XLK")
        self.assertEqual(context["upcoming_earnings_events"][0]["source_url"], "https://example.test")
        self.assertEqual(context["saved_user_strategy"]["investment_thesis"], "User thesis")
        self.assertIn("No filings", context["external_evidence_status"])
        watchlist.assert_called_once()

    def test_unverified_sector_classification_is_not_used_as_rotation_evidence(self):
        with patch("dashboard.research_context.load_history", side_effect=lambda _symbol: history(100)), \
             patch("dashboard.research_context.list_portfolio_classification_records", return_value={
                 "TEST": {"verification_status": "Unverified", "sector_benchmark": "XLK"},
             }), \
             patch("dashboard.research_context.list_upcoming_market_events", return_value=[]), \
             patch("dashboard.research_context.list_watchlist", return_value=[]):
            context = build_research_context("TEST")
        self.assertIsNone(context["sector_rotation"])

    def test_missing_history_and_stale_quote_are_reported_without_fallback_values(self):
        with patch("dashboard.research_context.load_history", return_value=[]), \
             patch("dashboard.research_context.list_portfolio_classification_records", return_value={}), \
             patch("dashboard.research_context.list_upcoming_market_events", return_value=[]), \
             patch("dashboard.research_context.list_watchlist", return_value=[]):
            context = build_research_context("TEST", {"price": 50, "quote_date": "2000-01-01"})
        self.assertTrue(context["cached_quote"]["stale"])
        self.assertIsNone(context["tiingo_adjusted_daily_history"]["latest_adjusted_close"])
        self.assertIsNone(context["tiingo_adjusted_daily_history"]["return_20d"])
        self.assertIsNone(context["spy_relative_performance"]["excess_return_20d"])


if __name__ == "__main__":
    unittest.main()

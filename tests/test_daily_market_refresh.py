import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from dashboard.daily_market_refresh import run_daily_market_refresh


class DailyMarketRefreshTests(unittest.TestCase):
    def test_sources_run_in_order_and_ai_is_not_called_by_refresh_stages(self):
        calls = []
        def history(symbols, **kwargs):
            calls.append("history")
            return {"updated": {"SPY": 1}, "errors": {}, "diagnostics": []}
        def vix(**kwargs):
            calls.append("vix")
            return {"updated": {"cboe_official_csv": 2}, "errors": {},
                    "current_candidate": {"date": "2026-11-02"}}
        def treasury(**kwargs):
            calls.append("treasury")
            return {"updated": 3, "errors": {}, "latest_date": "2026-11-02"}
        def calendar(**kwargs):
            calls.append("calendar")
            return {"updated": {"fed": {"inserted": 1}}, "errors": {}}
        stages = []
        result = run_daily_market_refresh(
            ["SPY"], now=datetime(2026, 11, 2, 17, tzinfo=ZoneInfo("America/New_York")),
            on_stage=lambda name, state, detail: stages.append(name),
            history_refresh=history, vix_refresh=vix, treasury_refresh=treasury,
            calendar_refresh=calendar,
        )
        self.assertEqual(calls, ["history", "vix", "treasury", "calendar"])
        self.assertEqual(result["expected_session"], "2026-11-02")
        self.assertTrue(result["data_changed"])
        self.assertEqual(stages, ["session", "historical", "vix", "treasury", "calendar",
                                  "recent_catalysts", "snapshot"])
        self.assertEqual(result["stages"]["recent_catalysts"]["status"], "cached_only")

    def test_source_failure_does_not_prevent_later_source_refreshes(self):
        calls = []
        def history(*args, **kwargs):
            raise RuntimeError("Tiingo unavailable")
        def vix(**kwargs):
            calls.append("vix")
            return {"updated": {}, "errors": {"Cboe": "unavailable"}}
        def treasury(**kwargs):
            calls.append("treasury")
            return {"updated": 0, "errors": {}, "latest_date": "2026-11-02"}
        def calendar(**kwargs):
            calls.append("calendar")
            return {"updated": {}, "errors": {}}
        result = run_daily_market_refresh(
            ["SPY"], now=datetime(2026, 11, 2, 17, tzinfo=ZoneInfo("America/New_York")),
            history_refresh=history, vix_refresh=vix, treasury_refresh=treasury,
            calendar_refresh=calendar,
        )
        self.assertEqual(calls, ["vix", "treasury", "calendar"])
        self.assertEqual(result["stages"]["historical"]["status"], "failed")
        self.assertEqual(result["treasury"]["latest_date"], "2026-11-02")


if __name__ == "__main__":
    unittest.main()

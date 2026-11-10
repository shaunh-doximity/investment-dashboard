import unittest
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

from dashboard.market_calendar import (
    latest_completed_us_equity_session, expected_sessions_between,
)


NY = ZoneInfo("America/New_York")


class MarketCalendarTests(unittest.TestCase):
    def test_dst_transition_uses_new_york_calendar_and_november_2_session(self):
        before_close = datetime(2026, 11, 2, 15, 59, tzinfo=NY)
        after_close = datetime(2026, 11, 2, 16, 1, tzinfo=NY)
        self.assertEqual(latest_completed_us_equity_session(before_close), "2026-10-30")
        self.assertEqual(latest_completed_us_equity_session(after_close), "2026-11-02")
        self.assertEqual(after_close.utcoffset().total_seconds(), -5 * 60 * 60)

    def test_aware_non_new_york_input_is_converted_before_session_comparison(self):
        shanghai = ZoneInfo("Asia/Shanghai")
        before_close = datetime(2026, 11, 3, 4, 59, tzinfo=shanghai)
        after_close = datetime(2026, 11, 3, 5, 1, tzinfo=shanghai)
        self.assertEqual(latest_completed_us_equity_session(before_close), "2026-10-30")
        self.assertEqual(latest_completed_us_equity_session(after_close), "2026-11-02")

    def test_weekend_and_exchange_holiday_roll_back_to_last_session(self):
        saturday = datetime(2026, 11, 7, 12, tzinfo=NY)
        thanksgiving = datetime(2026, 11, 26, 18, tzinfo=NY)
        self.assertEqual(latest_completed_us_equity_session(saturday), "2026-11-06")
        self.assertEqual(latest_completed_us_equity_session(thanksgiving), "2026-11-25")

    def test_early_close_is_completed_at_scheduled_close_not_1600(self):
        before = datetime(2026, 11, 27, 12, 59, tzinfo=NY)
        after = datetime(2026, 11, 27, 13, 1, tzinfo=NY)
        self.assertEqual(latest_completed_us_equity_session(before), "2026-11-25")
        self.assertEqual(latest_completed_us_equity_session(after), "2026-11-27")

    def test_holiday_weekend_gap_is_not_misclassified_as_missing_price_sessions(self):
        self.assertEqual(expected_sessions_between(date(2026, 11, 25), date(2026, 11, 27)), [])
        self.assertEqual(expected_sessions_between(date(2026, 11, 24), date(2026, 11, 30)),
                         [date(2026, 11, 25), date(2026, 11, 27)])


if __name__ == "__main__":
    unittest.main()

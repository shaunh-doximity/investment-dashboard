import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

from dashboard.data_access import (
    add_earnings_watch_symbol, get_market_event_timezone, list_earnings_watchlist,
    list_market_event_revisions, list_market_events, list_upcoming_market_events,
    remove_earnings_watch_symbol, save_manual_market_event, save_manual_public_event, save_market_event_timezone,
    upsert_market_events,
)
from dashboard.database import connect, initialize_database
from dashboard.event_calendar import (
    BLS_ICS_URL, filter_upcoming_events, parse_bea_calendar, parse_bls_calendar,
    parse_fec_election_dates, parse_fed_fomc_calendar, parse_fed_speeches, parse_ics_events,
    refresh_official_calendars,
)
from dashboard.event_calendar_ui import _display_event_datetime


class CalendarParsingTests(unittest.TestCase):
    def test_ics_unfolds_and_converts_eastern_time_across_dst(self):
        feed = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:cpi-1\r\nSUMMARY:Consumer Price Index \r\n release\r\nDTSTART;TZID=America/New_York:20260308T083000\r\nEND:VEVENT\r\nEND:VCALENDAR"
        events = parse_bls_calendar(feed)
        self.assertEqual(events[0]["name"], "Consumer Price Index release")
        self.assertEqual(events[0]["start_utc"], "2026-03-08T12:30:00Z")
        self.assertEqual(events[0]["source_timezone"], "America/New_York")
        self.assertEqual(events[0]["status"], "Confirmed")

    def test_display_time_is_eastern_with_daylight_saving_applied(self):
        event = {"start_utc": "2026-03-08T12:30:00Z", "all_day": False}
        day_label, time_label, _ = _display_event_datetime(event, ZoneInfo("America/New_York"))
        self.assertEqual(day_label, "Sun, Mar 8")
        self.assertEqual(time_label, "8:30 AM EDT")

    def test_earnings_session_displays_without_inventing_clock_time(self):
        event = {"start_utc": "2026-03-09T00:00:00Z", "event_date": "2026-03-08",
                 "earnings_session": "BMO", "category": "Earnings", "all_day": True}
        day_label, time_label, _ = _display_event_datetime(event, ZoneInfo("America/New_York"))
        self.assertEqual(day_label, "Sun, Mar 8")
        self.assertEqual(time_label, "Before Market Open")

    def test_official_ics_adapters_keep_only_relevant_releases_and_confirmed_labels(self):
        feed = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:gdp\nSUMMARY:Gross Domestic Product (Advance Estimate)\nDTSTART:20261029T123000Z\nEND:VEVENT\nBEGIN:VEVENT\nUID:other\nSUMMARY:International Trade release\nDTSTART:20261030T123000Z\nEND:VEVENT\nEND:VCALENDAR"""
        events = parse_bea_calendar(feed)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "Confirmed")
        self.assertEqual(events[0]["source_timezone"], "America/New_York")
        self.assertEqual(events[0]["start_utc"], "2026-10-29T12:30:00Z")

    def test_explicit_feed_tentative_status_is_preserved(self):
        feed = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:future-cpi\nSUMMARY:Consumer Price Index\nDTSTART;TZID=America/New_York:20261112T083000\nSTATUS:TENTATIVE\nEND:VEVENT\nEND:VCALENDAR"""
        self.assertEqual(parse_bls_calendar(feed)[0]["status"], "Tentative")

    def test_cancelled_ics_event_and_fed_fec_tentative_records(self):
        feed = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nSUMMARY:Producer Price Index\nDTSTART;VALUE=DATE:20261112\nSTATUS:CANCELLED\nEND:VEVENT\nEND:VCALENDAR"""
        parsed = parse_bls_calendar(feed)
        self.assertEqual(parsed[0]["status"], "Cancelled")
        self.assertTrue(parsed[0]["all_day"])
        fed = parse_fed_fomc_calendar("<h3>2026 FOMC Meetings</h3><h4>September</h4><p>15-16</p><h4>October</h4><p>27-28</p><h4>December</h4><p>8-9*</p>", today=date(2026, 10, 25))
        self.assertEqual(fed[0]["start_utc"], "2026-10-28T18:00:00Z")
        self.assertEqual(fed[0]["status"], "Confirmed")
        self.assertEqual(fed[1]["start_utc"], "2026-12-09T19:00:00Z")
        self.assertEqual(fed[1]["status"], "Tentative")
        fec = parse_fec_election_dates("<p>Tuesday, November 3, 2026, is the next regularly scheduled federal General Election date.</p>", today=date(2026, 10, 25))
        self.assertEqual(fec[0]["start_utc"], "2026-11-03T00:00:00Z")
        self.assertEqual(fec[0]["status"], "Tentative")

    def test_nonexistent_dst_wall_time_is_skipped_and_future_fed_speech_is_tentative(self):
        invalid = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:bad\nSUMMARY:Consumer Price Index\nDTSTART;TZID=America/New_York:20260308T023000\nEND:VEVENT\nEND:VCALENDAR"
        self.assertEqual(parse_bls_calendar(invalid), [])
        html = "<p>11/02/2026</p><p>Monetary Policy and the Economic Outlook</p><p>Vice Chair</p>"
        speeches = parse_fed_speeches(html, year=2026, today=date(2026, 10, 25))
        self.assertEqual(len(speeches), 1)
        self.assertEqual(speeches[0]["status"], "Confirmed")
        self.assertTrue(speeches[0]["all_day"])

    def test_filter_is_category_aware_sorted_and_excludes_past_cancelled_and_outside_window(self):
        def event(day, name, category="Macro", status="Confirmed", all_day=False):
            return {"start_utc": f"2026-10-{day:02d}T12:00:00Z", "name": name, "category": category,
                    "status": status, "all_day": all_day}
        values = [event(27, "Fed", "Fed"), event(26, "CPI"), event(28, "Old", status="Cancelled"), event(30, "Later")]
        selected = filter_upcoming_events(values, category="All", horizon_days=4,
                                          now_utc=datetime(2026, 10, 25, tzinfo=timezone.utc))
        self.assertEqual([row["name"] for row in selected], ["CPI", "Fed"])
        self.assertEqual([row["name"] for row in filter_upcoming_events(values, category="Fed", horizon_days=30,
                                                                         now_utc=datetime(2026, 10, 25, tzinfo=timezone.utc))], ["Fed"])


class CalendarPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "events.sqlite"
        initialize_database(self.db)

    def tearDown(self):
        self.directory.cleanup()

    def test_upsert_deduplicates_and_audits_rescheduled_and_cancelled_events(self):
        original = {"name": "CPI release", "category": "Macro", "start_utc": "2026-11-12T13:30:00Z",
                    "source_timezone": "America/New_York", "source_url": BLS_ICS_URL, "provider": "bls_ical",
                    "source_uid": "cpi-one", "status": "Tentative", "related_symbols": [], "all_day": False}
        first = upsert_market_events([original], self.db)
        duplicate = upsert_market_events([{**original, "provider": "bea_ical", "source_uid": "same-event"}], self.db)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(duplicate["unchanged"], 1)
        upsert_market_events([{**original, "start_utc": "2026-11-13T13:30:00Z"}], self.db)
        upsert_market_events([{**original, "status": "Cancelled"}], self.db)
        rows = list_market_events(self.db, include_cancelled=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "Cancelled")
        history = list_market_event_revisions(rows[0]["event_id"], self.db)
        self.assertEqual({item["action"] for item in history}, {"created", "rescheduled", "cancelled"})
        self.assertEqual(list_market_events(self.db), [])

    def test_manual_earnings_date_converts_dst_time_and_correction_is_audited(self):
        save_manual_market_event({"symbol": "NVDA", "date": "2026-03-08", "time": "08:30",
                                  "source_timezone": "America/New_York", "status": "Confirmed",
                                  "source_url": "https://investor.nvidia.com/", "notes": "IR announcement"}, self.db)
        event = list_market_events(self.db)[0]
        self.assertEqual(event["start_utc"], "2026-03-08T12:30:00Z")
        self.assertEqual(event["related_symbols"], ["NVDA"])
        save_manual_market_event({"symbol": "NVDA", "date": "2026-03-09", "time": "08:30",
                                  "source_timezone": "America/New_York", "status": "Estimated",
                                  "source_url": "https://investor.nvidia.com/", "notes": "Revised"}, self.db)
        rows = list_market_events(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "Estimated")
        self.assertEqual(len(list_market_event_revisions(rows[0]["event_id"], self.db)), 2)

    def test_manual_session_saves_without_source_or_fabricated_time(self):
        save_manual_market_event({"symbol": "NVDA", "date": "2026-11-02", "earnings_session": "AMC",
                                  "source_timezone": "America/New_York", "status": "Confirmed",
                                  "notes": "Company IR calendar"}, self.db)
        event = list_market_events(self.db)[0]
        self.assertEqual(event["event_date"], "2026-11-02")
        self.assertEqual(event["earnings_session"], "AMC")
        self.assertIsNone(event["exact_announcement_time_utc"])
        self.assertEqual(event["source_verification"], "User-reported")
        self.assertEqual(event["source_url"], "")
        self.assertEqual(event["last_verified_at"], "")
        self.assertTrue(event["all_day"])

    def test_optional_manual_earnings_source_url_is_validated_only_when_present(self):
        with self.assertRaises(ValueError):
            save_manual_market_event({"symbol": "NVDA", "date": "2026-11-02", "earnings_session": "TBD",
                                      "status": "Estimated", "source_url": "https://"}, self.db)
        save_manual_market_event({"symbol": "NVDA", "date": "2026-11-02", "earnings_session": "TBD",
                                  "status": "Estimated"}, self.db)
        self.assertEqual(len(list_market_events(self.db)), 1)

    def test_earnings_watchlist_timezone_and_all_statuses_persist(self):
        defaults = list_earnings_watchlist(self.db, include_user_symbols=False)
        self.assertEqual(set(defaults), {"NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA"})
        add_earnings_watch_symbol("COST", self.db)
        self.assertIn("COST", list_earnings_watchlist(self.db, include_user_symbols=False))
        remove_earnings_watch_symbol("COST", self.db)
        self.assertNotIn("COST", list_earnings_watchlist(self.db, include_user_symbols=False))
        save_market_event_timezone("Asia/Shanghai", self.db)
        self.assertEqual(get_market_event_timezone(self.db), "Asia/Shanghai")
        for status in ("Confirmed", "Estimated", "Unverified"):
            save_manual_market_event({"symbol": "NVDA", "date": "2026-11-19", "time": "08:30",
                                      "source_timezone": "America/New_York", "status": status,
                                      "source_url": "https://investor.nvidia.com/"}, self.db)
            self.assertEqual(list_market_events(self.db)[0]["status"], status)

    def test_user_entered_policy_deadline_requires_source_and_is_saved_as_utc(self):
        with self.assertRaises(ValueError):
            save_manual_public_event({"name": "Policy deadline", "date": "2026-11-01", "status": "Confirmed"}, self.db)
        save_manual_public_event({"name": "Policy deadline", "date": "2026-11-01", "time": "09:00",
                                  "status": "Confirmed", "source_url": "https://www.gov.example/notice"}, self.db)
        event = list_market_events(self.db)[0]
        self.assertEqual(event["category"], "Political/Other")
        self.assertEqual(event["start_utc"], "2026-11-01T14:00:00Z")

    def test_calendar_schema_migration_preserves_existing_holding(self):
        with connect(self.db) as connection:
            connection.execute("INSERT INTO holdings(symbol,shares,average_cost,current_price) VALUES ('ABC',3,10,11)")
        initialize_database(self.db)
        with connect(self.db) as connection:
            holding = connection.execute("SELECT symbol,shares,average_cost FROM holdings WHERE symbol='ABC'").fetchone()
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tuple(holding), ("ABC", 3.0, 10.0))
        self.assertTrue({"market_events", "market_event_revisions", "earnings_watchlist"}.issubset(tables))

    def test_failed_source_requests_preserve_cached_events(self):
        upsert_market_events([{"name": "Existing CPI", "category": "Macro", "start_utc": "2026-11-12T13:30:00Z",
                               "source_timezone": "America/New_York", "provider": "bls_ical", "source_uid": "old",
                               "status": "Tentative", "source_url": BLS_ICS_URL}], self.db)
        with patch("dashboard.event_calendar._fetch", side_effect=requests.Timeout):
            result = refresh_official_calendars(database_path=self.db, today=date(2026, 10, 25))
        self.assertTrue(result["errors"])
        self.assertEqual(list_market_events(self.db)[0]["name"], "Existing CPI")
        self.assertEqual(list_upcoming_market_events(database_path=self.db)[0]["name"], "Existing CPI")


if __name__ == "__main__":
    unittest.main()

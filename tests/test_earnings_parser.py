import tempfile
import unittest
from datetime import date
from pathlib import Path

from dashboard.data_access import list_market_events, save_quick_earnings_events, upsert_market_events
from dashboard.database import initialize_database
from dashboard.earnings_parser import parse_quick_earnings


class QuickEarningsParserTests(unittest.TestCase):
    def test_multiple_pairs_separated_by_spaces_commas_semicolons_and_newlines(self):
        parsed = parse_quick_earnings(
            "HOOD 10/27 AAPL 10/28 AMC, MSFT 2026-10-29 BMO;\nBRK.B 10/30/2026 before market",
            as_of=date(2026, 10, 25),
        )
        self.assertEqual(parsed["errors"], [])
        self.assertEqual([row["ticker"] for row in parsed["events"]], ["HOOD", "AAPL", "MSFT", "BRK.B"])
        self.assertEqual([row["date"] for row in parsed["events"]], [
            "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
        ])
        self.assertEqual([row["session"] for row in parsed["events"]], ["TBD", "AMC", "BMO", "BMO"])

    def test_timing_aliases_are_case_insensitive_and_ticker_is_normalized(self):
        parsed = parse_quick_earnings(
            "hood 11/18 after close; tsla 11/20 PRE-MARKET; brk-b 11/21 TBD",
            as_of=date(2026, 10, 25),
        )
        self.assertEqual([row["ticker"] for row in parsed["events"]], ["HOOD", "TSLA", "BRK-B"])
        self.assertEqual([row["session"] for row in parsed["events"]], ["AMC", "BMO", "TBD"])

    def test_year_rule_infers_next_upcoming_date_and_exposes_inference(self):
        parsed = parse_quick_earnings("AAPL 01/05", as_of=date(2026, 12, 30))
        self.assertEqual(parsed["events"][0]["date"], "2027-01-05")
        self.assertTrue(parsed["events"][0]["year_inferred"])
        self.assertIn("inferred", parsed["events"][0]["year_resolution"])

    def test_configured_year_pins_date_and_explicit_year_formats_are_supported(self):
        parsed = parse_quick_earnings(
            "AAPL 01/05 MSFT 2027-02-03 TSLA 02/04/2027",
            as_of=date(2026, 12, 30), calendar_year=2026,
        )
        self.assertEqual([row["date"] for row in parsed["events"]], [
            "2026-01-05", "2027-02-03", "2027-02-04",
        ])
        self.assertEqual(parsed["events"][0]["year_resolution"], "Configured calendar year 2026")

    def test_invalid_dates_and_malformed_text_are_reported(self):
        parsed = parse_quick_earnings("AAPL 02/30; nonsense; NVDA 2026-13-01", as_of=date(2026, 1, 1))
        self.assertEqual(parsed["events"], [])
        self.assertEqual(len(parsed["errors"]), 3)
        self.assertIn("Invalid calendar date", parsed["errors"][0]["error"])
        self.assertTrue(any("Could not parse" in item["error"] for item in parsed["errors"]))

    def test_leap_day_resolves_to_next_valid_occurrence(self):
        parsed = parse_quick_earnings("AAPL 02/29", as_of=date(2026, 3, 1))
        self.assertEqual(parsed["events"][0]["date"], "2028-02-29")


class QuickEarningsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.sqlite"
        initialize_database(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_quick_entries_persist_and_duplicates_update_user_events(self):
        entries = [
            {"ticker": "hood", "date": "2026-10-27", "session": "AMC"},
            {"ticker": "AAPL", "date": "2026-10-28", "session": "BMO"},
            {"ticker": "HOOD", "date": "2026-10-27", "session": "TBD"},
        ]
        first = save_quick_earnings_events(entries, original_input="hood 10/27 AMC AAPL 10/28 BMO", database_path=self.database)
        self.assertEqual((first["added"], first["updated"], first["skipped"]), (2, 0, 1))
        events = list_market_events(self.database)
        hood = next(event for event in events if event["related_symbols"] == ["HOOD"])
        self.assertEqual(hood["earnings_session"], "AMC")
        self.assertEqual(hood["status"], "Unverified")
        self.assertEqual(hood["source_verification"], "User-reported")
        self.assertEqual(hood["original_input"], "hood 10/27 AMC AAPL 10/28 BMO")

        revised = save_quick_earnings_events(
            [{"ticker": "HOOD", "date": "2026-10-27", "session": "BMO"}],
            original_input="HOOD 10/27 BMO", database_path=self.database,
        )
        self.assertEqual(revised["updated"], 1)
        initialize_database(self.database)
        hood_after_restart = next(event for event in list_market_events(self.database) if event["related_symbols"] == ["HOOD"])
        self.assertEqual(hood_after_restart["earnings_session"], "BMO")
        self.assertEqual(hood_after_restart["original_input"], "HOOD 10/27 BMO")

    def test_official_event_is_skipped_without_modification(self):
        official = {
            "name": "NVDA earnings", "category": "Earnings", "start_utc": "2026-11-18T05:00:00Z",
            "source_timezone": "America/New_York", "source_url": "https://investor.nvidia.com/",
            "provider": "company_ir", "source_uid": "nvda-q3-2026", "status": "Confirmed",
            "related_symbols": ["NVDA"], "all_day": True, "event_date": "2026-11-18",
        }
        upsert_market_events([official], self.database)
        result = save_quick_earnings_events(
            [{"ticker": "NVDA", "date": "2026-11-18", "session": "TBD"}],
            original_input="NVDA 11/18", database_path=self.database,
        )
        self.assertEqual(result["skipped"], 1)
        event = list_market_events(self.database)[0]
        self.assertEqual(event["provider"], "company_ir")
        self.assertEqual(event["status"], "Confirmed")
        self.assertEqual(event["source_url"], "https://investor.nvidia.com/")

    def test_distinct_dates_for_the_same_ticker_are_saved_as_separate_events(self):
        result = save_quick_earnings_events(
            [
                {"ticker": "HOOD", "date": "2026-10-27", "session": "AMC"},
                {"ticker": "HOOD", "date": "2027-01-28", "session": "TBD"},
            ], original_input="HOOD 10/27 AMC HOOD 01/28/2027", database_path=self.database,
        )
        self.assertEqual(result["added"], 2)
        events = [event for event in list_market_events(self.database) if event["related_symbols"] == ["HOOD"]]
        self.assertEqual({event["event_date"] for event in events}, {"2026-10-27", "2027-01-28"})

    def test_invalid_edited_rows_are_rejected_and_duplicate_saved_dates_count_as_skipped(self):
        result = save_quick_earnings_events(
            [
                {"ticker": "AAPL", "date": "2026-10-28", "session": "TBD"},
                {"ticker": "AAPL", "date": "2026-10-28", "session": "BMO"},
                {"ticker": "bad ticker", "date": "2026-10-29", "session": "TBD"},
            ], original_input="AAPL 10/28", database_path=self.database,
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["rejected"], 1)


if __name__ == "__main__":
    unittest.main()

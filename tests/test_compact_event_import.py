import tempfile
import unittest
from datetime import date
from pathlib import Path
import pandas as pd

from dashboard.compact_event_import import (
    apply_compact_preview_edits, build_compact_discovery_prompt,
    parse_compact_events, register_event_type,
)
from dashboard.data_access import list_market_events, save_manual_market_event
from dashboard.database import connect, initialize_database
from dashboard.event_import import approve_event_import, preview_event_import


class CompactEventImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "events.sqlite"
        initialize_database(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def parse(self, text, start="2026-11-01", end="2026-11-30"):
        return parse_compact_events(text, window_start=start, window_end=end)

    def test_three_piece_syntax_many_symbols_and_registry(self):
        parsed = self.parse("AAPL 11/02 AMC, BRK.B 11/03 BMO\nDE 11/25 TBD\nJOLTS 11/03 10:00\nTREASURY_FINANCING 11/02 15:00")
        self.assertEqual(parsed["invalid_lines"], [])
        self.assertEqual([event["ticker"] for event in parsed["events"][:3]], ["AAPL", "BRK.B", "DE"])
        self.assertEqual([event["earnings_session"] for event in parsed["events"][:3]], ["AMC", "BMO", "TBD"])
        self.assertEqual(parsed["events"][3]["start_utc"], "2026-11-03T15:00:00Z")
        self.assertEqual(parsed["events"][4]["start_utc"], "2026-11-02T20:00:00Z")
        self.assertTrue(all(event["status"] == "Unverified" for event in parsed["events"]))

    def test_arbitrary_valid_tickers_and_configurable_registry(self):
        register_event_type("FUNDING_DEADLINE", "Political/Other", "Funding deadline")
        parsed = self.parse("LONGTICKER 11/04 AMC\nFUNDING_DEADLINE 11/05 12:00")
        self.assertEqual(parsed["invalid_lines"], [])
        self.assertEqual(parsed["events"][0]["ticker"], "LONGTICKER")
        self.assertEqual(parsed["events"][1]["category"], "Political/Other")

    def test_explicit_formats_year_inference_dst_and_optional_et_url_metadata(self):
        parsed = self.parse("BRK-B 2026-11-02 BMO URL=https://example.com/news,archive\nCPI 11/03/2026 08:30 ET\nMETA 11/15/26 AMC")
        self.assertEqual(parsed["invalid_lines"], [])
        self.assertEqual(parsed["events"][0]["source_url"], "https://example.com/news,archive")
        self.assertEqual(parsed["events"][1]["start_utc"], "2026-11-03T13:30:00Z")
        self.assertEqual(parsed["events"][2]["start_date"], "2026-11-15")
        self.assertIn("daylight-saving", self.parse("CPI 03/08/2026 02:30", "2026-03-01", "2026-03-31")["invalid_lines"][0]["error"].lower())

    def test_copy_paste_artifacts_and_original_input_are_preserved(self):
        parsed = self.parse("```text\n- AAPL 11/02 AMC, DE 11/25 BMO\\n\n```\nCPI 11/10 08:30")
        self.assertEqual([row["ticker"] for row in parsed["events"]], ["AAPL", "DE", ""])
        self.assertEqual(parsed["events"][1]["original_input"], "DE 11/25 BMO")

    def test_impossible_ambiguous_bad_type_and_bad_timing_are_actionable(self):
        parsed = self.parse("CPI 02/30 08:30\nAAPL 11/31 AMC\nFOMC 11/04 AMC\nAAPL 11/06 08:30")
        self.assertEqual(len(parsed["events"]), 0)
        self.assertEqual(len(parsed["invalid_lines"]), 4)
        self.assertIn("impossible calendar date", parsed["invalid_lines"][0]["error"])
        self.assertIn("scheduled event", parsed["invalid_lines"][2]["error"])
        self.assertIn("earnings timing", parsed["invalid_lines"][3]["error"])
        unknown = self.parse("TREASURY_3Y_AUCTION 11/12 TBD")
        self.assertEqual(unknown["events"][0]["category"], "Macro")
        self.assertEqual(unknown["events"][0]["name"], "3-year Treasury auction")
        new_identifier = self.parse("UNLISTED_ECONOMIC_RELEASE 11/13 08:30")
        self.assertEqual(new_identifier["invalid_lines"], [])
        self.assertEqual(new_identifier["events"][0]["category"], "Macro")
        broad = self.parse("CPI 11/10 08:30", start="2026-11-10", end="2027-11-10")
        self.assertIn("ambiguous", broad["invalid_lines"][0]["error"])

    def test_approved_import_is_idempotent_and_manual_dates_are_protected(self):
        parsed = self.parse("AAPL 11/02 AMC")
        preview = preview_event_import({"schema_version": 1, "events": parsed["events"]}, database_path=self.db)
        first = approve_event_import(preview, original_json="AAPL 11/02 AMC", database_path=self.db)
        self.assertEqual(first["inserted"], 1)
        with connect(self.db) as connection:
            saved_input = connection.execute("SELECT original_json FROM market_event_import_batches WHERE batch_id=?", (first["batch_id"],)).fetchone()[0]
        self.assertEqual(saved_input, "AAPL 11/02 AMC")
        duplicate = preview_event_import({"schema_version": 1, "events": parsed["events"]}, database_path=self.db)
        again = approve_event_import(duplicate, original_json="AAPL 11/02 AMC", database_path=self.db)
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(len(list_market_events(self.db)), 1)
        save_manual_market_event({"symbol": "AAPL", "date": "2026-11-03", "earnings_session": "BMO",
            "source_url": "", "status": "Confirmed", "notes": "User-approved date", "original_input": "manual"}, self.db)
        conflict = self.parse("AAPL 11/04 TBD")
        rows = preview_event_import({"schema_version": 1, "events": conflict["events"]}, database_path=self.db)
        self.assertEqual(rows[0]["classification"], "conflicting date")
        result = approve_event_import(rows, original_json="AAPL 11/04 TBD", database_path=self.db)
        self.assertEqual(result["conflicts"], 1)
        manual = next(event for event in list_market_events(self.db) if event["provider"] == "manual_earnings")
        self.assertEqual(manual["event_date"], "2026-11-03")

    def test_preview_edits_and_prompt_use_simple_lines_only(self):
        parsed = self.parse("CPI 11/02 TBD\nAAPL 11/03 BMO")
        preview = preview_event_import({"schema_version": 1, "events": parsed["events"]}, database_path=self.db)
        edited = apply_compact_preview_edits(preview, [
            {"Row": 1, "Import": True, "Event": "CPI release", "Date": "2026-11-04", "Timing": "TBD"},
            {"Row": 2, "Import": True, "Event": "AAPL earnings", "Date": "2026-11-05", "Timing": "AMC"},
        ], window_start="2026-11-01", window_end="2026-11-30")
        self.assertTrue(edited[0]["event"]["all_day"])
        self.assertEqual(edited[1]["event"]["earnings_session"], "AMC")
        prompt = build_compact_discovery_prompt("2026-11-01", "2026-11-30", coverage="Holdings/watchlist + macro", tickers=["MCD"])
        self.assertNotIn("MCD", prompt)
        self.assertIn("authorized and available", prompt)
        self.assertIn("does not invoke Robinhood MCP", prompt)
        self.assertIn("EVENT DATE TIME", prompt)
        self.assertIn("Return ONLY", prompt)
        self.assertIn("No JSON", prompt)
        self.assertNotIn("URL=", prompt)
        self.assertNotIn("EARN AAPL", prompt)

    def test_streamlit_editor_nat_crash_is_reproduced_as_row_validation(self):
        parsed = self.parse("AAPL 11/02 AMC")
        preview = preview_event_import({"schema_version": 1, "events": parsed["events"]}, database_path=self.db)
        # This is the actual pandas data_editor shape when a required Date cell is cleared.
        edited = pd.DataFrame([{"Row": 1, "Import": True, "Override conflicting date": False,
                                "Event": "AAPL earnings", "Date": pd.NaT, "Timing": "AMC"}])
        with self.assertRaisesRegex(ValueError, r"Row 1: date is blank"):
            apply_compact_preview_edits(preview, edited, window_start="2026-11-01", window_end="2026-11-30")

    def test_editor_row_ids_survive_sort_and_missing_checkbox_fails_closed(self):
        parsed = self.parse("AAPL 11/02 AMC\nMSFT 11/03 BMO")
        preview = preview_event_import({"schema_version": 1, "events": parsed["events"]}, database_path=self.db)
        edited = pd.DataFrame([
            {"Row": 2, "Import": True, "Override conflicting date": False, "Event": "MSFT earnings", "Date": pd.Timestamp("2026-11-03"), "Timing": "BMO"},
            {"Row": 1, "Import": pd.NA, "Override conflicting date": False, "Event": "AAPL earnings", "Date": pd.Timestamp("2026-11-02"), "Timing": "AMC"},
        ])
        result = apply_compact_preview_edits(preview, edited, window_start="2026-11-01", window_end="2026-11-30")
        self.assertFalse(result[0]["include"])
        self.assertTrue(result[1]["include"])
        self.assertEqual(result[1]["event"]["ticker"], "MSFT")

    def test_invalid_selected_batch_is_rejected_before_any_import_or_batch_record(self):
        parsed = self.parse("AAPL 11/02 AMC\nMSFT 11/03 BMO")
        preview = preview_event_import({"schema_version": 1, "events": parsed["events"]}, database_path=self.db)
        valid = {**preview[0], "include": True}
        invalid = {**preview[1], "include": True, "event": {**preview[1]["event"], "start_utc": "not-a-date"}}
        with self.assertRaisesRegex(ValueError, "Row 2"):
            approve_event_import([valid, invalid], original_json="original", database_path=self.db)
        self.assertEqual(list_market_events(self.db), [])
        with connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM market_event_import_batches").fetchone()[0], 0)

    def test_excluded_invalid_row_does_not_block_valid_row(self):
        parsed = self.parse("AAPL 11/02 AMC\nMSFT 11/03 BMO")
        preview = preview_event_import({"schema_version": 1, "events": parsed["events"]}, database_path=self.db)
        edited = [
            {"Row": 1, "Import": True, "Override conflicting date": False, "Event": "AAPL earnings", "Date": "2026-11-02", "Timing": "AMC"},
            {"Row": 2, "Import": False, "Override conflicting date": False, "Event": "MSFT earnings", "Date": pd.NaT, "Timing": "BMO"},
        ]
        rows = apply_compact_preview_edits(preview, edited, window_start="2026-11-01", window_end="2026-11-30")
        saved = approve_event_import(rows, original_json="AAPL 11/02 AMC\nMSFT 11/03 BMO", database_path=self.db)
        self.assertEqual(saved["inserted"], 1)
        self.assertEqual(len(list_market_events(self.db)), 1)


if __name__ == "__main__":
    unittest.main()

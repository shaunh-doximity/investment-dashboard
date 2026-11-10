import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashboard.data_access import list_market_events, upsert_market_events
from dashboard.database import connect, initialize_database
from dashboard.event_import import (
    approve_event_import, build_gpt_discovery_prompt, event_import_template,
    normalize_event_import, preview_event_import, rollback_event_import,
)


class EventImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "events.sqlite"
        initialize_database(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_round_trip_and_earnings_session_is_not_exact_time(self):
        normalized = normalize_event_import(json.dumps(event_import_template()))
        event = normalized["events"][0]
        self.assertEqual(event["earnings_session"], "TBD")
        self.assertTrue(event["all_day"])
        self.assertIsNone(event["start_time"])
        self.assertEqual(event["start_utc"], "2027-01-20T05:00:00Z")

    def test_timezone_conversion_exact_time_and_date_only(self):
        payload = {"schema_version": 1, "events": [
            {"category":"Fed", "name":"Decision", "date":"2026-11-02", "time":"14:00", "timezone":"America/New_York"},
            {"category":"Macro", "name":"CPI", "date":"2026-11-03", "timezone":"America/New_York"},
        ]}
        events = normalize_event_import(json.dumps(payload))["events"]
        self.assertEqual(events[0]["start_utc"], "2026-11-02T19:00:00Z")
        self.assertFalse(events[0]["all_day"])
        self.assertTrue(events[1]["all_day"])

    def test_invalid_json_schema_date_and_url_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            normalize_event_import("{")
        with self.assertRaisesRegex(ValueError, "schema_version"):
            normalize_event_import('{"schema_version":2,"events":[]}')
        bad_date = {"schema_version":1,"events":[{"category":"Macro","name":"CPI","date":"2026-02-30"}]}
        with self.assertRaisesRegex(ValueError, "invalid calendar date"):
            normalize_event_import(json.dumps(bad_date))
        bad_url = {"schema_version":1,"events":[{"category":"Macro","name":"CPI","date":"2026-11-03","url":"not-url"}]}
        with self.assertRaisesRegex(ValueError, "complete http"):
            normalize_event_import(json.dumps(bad_url))

    def test_prompt_uses_31_day_max_query_chunks_and_user_tickers_only(self):
        prompt = build_gpt_discovery_prompt("2026-11-01", "2026-12-15", ["mcd", "BRK.B"])
        self.assertIn("get_earnings_calendar", prompt)
        self.assertIn("get_earnings_results", prompt)
        self.assertIn("2026-11-01 through 2026-12-01", prompt)
        self.assertIn("2026-12-02 through 2026-12-15", prompt)
        self.assertIn("MCD, BRK.B", prompt)
        self.assertNotIn("cash", prompt.lower())

    def test_preview_and_idempotent_approval(self):
        raw = json.dumps({"schema_version":1,"events":[{"category":"Earnings","name":"MCD earnings","ticker":"MCD","date":"2026-11-20","earnings_session":"AMC","source_url":"https://investor.example.com"}]})
        normalized = normalize_event_import(raw)
        preview = preview_event_import(normalized, database_path=self.db)
        first = approve_event_import(preview, original_json=raw, database_path=self.db)
        self.assertEqual(first["inserted"], 1)
        saved = list_market_events(self.db)
        self.assertEqual(saved[0]["status"], "Unverified")
        self.assertEqual(saved[0]["earnings_session"], "AMC")
        self.assertEqual(saved[0]["source_verification"], "GPT import · Unverified")
        again = preview_event_import(normalized, database_path=self.db)
        self.assertEqual(again[0]["classification"], "unchanged duplicate")
        second = approve_event_import(again, original_json=raw, database_path=self.db)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(len(list_market_events(self.db)), 1)

    def test_official_date_conflict_is_preserved_even_when_override_is_checked(self):
        upsert_market_events([{"name":"CPI release","category":"Macro","start_utc":"2026-11-05T12:30:00Z","source_timezone":"America/New_York","provider":"bls_ical","source_uid":"bls-cpi","source_url":"https://bls.gov","status":"Confirmed","related_symbols":[],"all_day":False,"source_verification":"Official source"}], self.db)
        raw = json.dumps({"schema_version":1,"events":[{"category":"Macro","name":"CPI release","date":"2026-11-06","source_url":"https://bls.gov/schedule"}]})
        normalized = normalize_event_import(raw)
        preview = preview_event_import(normalized, database_path=self.db)
        self.assertEqual(preview[0]["classification"], "conflicting date")
        preview[0]["include"] = True
        rejected = approve_event_import(preview, original_json=raw, database_path=self.db)
        self.assertEqual(rejected["conflicts"], 1)
        preview[0]["override_conflict"] = True
        approved = approve_event_import(preview, original_json=raw, database_path=self.db)
        self.assertEqual(approved["conflicts"], 1)
        saved = list_market_events(self.db)
        self.assertEqual(saved[0]["start_utc"], "2026-11-05T12:30:00Z")
        self.assertEqual(saved[0]["provider"], "bls_ical")

    def test_rollback_of_created_event_cancels_without_deleting_audit(self):
        raw = json.dumps({"schema_version":1,"events":[{"category":"Macro","name":"GDP release","date":"2026-11-10"}]})
        preview = preview_event_import(normalize_event_import(raw), database_path=self.db)
        result = approve_event_import(preview, original_json=raw, database_path=self.db)
        self.assertEqual(rollback_event_import(result["batch_id"], database_path=self.db)["rolled_back"], 1)
        self.assertEqual(list_market_events(self.db), [])
        self.assertEqual(len(list_market_events(self.db, include_cancelled=True)), 1)

    def test_rollback_leaves_rows_modified_after_the_import_untouched(self):
        raw = json.dumps({"schema_version":1,"events":[{"category":"Macro","name":"Retail Sales","date":"2026-11-18"}]})
        preview = preview_event_import(normalize_event_import(raw), database_path=self.db)
        result = approve_event_import(preview, original_json=raw, database_path=self.db)
        with connect(self.db) as connection:
            connection.execute("UPDATE market_events SET notes='Later reviewed by user' WHERE import_batch_id=?", (result["batch_id"],))
        outcome = rollback_event_import(result["batch_id"], database_path=self.db)
        self.assertEqual(outcome, {"rolled_back": 0, "conflicts": 1})
        self.assertEqual(list_market_events(self.db)[0]["notes"], "Later reviewed by user")

    def test_source_identifier_detects_reschedule_and_requires_explicit_override(self):
        original = {"schema_version":1,"events":[{"category":"Fed","name":"FOMC policy decision","date":"2026-12-16","source_provider":"Federal Reserve","source_id":"meeting-2026-12","source_url":"https://federalreserve.gov"}]}
        first_json = json.dumps(original)
        first_preview = preview_event_import(normalize_event_import(first_json), database_path=self.db)
        first = approve_event_import(first_preview, original_json=first_json, database_path=self.db)
        updated = {"schema_version":1,"events":[{"category":"Fed","name":"FOMC policy decision","date":"2026-12-17","source_provider":"Federal Reserve","source_id":"meeting-2026-12","source_url":"https://federalreserve.gov"}]}
        updated_json = json.dumps(updated)
        preview = preview_event_import(normalize_event_import(updated_json), database_path=self.db)
        self.assertEqual(preview[0]["classification"], "conflicting date")
        preview[0]["override_conflict"] = True
        result = approve_event_import(preview, original_json=updated_json, database_path=self.db)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(list_market_events(self.db)[0]["event_date"], "2026-12-17")
        self.assertEqual(rollback_event_import(result["batch_id"], database_path=self.db)["rolled_back"], 1)
        self.assertEqual(list_market_events(self.db)[0]["event_date"], "2026-12-16")

    def test_cancellation_claim_cannot_cancel_official_event(self):
        upsert_market_events([{"name":"CPI release","category":"Macro","start_utc":"2026-11-05T12:30:00Z","source_timezone":"America/New_York","provider":"bls_ical","source_uid":"cpi-2026-11","source_url":"https://bls.gov/schedule","status":"Confirmed","related_symbols":[],"all_day":False,"source_verification":"Official source"}], self.db)
        raw = json.dumps({"schema_version":1,"events":[{"category":"Macro","name":"CPI release","date":"2026-11-05","status":"cancelled","source_url":"https://bls.gov/schedule"}]})
        preview = preview_event_import(normalize_event_import(raw), database_path=self.db)
        self.assertEqual(preview[0]["classification"], "cancellation claim")
        preview[0]["include"] = True
        rejected = approve_event_import(preview, original_json=raw, database_path=self.db)
        self.assertEqual(rejected["conflicts"], 1)
        preview[0]["override_conflict"] = True
        accepted = approve_event_import(preview, original_json=raw, database_path=self.db)
        self.assertEqual(accepted["conflicts"], 1)
        self.assertEqual(list_market_events(self.db)[0]["status"], "Confirmed")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
import requests

from dashboard.database import initialize_database
from dashboard.data_access import list_treasury_yields, upsert_treasury_yields
from dashboard.treasury_yields import ENDPOINT, PROVIDER, parse_treasury_xml, refresh_treasury_yields, treasury_freshness
from dashboard.treasury_analytics import treasury_event_study, treasury_window_summary


XML = '''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><content><m:properties xmlns:m="x"><d:NEW_DATE xmlns:d="x">2026-10-29T00:00:00</d:NEW_DATE><d:BC_2YEAR xmlns:d="x">3.75</d:BC_2YEAR><d:BC_10YEAR xmlns:d="x">4.25</d:BC_10YEAR></m:properties></content></entry></feed>'''


class TreasuryYieldTests(unittest.TestCase):
    def test_parse_namespaced_feed_and_filter_future_or_invalid(self):
        rows = parse_treasury_xml(XML, through=date(2026, 10, 29), retrieved_at="2026-10-30T00:00:00Z")
        self.assertEqual(rows[0]["provider"], PROVIDER)
        self.assertEqual(rows[0]["two_year"], 3.75)
        self.assertEqual(rows[0]["ten_year"], 4.25)
        future = XML.replace("2026-10-29", "2026-10-30")
        self.assertEqual(parse_treasury_xml(future, through=date(2026, 10, 29)), [])
        with self.assertRaises(Exception):
            parse_treasury_xml("not xml")

    def test_spread_and_basis_point_changes(self):
        rows = [{"observation_date": "2026-10-28", "two_year": 3.70, "ten_year": 4.20},
                {"observation_date": "2026-10-29", "two_year": 3.75, "ten_year": 4.25}]
        spread_bp = (rows[-1]["ten_year"] - rows[-1]["two_year"]) * 100
        change_bp = (rows[-1]["ten_year"] - rows[0]["ten_year"]) * 100
        self.assertAlmostEqual(spread_bp, 50)
        self.assertAlmostEqual(change_bp, 5)

    def test_treasury_summary_exact_8bp_curve_change_example(self):
        rows = [{"observation_date": "2026-09-01", "two_year": 4.78, "ten_year": 5.24}]
        rows += [{"observation_date": f"2026-09-{day:02d}", "two_year": 4.78, "ten_year": 5.24} for day in range(2, 22)]
        rows.append({"observation_date": "2026-10-01", "two_year": 4.56, "ten_year": 4.94})
        summary = treasury_window_summary(rows, 20)
        self.assertAlmostEqual(summary["two_year_change_bp"], -22)
        self.assertAlmostEqual(summary["ten_year_change_bp"], -30)
        self.assertAlmostEqual(summary["spread_bp"], 38)
        self.assertAlmostEqual(summary["spread_change_bp"], -8)

    def test_summary_uses_matched_observations_and_rejects_misaligned_rows(self):
        rows = [
            {"observation_date":"2026-01-02", "two_year":4.0, "ten_year":4.5},
            {"observation_date":"2026-01-05", "two_year":3.9, "ten_year":4.4},
            {"observation_date":"2026-01-05", "two_year":3.8, "ten_year":None},
        ]
        summary = treasury_window_summary(rows, 5)
        self.assertEqual(summary["observations"], 2)
        self.assertEqual(summary["as_of"], "2026-01-05")
        self.assertIsNone(summary["spread_change_bp"])

    def test_event_study_reports_immediate_and_following_session_changes(self):
        rows = []
        from datetime import date, timedelta
        day = date(2026, 1, 1)
        for index in range(30):
            if day.weekday() < 5:
                rows.append({"observation_date":day.isoformat(), "two_year":4 + index*.01, "ten_year":4.5 + index*.02})
            day += timedelta(days=1)
        study = treasury_event_study(rows, "2026-01-08")
        self.assertEqual(study["status"], "available")
        self.assertLess(study["before_date"], "2026-01-08")
        self.assertGreater(study["after_date"], "2026-01-08")
        self.assertIsNotNone(study["forward_5"])

    def test_freshness_compares_to_completed_equity_session(self):
        rows = [{"observation_date": "2026-10-29"}]
        self.assertEqual(treasury_freshness(rows, completed_session="2026-10-29")["status"], "current")
        self.assertEqual(treasury_freshness(rows, completed_session="2026-10-30")["status"], "lagging")

    def test_refresh_uses_documented_feed_and_failure_preserves_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "rates.sqlite"
            initialize_database(db)
            upsert_treasury_yields([{"observation_date": "2026-10-29", "two_year": 3.75, "ten_year": 4.25, "retrieved_at": "2026-10-30T00:00:00Z", "provider": PROVIDER}], db)
            response = Mock(status_code=200, content=XML)
            response.raise_for_status.return_value = None
            with patch("dashboard.treasury_yields.requests.get", return_value=response) as get:
                result = refresh_treasury_yields(database_path=db, today=date(2026, 10, 31))
            self.assertEqual(get.call_args.args[0], ENDPOINT)
            self.assertIn("data", get.call_args.kwargs["params"])
            self.assertGreaterEqual(result["updated"], 1)
            with patch("dashboard.treasury_yields.requests.get", side_effect=requests.Timeout):
                # Use a distinct refresh date so the same-day cache guard does not mask the failure path.
                failed = refresh_treasury_yields(database_path=db, today=date(2026, 11, 3))
            self.assertTrue(any("timed out" in message for message in failed["errors"].values()))
            self.assertGreaterEqual(len(list_treasury_yields(database_path=db)), 1)

    def test_recent_treasury_request_timestamp_does_not_mark_old_observation_current(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "rates.sqlite"
            initialize_database(db)
            upsert_treasury_yields([{
                "observation_date": "2026-10-30", "two_year": 3.75, "ten_year": 4.25,
                "retrieved_at": "2026-11-03T15:00:00+00:00", "provider": PROVIDER,
            }], db)
            with patch("dashboard.treasury_yields.requests.get", side_effect=requests.Timeout) as get:
                result = refresh_treasury_yields(database_path=db, today=date(2026, 11, 2))
            self.assertTrue(get.called)
            self.assertTrue(any("timed out" in message for message in result["errors"].values()))
            self.assertEqual(list_treasury_yields(database_path=db)[-1]["observation_date"], "2026-10-30")


if __name__ == "__main__":
    unittest.main()

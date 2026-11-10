import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dashboard.database import initialize_database
from dashboard.recent_catalysts import load_recent_catalysts, save_recent_catalysts


class RecentCatalystTests(unittest.TestCase):
    def test_cache_is_dated_bounded_and_retains_provenance(self):
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        sources = [
            {"title":"Alphabet reports quarterly earnings and raises capex outlook", "snippet":"Capital expenditure rose.",
             "url":"https://abc.example/earnings", "date":"2026-10-30"},
            {"title":"Older market news", "url":"https://abc.example/old", "date":"2026-10-20"},
            {"title":"Undated report", "url":"https://abc.example/undated"},
        ]
        rows = save_recent_catalysts(sources, now=now)["records"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["ticker"], "GOOGL")
        self.assertEqual(row["event_date"], None)  # publication date is not mislabeled as event date
        self.assertEqual(row["publication_date"], "2026-10-30")
        self.assertEqual(row["source_verification"], "Retrieved source; not independently verified")
        self.assertTrue(row["source_url"].startswith("https://"))
        self.assertIn("technology", row["affected_segments_hypothesis"])
        expired = load_recent_catalysts(now=now + timedelta(days=8))
        self.assertEqual(expired["status"], "expired_or_empty")

    def test_cache_persists_in_sqlite(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "market.db"
            initialize_database(db)
            now = datetime(2026, 11, 2, tzinfo=timezone.utc)
            save_recent_catalysts([{"title":"FOMC statement", "url":"https://fed.example/x", "date":"2026-11-01"}],
                                  now=now, database_path=db)
            loaded = load_recent_catalysts(now=now, database_path=db)
            self.assertEqual(loaded["status"], "available")
            self.assertEqual(loaded["records"][0]["category"], "Macro")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
import requests

from dashboard.data_access import list_macro_series_observations
from dashboard.database import initialize_database
from dashboard.eia_wti import (PROVIDER, SERIES_ID, parse_eia_wti_response,
                               refresh_wti_spot, wti_window_change)


class EiaWtiTests(unittest.TestCase):
    def test_eia_spot_values_are_parsed_as_usd_per_barrel_with_dates(self):
        result = parse_eia_wti_response({"response":{"data":[
            {"period":"2026-10-30","value":"60.25"}, {"period":"2026-10-29","value":"59.50"},
            {"period":"2026-10-28","value":"bad"}] }}, retrieved_at="2026-11-02T00:00:00Z")
        self.assertEqual([row["observation_date"] for row in result], ["2026-10-29", "2026-10-30"])
        self.assertEqual(result[-1]["series_id"], SERIES_ID)
        self.assertEqual(result[-1]["provider"], PROVIDER)
        self.assertEqual(result[-1]["units"], "USD per barrel")
        self.assertTrue(result[-1]["initial_release_only"])

    def test_eia_refresh_persists_only_success_and_failure_keeps_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "market.db"
            initialize_database(db)
            response = Mock(ok=True, status_code=200)
            response.json.return_value = {"response":{"data":[{"period":"2026-10-30","value":"61.0"}]}}
            with patch("dashboard.eia_wti._api_key", return_value="placeholder"), patch("dashboard.eia_wti.requests.get", return_value=response):
                self.assertEqual(refresh_wti_spot(database_path=db)["status"], "success")
            cached = list_macro_series_observations([SERIES_ID], PROVIDER, db)[SERIES_ID]
            self.assertEqual(len(cached), 1)
            with patch("dashboard.eia_wti._api_key", return_value="placeholder"), patch("dashboard.eia_wti.requests.get", side_effect=requests.Timeout):
                failure = refresh_wti_spot(database_path=db)
            self.assertEqual(failure["status"], "timeout")
            self.assertEqual(list_macro_series_observations([SERIES_ID], PROVIDER, db)[SERIES_ID], cached)

    def test_window_changes_use_observation_count_not_etf_sessions(self):
        rows = [{"observation_date": (date(2026,10,1)+timedelta(days=i)).isoformat(), "value": 50+i}
                for i in range(21)]
        result = wti_window_change(rows, 5)
        self.assertEqual(result["status"], "available")
        self.assertAlmostEqual(result["change_percent"], (70/65-1)*100)
        self.assertEqual(wti_window_change(rows[:3], 5)["status"], "insufficient_history")


if __name__ == "__main__":
    unittest.main()

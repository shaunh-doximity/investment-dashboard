import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard.cboe_volatility import PROVIDER as CBOE
from dashboard.database import initialize_database
from dashboard.data_access import list_volatility_observations, upsert_volatility_observations
from dashboard.vix_sources import YFINANCE_PROVIDER, parse_yfinance_vix, refresh_vix_sources, select_vix_series


def row(day, provider, close=18.0):
    return {"symbol": "VIX", "observation_date": day, "close": close, "provider": provider, "retrieved_at": day + "T22:00:00+00:00"}


class VixSourceTests(unittest.TestCase):
    def test_cboe_preferred_on_same_date_and_fresher_fallback_not_replaced(self):
        cboe = {"VIX": [row("2026-10-22", CBOE)]}
        yahoo = {"VIX": [row("2026-10-22", YFINANCE_PROVIDER, 19), row("2026-10-23", YFINANCE_PROVIDER, 20)]}
        selected = select_vix_series(completed_session="2026-10-23", cboe=cboe, yahoo=yahoo)
        self.assertTrue(selected["current_available"])
        self.assertEqual(selected["selected"]["provider"], YFINANCE_PROVIDER)
        same_day = select_vix_series(completed_session="2026-10-22", cboe=cboe, yahoo=yahoo)
        self.assertEqual(same_day["selected"]["provider"], CBOE)

    def test_fred_is_historical_reference_never_current(self):
        selected = select_vix_series(completed_session="2026-10-23", fred=[row("2026-10-23", "fred_vixcls")])
        self.assertFalse(selected["current_available"])
        self.assertEqual(selected["historical_reference"]["provider"], "fred_vixcls")

    def test_stale_source_is_not_current(self):
        selected = select_vix_series(completed_session="2026-10-23", cboe={"VIX": [row("2026-10-22", CBOE)]})
        self.assertFalse(selected["current_available"])
        self.assertIsNone(selected["selected"])

    def test_yfinance_parser_validates_daily_close_without_adjustment(self):
        import pandas as pd
        frame = pd.DataFrame({"Close": [20.0, None]}, index=pd.to_datetime(["2026-10-22", "2026-10-23"]))
        parsed = parse_yfinance_vix(frame, through=date(2026, 10, 23))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["provider"], YFINANCE_PROVIDER)
        self.assertEqual(parsed[0]["close"], 20.0)

    def test_yahoo_fallback_is_only_called_after_explicit_opt_in_and_failures_keep_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "market.sqlite"
            initialize_database(db)
            upsert_volatility_observations([row("2026-10-22", CBOE)], db)
            with patch("dashboard.vix_sources.refresh_cboe_volatility", return_value={"updated": {}, "errors": {}}), \
                 patch("dashboard.vix_sources.load_cboe_volatility", return_value={"VIX": [row("2026-10-22", CBOE)]}), \
                 patch("dashboard.vix_sources.fetch_yfinance_vix") as yahoo:
                refresh_vix_sources(completed_session="2026-10-23", allow_yfinance=False, database_path=db)
                yahoo.assert_not_called()
                yahoo.side_effect = RuntimeError("offline")
                result = refresh_vix_sources(completed_session="2026-10-23", allow_yfinance=True, database_path=db)
            self.assertIn("yfinance", result["errors"])
            self.assertEqual(result["latest_cached"], {"source": CBOE, "date": "2026-10-22"})
            yahoo.assert_called_with(through=date(2026, 10, 23))
            self.assertEqual(list_volatility_observations(("VIX",), CBOE, db)["VIX"][0]["observation_date"], "2026-10-22")


if __name__ == "__main__":
    unittest.main()

import copy
import unittest
from datetime import date, timedelta

from dashboard.portfolio_risk import (
    aggregate_downside_to_stops, aligned_daily_return_matrix,
    correlation_matrix, portfolio_risk_analysis, position_reward_risk,
    sector_exposure,
)
from dashboard.market_data import quote_is_stale


def history_from_returns(returns, start=date(2025, 1, 1)):
    bars = [{"trading_date": start.isoformat(), "close": 100.0}]
    close = 100.0
    day = start
    for daily_return in returns:
        close *= 1.0 + daily_return
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append({"trading_date": day.isoformat(), "close": close})
    return bars


class PortfolioRiskTests(unittest.TestCase):
    def test_daily_returns_align_to_common_observation_dates_without_filling(self):
        spy = history_from_returns([.01, -.01, .02, -.02])
        asset = [row for row in history_from_returns([.02, -.02, .04, -.04]) if row["trading_date"] != spy[2]["trading_date"]]
        matrix = aligned_daily_return_matrix({"SPY": spy, "ABC": asset}, ["SPY", "ABC"], 10)
        self.assertEqual(matrix["available_observations"], 3)
        self.assertEqual(matrix["dates"], [spy[1]["trading_date"], spy[3]["trading_date"], spy[4]["trading_date"]])
        # The return spanning the omitted close uses the same endpoints for both assets.
        self.assertAlmostEqual(matrix["returns"]["ABC"][1], asset[2]["close"] / asset[1]["close"] - 1)
        self.assertEqual(matrix["required_observations"], 10)

    def test_beta_volatility_and_zero_return_cash_treatment(self):
        spy_returns = [.01, -.01, .02, -.02, .015, -.015]
        histories = {"SPY": history_from_returns(spy_returns), "ABC": history_from_returns([2 * value for value in spy_returns])}
        holdings = [{"symbol": "ABC", "shares": 1, "average_cost": 90}]
        quotes = {"ABC": {"price": 100}}
        result = portfolio_risk_analysis(holdings, quotes, cash=100, histories=histories, lookback=6)
        self.assertEqual(result["status"], "available")
        self.assertAlmostEqual(result["invested_equity_beta"], 2.0)
        self.assertAlmostEqual(result["portfolio_beta"], 1.0)
        self.assertAlmostEqual(result["cash_weight"], .5)
        self.assertAlmostEqual(result["annualized_volatility"], result["invested_equity_annualized_volatility"] * .5)
        self.assertAlmostEqual(result["risk_contributions"]["ABC"]["fraction_of_portfolio_volatility"], 1.0)

    def test_insufficient_history_is_explicit_and_cached_quote_is_not_required(self):
        holdings = [{"symbol": "ABC", "shares": 2, "average_cost": 10}]
        short = {"SPY": history_from_returns([.01]), "ABC": history_from_returns([.02])}
        insufficient = portfolio_risk_analysis(holdings, {"ABC": {"price": 11}}, 0, short, lookback=5)
        self.assertEqual(insufficient["status"], "insufficient_history")
        enough = {"SPY": history_from_returns([.01, -.01, .02]),
                  "ABC": history_from_returns([.02, -.02, .04])}
        no_quote = portfolio_risk_analysis(holdings, {}, 0, enough, lookback=2)
        self.assertEqual(no_quote["status"], "available")
        self.assertEqual(no_quote["quote_covered_positions"], 0)
        self.assertAlmostEqual(no_quote["invested_equity_beta"], 2.0)
        self.assertTrue(no_quote["weight_price_sources"]["ABC"].startswith("Tiingo adjusted close"))

    def test_missing_history_produces_labeled_subset_estimate_not_zero_returns(self):
        holdings = [{"symbol": "AAA", "shares": 2}, {"symbol": "BBB", "shares": 3}]
        histories = {"SPY": history_from_returns([.01, -.01, .02, -.02]),
                     "AAA": history_from_returns([.02, -.02, .04, -.04])}
        result = portfolio_risk_analysis(holdings, {}, 100, histories, lookback=4)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["covered_positions"], 1)
        self.assertEqual(result["missing_history_symbols"], ["BBB"])
        self.assertNotIn("BBB", result["capital_weights"])
        self.assertEqual(result["observations"], 4)

    def test_cash_only_portfolio_has_zero_modeled_risk(self):
        result = portfolio_risk_analysis([], {}, 500, {}, lookback=252)
        self.assertEqual(result["status"], "cash_only")
        self.assertEqual(result["portfolio_beta"], 0.0)
        self.assertEqual(result["annualized_volatility"], 0.0)
        self.assertIsNone(result["invested_equity_beta"])

    def test_sector_exposure_totals_cash_and_unclassified_holdings(self):
        holdings = [
            {"symbol": "AAA", "shares": 2, "average_cost": 8},
            {"symbol": "BBB", "shares": 1, "average_cost": 20},
        ]
        quotes = {"AAA": {"price": 10}, "BBB": {"price": 30}}
        exposure = sector_exposure(holdings, quotes, 50, {"AAA": "Technology"})
        by_name = {row["category"]: row for row in exposure["rows"]}
        self.assertAlmostEqual(by_name["Technology"]["value"], 20)
        self.assertAlmostEqual(by_name["Unclassified / diversified"]["value"], 30)
        self.assertAlmostEqual(by_name["Cash"]["value"], 50)
        self.assertAlmostEqual(sum(row["pct_total_portfolio"] for row in exposure["rows"]), 1.0)
        self.assertAlmostEqual(by_name["Technology"]["pct_invested_equity"], .4)
        self.assertIsNone(by_name["Cash"]["pct_invested_equity"])

    def test_partial_sector_valuation_reports_missing_and_no_total_percentages(self):
        holdings = [
            {"symbol": "AAA", "shares": 2, "average_cost": 8},
            {"symbol": "BBB", "shares": 1, "average_cost": 20},
        ]
        exposure = sector_exposure(holdings, {"AAA": {"price": 10}}, 50, {})
        self.assertEqual(exposure["missing_symbols"], ["BBB"])
        self.assertTrue(all(row["pct_total_portfolio"] is None for row in exposure["rows"]))
        self.assertEqual(exposure["priced_position_count"], 1)

    def test_sector_exposure_reports_verified_classification_coverage(self):
        holdings = [{"symbol": "AAA", "shares": 2, "average_cost": 8},
                    {"symbol": "BBB", "shares": 1, "average_cost": 20}]
        quotes = {"AAA": {"price": 10}, "BBB": {"price": 30}}
        classifications = {
            "AAA": {"sector": "Technology - XLK", "sector_benchmark": "XLK", "verification_status": "Verified"},
            "BBB": {"sector": "Diversified / Nasdaq-100", "verification_status": "Unverified"},
        }
        result = sector_exposure(holdings, quotes, 50, classifications)
        self.assertAlmostEqual(result["verified_sector_classification_coverage"], 20 / 50)
        rows = {row["category"]: row for row in result["rows"]}
        self.assertAlmostEqual(rows["Technology - XLK"]["pct_total_portfolio"], 20 / 100)
        self.assertAlmostEqual(rows["Diversified / Nasdaq-100"]["pct_invested_equity"], 30 / 50)

    def test_position_rrr_dollar_scenarios_and_crossed_levels(self):
        valid = position_reward_risk(shares=10, entry_price=100, current_price=110, target_price=130, stop_price=90)
        self.assertEqual(valid["entry_rrr"], 3.0)
        self.assertEqual(valid["remaining_rrr"], 1.0)
        self.assertEqual(valid["potential_upside_dollars"], 200)
        self.assertEqual(valid["potential_downside_to_stop_dollars"], 200)
        stopped = position_reward_risk(shares=10, entry_price=100, current_price=89, target_price=130, stop_price=90)
        self.assertEqual(stopped["status"], "stop_crossed")
        self.assertIsNone(stopped["remaining_rrr"])
        targeted = position_reward_risk(shares=10, entry_price=100, current_price=130, target_price=130, stop_price=90)
        self.assertEqual(targeted["status"], "target_crossed")
        self.assertEqual(targeted["potential_upside_dollars"], 0)
        missing = position_reward_risk(shares=1, entry_price=100, current_price=None, target_price=130, stop_price=90)
        self.assertIsNone(missing["remaining_rrr"])

    def test_aggregate_stop_downside_has_coverage_and_crossed_status(self):
        holdings = [
            {"symbol": "AAA", "shares": 10}, {"symbol": "BBB", "shares": 2},
            {"symbol": "CCC", "shares": 1},
        ]
        quotes = {"AAA": {"price": 110}, "BBB": {"price": 80}}
        strategies = {"AAA": {"invalidation_price": 100}, "BBB": {"invalidation_price": 90}}
        result = aggregate_downside_to_stops(holdings, quotes, strategies)
        self.assertEqual(result["dollars"], 100)
        self.assertEqual(result["covered"], 1)
        self.assertEqual(result["crossed_symbols"], ["BBB"])
        self.assertEqual(result["missing_symbols"], ["CCC"])

    def test_correlation_uses_only_common_dates(self):
        histories = {
            "AAA": history_from_returns([.01, -.01, .02, -.02]),
            "BBB": history_from_returns([.02, -.02, .04, -.04]),
        }
        result = correlation_matrix(histories, ["AAA", "BBB"], 4)
        self.assertEqual(result["observations"], 4)
        self.assertAlmostEqual(result["correlations"]["AAA"]["BBB"], 1.0)

    def test_simulator_calculation_does_not_mutate_saved_position_inputs(self):
        holding = {"symbol": "ABC", "shares": 4, "average_cost": 100}
        quote = {"price": 110}
        before = copy.deepcopy((holding, quote))
        position_reward_risk(shares=8, entry_price=100, current_price=110, target_price=130, stop_price=90)
        self.assertEqual((holding, quote), before)

    def test_stale_and_missing_quote_dates_are_classified_as_stale(self):
        today = date(2025, 2, 1)
        self.assertTrue(quote_is_stale({"quote_date": "2025-01-20"}, today=today))
        self.assertTrue(quote_is_stale({"price": 10}, today=today))
        self.assertFalse(quote_is_stale({"quote_date": "2025-01-30"}, today=today))

    def test_streamlit_simulator_does_not_save_hypothetical_changes(self):
        try:
            from pathlib import Path
            from streamlit.testing.v1 import AppTest
            from unittest.mock import patch
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        holding = {"id": 1, "symbol": "AAPL", "shares": 4.0, "average_cost": 100.0,
                   "investment_horizon": "Medium", "thesis_status": "Active"}
        with patch("dashboard.pages.portfolio.list_holdings", return_value=[holding]), \
             patch("dashboard.pages.portfolio.list_cached_quotes", return_value={"AAPL": {"price": 110.0, "change": 1.0, "quote_date": "2025-01-30"}}), \
             patch("dashboard.pages.portfolio.get_cash_balance", return_value=500.0), \
             patch("dashboard.pages.portfolio.get_strategy_record", return_value={"target_price": 130.0, "invalidation_price": 90.0}), \
             patch("dashboard.pages.portfolio.list_portfolio_classification_records", return_value={}), \
             patch("dashboard.pages.portfolio.ensure_portfolio_classifications"), \
             patch("dashboard.pages.portfolio.load_history", side_effect=lambda *_args: history_from_returns([.002 if i % 2 == 0 else -.001 for i in range(260)])), \
             patch("dashboard.pages.portfolio.refresh_quotes") as refresh_quotes, \
             patch("dashboard.pages.portfolio.refresh_historical_data") as refresh_history, \
             patch("dashboard.pages.portfolio.save_holding") as save_holding:
            result = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
            result.session_state["investment_command_center_tab"] = "Portfolio"
            result.run()
            self.assertFalse(result.exception)
            # The simulation action remains local and does not save/requery.
            result.button(key="simulate_AAPL").click().run()
            self.assertFalse(result.exception)
            save_holding.assert_not_called()
            refresh_quotes.assert_not_called()
            refresh_history.assert_not_called()


if __name__ == "__main__":
    unittest.main()

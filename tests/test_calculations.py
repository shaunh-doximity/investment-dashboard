import unittest

from dashboard.calculations import holding_metrics, portfolio_totals


class PortfolioCalculationTests(unittest.TestCase):
    def setUp(self):
        self.holdings = [{"symbol": "MSFT", "shares": 4, "average_cost": 10}]
        self.quotes = {"MSFT": {"price": 12, "change": 0.5}}

    def test_holding_metrics_use_provider_quote(self):
        result = holding_metrics(self.holdings[0], self.quotes["MSFT"])
        self.assertEqual(result["cost_basis"], 40)
        self.assertEqual(result["market_value"], 48)
        self.assertEqual(result["unrealized_pl"], 8)
        self.assertEqual(result["daily_pl"], 2)

    def test_totals_include_cash_and_cash_allocation(self):
        result = portfolio_totals(self.holdings, self.quotes, cash=8)
        self.assertEqual(
            result["total_value"], 56
        )
        self.assertEqual(result["unrealized_pl"], 8)
        self.assertEqual(result["daily_pl"], 2)
        self.assertAlmostEqual(result["cash_allocation"], 8 / 56)

    def test_cash_allocation_is_cash_divided_by_complete_portfolio_value(self):
        holdings = [{"symbol": "ABC", "shares": 10, "average_cost": 80}]
        quotes = {"ABC": {"price": 90, "change": 1}}
        result = portfolio_totals(holdings, quotes, cash=100)
        self.assertEqual(result["total_value"], 1000)
        self.assertAlmostEqual(result["cash_allocation"], 0.10)

    def test_partial_valuation_does_not_report_cash_percentage(self):
        holdings = [
            {"symbol": "ABC", "shares": 10, "average_cost": 80},
            {"symbol": "XYZ", "shares": 2, "average_cost": 50},
        ]
        result = portfolio_totals(holdings, {"ABC": {"price": 90, "change": 1}}, cash=100)
        self.assertIsNone(result["total_value"])
        self.assertIsNone(result["cash_allocation"])
        self.assertEqual(result["missing_prices"], 1)

    def test_missing_quote_does_not_create_a_price_or_total(self):
        result = portfolio_totals(self.holdings, {}, cash=8)
        self.assertIsNone(result["total_value"])
        self.assertIsNone(result["unrealized_pl"])
        self.assertIsNone(result["cash_allocation"])
        self.assertEqual(result["missing_prices"], 1)

    def test_empty_portfolio_can_still_show_cash(self):
        result = portfolio_totals([], {}, cash=250)
        self.assertEqual(result["total_value"], 250)
        self.assertEqual(result["cash_allocation"], 1.0)


if __name__ == "__main__":
    unittest.main()

import unittest

from dashboard.market_data import MarketDataError, parse_global_quote


class AlphaVantageQuoteParsingTests(unittest.TestCase):
    def test_parses_documented_global_quote_fields(self):
        quote = parse_global_quote(
            {
                "Global Quote": {
                    "01. symbol": "SPY",
                    "05. price": "500.0000",
                    "07. latest trading day": "2025-01-02",
                    "09. change": "1.5000",
                    "10. change percent": "0.3000%",
                }
            },
            "SPY",
        )
        self.assertEqual(quote["symbol"], "SPY")
        self.assertEqual(quote["price"], 500.0)
        self.assertEqual(quote["change"], 1.5)
        self.assertEqual(quote["change_percent"], 0.3)
        self.assertEqual(quote["quote_date"], "2025-01-02")
        self.assertIn("not real-time", quote["freshness"])

    def test_rate_limit_is_reported_as_an_error(self):
        with self.assertRaisesRegex(MarketDataError, "request limit"):
            parse_global_quote({"Note": "API rate limit reached"}, "SPY")

    def test_missing_quote_is_not_filled_with_a_price(self):
        with self.assertRaisesRegex(MarketDataError, "did not return a quote"):
            parse_global_quote({"Global Quote": {}}, "SPY")


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import date, timedelta

from dashboard.analytics import (
    annualized_realized_volatility, beta_and_correlation, cumulative_returns,
    daily_returns, maximum_drawdown, momentum, moving_average, relative_performance,
    rsi14, sector_performance_rankings, sma20, sma50, sma200,
)


def bars_from_closes(closes):
    day = date(2024, 1, 1)
    rows = []
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        rows.append({"trading_date": day.isoformat(), "close": close})
        day += timedelta(days=1)
    return rows


class AnalyticsTests(unittest.TestCase):
    def test_daily_and_cumulative_returns(self):
        bars = bars_from_closes([100, 110, 99])
        returns = daily_returns(bars)
        self.assertAlmostEqual(returns[0]["return"], 0.1)
        cumulative = cumulative_returns(bars)
        self.assertAlmostEqual(cumulative[-1]["return"], -0.01)

    def test_momentum_and_moving_averages(self):
        bars = bars_from_closes(list(range(1, 202)))
        self.assertAlmostEqual(momentum(bars, 5), 201 / 196 - 1)
        self.assertAlmostEqual(sma20(bars), sum(range(182, 202)) / 20)
        self.assertAlmostEqual(sma50(bars), sum(range(152, 202)) / 50)
        self.assertAlmostEqual(sma200(bars), sum(range(2, 202)) / 200)
        self.assertIsNone(moving_average(bars[:10], 20))

    def test_rsi_volatility_and_drawdown(self):
        rising = bars_from_closes(list(range(1, 22)))
        self.assertEqual(rsi14(rising), 100.0)
        constant = bars_from_closes([100] * 22)
        self.assertEqual(annualized_realized_volatility(constant), 0.0)
        drawdown = maximum_drawdown(bars_from_closes([100, 120, 90, 110]))
        self.assertAlmostEqual(drawdown, -0.25)

    def test_gap_is_not_treated_as_one_daily_return_or_rolling_window(self):
        bars = [
            {"trading_date": "2024-01-01", "close": 100},
            {"trading_date": "2024-01-10", "close": 110},
        ]
        self.assertEqual(daily_returns(bars), [])
        self.assertIsNone(momentum(bars, 1))

    def test_beta_correlation_relative_performance_and_sector_ranking(self):
        benchmark_closes = [100, 101, 100, 102, 101, 103]
        benchmark = bars_from_closes(benchmark_closes)
        changes = [new / old - 1 for old, new in zip(benchmark_closes, benchmark_closes[1:])]
        asset_closes = [200]
        for change in changes:
            asset_closes.append(asset_closes[-1] * (1 + 2 * change))
        asset = bars_from_closes(asset_closes)
        stats = beta_and_correlation(asset, benchmark)
        self.assertAlmostEqual(stats["beta"], 2.0)
        self.assertAlmostEqual(stats["correlation"], 1.0)
        histories = {"SPY": benchmark, "RSP": asset, "XLK": bars_from_closes([100, 101, 102, 103, 104, 106]), "XLF": bars_from_closes([100] * 6)}
        relative = relative_performance(histories, symbols=("SPY", "RSP"))
        self.assertAlmostEqual(relative[1]["excess_5d"], momentum(asset, 5) - momentum(benchmark, 5))
        rankings = sector_performance_rankings(histories, {"XLK": "Technology", "XLF": "Financials", "XLE": "Energy"})
        self.assertEqual(rankings[5][0]["symbol"], "XLK")
        self.assertEqual(rankings[5][1]["symbol"], "XLF")
        self.assertIsNone(rankings[5][-1]["return"])


if __name__ == "__main__":
    unittest.main()

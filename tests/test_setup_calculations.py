import unittest

from dashboard.setup_calculations import (
    calculate_long_setup, calculate_position_sizing,
    format_return_percent, format_return_range,
)


class LongSetupCalculationTests(unittest.TestCase):
    def test_single_entry_planned_and_current_rrr_are_separate(self):
        result = calculate_long_setup(
            entry_price=100, stop_price=90, target_price=130, current_price=110
        )
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["planned_rrr"], 3.0)
        self.assertEqual(result["current_rrr"], 1.0)
        self.assertEqual(result["upside_percent"], 0.3)
        self.assertEqual(result["downside_percent"], -0.1)

    def test_entry_range_reports_both_ends_without_midpoint_assumption(self):
        result = calculate_long_setup(
            entry_low=100, entry_high=110, stop_price=90, target_price=130
        )
        self.assertEqual([item["entry"] for item in result["planned_scenarios"]], [100, 110])
        self.assertEqual([item["planned_rrr"] for item in result["planned_scenarios"]], [3.0, 1.0])
        self.assertIsNone(result["planned_rrr"])

    def test_invalid_long_order_and_zero_risk_are_explicit(self):
        result = calculate_long_setup(entry_price=100, stop_price=100, target_price=130)
        self.assertEqual(result["status"], "zero_risk")
        invalid = calculate_long_setup(entry_price=100, stop_price=101, target_price=130)
        self.assertEqual(invalid["status"], "invalid_setup")

    def test_incomplete_range_and_zero_values_are_invalid(self):
        incomplete = calculate_long_setup(entry_low=100, stop_price=90, target_price=130)
        self.assertEqual(incomplete["status"], "invalid_input")
        zero = calculate_long_setup(entry_price=100, stop_price=0, target_price=130)
        self.assertEqual(zero["status"], "invalid_input")

    def test_missing_current_price_and_crossed_levels_are_explicit(self):
        missing = calculate_long_setup(entry_price=100, stop_price=90, target_price=130)
        self.assertEqual(missing["current_status"], "missing_data")
        stopped = calculate_long_setup(entry_price=100, stop_price=90, target_price=130, current_price=89)
        self.assertEqual(stopped["current_status"], "stop_crossed")
        targeted = calculate_long_setup(entry_price=100, stop_price=90, target_price=130, current_price=130)
        self.assertEqual(targeted["current_status"], "target_crossed")

    def test_position_sizing_uses_risk_budget_and_whole_shares(self):
        result = calculate_position_sizing(
            entry_price=100, stop_price=90, target_price=130,
            risk_budget=250, current_portfolio_value=10_000,
        )
        self.assertEqual(result["max_shares"], 25)
        self.assertEqual(result["planned_dollar_risk"], 250)
        self.assertEqual(result["potential_dollar_upside"], 750)
        self.assertEqual(result["portfolio_allocation_after_change"], 0.2)

    def test_return_format_uses_fractional_internal_units(self):
        self.assertEqual(format_return_percent(.1453), "+14.53%")
        self.assertEqual(format_return_percent(-.0823), "−8.23%")
        self.assertEqual(format_return_range((.0801, .1783)), "+8.01% to +17.83%")

    def test_examples_format_mcd_and_ttwo_returns_as_fractional_returns(self):
        mcd_up = ((284.56 / 248.46 - 1), (285 / 248.46 - 1))
        mcd_down = (228 / 248.46 - 1, 228 / 248.46 - 1)
        self.assertEqual(format_return_range(mcd_up), "+14.53% to +14.71%")
        self.assertEqual(format_return_range(mcd_down), "−8.23%")
        ttwo_up = ((220 / 203.69 - 1), (240 / 203.69 - 1))
        ttwo_down = (186.5 / 203.69 - 1, 186.5 / 203.69 - 1)
        self.assertEqual(format_return_range(ttwo_up), "+8.01% to +17.83%")
        self.assertEqual(format_return_range(ttwo_down), "−8.44%")

    def test_mcd_and_ttwo_range_returns_and_rrr(self):
        cases = [
            (248.46, 284.56, 285.00, 228.00, (.1453, .1471), -.0823, (1.764, 1.786)),
            (203.69, 220.00, 240.00, 186.50, (.0801, .1783), -.0844, None),
        ]
        for current, target_low, target_high, stop, upsides, downside, rrrs in cases:
            outputs = [calculate_long_setup(entry_price=current, stop_price=stop,
                                            target_price=target, current_price=current)
                       for target in (target_low, target_high)]
            self.assertAlmostEqual(outputs[0]["upside_percent"], upsides[0], places=4)
            self.assertAlmostEqual(outputs[1]["upside_percent"], upsides[1], places=4)
            self.assertAlmostEqual(outputs[0]["downside_percent"], downside, places=3)
            self.assertAlmostEqual(outputs[1]["downside_percent"], downside, places=3)
            if rrrs:
                self.assertAlmostEqual(outputs[0]["planned_rrr"], rrrs[0], places=3)
                self.assertAlmostEqual(outputs[1]["planned_rrr"], rrrs[1], places=3)
            else:
                self.assertAlmostEqual(outputs[0]["planned_rrr"], (220 - current) / (current - stop), places=8)
                self.assertAlmostEqual(outputs[1]["planned_rrr"], (240 - current) / (current - stop), places=8)

    def test_invalid_stop_produces_no_percentage_or_rrr(self):
        invalid = calculate_long_setup(entry_price=100, stop_price=105, target_price=130)
        self.assertEqual(invalid["status"], "invalid_setup")
        self.assertIsNone(invalid["upside_percent"])
        self.assertIsNone(invalid["planned_rrr"])

    def test_position_sizing_rejects_invalid_or_missing_levels(self):
        missing = calculate_position_sizing(entry_price=None, stop_price=90)
        self.assertEqual(missing["status"], "invalid_input")
        crossed = calculate_position_sizing(entry_price=90, stop_price=100)
        self.assertEqual(crossed["status"], "invalid_setup")


if __name__ == "__main__":
    unittest.main()

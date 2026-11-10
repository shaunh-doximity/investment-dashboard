import json
import tempfile
import unittest
from pathlib import Path

from dashboard.database import initialize_database
from dashboard.data_access import (
    get_position_plan, get_position_planner_policy, list_position_plan_revisions,
    save_position_plan, save_position_planner_policy, save_strategy_record,
    save_holding, list_holdings, save_cash_balance, get_cash_balance,
    record_position_plan_execution, list_position_plan_executions,
)
from dashboard.position_planner import (
    calculate_position_capacity, plan_entry_tranches, resolve_planner_reference,
    simulate_exit_tranches,
)
from dashboard.kelly_sizing import calculate_binary_kelly


class PositionPlannerCalculationTests(unittest.TestCase):
    def base(self, **overrides):
        values = dict(portfolio_equity=100_000, current_market_value=5_000,
                      current_shares=50, average_cost=95, cash=20_000,
                      entry_price=100, stop_price=90, target_price=130,
                      target_allocation=.10, max_allocation=.15,
                      max_risk_fraction=.01, minimum_cash_reserve=5_000,
                      sector_value=20_000, sector_ceiling=.30)
        values.update(overrides)
        return calculate_position_capacity(**values)

    def test_capacity_is_minimum_of_all_independent_limits_and_fraction_convention(self):
        result = self.base()
        self.assertEqual(result["status"], "valid")
        # Risk: existing risk is 50*(100-90)=500; remaining risk $500 -> 50 shares.
        # Allocation headroom $10,000, deployable cash $15,000, sector headroom $10,000.
        self.assertEqual(result["additional_permitted_shares"], 50)
        self.assertEqual(result["binding_constraint"], "stop risk")
        self.assertAlmostEqual(result["current_total_allocation"], .05)
        self.assertEqual(result["additional_capital_required"], 5_000)
        self.assertAlmostEqual(result["stop_risk_portfolio_fraction"], .01)

    def test_allocation_cash_reserve_and_sector_limits_bind_separately(self):
        self.assertEqual(self.base(max_risk_fraction=None)["additional_permitted_shares"], 100)
        self.assertEqual(self.base(max_risk_fraction=None, minimum_cash_reserve=19_950)["additional_permitted_shares"], 0)
        self.assertEqual(self.base(max_risk_fraction=None, sector_ceiling=.21)["additional_permitted_shares"], 10)

    def test_over_limit_position_warns_without_auto_sale(self):
        result = self.base(current_market_value=20_000, current_shares=200)
        self.assertEqual(result["additional_permitted_shares"], 0)
        self.assertGreater(result["allocation_excess_dollars"], 0)
        self.assertTrue(any("No automatic sale" in warning for warning in result["warnings"]))

    def test_missing_stop_does_not_invent_stop_risk(self):
        result = self.base(stop_price=None)
        self.assertEqual(result["status"], "valid")
        self.assertIsNone(result["total_stop_risk_dollars"])
        self.assertTrue(any("No numeric stop" in warning for warning in result["warnings"]))

    def test_stale_quote_and_crossed_target_or_stop_are_visible(self):
        stale = self.base(quote_stale=True)
        self.assertEqual(stale["status"], "stale_reference")
        manual = self.base(quote_stale=False, current_price=101, current_price_is_manual=True)
        self.assertTrue(any("manually entered" in warning for warning in manual["warnings"]))
        target = self.base(current_price=135)
        self.assertTrue(any("already been reached" in warning for warning in target["warnings"]))
        self.assertEqual(target["additional_permitted_shares"], 0)
        stopped = self.base(current_price=89)
        self.assertTrue(any("at or below" in warning for warning in stopped["warnings"]))
        self.assertEqual(stopped["additional_permitted_shares"], 0)
        incompatible = self.base(price_basis_compatible=False)
        self.assertEqual(incompatible["status"], "incompatible_price_basis")
        fresh_different_date = self.base(price_basis_compatible=False,
            price_compatibility_status="different_observation_date",
            price_compatibility_note="The observations have different dates; freshness is separate from price basis.")
        self.assertEqual(fresh_different_date["status"], "different_observation_date")
        self.assertIn("freshness", fresh_different_date["message"])

    def test_invalid_long_levels_and_unsupported_positions_are_explicit(self):
        self.assertEqual(self.base(stop_price=110)["status"], "invalid")
        self.assertEqual(self.base(position_type="short_equity")["status"], "unsupported")
        with self.assertRaises(ValueError):
            self.base(target_allocation=1.1)

    def test_stale_quote_requires_dated_explicit_manual_reference(self):
        quote = {"price": 203.69, "quote_date": "2026-10-23"}
        stale = {"status": "stale", "price": None, "message": "Stale quote dated 2026-10-23."}
        self.assertFalse(resolve_planner_reference(stale, quote, quote_basis_confirmed=True)["available"])
        unconfirmed = resolve_planner_reference(stale, quote, manual_price=203.69,
                                                manual_date="2026-10-28", manual_basis_confirmed=False)
        self.assertFalse(unconfirmed["available"])
        manual = resolve_planner_reference(stale, quote, manual_price=203.69,
                                           manual_date="2026-10-28", manual_basis_confirmed=True)
        self.assertEqual(manual["source"], "User-confirmed manual reference")
        self.assertTrue(manual["is_manual"])

    def test_fresh_unconfirmed_basis_needs_explicit_confirmation(self):
        quote = {"price": 205, "quote_date": "2026-10-27"}
        assessment = {"status": "different_observation_date", "message": "Dates differ; freshness only."}
        self.assertFalse(resolve_planner_reference(assessment, quote)["available"])
        accepted = resolve_planner_reference(assessment, quote, quote_basis_confirmed=True)
        self.assertEqual(accepted["price"], 205)
        self.assertEqual(accepted["source"], "User-confirmed Alpha Vantage quote basis")

    def test_entry_tranches_share_capacity_and_do_not_reuse_cash_or_risk(self):
        capacity = self.base()
        result = plan_entry_tranches([
            {"sequence": 1, "id": "a", "fraction": .4, "trigger_price": 100},
            {"sequence": 2, "id": "b", "fraction": .3, "trigger_price": 100},
            {"sequence": 3, "id": "c", "fraction": .3, "trigger_price": 100},
        ], capacity_result=capacity, entry_price=100, stop_price=90)
        self.assertEqual([row["planned_shares"] for row in result["tranches"]], [20, 15, 15])
        self.assertEqual(sum(row["maximum_capital_commitment"] for row in result["tranches"]), 5_000)
        self.assertEqual([row["cumulative_projected_shares"] for row in result["tranches"]], [70, 85, 100])
        self.assertEqual([row["projected_allocation_after_stage"] for row in result["tranches"]], [.07, .085, .1])
        with self.assertRaises(ValueError):
            plan_entry_tranches([{"fraction": .7, "trigger_price": 100}, {"fraction": .4, "trigger_price": 100}], capacity_result=capacity, entry_price=100)

    def test_exit_tranches_are_sequential_clamped_and_support_partial_fills(self):
        result = simulate_exit_tranches([
            {"sequence": 1, "percent": .5, "percent_basis": "remaining", "trigger_price": 120},
            {"sequence": 2, "percent": .5, "percent_basis": "original", "trigger_price": 130},
            {"sequence": 3, "shares": 50, "trigger_price": 140},
        ], current_shares=100, current_price=110, stop_price=90, portfolio_equity=10_000)
        self.assertEqual([row["sell_shares"] for row in result["tranches"]], [50, 50, 0])
        self.assertEqual(result["remaining_shares"], 0)
        partial = simulate_exit_tranches([{"status": "partial_fill", "filled_shares": 12,
                                          "trigger_price": 100}], current_shares=30)
        self.assertEqual(partial["remaining_shares"], 18)


class KellySizingTests(unittest.TestCase):
    def calculate(self, **overrides):
        values = dict(entry_price=100, target_price=130, stop_price=90,
                      probability=.5, probability_source="User-provided hypothetical assumption",
                      fractional_kelly=.25, portfolio_equity=100_000)
        values.update(overrides)
        return calculate_binary_kelly(**values)

    def test_binary_kelly_formula_fractions_and_risk_capital_are_distinct(self):
        full = self.calculate(fractional_kelly=1)
        quarter = self.calculate(fractional_kelly=.25)
        half = self.calculate(fractional_kelly=.5)
        # Gain=.30, stop loss=.10, p=.5: f*=(.5*.30-.5*.10)/(.30*.10)=3.3333.
        self.assertAlmostEqual(full["full_kelly_allocation_fraction"], 10 / 3)
        self.assertTrue(full["no_leverage_cap_applied"])
        self.assertEqual(full["selected_allocation_fraction"], 1.0)
        self.assertAlmostEqual(quarter["quarter_kelly_allocation_fraction"], 5 / 6)
        self.assertAlmostEqual(quarter["selected_risk_capital_fraction"], 1 / 12)
        self.assertAlmostEqual(quarter["quarter_kelly_risk_capital_fraction"], 1 / 12)
        self.assertAlmostEqual(half["selected_allocation_fraction"], 1.0)
        self.assertAlmostEqual(quarter["break_even_probability"], .25)

    def test_missing_probability_and_negative_edge_never_create_positive_size(self):
        missing = self.calculate(probability=None)
        self.assertEqual(missing["status"], "unavailable")
        no_edge = self.calculate(probability=.1)
        self.assertEqual(no_edge["status"], "no_positive_edge")
        self.assertIsNone(no_edge["selected_allocation_fraction"])

    def test_probability_integrity_uncertainty_and_gap_sensitivity(self):
        with self.assertRaisesRegex(ValueError, "explicit supported probability source"):
            self.calculate(probability_source="AI said so")
        with self.assertRaisesRegex(ValueError, "Provide the method or evidence"):
            self.calculate(probability_source="ChatGPT research estimate (unverified)")
        result = self.calculate(probability_source="Historical or validated probability model",
                                probability_evidence="Out-of-sample trade study; 250 observations",
                                probability_low=.4, probability_high=.6,
                                gap_slippage_fraction=.03)
        self.assertEqual(result["status"], "valid")
        self.assertLess(result["selected_allocation_fraction"], self.calculate()["selected_allocation_fraction"])
        self.assertGreater(result["break_even_probability"], result["planned_stop_break_even_probability"])
        self.assertEqual(result["probability_sensitivity"]["label"], "Hypothetical sensitivity only; not a validated allocation range.")
        with self.assertRaisesRegex(ValueError, "Probability range"):
            self.calculate(probability_low=.6, probability_high=.7)

    def test_invalid_long_prices_are_rejected(self):
        result = self.calculate(stop_price=100)
        self.assertEqual(result["status"], "invalid")

    def test_kelly_illustration_remains_subject_to_portfolio_constraints(self):
        kelly = self.calculate()["quarter_kelly_allocation_fraction"]
        capacity = calculate_position_capacity(
            portfolio_equity=100_000, current_market_value=5_000,
            current_shares=50, average_cost=95, cash=20_000,
            entry_price=100, stop_price=90, target_price=130,
            target_allocation=kelly, max_allocation=.15,
            max_risk_fraction=.01, minimum_cash_reserve=5_000,
            sector_value=20_000, sector_ceiling=.30,
        )
        self.assertLessEqual(capacity["additional_capital_required"], 10_000)
        self.assertLessEqual(capacity["total_stop_risk_dollars"], 1_000)


class PositionPlanPersistenceTests(unittest.TestCase):
    def test_additive_migration_policy_and_versioned_plan_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "planner.db"
            initialize_database(db)
            save_strategy_record("MCD", {"investment_thesis": "Thesis", "structured_analysis_json": "{}"}, db)
            save_holding("MCD", 12, 240, database_path=db)
            save_cash_balance(7000, db)
            before_holding = list_holdings(db)[0]
            save_position_planner_policy({"max_position_allocation": .2, "target_position_allocation": .1,
                "max_loss_per_position": .015, "minimum_cash_reserve": 1000,
                "max_sector_allocation": .35, "rrr_monitoring_threshold": 1.2}, db)
            first = {"schema_version": 1, "position_type": "long_equity", "entry_reference": 250,
                     "target_allocation": .1, "max_allocation": .15,
                     "max_loss_per_position": .01, "max_sector_allocation": .30,
                     "entry_tranches": [{"id": "buy-1", "sequence": 1, "fraction": .5,
                                         "trigger_price": 250, "status": "planned"}],
                     "exit_tranches": []}
            plan_id = save_position_plan("MCD", "Medium-term", first, strategy_revision_id=1, database_path=db)
            second = {**first, "entry_reference": 255}
            save_position_plan("MCD", "Medium-term", second, strategy_revision_id=1, database_path=db)
            with self.assertRaisesRegex(ValueError, "Confirm"):
                record_position_plan_execution(plan_id, "MCD", "buy-1", "buy", 2, 250, "2026-10-28", database_path=db)
            record_position_plan_execution(plan_id, "MCD", "buy-1", "buy", 2, 250, "2026-10-28", confirmed=True, database_path=db)
            initialize_database(db)
            current = get_position_plan("mcd", "Medium-term", db)
            self.assertEqual(current["plan"]["entry_reference"], 255)
            self.assertEqual(len(list_position_plan_revisions("MCD", "Medium-term", db)), 2)
            self.assertEqual(get_position_planner_policy(db)["max_position_allocation"], .2)
            self.assertEqual(json.loads(json.dumps(current["plan"])), second)
            self.assertEqual(len(list_position_plan_revisions("MCD", "Long-term", db)), 0)
            after_holding = list_holdings(db)[0]
            self.assertEqual((before_holding["shares"], before_holding["average_cost"]),
                             (after_holding["shares"], after_holding["average_cost"]))
            self.assertEqual(get_cash_balance(db), 7000)
            self.assertEqual(len(list_position_plan_executions("MCD", db)), 1)


if __name__ == "__main__":
    unittest.main()

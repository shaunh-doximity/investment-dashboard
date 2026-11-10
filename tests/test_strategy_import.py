import json
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from dashboard.data_access import (
    create_research_workspace, get_strategy_record, list_research_strategy_scenarios,
    list_strategy_revisions, save_strategy_record,
)
from dashboard.database import initialize_database
from dashboard.strategy_import import (
    SCHEMA_VERSION, approve_import, assess_current_quote, canonical_import_example,
    chatgpt_export_instructions, compatible_current_quote, json_template,
    normalize_strategy_import, preview_import, validate_import_json,
    validate_normalized_strategy,
)


def payload(ticker="MCD"):
    scenario = {
        "thesis": "Conditional setup", "holding_horizon": "3-6 months",
        "entry_conditions": "Wait for support", "entry_price_low": 90,
        "entry_price_high": 100, "target_price_low": 130,
        "target_price_high": 140, "stop_price": 80,
        "fundamental_invalidation": "Margins deteriorate", "invalidation_conditions": "Break support",
        "bull_case": "Growth holds", "base_case": "Steady business", "bear_case": "Sales weaken",
        "catalysts": "Product launch", "risks": "Valuation", "evidence": "User supplied source",
        "assumptions": "Assumed stable margins", "uncertainty": "No current filings",
    }
    missing = {key: None for key in ("entry_price_low", "entry_price_high", "target_price_low", "target_price_high", "stop_price")}
    missing.update({key: "" for key in (
        "thesis", "holding_horizon", "entry_conditions", "fundamental_invalidation", "invalidation_conditions",
        "bull_case", "base_case", "bear_case", "catalysts", "risks", "evidence", "assumptions", "uncertainty",
    )})
    return {
        "schema_version": SCHEMA_VERSION, "ticker": ticker, "company_name": "McDonald's",
        "research_date": "2026-10-20", "strategy_type": "Quality growth",
        "investment_thesis": "Durable demand with disciplined valuation.",
        "fundamental_analysis": "User supplied analysis", "valuation_analysis": "Valuation needs verification",
        "technical_analysis": "Price above long term trend", "bull_case": "Growth", "base_case": "Steady",
        "bear_case": "Margin compression", "catalysts": "Menu innovation", "risks": "Costs",
        "uncertainties": "Recent data unavailable", "research_gaps": "Verify filings",
        "market_price_reference": {"price": 105, "date": "2026-10-23", "basis": "unadjusted", "source": "quote"},
        "evidence": [{"title": "IR", "url": "https://example.com/ir", "source": "Company IR", "as_of": "2026-10-20"}],
        "scenarios": {"short_term": dict(scenario), "medium_term": dict(scenario), "long_term": dict(missing)},
    }


class StrategyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "research.db"
        initialize_database(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def validate(self, value=None, ticker="MCD"):
        return validate_import_json(json.dumps(value if value is not None else payload()), ticker)

    def test_valid_versioned_json_and_template(self):
        result = self.validate()
        self.assertEqual(result["ticker"], "MCD")
        self.assertEqual(result["scenarios"]["medium_term"]["target_price_high"], 140)
        template = json.loads(json_template("MCD"))
        self.assertEqual(template["schema_version"], 3)
        self.assertEqual(set(template["scenarios"]), {"short_term", "medium_term", "long_term"})

    def test_structured_thesis_is_combined_without_losing_summary_or_reasoning(self):
        value = payload()
        value["investment_thesis"] = {"summary": "Durable brand", "reasoning": "Price power supports margins."}
        result = self.validate(value)
        self.assertIn("Durable brand", result["investment_thesis"])
        self.assertIn("Price power", result["investment_thesis"])
        self.assertTrue(any(change["path"] == "investment_thesis" for change in result["normalization_metadata"]["changes"]))

    def test_evidence_object_array_and_url_variants_normalize(self):
        value = payload()
        value["evidence"] = {"title": "Filing", "url": {"href": "https://www.sec.gov/", "label": "open"}, "source": "SEC"}
        result = self.validate(value)
        self.assertEqual(result["evidence"][0]["url"], "https://www.sec.gov/")
        self.assertTrue(result["normalization_metadata"]["review_flags"])

        value = payload()
        value["evidence"] = ["https://www.sec.gov/", {"title": "IR", "source_url": "https://example.org/ir"}]
        result = self.validate(value)
        self.assertEqual([item["url"] for item in result["evidence"]], ["https://www.sec.gov/", "https://example.org/ir"])

    def test_optional_nulls_and_numeric_strings_are_normalized(self):
        value = payload()
        value["company_name"] = None
        value["market_price_reference"] = None
        value["scenarios"]["medium_term"]["entry_price_low"] = "$90.50"
        value["scenarios"]["medium_term"]["entry_price_high"] = "100"
        result = self.validate(value)
        self.assertIsNone(result["market_price_reference"])
        self.assertEqual(result["scenarios"]["medium_term"]["entry_price_low"], 90.5)
        self.assertTrue(any("numeric string" in change["action"] for change in result["normalization_metadata"]["changes"]))

    def test_aliases_and_scenario_array_are_mapped_when_horizon_is_clear(self):
        value = payload()
        value["investment_thesis"] = value.pop("investment_thesis")
        value["scenarios"] = [
            {"horizon": "short-term", "entry_range": ["90", "100"], "target_price": "130", "stop_loss": "80"},
            {"horizon": "medium", "entry_price": 95, "target_price": 135, "stop": 80},
            {"horizon": "long", "entry_range": [None, None], "target_range": [None, None], "stop_price": None},
        ]
        result = self.validate(value)
        self.assertEqual(result["scenarios"]["short_term"]["entry_price_high"], 100)
        self.assertEqual(result["scenarios"]["medium_term"]["entry_price_low"], 95)
        self.assertEqual(result["scenarios"]["long_term"]["stop_price"], None)

    def test_ambiguous_scenario_and_unrecognized_evidence_are_preserved_for_review(self):
        value = payload()
        value["evidence"] = {"claim": "Strong demand observed"}
        value["scenarios"]["short_term"]["risk_score"] = "high"
        result = self.validate(value)
        flags = result["normalization_metadata"]["review_flags"]
        self.assertTrue(any(flag["path"].startswith("evidence") for flag in flags))
        self.assertTrue(any("short_term.unrecognized" in flag["path"] for flag in flags))
        preserved = result["normalization_metadata"]["preserved_unrecognized"]
        self.assertIn("evidence", preserved)
        self.assertIn("scenarios.short_term.unrecognized", preserved)

    def test_required_fields_have_path_specific_errors(self):
        value = payload()
        del value["investment_thesis"]
        with self.assertRaisesRegex(ValueError, r"investment_thesis: expected text string"):
            self.validate(value)
        value = payload()
        del value["scenarios"]["long_term"]
        with self.assertRaisesRegex(ValueError, r"scenarios.long_term: expected scenario object"):
            self.validate(value)
        value = payload()
        value["evidence"] = [{"url": {"href": "https://example.com"}}]
        # This known URL object is normalizable; a malformed URL field is not.
        value["evidence"] = [{"url": {"href": 42}}]
        result = self.validate(value)
        self.assertTrue(result["normalization_metadata"]["review_flags"])
        strict_payload = payload()
        strict_payload["evidence"] = [{"title": "bad", "url": {"href": 42}}]
        with self.assertRaisesRegex(ValueError, r"evidence\[0\]\.url: expected URL string; received object"):
            validate_normalized_strategy(strict_payload, "MCD")

    def test_invalid_json_version_dates_prices_and_urls_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            validate_import_json("{oops", "MCD")
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            validate_import_json('{"schema_version":1,"ticker":"MCD","investment_thesis":"x","research_date":NaN,"scenarios":{}}', "MCD")
        bad = payload(); bad["schema_version"] = 99
        with self.assertRaisesRegex(ValueError, r"schema_version: expected one of \[1, 2, 3\]"):
            self.validate(bad)
        bad = payload(); bad["research_date"] = "10/20/2026"
        with self.assertRaisesRegex(ValueError, "ISO date"):
            self.validate(bad)
        bad = payload(); bad["scenarios"]["short_term"]["stop_price"] = -1
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            self.validate(bad)
        bad = payload(); bad["evidence"][0]["url"] = "javascript:alert(1)"
        with self.assertRaisesRegex(ValueError, "http/https"):
            self.validate(bad)

    def test_missing_numeric_values_are_preserved_as_null(self):
        result = self.validate()
        self.assertIsNone(result["scenarios"]["long_term"]["stop_price"])
        prepared = preview_import(result, today=date(2026, 10, 25))
        self.assertEqual(prepared["calculated_setups"]["long_term"]["status"], "missing_data")

    def test_ticker_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected MSFT to match this workspace"):
            self.validate(ticker="MSFT")

    def test_current_quote_requires_matching_source_date_and_price_basis(self):
        imported = self.validate()
        quote = {"price": 110.0, "quote_date": "2026-10-23"}
        value, message = compatible_current_quote(imported, quote, is_stale=False)
        self.assertIsNone(value)
        self.assertIn("basis/value", message)
        imported["market_price_reference"].update({"source": "Alpha Vantage", "basis": "unadjusted"})
        imported["market_price_reference"]["date"] = "2026-10-22"
        value, message = compatible_current_quote(imported, quote, is_stale=False)
        self.assertIsNone(value)  # reference date is Oct 23 in the sample; matching required
        imported["market_price_reference"]["date"] = "2026-10-23"
        value, message = compatible_current_quote(imported, quote, is_stale=False)
        self.assertEqual(value, 110.0)
        self.assertIsNone(message)
        value, message = compatible_current_quote(imported, quote, is_stale=True)
        self.assertIsNone(value)
        self.assertIn("stale", message)

    def test_same_date_price_match_still_requires_explicit_unadjusted_basis(self):
        imported = self.validate()
        imported["market_price_reference"] = {
            "price": 105.0, "date": "2026-10-23",
            "basis": "NYSE regular-session closing price in USD", "source": "https://example.com/history",
        }
        quote = {"price": 105.0, "quote_date": "2026-10-23"}
        value, message = compatible_current_quote(imported, quote, is_stale=False)
        self.assertIsNone(value)
        self.assertIn("not confirmed compatible", message)
        imported["market_price_reference"]["basis"] = "NYSE regular-session unadjusted closing price in USD"
        self.assertEqual(compatible_current_quote(imported, quote, is_stale=False), (105.0, None))
        quote["price"] = 106.0
        value, message = compatible_current_quote(imported, quote, is_stale=False)
        self.assertIsNone(value)
        self.assertIn("not confirmed compatible", message)
        quote["price"] = 105.0
        imported["market_price_reference"]["basis"] = "dividend-adjusted close"
        value, message = compatible_current_quote(imported, quote, is_stale=False)
        self.assertIsNone(value)
        self.assertIn("not confirmed compatible", message)

    def test_schema_v1_and_v2_remain_accepted_after_additive_schema_v3(self):
        old = payload()
        old["schema_version"] = 1
        old.pop("investment_logic", None)
        result = self.validate(old)
        self.assertEqual(result["schema_version"], 3)
        self.assertIsNone(result["investment_logic"])
        previous = payload(); previous["schema_version"] = 2
        self.assertEqual(self.validate(previous)["schema_version"], 3)

    def test_optional_probability_estimate_is_preserved_as_unverified_proposal(self):
        value = payload()
        value["position_sizing"] = {"probability_estimate": {
            "probability": .58, "range": [.45, .65], "source": "ChatGPT research estimate",
            "method": "Qualitative judgment", "evidence": "Not independently verified"}}
        result = self.validate(value)
        estimate = result["position_sizing"]["probability_estimate"]
        self.assertEqual(estimate["probability"], .58)
        self.assertIn("not an approved sizing input", estimate["approval_status"])
        value["position_sizing"]["probability_estimate"]["probability"] = 1.5
        with self.assertRaisesRegex(ValueError, "probability_estimate.probability"):
            self.validate(value)

    def test_quote_diagnostics_separate_staleness_from_date_or_basis(self):
        imported = self.validate()
        quote = {"price": 110.0, "quote_date": "2026-10-23"}
        stale = assess_current_quote(imported, quote, is_stale=True)
        self.assertEqual(stale["status"], "stale")
        self.assertIn("stale", stale["message"])
        imported["market_price_reference"].update({"source": "Alpha Vantage", "basis": "unadjusted", "date": "2026-10-22"})
        mismatch = assess_current_quote(imported, quote, is_stale=False)
        self.assertEqual(mismatch["status"], "different_observation_date")
        self.assertIn("freshness difference", mismatch["message"])

    def test_structured_investment_logic_is_validated_and_persistable(self):
        value = payload()
        value["investment_logic"] = {
            "bullish_drivers": [{"label": "Pricing power", "explanation": "Can offset input costs.",
                                 "evidence_status": "Imported / unverified", "details": None}],
            "bearish_risks": [], "catalysts": [], "invalidation_signals": [],
        }
        result = self.validate(value)
        self.assertEqual(result["investment_logic"]["bullish_drivers"][0]["label"], "Pricing power")
        value["investment_logic"]["bullish_drivers"][0]["evidence_status"] = "Definitely true"
        with self.assertRaisesRegex(ValueError, r"investment_logic\.bullish_drivers\[0\]\.evidence_status"):
            self.validate(value)

    def test_position_sizing_proposals_are_optional_and_calculated_locally(self):
        value = payload()
        value["position_sizing"] = {
            "target_allocation": .08, "maximum_allocation": .12,
            "maximum_loss_fraction": .01,
            "allocation_rationale": "Limit exposure pending updated filings.",
            "assumptions": "Portfolio equity comes from the local dashboard.",
            "constraints": ["Keep a cash reserve"], "evidence": "User research note",
            "entry_tranches": [{"sequence": 1, "fraction": .4, "trigger_price": 100,
                                "confirmation_conditions": "Hold above support", "rationale": "Initial tranche"}],
            "exit_tranches": [{"sequence": 1, "percent": .5, "percent_basis": "remaining",
                               "trigger_price": 130, "condition": "Target reached"}],
        }
        parsed = self.validate(value)
        self.assertEqual(parsed["position_sizing"]["target_allocation"], .08)
        self.assertEqual(parsed["position_sizing"]["entry_tranches"][0]["fraction"], .4)
        value["position_sizing"]["shares"] = 50
        with self.assertRaisesRegex(ValueError, "not authoritative"):
            self.validate(value)

    def test_position_sizing_null_and_fraction_validation(self):
        value = payload()
        value["position_sizing"] = None
        self.assertIsNone(self.validate(value)["position_sizing"])
        value["position_sizing"] = {"target_allocation": 8.0}
        with self.assertRaisesRegex(ValueError, "fraction from 0 to 1"):
            self.validate(value)
        value["position_sizing"] = {"target_allocation_range": [.04, .08]}
        self.assertEqual(self.validate(value)["position_sizing"]["target_allocation_range"], [.04, .08])
        value["position_sizing"] = {"target_allocation": .06, "target_allocation_range": [.04, .08]}
        with self.assertRaisesRegex(ValueError, "not both"):
            self.validate(value)

    def test_preview_computes_rrr_and_warns_about_stale_references(self):
        result = self.validate()
        result["research_date"] = (date(2026, 10, 25) - timedelta(days=120)).isoformat()
        result["market_price_reference"]["date"] = "2026-10-10"
        preview = preview_import(result, current_price=110, current_quote_date="2026-10-23", today=date(2026, 10, 25))
        setup = preview["calculated_setups"]["medium_term"]
        self.assertEqual(setup["status"], "missing_data")  # target range stays a range
        self.assertEqual(setup["planned_rrr_range"], [1.5, 5.0])
        self.assertAlmostEqual(setup["current_rrr_range"][0], 2 / 3)
        self.assertEqual(setup["current_rrr_range"][1], 1.0)
        self.assertEqual(len(preview["validation_warnings"]), 3)

    def test_preview_never_overwrites_approved_strategy_and_approval_appends_history(self):
        create_research_workspace("MCD", self.db)
        create_research_workspace("MSFT", self.db)
        save_strategy_record("MCD", {"investment_thesis": "Prior approved thesis", "investment_horizon": "Long"}, self.db)
        save_strategy_record("MSFT", {"investment_thesis": "Separate ticker", "investment_horizon": "Medium"}, self.db)
        before_count = len(list_strategy_revisions("MCD", self.db))
        preview = preview_import(self.validate(), current_price=110, today=date(2026, 10, 25))
        self.assertEqual(get_strategy_record("MCD", self.db)["investment_thesis"], "Prior approved thesis")
        self.assertEqual(len(list_strategy_revisions("MCD", self.db)), before_count)
        self.assertTrue(approve_import(preview, self.db))
        self.assertEqual(get_strategy_record("MCD", self.db)["investment_thesis"], "Durable demand with disciplined valuation.")
        self.assertEqual(get_strategy_record("MSFT", self.db)["investment_thesis"], "Separate ticker")
        self.assertEqual(len(list_strategy_revisions("MCD", self.db)), before_count + 1)
        scenarios = list_research_strategy_scenarios("MCD", database_path=self.db)
        self.assertEqual(len(scenarios), 3)
        medium_saved = next(item["scenario"] for item in scenarios if item["horizon"] == "Medium-term")
        self.assertEqual(medium_saved["target_price_low"], 130)
        self.assertEqual(medium_saved["target_price_high"], 140)
        self.assertEqual(medium_saved["stop_price"], 80)
        history = list_research_strategy_scenarios("MCD", history=True, database_path=self.db)
        self.assertEqual(len(history), 3)

    def test_reapproval_creates_new_revision_and_preserves_prior_revision(self):
        first = preview_import(self.validate(), today=date(2026, 10, 25))
        approve_import(first, self.db)
        changed_payload = payload(); changed_payload["investment_thesis"] = "Changed after review"
        second = preview_import(self.validate(changed_payload), today=date(2026, 10, 25))
        approve_import(second, self.db)
        revisions = list_strategy_revisions("MCD", self.db)
        self.assertEqual(len(revisions), 2)
        self.assertEqual({row["investment_thesis"] for row in revisions}, {"Durable demand with disciplined valuation.", "Changed after review"})

    def test_generated_example_round_trips_through_approval_and_revision_history(self):
        raw = json_template("MCD")
        example = json.loads(raw)
        normalized = normalize_strategy_import(raw)
        validated = validate_normalized_strategy(normalized, "MCD")
        self.assertEqual(len(validated["evidence"]), 1)
        self.assertEqual(set(validated["scenarios"]), {"short_term", "medium_term", "long_term"})
        preview = preview_import(validated)
        self.assertEqual(preview["calculated_setups"]["long_term"]["status"], "missing_data")
        self.assertTrue(approve_import(preview, self.db))
        saved = get_strategy_record("MCD", self.db)
        self.assertEqual(saved["investment_thesis"], example["investment_thesis"])
        self.assertEqual(len(list_strategy_revisions("MCD", self.db)), 1)
        self.assertEqual(len(list_research_strategy_scenarios("MCD", history=True, database_path=self.db)), 3)
        instructions = chatgpt_export_instructions("MCD")
        self.assertIn("schema_version", instructions)
        self.assertIn("short_term", instructions)
        self.assertIn("investment_logic", instructions)
        self.assertIn("position_sizing", instructions)
        self.assertIn("probability_estimate", instructions)
        self.assertIn("Never invent unsupported prices, probabilities", instructions)
        self.assertIn("evidence_status", instructions)
        self.assertIn("https://www.sec.gov/", instructions)


if __name__ == "__main__":
    unittest.main()

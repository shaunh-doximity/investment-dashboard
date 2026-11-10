import unittest
import json
from datetime import date, timedelta
from unittest.mock import patch

from dashboard.market_snapshot import (
    SECTION_ORDER, analysis_to_markdown, build_market_snapshot,
    compare_market_snapshots, metric_references, parse_market_analysis_json,
    market_snapshot_fingerprint, provider_accent, saved_report_is_stale,
    select_saved_market_report, snapshot_with_recent_catalysts, validate_market_analysis,
)


def snapshot(value=2.0, window=20):
    return build_market_snapshot({"regime": {"as_of": "2026-11-01", "source": "Tiingo",
        "window": window, "status": "available", "metrics": {
            "rsp_spy": {"value": value, "unit": "pp", "window": window,
                        "universe": "RSP/SPY", "definition": "ratio return", "as_of": "2026-11-01"}}}})


def valid_response(snap):
    refs = sorted(metric_references(snap))
    return {"schema_version": "market-analyst-response-v1",
        "highlights": [{"section_id": section, "headline": "No material change",
            "category": "insufficient evidence", "insight": "No complete new signal is supported.",
            "why_it_matters": "The section has no verified change.", "monitor_next": "Wait for a new observation.",
            "metric_refs": refs if section == "regime" else []} for section in SECTION_ORDER],
        "briefing": {"executive_takeaway": "The available market snapshot is mixed.",
            "risks": [], "supportive": [], "sector_implications": [],
            "conflicting_evidence": [], "catalysts": [], "conditions": [],
            "metric_refs": {"executive_takeaway": refs,
                "risks": [], "supportive": [], "sector_implications": [],
                "conflicting_evidence": [], "catalysts": [], "conditions": []}}
        }


class MarketSnapshotTests(unittest.TestCase):
    def test_version_order_fingerprint_stability_and_missing_sections(self):
        one, two = snapshot(), snapshot()
        self.assertEqual(one["fingerprint"], two["fingerprint"])
        self.assertEqual(one["sections_in_reading_order"], list(SECTION_ORDER))
        self.assertEqual(one["sections"]["vix"]["status"], "unavailable")
        self.assertEqual(one["sections"]["vix"]["gaps"], ["Section data unavailable"])

    def test_view_selection_does_not_change_global_market_fingerprint(self):
        sections = {"relative_strength": {"status": "available", "metrics": {
            "smh_spy_excess": {"value": 2.2, "unit": "pp", "window": "20 sessions",
                               "universe": "SMH/SPY", "definition": "matched return", "as_of": "2026-11-01"}}}}
        first = build_market_snapshot(sections, view_state={"relative_strength": {"first": "SMH", "second": "SPY"}})
        second = build_market_snapshot(sections, view_state={"relative_strength": {"first": "XLY", "second": "XLP"}})
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["view_fingerprint"], second["view_fingerprint"])

    def test_catalyst_cache_refreshed_during_analysis_is_bound_to_report_fingerprint(self):
        initial = snapshot()
        initial["view_state"] = {"selected_pair": ["SMH", "SPY"]}
        refreshed = snapshot_with_recent_catalysts(initial, {
            "status": "available", "refreshed_at": "2026-11-02T12:00:00+00:00",
            "records": [{"event_id": "evt1", "publication_date": "2026-11-01",
                         "source_url": "https://example.com/source"}],
        })
        rebuilt = build_market_snapshot(refreshed["sections"], configurations=refreshed["configurations"])
        self.assertEqual(refreshed["fingerprint"], rebuilt["fingerprint"])
        self.assertEqual(refreshed["fingerprint"], market_snapshot_fingerprint(refreshed))
        self.assertNotEqual(refreshed["fingerprint"], initial["fingerprint"])
        self.assertEqual(refreshed["view_state"], initial["view_state"])

    def test_market_monitor_snapshot_contains_actual_broad_index_matrix(self):
        from dashboard import market_monitor_ui as ui
        from dashboard.market_history import BROAD_MARKET_SYMBOLS, MACRO_PROXY_SYMBOLS, CREDIT_PROXY_SYMBOLS, SECTOR_SYMBOLS
        symbols = tuple(dict.fromkeys((*BROAD_MARKET_SYMBOLS, *MACRO_PROXY_SYMBOLS,
                                      *CREDIT_PROXY_SYMBOLS, *SECTOR_SYMBOLS)))
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(520)]
        days = [day for day in days if day.weekday() < 5]
        histories = {symbol:[{"symbol":symbol,"trading_date":day.isoformat(),
            "close":100 + index * (1 + (len(symbol) % 4) / 10)} for index,day in enumerate(days)]
            for symbol in symbols}
        prefs = {}
        with patch.object(ui, "get_app_preference", side_effect=lambda key, default=None: prefs.get(key, default)):
            with patch.object(ui, "save_market_regime_transition_record", return_value={"inserted": False}):
                with patch.object(ui, "load_recent_catalysts", return_value={"status":"available","refreshed_at":"2026-11-01T00:00:00Z",
                        "records":[{"event_id":"x","ticker":"GOOGL","publication_date":"2026-10-31","source_url":"https://example.com"}]}):
                    with patch.object(ui, "list_market_events", return_value=[]):
                        with patch.object(ui, "list_deepseek_market_research_reports", return_value=[]):
                            with patch.object(ui, "list_market_explorer_favorites", return_value=[]):
                                snap = ui._build_market_snapshot(histories, 20, {}, {}, [], [])
                                prefs["market_broad_index_view_v1"] = {"window":60,
                                    "mode":"Relative cumulative performance vs SPY", "visible_symbols":["DIA","IWM"]}
                                changed_view = ui._build_market_snapshot(histories, 20, {}, {}, [], [])
        self.assertEqual(snap["schema_version"], "market-intelligence-snapshot-v3")
        regime = snap["sections"]["regime"]
        self.assertEqual(regime["coverage"]["broad_index_symbols"], 5)
        self.assertEqual(regime["broad_index_comparisons"]["DIA_SPY_20D"]["status"], "available")
        self.assertIn("regime.broad_dia_20d_excess_vs_spy", metric_references(snap))
        self.assertIn("regime.sectors_positive_absolute_20d", metric_references(snap))
        self.assertIn("regime.breadth_condition", metric_references(snap))
        self.assertIn("methodology_version", regime["breadth_interpretation"])
        self.assertEqual(regime["breadth_interpretation"]["window"], 20)
        self.assertIn("regime.sectors_outperforming_spy_20d", metric_references(snap))
        self.assertIn("regime.rsp_spy_20d_rsp_return", metric_references(snap))
        self.assertIn("regime.rsp_spy_20d_spy_return", metric_references(snap))
        self.assertIn("regime.rsp_spy_20d_excess_pp", metric_references(snap))
        self.assertIn("regime.positive_sector_participation_change_5d", metric_references(snap))
        self.assertIn("broad_index_chart", snap["view_state"])
        self.assertEqual(snap["fingerprint"], changed_view["fingerprint"])
        self.assertNotEqual(snap["view_fingerprint"], changed_view["view_fingerprint"])
        self.assertIn("rates_macro.uso_relative_return_pp_20d", metric_references(snap))
        self.assertNotIn("rates_macro.wti_spot_price", metric_references(snap))
        self.assertIn("USO", snap["sections"]["rates_macro"]["macro_proxy_comparisons"])
        self.assertIn("risk_appetite", snap["sections"]["sector_rotation"])
        self.assertIn("regime_transition", snap["sections"]["sector_rotation"])
        self.assertIn("sector_rotation.regime_transition_status", metric_references(snap))
        self.assertIn("sector_rotation.risk_appetite_classification", metric_references(snap))
        evidence = snap["sections"]["sector_rotation"]["regime_evidence"]
        self.assertEqual(evidence["methodology_version"], "regime-evidence-v2")
        self.assertIn("regime_evidence_classification", snap["sections"]["sector_rotation"]["metrics"])
        if evidence.get("relative_evidence_support"):
            self.assertAlmostEqual(sum(evidence["relative_evidence_support"].values()), 100.0)
        self.assertIn("sector_rotation.turning_point_volatility_stabilization_state", metric_references(snap))
        self.assertIn("sector_rotation.turning_point_technology_mag7_recovery_state", metric_references(snap))
        watch = snap["sections"]["sector_rotation"]["turning_point_watch"]
        self.assertEqual(set(watch["signals"]), {"volatility_stabilization", "technology_mag7_recovery",
            "broad_market_participation", "defensive_leadership", "credit_confirmation"})
        self.assertIn("sector_rotation.turning_point_credit_confirmation_state", metric_references(snap))
        self.assertEqual(snap["sections"]["sector_rotation"]["risk_appetite"]["methodology_version"], "risk-appetite-v5")
        self.assertEqual(snap["sections"]["catalysts"]["recent_developments"][0]["ticker"], "GOOGL")

    def test_v2_requires_comparison_coverage_and_valid_hypothesis_references(self):
        snap = build_market_snapshot({
            "regime": {"status": "available", "metrics": {"breadth": {"value": 7, "unit": "sectors",
                "window": "20D", "universe": "11 sectors", "definition": "above SMA50", "as_of": "2026-11-01"}}},
            "relative_strength": {"status": "available", "ai_comparison_ids": ["RSP_SPY_20D"],
                "comparisons": {"RSP_SPY_20D": {}}, "metrics": {"rsp_spy_excess": {"value": 1.2,
                "unit": "pp", "window": "20 sessions", "universe": "RSP/SPY", "definition": "matched returns", "as_of": "2026-11-01"}}}})
        refs = sorted(metric_references(snap))
        response = {"schema_version": "market-analyst-response-v2", "highlights": [
            {"section_id": section, "headline": "No material change", "category": "insufficient evidence",
             "insight": "No supported new interpretation.", "why_it_matters": "No conclusion is warranted.",
             "monitor_next": "Wait for another observation.", "metric_refs": []} for section in SECTION_ORDER],
            "comparison_highlights": [{"comparison_id": "RSP_SPY_20D", "headline": "Equal weight remains close",
                "category": "mixed", "insight": "The matched relative return is modest.",
                "monitor_next": "Watch whether the difference persists.", "metric_refs": ["relative_strength.rsp_spy_excess"]}],
            "briefing": {"executive_takeaway": "Participation remains mixed.", "risks": [], "supportive": [],
                "sector_implications": [], "conflicting_evidence": [], "catalysts": [], "conditions": [],
                "hypotheses": [], "metric_refs": {"executive_takeaway": ["regime.breadth"], "risks": [],
                    "supportive": [], "sector_implications": [], "conflicting_evidence": [], "catalysts": [], "conditions": [], "hypotheses": []}}}
        validated = validate_market_analysis(response, snap)
        self.assertEqual(validated["comparison_highlights"][0]["comparison_id"], "RSP_SPY_20D")
        response["comparison_highlights"] = []
        with self.assertRaisesRegex(ValueError, "cover every standard comparison"):
            validate_market_analysis(response, snap)

    def test_comparison_only_deltas_compatible_units_windows_universes_and_definitions(self):
        old, new = snapshot(1), snapshot(4)
        result = compare_market_snapshots(new, old)
        self.assertEqual(result["status"], "comparable")
        self.assertEqual(result["changes"][0]["change"], 3)
        incompatible = compare_market_snapshots(snapshot(4, 60), old)
        self.assertEqual(incompatible["status"], "no_compatible_metrics")
        self.assertEqual(incompatible["incompatible"], ["regime.rsp_spy"])
        self.assertEqual(compare_market_snapshots(new, None)["status"], "no_previous_snapshot")

    def test_structured_response_requires_all_highlights_and_valid_metric_references(self):
        snap = snapshot()
        payload = valid_response(snap)
        rendered = parse_market_analysis_json(__import__("json").dumps(payload), snap)
        self.assertIn("Market regime / key takeaway", analysis_to_markdown(rendered))
        payload["highlights"][0]["metric_refs"] = ["regime.nonexistent"]
        with self.assertRaisesRegex(ValueError, "regime.nonexistent"):
            parse_market_analysis_json(__import__("json").dumps(payload), snap)
        payload = valid_response(snap)
        payload["highlights"].pop()
        with self.assertRaisesRegex(ValueError, "Missing highlights"):
            parse_market_analysis_json(__import__("json").dumps(payload), snap)

    def test_deepseek_prompt_provides_finite_section_specific_metric_registry(self):
        from dashboard.deepseek_market_research import _market_analyst_prompt
        snap = snapshot()
        prompt = _market_analyst_prompt("Analyze snapshot", snap, [], "dashboard_analysis")
        self.assertIn('"regime.rsp_spy"', prompt)
        self.assertIn("Allowed refs grouped by section", prompt)
        self.assertIn("never derive or abbreviate an ID", prompt)
        self.assertIn('"signal_id":"volatility_stabilization"', prompt)
        self.assertIn("Include every signal_id exactly once", prompt)

    def test_known_qqq_spy_metric_alias_maps_to_the_canonical_regime_metric(self):
        snap = build_market_snapshot({
            "regime": {"as_of": "2026-11-06", "status": "available", "metrics": {
                "broad_qqq_20d_excess_vs_spy": {"value": -7.1089731034, "unit": "pp",
                    "window": "20 sessions", "universe": "QQQ/SPY", "definition": "QQQ return minus SPY return",
                    "as_of": "2026-11-06"}}}})
        payload = valid_response(snap)
        payload["highlights"][0]["metric_refs"] = ["relative_strength.qqq_spy_20d_excess_pp"]
        validated = validate_market_analysis(payload, snap)
        self.assertEqual(validated["highlights"][0]["metric_refs"], [
            "regime.broad_qqq_20d_excess_vs_spy"])

    def test_known_broad_index_alias_is_not_mapped_when_observation_is_unavailable(self):
        snap = snapshot()
        payload = valid_response(snap)
        payload["highlights"][0]["metric_refs"] = ["relative_strength.qqq_spy_20d_excess_pp"]
        with self.assertRaisesRegex(ValueError, "invalid metric ID"):
            validate_market_analysis(payload, snap)

    def test_v5_transition_outlook_covers_four_signals_and_requires_explicit_ai_disagreement(self):
        snap = build_market_snapshot({
            "regime": {"as_of": "2026-11-02", "status": "available", "metrics": {
                "rsp_spy": {"value": 2.0, "unit": "pp", "window": "20D", "universe": "RSP/SPY",
                    "definition": "matched relative return", "as_of": "2026-11-02"}}},
            "sector_rotation": {"status": "available", "metrics": {
                "regime_transition_status": {"value": "Mixed / inconclusive", "unit": "state",
                    "window": "20D/5D", "universe": "broad US market", "definition": "Python rule state",
                    "as_of": "2026-11-02"},
                "regime_transition_rsp_momentum_change_pp": {"value": 0.4, "unit": "pp",
                    "window": "20D/5D", "universe": "RSP/SPY", "definition": "relative momentum change", "as_of": "2026-11-02"},
                "regime_transition_iwm_momentum_change_pp": {"value": -0.2, "unit": "pp",
                    "window": "20D/5D", "universe": "IWM/SPY", "definition": "relative momentum change", "as_of": "2026-11-02"}}}})
        payload = valid_response(snap)
        payload["schema_version"] = "market-analyst-response-v5"
        payload["comparison_highlights"] = []
        briefing = payload["briefing"]
        briefing["hypotheses"] = []
        briefing["transition_outlook"] = {
            "current_regime_interpretation": "The observed state remains mixed.",
            "direction_classification": "conflicting",
            "emerging_direction": "Breadth improved, while credit proxies weakened.",
            "scenarios": [{"scenario_id": "broader_risk_on_participation",
                "assessment": "plausible but unconfirmed",
                "interpretation": "A broader advance is possible if participation persists.",
                "monitor_next": "Check RSP/SPY and breadth over the next five sessions.",
                "metric_refs": ["regime_transition_rsp_momentum_change_pp", "regime_transition_iwm_momentum_change_pp"]}],
            "confirmation_invalidation": "Sustained breadth improvement would support the interpretation.",
            "main_uncertainty": "Credit confirmation remains weak.",
            "python_alignment": "disagrees",
            "disagreement_explanation": "The AI weighs improving breadth more heavily than the rule's mixed component state.",
            "turning_point_interpretations": [
                {"signal_id": signal_id, "interpretation": f"The {signal_id.replace('_', ' ')} observation remains conditional.",
                 "monitor_next": "Recheck the matched signal next week.",
                 "metric_refs": ["sector_rotation.regime_transition_status"]}
                for signal_id in ("volatility_stabilization", "technology_mag7_recovery",
                    "broad_market_participation", "defensive_leadership")],
            "metric_refs": ["sector_rotation.regime_transition_status", "regime.rsp_spy"]}
        briefing["metric_refs"] = {
            "executive_takeaway": ["regime.rsp_spy"], "risks": [], "supportive": [],
            "sector_implications": [], "conflicting_evidence": [], "catalysts": [],
            "conditions": [], "hypotheses": [],
            "transition_outlook": ["sector_rotation.regime_transition_status", "regime.rsp_spy"]}
        validated = validate_market_analysis(payload, snap)
        self.assertEqual(validated["briefing"]["transition_outlook"]["python_alignment"], "disagrees")
        self.assertEqual(len(validated["briefing"]["transition_outlook"]["turning_point_interpretations"]), 4)
        self.assertEqual(validated["briefing"]["transition_outlook"]["scenarios"][0]["metric_refs"], [
            "sector_rotation.regime_transition_rsp_momentum_change_pp",
            "sector_rotation.regime_transition_iwm_momentum_change_pp"])
        v6 = __import__("copy").deepcopy(validated)
        v6["schema_version"] = "market-analyst-response-v6"
        v6["briefing"]["transition_outlook"]["turning_point_interpretations"].append({
            "signal_id": "credit_confirmation", "interpretation": "Credit proxies are mixed.",
            "monitor_next": "Check HYG relative performance on aligned dates.",
            "metric_refs": ["sector_rotation.regime_transition_status"]})
        self.assertEqual(len(validate_market_analysis(v6, snap)["briefing"]["transition_outlook"]["turning_point_interpretations"]), 5)
        # Saved v4 reports predate the four per-signal interpretations and remain valid.
        old_v4 = __import__("copy").deepcopy(payload)
        old_v4["schema_version"] = "market-analyst-response-v4"
        old_v4["briefing"]["transition_outlook"].pop("turning_point_interpretations")
        self.assertEqual(validate_market_analysis(old_v4, snap)["schema_version"], "market-analyst-response-v4")
        payload["briefing"]["transition_outlook"]["disagreement_explanation"] = ""
        with self.assertRaisesRegex(ValueError, "required when AI differs"):
            validate_market_analysis(payload, snap)

    def test_v6_turning_point_ids_are_recovered_only_from_unique_local_metric_refs(self):
        snap = snapshot()
        signal_ids = (
            "volatility_stabilization", "technology_mag7_recovery",
            "broad_market_participation", "defensive_leadership", "credit_confirmation",
        )
        metrics = snap["sections"]["sector_rotation"]["metrics"]
        for signal_id in signal_ids:
            metrics[f"turning_point_{signal_id}_state"] = {
                "value": "mixed", "unit": "state", "window": "5D/20D",
                "universe": signal_id, "definition": "deterministic signal", "as_of": "2026-11-01",
            }
        payload = valid_response(snap)
        payload["schema_version"] = "market-analyst-response-v6"
        payload["comparison_highlights"] = []
        payload["briefing"]["hypotheses"] = []
        payload["briefing"]["transition_outlook"] = {
            "current_regime_interpretation": "The observed state remains mixed.",
            "direction_classification": "conflicting",
            "emerging_direction": "Breadth and credit evidence diverge.",
            "scenarios": [],
            "turning_point_interpretations": [
                {"interpretation": f"Signal {index} remains conditional.",
                 "monitor_next": "Recheck the matched measurement next session.",
                 "metric_refs": [f"sector_rotation.turning_point_{signal_id}_state"]}
                for index, signal_id in enumerate(signal_ids)
            ],
            "confirmation_invalidation": "Persistence would clarify the direction.",
            "main_uncertainty": "Credit and breadth disagree.",
            "python_alignment": "inconclusive",
            "disagreement_explanation": "",
            "metric_refs": ["sector_rotation.turning_point_volatility_stabilization_state"],
        }
        payload["briefing"]["metric_refs"] = {
            "executive_takeaway": ["sector_rotation.turning_point_volatility_stabilization_state"], "risks": [], "supportive": [],
            "sector_implications": [], "conflicting_evidence": [], "catalysts": [],
            "conditions": [], "hypotheses": [],
            "transition_outlook": ["sector_rotation.turning_point_volatility_stabilization_state"],
        }
        validated = validate_market_analysis(payload, snap)
        actual_ids = [item["signal_id"] for item in validated["briefing"]["transition_outlook"]["turning_point_interpretations"]]
        self.assertEqual(actual_ids, list(signal_ids))
        # Validation canonicalizes a copy and does not mutate the model's raw JSON.
        self.assertNotIn("signal_id", payload["briefing"]["transition_outlook"]["turning_point_interpretations"][0])

        duplicate = __import__("copy").deepcopy(validated)
        duplicate_items = duplicate["briefing"]["transition_outlook"]["turning_point_interpretations"]
        duplicate_items[1]["signal_id"] = duplicate_items[0]["signal_id"]
        with self.assertRaisesRegex(ValueError, "duplicates"):
            validate_market_analysis(duplicate, snap)

        unknown = __import__("copy").deepcopy(validated)
        unknown["briefing"]["transition_outlook"]["turning_point_interpretations"][0]["signal_id"] = "vix_stress_unknown"
        with self.assertRaisesRegex(ValueError, "unknown value"):
            validate_market_analysis(unknown, snap)

    def test_missing_briefing_metric_index_is_rebuilt_from_existing_exact_citations(self):
        snap = snapshot()
        payload = valid_response(snap)
        payload["schema_version"] = "market-analyst-response-v5"
        payload["comparison_highlights"] = []
        payload["briefing"]["hypotheses"] = []
        payload["briefing"]["transition_outlook"] = {
            "current_regime_interpretation": "The observed regime is mixed.",
            "direction_classification": "conflicting",
            "emerging_direction": "No clear direction is established.", "scenarios": [],
            "confirmation_invalidation": "Wait for aligned breadth and credit evidence.",
            "main_uncertainty": "The next observations may diverge.",
            "python_alignment": "agrees", "disagreement_explanation": "",
            "turning_point_interpretations": [
                {"signal_id": signal_id, "interpretation": "No material change is established.",
                 "monitor_next": "Review the next matched observations.",
                 "metric_refs": ["regime.rsp_spy"]}
                for signal_id in ("volatility_stabilization", "technology_mag7_recovery",
                    "broad_market_participation", "defensive_leadership")],
            "metric_refs": ["regime.rsp_spy"]}
        payload["briefing"].pop("metric_refs")
        # This reproduces the live response shape: content and highlights were
        # complete, but DeepSeek omitted only the redundant top-level index.
        validated = validate_market_analysis(payload, snap)
        refs = validated["briefing"]["metric_refs"]
        self.assertIn("executive_takeaway", refs)
        self.assertEqual(set(refs), {"executive_takeaway", "risks", "supportive",
            "sector_implications", "conflicting_evidence", "catalysts", "conditions",
            "hypotheses", "transition_outlook"})
        self.assertTrue(refs["executive_takeaway"])
        self.assertEqual(refs["risks"], [])
        self.assertTrue(set(refs["executive_takeaway"]).issubset(metric_references(snap)))

    def test_missing_required_briefing_content_names_the_missing_section(self):
        snap = snapshot()
        payload = valid_response(snap)
        del payload["briefing"]["supportive"]
        with self.assertRaisesRegex(ValueError, "supportive"):
            validate_market_analysis(payload, snap)

    def test_old_saved_briefing_remains_valid_without_transition_outlook(self):
        snap = snapshot()
        old_payload = valid_response(snap)
        validated = validate_market_analysis(old_payload, snap)
        self.assertNotIn("transition_outlook", validated["briefing"])
        self.assertNotIn("Market Regime Transition Outlook", analysis_to_markdown(validated))

    def test_hypothesis_sector_implications_list_is_canonicalized_without_losing_text(self):
        snap = snapshot()
        payload = valid_response(snap)
        payload["schema_version"] = "market-analyst-response-v3"
        payload["comparison_highlights"] = []
        refs = sorted(metric_references(snap))
        payload["briefing"]["hypotheses"] = [{
            "title": "Leadership rotation", "supporting_evidence": "Defensives led.",
            "competing_explanation": "The move may be temporary.",
            "confirm_or_invalidate": "Check relative returns next week.",
            "sector_implications": ["Health care", "Consumer staples"],
            "metric_refs": refs,
        }]
        payload["briefing"]["metric_refs"]["hypotheses"] = refs
        validated = validate_market_analysis(payload, snap)
        self.assertEqual(validated["briefing"]["hypotheses"][0]["sector_implications"],
                         "Health care; Consumer staples")
        self.assertEqual(payload["briefing"]["hypotheses"][0]["sector_implications"],
                         ["Health care", "Consumer staples"])
        self.assertIn("Implications: Health care; Consumer staples", analysis_to_markdown(validated))

    def test_saved_highlight_report_selection_is_single_report_and_marks_staleness(self):
        current = snapshot()["fingerprint"]
        old = {"research_status": "complete", "report_text": "old", "snapshot": {"schema_version": "market-intelligence-snapshot-v1", "fingerprint": "old"}}
        exact = {"research_status": "complete", "report_text": "current", "snapshot": {"schema_version": "market-intelligence-snapshot-v1", "fingerprint": current}}
        failed = {"research_status": "failed", "report_text": "", "snapshot": exact["snapshot"]}
        self.assertIs(select_saved_market_report([old, exact, failed], current), exact)
        self.assertIs(select_saved_market_report([old, failed], current), old)
        self.assertTrue(saved_report_is_stale(old, current))
        self.assertFalse(saved_report_is_stale(exact, current))

    def test_stale_saved_ai_briefing_remains_visible_with_explicit_refresh_action(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit AppTest unavailable")
        current = snapshot()
        payload = valid_response(current)
        report = {"research_status": "complete", "generated_at": "2026-11-01T15:00:00+00:00",
            "model": "deepseek-flash", "report_text": json.dumps(payload), "sources": [],
            "snapshot": current, "usage": {"prompt_version": "market-analyst-v11",
                "actual_model": "deepseek-flash", "search_mode": "dashboard_analysis"}}
        script = ("import streamlit as st\n"
            "import json\n"
            "from dashboard.market_monitor_ui import render_saved_market_briefing\n"
            f"st.session_state['_market_saved_report_for_page'] = json.loads({json.dumps(report)!r})\n"
            f"st.session_state['_market_snapshot_for_page'] = json.loads({json.dumps(current)!r})\n"
            "render_saved_market_briefing()")
        result = AppTest.from_string(script).run()
        self.assertFalse(result.exception)
        markdown = "\n".join(str(item.value) for item in result.markdown)
        self.assertIn("AI Market Intelligence Desk", markdown)
        self.assertTrue(any("stale" in str(item.value).lower() for item in [*result.info, *result.caption]))
        self.assertIn("available market snapshot is mixed", markdown)
        self.assertTrue(any(button.label == "Refresh Full Analysis" for button in result.button))

    def test_provider_accent_is_centralized_and_not_a_signal_color(self):
        self.assertEqual(provider_accent("deepseek"), "#68a9ff")
        self.assertEqual(provider_accent("claude"), "#d8b46a")
        self.assertEqual(provider_accent("openai"), "#65c695")
        self.assertEqual(provider_accent(None), "#8f9aa8")


if __name__ == "__main__":
    unittest.main()

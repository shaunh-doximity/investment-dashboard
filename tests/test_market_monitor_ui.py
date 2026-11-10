import unittest
from unittest.mock import MagicMock, patch

from dashboard.market_monitor_ui import (CROSS_ASSET_RELATIONSHIP_VIEWS, MARKET_SECTION_NAV,
    INTEGRATED_OUTLOOK_LAYOUT, REGIME_OUTLOOK_COLORS, TURNING_POINT_SIGNAL_LABELS,
    _paired_market_chart_layout, scenario_status_label, turning_point_signal_color,
    scenario_status_color,
    VIX_SIGNAL_LAYOUT, _public_market_catalyst_rows)


class MarketMonitorUILayoutTests(unittest.TestCase):
    def test_contextual_highlight_is_rendered_in_visible_three_to_one_column(self):
        from dashboard import market_monitor_ui as ui
        main, side = MagicMock(), MagicMock()
        main.__enter__.return_value = main
        side.__enter__.return_value = side
        content = MagicMock()
        highlight = MagicMock()
        with patch.object(ui.st, "columns", return_value=(main, side)) as columns, \
             patch.object(ui, "_render_section_highlight", highlight), \
             patch.object(ui.st, "expander") as expander:
            ui._render_market_section("regime", {}, None, {"fingerprint": "fp"}, content)
        columns.assert_called_once_with([3, 1], gap="large", vertical_alignment="top")
        content.assert_called_once_with()
        highlight.assert_called_once_with("regime", {}, None, {"fingerprint": "fp"}, None, None, None)
        expander.assert_not_called()

    def test_each_existing_analysis_section_has_its_own_saved_highlight_id(self):
        from dashboard.market_snapshot import SECTION_ORDER
        self.assertEqual(SECTION_ORDER, ("catalysts", "regime", "sector_rotation", "relative_strength", "rates_macro", "vix"))
        self.assertNotIn("briefing", SECTION_ORDER)
        self.assertNotIn("risk_appetite", SECTION_ORDER)  # included in sector_rotation's canonical evidence
        import inspect
        from dashboard import market_monitor_ui as ui
        source = inspect.getsource(ui._render_overview)
        for section_id in SECTION_ORDER:
            section_source = inspect.getsource(ui._render_rates_credit) if section_id == "rates_macro" else source
            self.assertIn(f'_render_market_section("{section_id}"', section_source)
        self.assertNotIn("AI Highlight selector", source)

    def test_macro_proxy_universe_uses_cached_uso_not_spot_wti(self):
        from dashboard.market_history import MACRO_PROXY_SYMBOLS
        self.assertEqual(MACRO_PROXY_SYMBOLS, ("TLT", "GLD", "USO"))
        self.assertNotIn("WTI", MACRO_PROXY_SYMBOLS)

    def test_side_by_side_market_charts_share_plot_geometry(self):
        basket = _paired_market_chart_layout("Cumulative return (%) · 20D")
        comparison = _paired_market_chart_layout("Cumulative excess return (percentage points)")
        self.assertEqual(basket["height"], comparison["height"])
        self.assertEqual(basket["margin"], comparison["margin"])
        self.assertEqual(basket["legend"], comparison["legend"])
        self.assertEqual(basket["xaxis"], comparison["xaxis"])
        self.assertEqual(basket["yaxis"], comparison["yaxis"])
        self.assertEqual(basket["hovermode"], "x unified")

    def test_vix_layout_reserves_three_quarters_for_state_and_chart(self):
        self.assertEqual(VIX_SIGNAL_LAYOUT["column_ratio"], (1.0, 1.25))
        self.assertTrue(VIX_SIGNAL_LAYOUT["responsive_stack"])
        self.assertEqual(VIX_SIGNAL_LAYOUT["primary_headline"], "VIX close")
        self.assertEqual(len(VIX_SIGNAL_LAYOUT["signal_cards"]), 6)
        self.assertEqual(len(set(VIX_SIGNAL_LAYOUT["signal_cards"])), 6)
        self.assertNotIn("VIX close", VIX_SIGNAL_LAYOUT["signal_cards"])
        self.assertIn("chart", VIX_SIGNAL_LAYOUT["chart_heading"].lower())

    def test_market_navigation_uses_unique_stable_anchors_and_existing_section_ids(self):
        labels, anchors, section_ids = zip(*MARKET_SECTION_NAV)
        self.assertEqual(len(labels), 7)
        self.assertEqual(len(set(anchors)), 7)
        self.assertEqual(len(set(section_ids)), 7)
        self.assertIn(("AI Market Intelligence Desk", "ai-market-briefing", "briefing"), MARKET_SECTION_NAV)
        self.assertEqual(section_ids, ("catalysts", "regime", "sector_rotation", "relative_strength", "rates_macro", "vix", "briefing"))
        self.assertEqual(len(MARKET_SECTION_NAV), 7)

    def test_integrated_outlook_uses_wrapping_responsive_cards_and_semantic_colors(self):
        self.assertEqual(INTEGRATED_OUTLOOK_LAYOUT["scenario_columns"], 2)
        self.assertEqual(INTEGRATED_OUTLOOK_LAYOUT["signal_columns"], 2)
        self.assertTrue(INTEGRATED_OUTLOOK_LAYOUT["responsive_stack"])
        self.assertEqual(INTEGRATED_OUTLOOK_LAYOUT["principal_headline_wrap"], "anywhere")
        self.assertFalse(INTEGRATED_OUTLOOK_LAYOUT["fixed_headline_truncation"])
        self.assertEqual(REGIME_OUTLOOK_COLORS["regime"]["Rotation / Mixed"], "#e3ad4f")
        self.assertEqual(REGIME_OUTLOOK_COLORS["regime"]["Risk-off"], "#e05a67")
        self.assertEqual(TURNING_POINT_SIGNAL_LABELS["broad_market_participation"], "Broad Equity Participation")
        self.assertEqual(TURNING_POINT_SIGNAL_LABELS["credit_confirmation"], "Credit Confirmation")
        self.assertEqual(len(TURNING_POINT_SIGNAL_LABELS), 5)
        self.assertEqual(turning_point_signal_color("defensive_leadership", "Dominant and strengthening"), "#e3ad4f")
        self.assertEqual(turning_point_signal_color("volatility_stabilization", "Pressure rising"), "#e05a67")
        self.assertEqual(turning_point_signal_color("volatility_stabilization", "Pressure easing"), "#35b779")
        self.assertEqual(scenario_status_label("supported"), "Supported now")
        self.assertEqual(scenario_status_label("plausible but unconfirmed"), "Plausible but unconfirmed")
        self.assertEqual(scenario_status_color("supported", "stress_spreading_across_sectors"), "#e05a67")
        self.assertEqual(scenario_status_color("plausible but unconfirmed", "broader_risk_on_participation"), "#e3ad4f")

    def test_integrated_outlook_has_one_primary_narrative_heading(self):
        import inspect
        from dashboard import market_monitor_ui as ui
        source = inspect.getsource(ui.render_saved_market_briefing)
        self.assertIn("AI Market Intelligence Desk", source)
        self.assertIn("_render_market_wide_chat", source)
        self.assertIn("_render_premarket_what_if", source)
        self.assertNotIn("render_market_scenario_notebook", source)
        self.assertNotIn("Risk Appetite & Market Regime Outlook", source)
        self.assertNotIn("Integrated AI Market Briefing", source)
        overview_source = inspect.getsource(ui._render_overview)
        self.assertEqual(overview_source.count("render_saved_market_briefing("), 1)
        self.assertGreater(overview_source.find("render_saved_market_briefing("), overview_source.find("_render_vix_monitor"))

    def test_broad_index_chart_is_regime_only_and_rates_keeps_advanced_cross_asset_views(self):
        self.assertEqual(CROSS_ASSET_RELATIONSHIP_VIEWS, (
            "ETF returns vs macro changes", "Rolling correlations", "Scatter relationship", "Exploratory lead / lag"))
        self.assertNotIn("Normalized ETF performance", CROSS_ASSET_RELATIONSHIP_VIEWS)

    def test_catalyst_snapshot_prioritizes_public_events_without_enumerating_private_tickers(self):
        rows = _public_market_catalyst_rows([
            {"name":"Manual private earnings", "category":"Earnings", "event_date":"2026-11-03",
             "related_symbols":["MYPRIVATEWATCH"]},
            {"name":"AAPL earnings", "category":"Earnings", "event_date":"2026-11-04",
             "related_symbols":["AAPL"], "status":"Estimated"},
            {"name":"CPI", "category":"Macro", "event_date":"2026-11-02", "status":"Confirmed"},
        ])
        self.assertEqual([row["name"] for row in rows], ["CPI", "AAPL earnings"])
        self.assertNotIn("MYPRIVATEWATCH", str(rows))

    def test_user_confirmation_is_separate_from_independent_source_verification(self):
        rows = _public_market_catalyst_rows([
            {"name":"AAPL earnings", "category":"Earnings", "event_date":"2026-11-04",
             "related_symbols":["AAPL"], "status":"Confirmed", "provider":"manual_earnings",
             "source_verification":"User-reported"},
            {"name":"Official CPI", "category":"Macro", "event_date":"2026-11-05",
             "status":"Confirmed", "provider":"bls_ical", "source_verification":"Official source"},
            {"name":"Unverified GDP estimate", "category":"Macro", "status":"Estimated",
             "provider":"manual_public", "source_verification":"User-reported"},
        ])
        by_name = {row["name"]: row for row in rows}
        aapl, cpi, gdp = by_name["AAPL earnings"], by_name["Official CPI"], by_name["Unverified GDP estimate"]
        self.assertEqual(aapl["confirmation_state"], "User-confirmed")
        self.assertTrue(aapl["user_confirmed"])
        self.assertFalse(aapl["source_verified"])
        self.assertEqual(cpi["confirmation_state"], "Source-verified")
        self.assertTrue(cpi["source_verified"])
        self.assertEqual(gdp["confirmation_state"], "Unconfirmed")


if __name__ == "__main__":
    unittest.main()

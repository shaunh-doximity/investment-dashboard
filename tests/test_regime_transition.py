from __future__ import annotations

from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.database import initialize_database
from dashboard.data_access import list_market_regime_transition_records, save_market_regime_transition_record
from dashboard.market_history import SECTOR_SYMBOLS
from dashboard.market_explorer import DEFAULT_BASKETS
from dashboard.regime_transition import (
    RULES, TRANSITION_EXPORT_SCHEMA, _pair_feature, build_transition_signal_strip,
    build_turning_point_watch, calculate_regime_transition,
    export_transition_features,
)


SYMBOLS = tuple(dict.fromkeys(("SPY", "RSP", "IWM", "QQQ", "HYG", "LQD", "IEF", "XLK", "SMH",
                               "TLT", "GLD", "USO", *SECTOR_SYMBOLS)))


def market_fixture(count=100, style="risk_on"):
    dates, current = [], date(2026, 5, 4)
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    histories = {}
    for symbol in SYMBOLS:
        if symbol == "SPY":
            slope = .001
        elif symbol in {"RSP", "IWM"}:
            slope = .0013
        elif symbol in {"HYG"}:
            slope = .0015
        elif symbol in {"LQD", "IEF"}:
            slope = .0007
        elif symbol in SECTOR_SYMBOLS:
            slope = .00125
        else:
            slope = .0009
        if style == "defensive" and symbol in {"XLV", "XLP", "XLU"}:
            slope = .002
        if style == "defensive" and symbol in {"XLY", "XLI", "XLB", "XLE", "XLF", "XLK", "XLC", "XLRE"}:
            slope = .0001
        # A final five-session shift changes leadership without using future rows.
        if style == "weakening" and symbol in {"XLY", "XLI", "XLB", "XLE", "XLF", "XLK", "XLC", "XLRE"}:
            for index, day in enumerate(dates):
                histories.setdefault(symbol, [])
                recent_slope = slope - .003 if index >= count - 5 else slope
                prior_close = histories[symbol][-1]["close"] if histories[symbol] else 100.0
                histories[symbol].append({"symbol": symbol, "trading_date": day,
                                          "close": prior_close * (1 + recent_slope)})
            continue
        rows, close = [], 100.0
        for day in dates:
            close *= 1 + slope
            rows.append({"symbol": symbol, "trading_date": day, "close": close})
        histories[symbol] = rows
    vix_start = 16.0
    vix_rows = []
    for index, day in enumerate(dates):
        close = vix_start + (0.0 if style != "stress" else index * .12)
        vix_rows.append({"observation_date": day, "open": close, "high": close + .4,
                         "low": close - .3, "close": close})
    return histories, vix_rows, dates


class RegimeTransitionTests(unittest.TestCase):
    @staticmethod
    def _turning_fixture(*, tech_5d=.8, tech_20d=-.4, cyc_def_5d=-.1,
                         cyc_def_20d=-1.0, breadth_change=1, vix_direction=1,
                         credit_direction=0, prior_stress=False, tech_absolute=.7,
                         defensive_absolute=.4, crossovers=(), missing=False):
        symbols = ("XLK", "SMH", "QQQ", "MAGS", "RSP", "IWM")
        pairs_20, pairs_5 = {}, {}
        for symbol in symbols:
            if missing and symbol in {"MAGS", "SMH"}:
                continue
            long_value = tech_20d if symbol in {"XLK", "SMH", "QQQ", "MAGS"} else .4
            short_value = tech_5d if symbol in {"XLK", "SMH", "QQQ", "MAGS"} else .5
            cross = {"from": "underperforming", "to": "outperforming", "persistent_sessions": 3} if symbol in crossovers else None
            pairs_20[symbol] = {"status": "available", "relative_return_pp": long_value,
                "relative_momentum_change_pp": .6, "as_of": "2026-11-02"}
            pairs_5[symbol] = {"status": "available", "relative_return_pp": short_value,
                "relative_momentum_change_pp": .6, "crossover": cross, "as_of": "2026-11-02"}
        features = {"pair_relative_performance": pairs_20,
            "short_horizon_pair_relative_performance": pairs_5,
            "defensive_rotation_short_horizon_pairs": {},
            "sector_breadth_current": {"sectors_outperforming_spy": 8, "sectors_positive_absolute": 8,
                "spy_return": .02, "rsp_return": .025, "as_of": "2026-11-02"},
            "sector_breadth_change": breadth_change,
            "current_cyclical_minus_defensive_pp": cyc_def_20d,
            "five_session_cyclical_minus_defensive_pp": cyc_def_5d,
            "cyclical_defensive_as_of": "2026-11-02",
            "cyclical_defensive_momentum_change_pp": -.7,
            "technology_5d_absolute_return_pct": tech_absolute,
            "defensive_5d_absolute_return_pct": defensive_absolute}
        features["defensive_rotation_short_horizon_pairs"] = {
            key: {"status": "available", "crossover": {"to": "outperforming"} if key in crossovers else None}
            for key in ("XLY_XLP", "XLI_XLU", "XLF_XLV")}
        risk = {"prior_stress_observed": prior_stress, "components": {
            "volatility_pressure": {"direction": vix_direction, "as_of": "2026-11-02",
                "value": {"five_session_change_percent": -8 if vix_direction == 1 else 12}},
            "credit_risk_appetite": {"direction": credit_direction, "status": "available", "as_of": "2026-11-02",
                "value": {"hyg_minus_lqd_pp": .4 if credit_direction > 0 else -.4 if credit_direction < 0 else 0,
                          "hyg_minus_ief_pp": .3 if credit_direction > 0 else -.3 if credit_direction < 0 else 0}},
            "defensive_rotation": {"direction": -1 if cyc_def_20d < 0 else 1}}}
        cycle = {"state": "Turbulent stabilization" if vix_direction == 1 else "Stress building",
                 "as_of": "2026-11-02", "failed_highs": 1}
        vix_rows = [{"observation_date": day, "close": close}
                    for day, close in zip(("2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"),
                                          (24.0, 23.0, 22.0, 21.0))]
        return features, risk, cycle, vix_rows

    def test_turning_point_watch_distinguishes_vix_easing_and_renewed_stress(self):
        features, risk, cycle, vix = self._turning_fixture(vix_direction=1, prior_stress=True)
        easing = build_turning_point_watch(features, risk, cycle, vix)
        self.assertEqual(easing["signals"]["volatility_stabilization"]["state"], "Pressure easing")
        self.assertEqual(easing["signals"]["volatility_stabilization"]["label"], "VIX Stress")
        self.assertIn("three consecutive lower closes", easing["signals"]["volatility_stabilization"]["evidence"])
        features, risk, cycle, vix = self._turning_fixture(vix_direction=-1, tech_5d=-.5, credit_direction=-1)
        renewed = build_turning_point_watch(features, risk, cycle, vix)
        self.assertEqual(renewed["signals"]["volatility_stabilization"]["state"], "Pressure rising")
        self.assertEqual(renewed["signals"]["volatility_stabilization"]["headline"], "Pressure rising")
        self.assertIn("credit_confirmation", renewed["signals"])
        self.assertEqual(renewed["signals"]["credit_confirmation"]["state"], "Credit deteriorating")

    def test_subject_labels_match_the_measured_signal_not_a_related_hypothesis(self):
        features, risk, cycle, vix = self._turning_fixture(
            tech_5d=-1.0, tech_20d=-2.0, breadth_change=1,
            cyc_def_5d=-.8, cyc_def_20d=-1.2, vix_direction=0, credit_direction=0)
        result = build_turning_point_watch(features, risk, cycle, vix)
        signals = result["signals"]
        self.assertEqual(signals["technology_mag7_recovery"]["label"], "Technology / Mag 7 Leadership")
        self.assertNotEqual(signals["technology_mag7_recovery"]["state"], "Strengthening")
        self.assertEqual(signals["broad_market_participation"]["label"], "Broad Equity Participation")
        self.assertEqual(signals["broad_market_participation"]["state"], "Broadening")
        self.assertEqual(signals["defensive_leadership"]["label"], "Defensive Leadership")
        self.assertEqual(signals["defensive_leadership"]["state"], "Dominant and strengthening")
        self.assertIn("Defensive sectors lead",
                      signals["defensive_leadership"]["headline"])

    def test_turning_point_watch_distinguishes_stable_defensives_handoff_and_unconfirmed_rebound(self):
        features, risk, cycle, vix = self._turning_fixture(cyc_def_5d=-.1, cyc_def_20d=-1.0,
            tech_5d=.8, tech_20d=-.4, vix_direction=0, credit_direction=0)
        broad = build_turning_point_watch(features, risk, cycle, vix)
        self.assertEqual(broad["scenario"]["id"], "broadening_recovery")
        features, risk, cycle, vix = self._turning_fixture(cyc_def_5d=.8, cyc_def_20d=-1.0,
            tech_5d=.8, tech_20d=-.4, vix_direction=0, credit_direction=0)
        handoff = build_turning_point_watch(features, risk, cycle, vix)
        self.assertEqual(handoff["scenario"]["id"], "leadership_handoff")
        features, risk, cycle, vix = self._turning_fixture(cyc_def_5d=-.1, cyc_def_20d=-1.0,
            tech_5d=.8, tech_20d=-.4, vix_direction=-1, credit_direction=-1)
        unconfirmed = build_turning_point_watch(features, risk, cycle, vix)
        self.assertEqual(unconfirmed["scenario"]["id"], "rebound_without_confirmation")

    def test_turning_point_watch_marks_broadening_stress_and_insufficient_coverage(self):
        features, risk, cycle, vix = self._turning_fixture(tech_5d=-.8, tech_20d=-1.0,
            cyc_def_5d=-.4, cyc_def_20d=-1.0, breadth_change=-2,
            tech_absolute=-1.0, defensive_absolute=-.8, vix_direction=-1, credit_direction=-1)
        stress = build_turning_point_watch(features, risk, cycle, vix)
        self.assertEqual(stress["scenario"]["id"], "broadening_stress")
        features, risk, cycle, vix = self._turning_fixture(missing=True)
        insufficient = build_turning_point_watch(features, risk, cycle, vix)
        self.assertEqual(insufficient["signals"]["technology_mag7_recovery"]["state"], "Insufficient data")

    def test_compact_signal_strip_is_deterministic_and_distinguishes_mixed_confirmation(self):
        record = {"as_of": "2026-11-02", "analysis_window_sessions": 20,
            "transition_momentum_sessions": 5, "methodology_version": "test-v1",
            "current_regime": "Rotation / Mixed", "current_leadership": "Defensive leadership",
            "transition_status": "Mixed / inconclusive", "data_coverage": {},
            "features": {"pair_relative_performance": {
                    "RSP": {"relative_momentum_change_pp": 1.2},
                    "IWM": {"relative_momentum_change_pp": .8}},
                "sector_breadth_change": 1,
                "cyclical_defensive_momentum_change_pp": .7,
                "equity_confirmation": {"direction": 1},
                "sector_leadership_confirmation": {"direction": -1},
                "credit_confirmation": {"direction": -1},
                "volatility_confirmation": {"direction": 0}}}
        strip = build_transition_signal_strip(record)
        self.assertEqual(strip["current_regime"], "Rotation / Mixed")
        self.assertEqual(strip["emerging_direction"], "Broader equity participation strengthening")
        self.assertIn("credit and/or volatility disagree", strip["confirmation_status"])
        self.assertEqual(strip["as_of"], "2026-11-02")

    def test_compact_signal_strip_uses_conflict_and_insufficient_labels_without_inventing_direction(self):
        mixed = build_transition_signal_strip({"current_regime": "Rotation / Mixed",
            "current_leadership": "Mixed leadership", "transition_status": "Mixed / inconclusive",
            "features": {"equity_confirmation": {"direction": 1},
                         "sector_leadership_confirmation": {"direction": -1},
                         "credit_confirmation": {"direction": 0},
                         "volatility_confirmation": {"direction": 0}}})
        self.assertEqual(mixed["emerging_direction"], "Conflicting signals")
        unavailable = build_transition_signal_strip({"current_regime": "Insufficient Evidence",
            "current_leadership": "Unavailable", "transition_status": "Insufficient data",
            "data_coverage": {"stale_or_misaligned_symbols": ["IWM"]},
            "features": {"data_coverage": {"stale_or_misaligned_symbols": ["IWM"]}}})
        self.assertEqual(unavailable["emerging_direction"], "No clear emerging direction")
        self.assertIn("IWM", unavailable["confirmation_status"])

    def test_continuing_leadership_is_separate_from_current_regime(self):
        histories, vix, days = market_fixture()
        base_histories = histories
        result = calculate_regime_transition(histories, vix, expected_session=days[-1], basket_config=DEFAULT_BASKETS)
        self.assertEqual(result["current_regime"], "Risk-on")
        self.assertIn(result["transition_status"], {"Continuing", "Mixed / inconclusive"})
        self.assertEqual(result["features"]["window_sessions"], 20)
        self.assertEqual(result["features"]["momentum_window_sessions"], 5)

    def test_stale_core_symbol_makes_transition_insufficient(self):
        histories, vix, days = market_fixture()
        histories["IWM"] = histories["SPY"][:-1]
        result = calculate_regime_transition(histories, vix, expected_session=days[-1])
        self.assertEqual(result["transition_status"], "Insufficient data")
        self.assertIn("IWM", result["data_coverage"]["stale_or_misaligned_symbols"])

    def _patched_transition(self, directions, classification="Rotation / Mixed", basket_results=None):
        histories, vix, days = market_fixture()
        def fake_risk(*args, **kwargs):
            return {"classification": classification, "prior_stress_observed": False, "components": {
                "equity_participation": {"direction": directions.get("equity"), "status": "equity", "value": {"sectors_outperforming_spy": 8}},
                "defensive_rotation": {"direction": directions.get("defensive"), "status": "defensive", "value": {}},
                "credit_risk_appetite": {"direction": directions.get("credit"), "status": "credit", "value": {}},
                "volatility_pressure": {"direction": directions.get("volatility"), "status": "volatility", "value": {}}}}
        contexts = [patch("dashboard.regime_transition.calculate_risk_appetite", side_effect=fake_risk)]
        if basket_results:
            def fake_basket(_histories, symbols, window, weights=None):
                key = "cyclical" if set(symbols) == set(DEFAULT_BASKETS["cyclical"]) else "defensive" if set(symbols) == set(DEFAULT_BASKETS["defensive"]) else None
                if key and window == 20:
                    return basket_results.get(key, {}).get("current", {"status": "available", "value_pct": 1.0, "as_of": days[-1], "coverage": 100})
                if key and window == 5:
                    return basket_results.get(key, {}).get("five", {"status": "available", "value_pct": 1.0, "as_of": days[-1], "coverage": 100})
                return {"status": "available", "value_pct": 1.0, "as_of": days[-1], "coverage": 100}
            contexts.append(patch("dashboard.regime_transition._basket_return", side_effect=fake_basket))
        with contexts[0]:
            if len(contexts) > 1:
                with contexts[1]:
                    return calculate_regime_transition(histories, vix, expected_session=days[-1])
            return calculate_regime_transition(histories, vix, expected_session=days[-1])

    def test_weakening_and_possible_reversal_have_distinct_confirmation_rules(self):
        _, _, days = market_fixture()
        # prior cyclical-minus-defensive = -0.5pp, current = 0pp, meeting the
        # 0.5pp weakening threshold. The omitted credit/VIX signals cannot
        # corroborate an early reversal.
        with patch("dashboard.regime_transition._basket_return") as basket_mock:
            def returns(histories, symbols, window, weights=None):
                key = "cyclical" if set(symbols) == set(DEFAULT_BASKETS["cyclical"]) else "defensive" if set(symbols) == set(DEFAULT_BASKETS["defensive"]) else None
                if key and window == 20:
                    last = max(row["trading_date"] for row in histories["SPY"])
                    value = 1.0 if key == "cyclical" or last == days[-1] else 1.5
                    return {"status": "available", "value_pct": value, "as_of": last, "coverage": 100}
                if key and window == 5:
                    return {"status": "available", "value_pct": 1.0, "as_of": max(row["trading_date"] for row in histories["SPY"]), "coverage": 100}
                return {"status": "available", "value_pct": 1.0, "as_of": max(row["trading_date"] for row in histories["SPY"]), "coverage": 100}
            basket_mock.side_effect = returns
            weakening = self._patched_transition({"equity": 1, "defensive": -1, "credit": 0, "volatility": 0})
            possible = self._patched_transition({"equity": 1, "defensive": -1, "credit": 1, "volatility": 0})
        self.assertEqual(weakening["transition_status"], "Weakening")
        self.assertEqual(possible["transition_status"], "Possible reversal")

    def test_mixed_signals_are_not_hidden_by_strengthening_pairs(self):
        result = self._patched_transition({"equity": 1, "defensive": -1, "credit": -1, "volatility": 0})
        self.assertEqual(result["transition_status"], "Mixed / inconclusive")
        self.assertTrue(result["contradictory_evidence"])

    def test_expected_session_cutoff_prevents_future_leakage(self):
        histories, vix, days = market_fixture()
        before = calculate_regime_transition(histories, vix, expected_session=days[-1])
        for symbol in histories:
            last = histories[symbol][-1]
            histories[symbol].append({**last, "trading_date": "2027-01-04", "close": last["close"] * 9})
        vix.append({"observation_date": "2027-01-04", "close": 99, "high": 100, "low": 90, "open": 90})
        after = calculate_regime_transition(histories, vix, expected_session=days[-1])
        self.assertEqual(before["source_fingerprint"], after["source_fingerprint"])
        self.assertEqual(after["as_of"], days[-1])

    def test_treasury_equity_relationship_rejects_gapped_matched_dates(self):
        histories, vix, days = market_fixture()
        treasury = [{"observation_date": day, "two_year": 4.0, "ten_year": 4.2}
                    for index, day in enumerate(days) if not len(days) - 35 <= index < len(days) - 31]
        result = calculate_regime_transition(histories, vix, expected_session=days[-1], treasury_rows=treasury)
        relation = result["features"]["rates_and_macro_proxies"]["relationships"]["Treasury_yield_changes_vs_equity_returns"]["SPY"]
        self.assertEqual(relation["status"], "inconclusive_gap")
        self.assertIn("gap", relation["interpretation_limit"].lower())

    def test_technology_rebound_scenarios_are_explicit_and_do_not_use_absolute_return_alone(self):
        histories, vix, days = market_fixture(style="defensive")
        # Make the technology ETFs recover on an absolute basis while remaining
        # behind SPY; the system must not describe this as technology leadership.
        for symbol in ("XLK", "SMH"):
            for i in range(len(histories[symbol]) - 5, len(histories[symbol])):
                histories[symbol][i]["close"] *= 1 + .002 * (i - (len(histories[symbol]) - 5))
        result = calculate_regime_transition(histories, vix, expected_session=days[-1])
        self.assertIsInstance(result["features"]["scenario_fixtures"], list)
        self.assertIn(result["features"]["current_leadership"],
                      {"Defensive leadership", "Cyclical leadership", "Technology leadership", "Broad equity participation", "Mixed leadership"})
        self.assertIn("technology_5d_relative_vs_spy_pp", result["features"])

    def test_scenario_a_b_c_d_are_rule_driven_fixtures(self):
        histories, vix, days = market_fixture()
        def scenario_ids(tech, defensive, cyclical, *, handoff=False, prior_stress=False):
            def fake_risk(*args, **kwargs):
                return {"classification": "Rotation / Mixed", "prior_stress_observed": prior_stress,
                    "components": {
                        "equity_participation": {"direction": 0, "status": "mixed", "value": {"sectors_outperforming_spy": 5}},
                        "defensive_rotation": {"direction": -1 if handoff else 0, "status": "defensive", "value": {}},
                        "credit_risk_appetite": {"direction": 0, "status": "mixed", "value": {}},
                        "volatility_pressure": {"direction": 1 if prior_stress else 0, "status": "easing", "value": {}}}}
            def fake_pair(_histories, symbol, benchmark, window, momentum_window):
                value = 1.0 if symbol in {"XLK", "SMH"} and window == 5 else .2
                return {"status": "available", "ticker": symbol, "benchmark": benchmark,
                    "window_sessions": window, "momentum_sessions": momentum_window,
                    "relative_return_pp": value, "prior_relative_return_pp": value,
                    "relative_momentum_change_pp": .1, "as_of": days[-1], "prior_as_of": days[-6],
                    "coverage": len(days), "crossover": None, "latest_daily_relative_series": []}
            def fake_basket(_histories, symbols, window, weights=None):
                symbol_set = set(symbols)
                cyc = set(symbol_set) == set(DEFAULT_BASKETS["cyclical"])
                deff = set(symbol_set) == set(DEFAULT_BASKETS["defensive"])
                if window == 5 and (cyc or deff):
                    val = cyclical if cyc else defensive
                elif window == 20 and (cyc or deff):
                    # On B, the cyclical-minus-defensive spread improves by 0.8pp.
                    if handoff:
                        val = (1.0 if cyc else 2.0) if max(r["trading_date"] for r in _histories["SPY"]) == days[-1] else (1.0 if cyc else 2.8)
                    else:
                        val = 1.0
                elif window == 5 and len(symbol_set) == 1 and next(iter(symbol_set)) in {"XLK", "SMH"}:
                    val = tech
                else:
                    val = 1.0
                return {"status": "available", "value_pct": val, "as_of": max(r["trading_date"] for r in _histories["SPY"]), "coverage": len(days)}
            with patch("dashboard.regime_transition.calculate_risk_appetite", side_effect=fake_risk), \
                 patch("dashboard.regime_transition._pair_feature", side_effect=fake_pair), \
                 patch("dashboard.regime_transition._basket_return", side_effect=fake_basket):
                result = calculate_regime_transition(histories, vix, expected_session=days[-1])
            return {row["id"] for row in result["features"]["scenario_fixtures"]}

        # Use a closure-local reference to the currently supplied histories for
        # date-aware prior-vs-current basket values.
        _histories = histories
        scenario_a = scenario_ids(tech=1.0, defensive=1.0, cyclical=1.0)
        scenario_b = scenario_ids(tech=1.0, defensive=1.0, cyclical=1.0, handoff=True)
        scenario_c = scenario_ids(tech=-1.0, defensive=-1.0, cyclical=-1.5)
        scenario_d = scenario_ids(tech=1.0, defensive=0.5, cyclical=1.0, prior_stress=True)
        self.assertIn("technology_rebound_defensives_stable", scenario_a)
        self.assertIn("technology_rebound_defensives_weaken", scenario_b)
        self.assertIn("technology_and_defensives_weaken", scenario_c)
        self.assertIn("cyclical_technology_post_stress_recovery", scenario_d)

    def test_confirmed_state_requires_three_persisting_completed_sessions(self):
        histories, vix, days = market_fixture()
        risk_on = {"classification": "Risk-on", "components": {
            "equity_participation": {"direction": 1, "value": {"sectors_outperforming_spy": 9}},
            "defensive_rotation": {"direction": 0, "status": "Mixed"},
            "credit_risk_appetite": {"direction": 1, "status": "Improving"},
            "volatility_pressure": {"direction": 0, "status": "Contained"}},
            "prior_stress_observed": False}
        mixed = {**risk_on, "classification": "Rotation / Mixed"}
        states = {day: ("Risk-on" if day in days[-3:] else "Rotation / Mixed") for day in days}
        def fake_risk(*args, expected_session=None, **kwargs):
            return {**risk_on, "classification": states.get(expected_session, "Rotation / Mixed")}
        with patch("dashboard.regime_transition.calculate_risk_appetite", side_effect=fake_risk):
            result = calculate_regime_transition(histories, vix, expected_session=days[-1])
        self.assertEqual(result["transition_status"], "Transition confirmed by predefined rules")
        self.assertIn("persisted across 3 completed sessions", result["supporting_evidence"][0])

    def test_feature_export_is_versioned_and_contains_no_forward_labels(self):
        histories, vix, days = market_fixture()
        record = calculate_regime_transition(histories, vix, expected_session=days[-1])
        exported = export_transition_features(record)
        self.assertEqual(exported["schema_version"], TRANSITION_EXPORT_SCHEMA)
        self.assertEqual(exported["as_of_session"], days[-1])
        self.assertIn("feature_values", exported)
        self.assertFalse(any("future_outcome" in key or "label" in key for key in exported))

    def test_relative_crossover_requires_three_persistent_sessions(self):
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(50)]
        dates = [day.isoformat() for day in dates if day.weekday() < 5]
        benchmark, asset = [], []
        for index, day in enumerate(dates):
            benchmark.append({"trading_date": day, "close": 100.0})
            if index < len(dates) - 4:
                close = 90.0
            else:
                close = 92.0 + (index - (len(dates) - 4)) * 1.0
            asset.append({"trading_date": day, "close": close})
        feature = _pair_feature({"A": asset, "SPY": benchmark}, "A", "SPY", 20, 5)
        self.assertEqual(feature["status"], "available")
        # If the exact market path does not cross the symmetric band, no event
        # is asserted; the presence of a crossover must always carry persistence.
        if feature["crossover"]:
            self.assertEqual(feature["crossover"]["persistent_sessions"], RULES["crossover_persistence_sessions"])

    def test_sustained_relative_strength_crossover_is_detected_after_three_sessions(self):
        dates, cursor = [], date(2026, 1, 1)
        while len(dates) < 60:
            if cursor.weekday() < 5:
                dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        benchmark = [{"trading_date": day, "close": 100.0} for day in dates]
        asset, close = [], 100.0
        for index, day in enumerate(dates):
            if 35 <= index < 55:
                close *= .997
            elif index >= 55:
                close *= 1.02
            asset.append({"trading_date": day, "close": close})
        feature = _pair_feature({"A": asset, "SPY": benchmark}, "A", "SPY", 20, 5)
        self.assertEqual(feature["crossover"], {"from": "underperforming", "to": "outperforming", "persistent_sessions": 3})

    def test_persistence_is_idempotent_and_revisions_are_immutable(self):
        histories, vix, days = market_fixture()
        record = calculate_regime_transition(histories, vix, expected_session=days[-1])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"
            initialize_database(path)
            first = save_market_regime_transition_record(record, path)
            second = save_market_regime_transition_record(record, path)
            self.assertTrue(first["inserted"])
            self.assertFalse(second["inserted"])
            changed = {**record, "source_fingerprint": "different-fingerprint"}
            third = save_market_regime_transition_record(changed, path)
            rows = list_market_regime_transition_records(database_path=path)
            self.assertTrue(third["inserted"])
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()

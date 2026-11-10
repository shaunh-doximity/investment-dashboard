import unittest

from dashboard.regime_evidence import (
    METHODOLOGY_VERSION, REGIMES, build_regime_evidence, compare_scenario_support,
)


AS_OF = "2026-11-03"


def component(direction, *, status="available", value=None):
    return {"direction": direction, "status": status, "as_of": AS_OF,
            "value": value or {}, "evidence": ["dated local observation"]}


def inputs(*, equity=1, defensive=1, credit=1, volatility=1, tech=1, prior_stress=False):
    risk = {"as_of": AS_OF, "prior_stress_observed": prior_stress, "components": {
        "equity_participation": component(equity, value={"rsp_excess_pp": 1.2, "iwm_excess_pp": .7,
            "sectors_outperforming_spy": 8, "sectors_above_sma50": 9}),
        "defensive_rotation": component(defensive, value={"cyclical_minus_defensive_pp": .8}),
        "credit_risk_appetite": component(credit, value={"hyg_minus_lqd_pp": .6, "hyg_minus_ief_pp": .3}),
        "volatility_pressure": component(volatility, value={"close": 17, "regime": "Normal",
            "daily_change_percent": -2, "five_session_change_percent": -6}),
    }}
    transition = {"as_of": AS_OF, "features": {"pair_relative_performance": {
        symbol: {"status": "available", "as_of": AS_OF, "relative_return_pp": tech}
        for symbol in ("XLK", "SMH", "MAGS")}}}
    return risk, transition


class RegimeEvidenceTests(unittest.TestCase):
    def test_scores_normalize_and_are_explicitly_nonprobabilistic(self):
        risk, transition = inputs()
        result = build_regime_evidence(risk, transition)
        self.assertEqual(result["methodology_version"], METHODOLOGY_VERSION)
        self.assertAlmostEqual(sum(result["relative_evidence_support"].values()), 100.0)
        self.assertEqual(result["as_of"], AS_OF)
        self.assertNotIn("probability", result)
        self.assertEqual(set(result["relative_evidence_support"]), set(REGIMES))
        self.assertEqual(REGIMES, ("Risk-on Expansion", "Rotation / Mixed", "Risk-off Contraction"))
        self.assertNotIn("Stress Recovery", result["raw_support"])

    def test_missing_groups_are_excluded_and_available_weights_renormalize(self):
        risk, transition = inputs()
        risk["components"]["credit_risk_appetite"] = component(None, status="unavailable")
        result = build_regime_evidence(risk, transition)
        self.assertEqual(result["status"], "available")
        self.assertIn("credit", result["missing_groups"])
        self.assertNotIn("credit", result["normalized_weights_used"])
        self.assertAlmostEqual(sum(result["relative_evidence_support"].values()), 100.0)

    def test_insufficient_evidence_does_not_force_regime(self):
        risk, transition = inputs()
        for key in ("credit_risk_appetite", "volatility_pressure"):
            risk["components"][key] = component(None, status="Stale observations")
        result = build_regime_evidence(risk, transition)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["relative_evidence_support"])
        self.assertEqual(result["classification"], "Insufficient Evidence")

    def test_breadth_improvement_increases_constructive_relative_support(self):
        risk, transition = inputs(equity=-1, defensive=-1, credit=1, volatility=1, tech=-1)
        base = build_regime_evidence(risk, transition)
        scenario = build_regime_evidence(risk, transition, overrides={"equity_participation": 1})
        self.assertGreater(scenario["relative_evidence_support"]["Risk-on Expansion"],
                           base["relative_evidence_support"]["Risk-on Expansion"])
        self.assertLess(scenario["relative_evidence_support"]["Risk-off Contraction"],
                        base["relative_evidence_support"]["Risk-off Contraction"])

    def test_vix_easing_does_not_hide_weak_credit(self):
        risk, transition = inputs(equity=1, defensive=-1, credit=-1, volatility=-1, tech=1)
        baseline = build_regime_evidence(risk, transition)
        eased = build_regime_evidence(risk, transition, overrides={"volatility": 1})
        self.assertGreater(eased["relative_evidence_support"]["Risk-on Expansion"],
                           baseline["relative_evidence_support"]["Risk-on Expansion"])
        self.assertGreater(eased["relative_evidence_support"]["Rotation / Mixed"], 0)
        self.assertEqual(eased["groups"]["credit"]["direction"], -1)

    def test_technology_rebound_is_aggregated_with_defensive_leadership(self):
        risk, transition = inputs(equity=-1, defensive=-1, credit=-1, volatility=-1, tech=-1)
        weak = build_regime_evidence(risk, transition)
        defensive_stable = build_regime_evidence(risk, transition, overrides={"technology_leadership": 1})
        defensive_weakening = build_regime_evidence(risk, transition,
            overrides={"technology_leadership": 1, "defensive_leadership": -1})
        self.assertGreater(defensive_weakening["groups"]["sector_leadership"]["direction"],
                           defensive_stable["groups"]["sector_leadership"]["direction"])
        self.assertGreater(defensive_stable["groups"]["sector_leadership"]["direction"],
                           weak["groups"]["sector_leadership"]["direction"])

    def test_stress_recovery_requires_prior_stress(self):
        risk, transition = inputs(prior_stress=False)
        without_prior = build_regime_evidence(risk, transition)
        self.assertIn("stress_easing_state", without_prior["transition_characteristics"])
        self.assertFalse(without_prior["transition_characteristics"]["recovery_from_stress_confirmed"])
        risk["prior_stress_observed"] = True
        with_prior = build_regime_evidence(risk, transition)
        self.assertTrue(with_prior["transition_characteristics"]["recovery_from_stress_confirmed"])

    def test_stress_easing_can_be_described_without_prior_stress(self):
        risk, transition = inputs(prior_stress=False)
        result = build_regime_evidence(risk, transition)
        self.assertEqual(result["transition_characteristics"]["stress_easing_state"], "Stress easing")
        self.assertFalse(result["transition_characteristics"]["recovery_from_stress_confirmed"])

    def test_scenario_comparison_is_pure_and_tracks_changed_group(self):
        risk, transition = inputs()
        baseline = build_regime_evidence(risk, transition)
        hypothetical = build_regime_evidence(risk, transition, overrides={"volatility": -1})
        compared = compare_scenario_support(baseline, hypothetical)
        self.assertTrue(compared["hypothetical_only"])
        self.assertEqual(compared["changed_groups"][0]["group"], "volatility")
        self.assertEqual(risk["components"]["volatility_pressure"]["direction"], 1)
        self.assertEqual(compared["baseline"], baseline["relative_evidence_support"])

    def test_correlated_metrics_stay_inside_one_weighted_group(self):
        risk, transition = inputs()
        first = build_regime_evidence(risk, transition)
        risk["components"]["equity_participation"]["evidence"].extend(
            ["extra RSP detail", "extra IWM detail", "extra sector detail"])
        second = build_regime_evidence(risk, transition)
        self.assertEqual(first["normalized_weights_used"], second["normalized_weights_used"])
        self.assertEqual(first["relative_evidence_support"], second["relative_evidence_support"])

    def test_oas_series_are_aligned_and_aggregated_within_credit_group(self):
        risk, transition = inputs(credit=-1)
        transition["features"]["rates_and_macro_proxies"] = {"verified_credit_spreads": {
            "HY_OAS": {"status": "available", "as_of": AS_OF, "change_20_observations_bp": 10},
            "IG_OAS": {"status": "available", "as_of": AS_OF, "change_20_observations_bp": 10},
            "STALE_OAS": {"status": "stale", "as_of": "2026-10-30", "change_20_observations_bp": 30},
        }}
        result = build_regime_evidence(risk, transition)
        # Both OAS series first collapse to one +/− spread sub-signal, then
        # combine with the HYG proxy; stale data is excluded.
        self.assertAlmostEqual(result["groups"]["credit"]["direction"], -0.75)
        self.assertNotIn("STALE_OAS", result["groups"]["credit"]["metrics"]["verified_spread_changes"])


if __name__ == "__main__":
    unittest.main()

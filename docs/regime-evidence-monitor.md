# Market regime evidence monitor

## Taxonomy and versions

The current evidence monitor uses **regime-evidence-v2** and three contemporaneous regimes: Risk-on Expansion, Rotation / Mixed, and Risk-off Contraction. Stress Recovery was removed as a competing regime. The previous v1 formula and saved reports remain intact in their historical records; new values are calculated from source groups and are not derived by transforming the old four-regime percentages.

Stress easing is retained as a separate transition characteristic. It summarizes whether current VIX pressure, credit proxies, equity breadth, and cyclical/technology leadership are improving. This description does not require a prior-stress label. `recovery_from_stress_confirmed` is a separate boolean and requires an explicitly observed prior-stress episode plus improving breadth, credit, and VIX. It is not a forecast.

The risk-appetite classification and transition signal vocabulary are versioned independently (`risk-appetite-v4`, `market-regime-transition-v3`, feature export v3). Historical daily records are not rewritten; each new record carries its own methodology version.

## Inputs and weights

| Evidence group | Weight | Main inputs |
|---|---:|---|
| Equity participation | 30% | Matched RSP/SPY, IWM/SPY, sector participation |
| Sector leadership | 25% | XLK/SMH/MAGS versus SPY, configured cyclical versus defensive basket |
| Credit | 25% | HYG/LQD and HYG/IEF price-return proxies; available verified OAS series aggregated into one sub-signal |
| Volatility | 20% | VIX level/regime, close changes and reversal behavior |
| Rates/macro | 0% directional | Dated Treasury, TLT, GLD and USO context only |

Related observations are first aggregated within each group. The two HYG ratios form one proxy direction; available OAS changes are averaged into one spread sub-signal before joining that group. OAS widening maps to a negative direction, with a 5 bp deadband and 20 bp full scale. Group directions are bounded to [-1,+1]. Technology relative return is divided by 2.5 percentage points and clamped before averaging. No AI-generated weights are used.

## Three-regime score

Only current, date-aligned groups vote. Equity participation and at least two additional voting groups are required. Missing, stale, or date-misaligned groups are excluded and remaining weights are renormalized. Rates and macro proxies remain non-voting because the same move can have competing explanations.

For normalized available group weights `w` and signed directions `d`:

- Positive support `P = Σ w × max(d, 0)`
- Negative support `N = Σ w × max(-d, 0)`
- Neutral support `Z = Σ w × (1 - |d|)`
- Cross-group disagreement `D = 2 × min(P, N)`
- Raw Risk-on Expansion `= P`
- Raw Risk-off Contraction `= N`
- Raw Rotation / Mixed `= Z + D`

These three raw scores are normalized to shares totaling 100% only for display. They express how the available evidence aligns under this deterministic rule. They are **not probabilities, odds, calibrated confidence, or expected future returns**. Values are recomputed from source observations; old v1 four-way shares are never redistributed.

## Credit Confirmation and turning-point cards

Credit Confirmation compares current HYG/LQD and HYG/IEF ETF relative returns on matched dates and reports whether their direction agrees with equity participation. These prices are not corporate credit spreads. A valid spread observation may contribute to the monitor's credit group only when sourced from existing authorized data and aligned to the credit proxy date.

The five Turning-Point cards use subject-specific states: VIX Stress, Technology/Mag 7 Leadership, Broad Equity Participation, Defensive Leadership, and Credit Confirmation. Card state labels describe the measured subject. Their rules and exact values live in the versioned transition record.

## Scenario explorer

Scenario controls apply local hypothetical directions to the same current group calculations. They do not change source observations, current regime history, market records, portfolio state, or saved reports, and do not call DeepSeek. Reset returns controls to actual observations.

## Limitations

Sector ETFs overlap with index constituents, so groups are not statistically independent even after within-group aggregation. Defensive leadership does not establish recession. VIX is implied volatility, not equity direction. HYG ETF performance is not an observed spread. Treasury, gold and oil proxies have multiple drivers. This descriptive monitor has no chronological out-of-sample calibration and must not be interpreted as a predictive probability model.

## Future prediction-import contract

`market-regime-prediction-import-v1` is an interface specification only. Any future forecast requires a separately validated model version, forecast date, trading-session horizon, outcome definition, calibrated probabilities, uncertainty information when supported, and data provenance. This dashboard does not import or display forecast probabilities; that work belongs in Market Prediction Lab.

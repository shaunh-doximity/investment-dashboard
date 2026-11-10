# Market Regime Transition Monitor — Phase 1

This is a deterministic description of how observed market leadership is changing. It does not predict returns, estimate reversal probabilities, establish a trading edge, or give trading instructions. The current Risk Appetite label describes the market state; the Transition label describes changes in that state.

## Versioned rules

Current methodology: `market-regime-transition-v2`. The default leadership context is 20 completed sessions, with a 5-session transition-momentum comparison. Supported leadership windows are 5, 20, and 60 sessions. Price comparisons use adjusted closes on exact common dates; no missing bars are filled. A feature series with a calendar gap greater than four days is rejected as gapped. Version 2 adds the four deterministic Turning-Point Watch states and matched 5D/leadership-window XLK, SMH, QQQ, MAGS, RSP and IWM relative-return features; it preserves the prior Risk Appetite and Transition classifications.

The minimum aligned SPY history is `max(21, leadership_window + transition_momentum_window + 1)` sessions (26 sessions for the default 20D/5D view). The source series is truncated to the requested completed session before features are calculated.

The current configured thresholds are exposed in `dashboard.regime_transition.RULES` and in the panel's collapsed methodology table:

- Relative momentum deterioration: at least 0.50 percentage points.
- Opposing relative-strength crossover: from at most −0.50 pp to at least +0.50 pp, or the reverse, sustained for three sessions.
- Material breadth contraction/broadening: at least two sector ETFs over the 5-session comparison.
- A rule-confirmed state change requires the risk-appetite classification to remain `Risk-on` or `Risk-off` for three completed sessions, differing from the preceding state. Those states themselves require aligned equity participation and at least three current component groups; a risk-off label additionally requires deterioration in equity participation, defensive sector leadership, credit ETF proxies, and VIX pressure.
- Stress recovery requires observed prior VIX stress (at least 25 points in the preceding reference block), improving breadth, stabilizing credit proxies, and VIX easing under the existing Risk Appetite rules.

Other measured features include RSP/SPY, IWM/SPY and QQQ/SPY return spreads and momentum changes; sector/SPY relative returns, rankings and crossovers; cyclical-minus-defensive basket spreads; HYG/LQD and HYG/IEF price proxies; VIX level and close changes; Treasury yield changes in basis points; and TLT, GLD and USO price-proxy behavior. Rolling return/yield correlations are descriptive aligned-sample associations only. USO is a futures-based ETF, not spot WTI. No ETF price movement is treated as proof of fund flows.

## Transition states

- **Continuing** — observed leadership remains and there is no material weakening condition.
- **Weakening** — current leadership remains positive, but relative momentum or breadth deteriorates without a persistent opposing crossover.
- **Possible reversal** — weakening leadership has an opposing crossover or basket shift plus early breadth, credit, or VIX confirmation; persistence requirements are not yet met.
- **Transition confirmed by predefined rules** — a new qualifying Risk Appetite state persisted for three completed sessions and has the component confirmation defined above. This confirms only that the observations met the rule; it does not confirm a future market move.
- **Mixed / inconclusive** — component groups disagree or remain near thresholds.
- **Insufficient data** — minimum history is absent, core equity observations are stale/misaligned, or the current risk-appetite inputs cannot support a state.

Scenarios A–D distinguish a technology rebound with stable defensives, a technology rebound during weakening defensive leadership, technology and defensive weakness occurring together, and technology/cyclicals strengthening after prior stress. They are conditional descriptive combinations, not outcomes or forecasts.

## Turning-Point Watch rules

The watch is descriptive and uses no probability. VIX stabilization uses the existing risk-appetite VIX direction and five-session close change; a rule-confirmed easing state additionally requires prior VIX stress, a five-session VIX change of −5% or less, and three consecutive lower closes. Technology/Mag 7 requires XLK and at least two of SMH, QQQ, and MAGS with aligned 5D and leadership-window comparisons. A confirmed technology crossover requires at least two persistent 5D rolling relative-strength crossovers, including XLK. Otherwise, positive short-window performance while the longer window is negative is labeled a possible turning point; stronger or weakening states require the relevant covered comparisons to agree.

Broad participation uses matched RSP/SPY and IWM/SPY 5D and leadership-window comparisons plus the change in the count of sectors outperforming SPY. A rule-confirmed improvement requires both RSP and IWM rolling 5D relative-strength crossovers and non-contracting breadth. Defensive leadership compares the configured cyclical and defensive basket spreads at 5D and the selected leadership window; a possible handoff requires a defensive lead in the longer window with cyclical outperformance greater than 0.50 percentage points in 5D, and a rule-confirmed change additionally requires two persistent cyclical-versus-defensive pair crossovers. Broadening stress requires technology and defensive absolute 5D returns below zero plus a decrease of at least two outperforming sectors, or the corresponding weakening relative states and breadth. Missing or unaligned windows produce `Insufficient data` or `Mixed/inconclusive`, not an inferred direction. All state thresholds are serialized in each feature record.

## Persistence and research export

Daily feature records use `market-regime-transition-features-v2`, include as-of and feature timestamps, units, methodology, state, coverage and source fingerprint, and contain no future labels. SQLite inserts are idempotent for the same session, methodology and fingerprint; changed same-session inputs are stored as a separate immutable revision. Version 1 records and exports are retained unchanged. This export is intended for later chronological evaluation in Market Prediction Lab. Historical retrospective labels are not presented as evidence of out-of-sample accuracy.

Important limitations: sector ETFs overlap broad indexes; risk inputs are not independent; VIX is implied volatility rather than direction; credit ETFs are not direct spreads; rates and oil can move for multiple reasons. Data publication lags, missing observations and provider adjustments can make a state unavailable or mixed.

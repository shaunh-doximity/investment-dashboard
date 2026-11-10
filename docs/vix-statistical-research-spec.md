# VIX Descriptive Indicators and Future Validation Specification

This document describes the Market tab's deterministic, descriptive indicators and a future research plan. **No bottom-prediction model or probability is implemented.** The separate Market Prediction Lab is out of scope.

## Current VIX signals

All VIX inputs are official Cboe daily index observations already cached locally. VIX is an option-implied volatility index, not an investable ETF or a measure of market direction. A successful download is not evidence that a close is current; the visible current reading must match the expected completed U.S. session.

| Indicator | Definition | Window / guard |
| --- | --- | --- |
| Absolute regime | User-configurable descriptive close thresholds (default <15 subdued, 15–<20 normal, 20–<30 elevated, ≥30 extreme) | VIX points; no forecast meaning |
| Close z-score | `(latest close − mean of trailing 60 closes) / population standard deviation` | Includes only observations through that close; zero standard deviation, gaps, and incomplete windows are unavailable |
| Recent variability | Population standard deviation of daily VIX log returns | 20 close-to-close returns; compared with up to 252 prior rolling windows, excluding the current window; requires at least 60 prior windows for a percentile; label “unusually turbulent” only at/above the 90th historical percentile |
| Qualified reversal frequency | Count a session only if its high is at least 10% above the prior close; call it a reversal when at least 50% of that peak surge is unwound by the close | Trailing 20 sessions; report fraction only among qualifying spikes. Missing OHLC or prior closes invalidates the full window |
| Large intraday range | `(high − low) / prior close ≥ 10%` | Counted separately from qualified reversals |
| Momentum | Cumulative 3/5-session close changes, 5-session return minus preceding 5-session return, and large up/down session counts | Rule-based descriptions, not probabilities |
| Equity context | SPY position vs 50/200-session averages, five-session return, and standard sector ETF participation/relative leadership | Each series is truncated to no later than the VIX observation date; dated ETF proxies are not constituent-level breadth |

Rolling standard deviation uses a population denominator (`ddof=0`) for descriptive consistency. A percentile is a rank in the available historical comparison sample, not a calibrated probability. Signals may disagree; the UI does not collapse them into one risk score. VIX reversal patterns do not establish that an equity-market bottom occurred.

The z-score's paired empirical percentile uses the **same trailing 60 closes**, including the as-of close, and the midrank definition `(count below + 0.5 × count tied) / sample size`. This avoids comparing a 60-session z-score with a different 252-session percentile as if they described the same reference distribution. The separate one-year percentile remains labeled as a 252-observation rank.

The optional normal-equivalent percentile is `Φ(z)` under a standard normal assumption and is shown only as educational context. VIX observations are skewed and may contain outliers, so this theoretical percentile can differ from the empirical observed rank. With zero variance, the z-score and normal-equivalent percentile are undefined; the empirical midrank may still be available. A gap or insufficient sample suppresses the paired calculation. None of these statistics is a probability of a crash, recovery, mean reversion, option profitability, or a market bottom.

## Candidate future binary labels (research only)

Each sample is an end-of-session observation `t`. Features may use data published or observed through that completed session only. The targets below are candidates for later study, not forecasts:

1. **VIX +15% within five trading sessions:** label true if any close from `t+1` through `t+5` is at least `1.15 × VIX_close(t)`.
2. **VIX −15% within ten trading sessions:** label true if any close from `t+1` through `t+10` is at most `0.85 × VIX_close(t)`.
3. **New 20-session VIX closing high within five sessions:** label true if any next-five close is strictly greater than the maximum VIX close over `t−19 … t`.
4. **SPY avoids a new ten-session closing low over ten sessions:** baseline is the minimum SPY close over `t−9 … t`; label true only if every next-ten close is greater than or equal to that baseline. A strict lower close is a new low.
5. **Stress normalization after extreme VIX:** define the extreme threshold before fitting (for example a point threshold or trailing percentile), then label whether VIX closes below a predeclared normalization level within a fixed horizon. Count each episode once and document episode spacing.
6. **Equity stabilization after repeated VIX reversals:** define the qualifying spike/reversal count and SPY stabilization outcome before label construction; evaluate only complete forward windows and do not treat the reversal pattern as causal.

Do not label a sample when its full forward horizon or required starting history is incomplete. Preserve the as-of time and source date for every input. Use completed sessions and point-in-time releases; do not backfill a value that was not known at `t`.

### Candidate features

- VIX close level, trailing z-score, 1-year empirical rank, close-to-close log-return variability and its prior-window rank.
- 1/3/5/10-session VIX changes, acceleration, five-session high/low expansion, qualified spike/reversal counts, large daily up/down moves, and consecutive sessions above explicitly documented levels.
- SPY close versus trailing averages, trailing drawdown, recent lows and dated returns; standard-sector ETF participation and relative returns on common dates.
- Optional future VVIX, SKEW, compatible realized volatility, and VIX futures curve observations only after source permissions, date coverage, and point-in-time availability are established.

### Coverage and validation requirements

Before fitting, inventory the exact local observation range, complete OHLC count, missing sessions, source and retrieval metadata, and usable forward-label count per target. Do not assume that the broad public VIX history also exists for SPY, sectors, or optional volatility series.

Use expanding/rolling chronological train-validation-test splits. Purge or embargo at least the maximum label horizon (10 sessions for the proposed set) around split boundaries so overlapping forward windows cannot leak labels across partitions. Never random-shuffle time series. Compare out-of-sample results with the training-period unconditional base rate and a simple past-frequency baseline. Report sample counts and uncertainty intervals. If output is a probability, evaluate calibration with reliability plots and Brier/log loss; discrimination metrics alone are not calibration. Require stable, genuinely out-of-sample calibration before showing any probability.

No target, feature, baseline, or evaluation threshold in this document is a validated market signal. No probability will appear in the dashboard until a real model has been trained and evaluated under these requirements.

## Possible supplementary volatility data

- **VVIX:** Cboe's volatility-of-VIX index, a distinct options-implied measure. Cboe lists downloadable historical VVIX index data. It is not a direct forecast of VIX and has its own data-use conditions.
- **SKEW:** Cboe publishes an index dashboard and historical index information. SKEW is an option-implied tail-risk/skew measure, not a direct quote for the price of a specific put. Data access and redistribution terms must be verified before integration.
- **Implied versus realized volatility:** realized volatility can be computed locally from compatible, dated S&P 500 index closes (or a clearly labeled ETF proxy), using an explicitly matched horizon and annualization convention. VIX is a roughly 30-day, annualized implied measure; mismatched horizons and ETF distributions/tracking differences matter.
- **VIX futures term structure:** requires contract-level dated settlement data and careful roll/maturity handling. Cboe presents futures market data as a market-data product with licensing options; this app does not assume a free, authorized historical settlement feed.

No subscription source is added by this specification. Cboe availability does not, by itself, grant unrestricted redistribution rights.

## Official source references checked 2026-11-01

- [Cboe volatility-index historical data](https://www.cboe.com/tradable-products/vix/vix-historical-data) lists daily VIX closing history and downloadable history for VVIX and VIX9D.
- [Cboe VVIX index dashboard](https://www.cboe.com/us/indices/dashboard/vvix/) describes VVIX as an index related to volatility of VIX.
- [Cboe SKEW index dashboard](https://www.cboe.com/us/indices/dashboard/skew/) is the official public index reference; data rights still require verification before reuse.
- [Cboe U.S. futures market-data products](https://www.cboe.com/market_data_services/us/futures/) identifies CFE as the home of volatility futures and describes its data offerings with flexible licensing options.

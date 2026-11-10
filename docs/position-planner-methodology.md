# Position Planner assumptions

The Position Planner is a local planning aid. Saving a plan creates a versioned plan revision linked to the approved strategy revision; it does not modify holdings, cash, cost basis, or place orders.

## Price reference safety

Alpha Vantage `GLOBAL_QUOTE` values are end-of-day quote observations and are not real-time. The planner separates quote freshness from price-basis compatibility. An observation-date mismatch is a freshness difference, not proof that two providers disagree. A raw quote is automatically accepted against an imported strategy reference only when the reference date matches and its price basis explicitly identifies an unadjusted/raw close with the same value. The imported TTWO reference says $203.69 on 2026-10-23 and the local Alpha Vantage cache also contains $203.69 dated 2026-10-23, but the reference text does not explicitly identify its adjustment basis. At the dashboard's 2026-10-28 date, that cached quote is also stale. Therefore it is not used for actionable sizing. Enter a separately checked manual price and as-of date, then explicitly confirm basis compatibility, or refresh the provider quote and confirm the basis when needed. Tiingo dividend-adjusted closes remain the source for historical technical calculations; the app does not convert them to raw quote prices because it has no adjustment-factor reconciliation for that comparison.

## Allocation and staged actions

Allocation fractions are fractions of total portfolio equity, including cash. The permitted added shares are limited by the tightest of remaining position-allocation headroom, deployable cash after minimum reserve, remaining stop-risk budget, and verified-sector headroom. The planner uses whole shares. A target allocation is an intended policy amount; maximum permitted allocation/capacity is a separate ceiling. A capacity figure is not a recommendation to buy. The default entry fractions are 40/30/30 as an editable template, but only the first stage is prefilled with an entry. Other stage triggers must be supplied explicitly. Sequential exits are clamped to remaining shares.

## Optional Kelly illustration

The simplified model assumes one binary outcome: target reached before stop, with a win probability `p`, gain `g = (target - entry) / entry`, and planned loss `l = (entry - stop) / entry`. If extra gap/slippage is supplied as fraction `s` of entry, modeled loss becomes `l' = l + s`.

The unconstrained full-Kelly fraction of invested capital is:

`f* = (p*g - (1-p)*l') / (g*l')`

The corresponding fraction of portfolio capital at risk under the scenario is `f* * l'`. Break-even probability is `l' / (g + l')`. Quarter-Kelly and half-Kelly multiply the full-Kelly allocation fraction by 0.25 and 0.5. Negative or zero edge yields no positive Kelly allocation. Since this planner does not support leverage, displayed cash allocation is capped at 100%; saved portfolio policy constraints still apply and can cap it much lower.

Kelly's output is only an illustration conditional on a user-entered probability and this two-outcome payoff model. It does not model interim path, stop slippage beyond the explicit sensitivity, overnight gaps unless included, changing odds, taxes, transaction costs, or correlation with other holdings. Stop fills are not guaranteed. No probability is inferred from AI text or copied from a broad-market model. A ChatGPT estimate remains unverified until the user independently reviews it; the planner never imports that estimate as an active probability automatically. Probability sensitivity is hypothetical, not a validated allocation range.

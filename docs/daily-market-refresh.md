# Daily Market Refresh

The Market page's **Refresh Market Data** action performs one explicit sequential refresh:

1. Determine the last completed XNYS session using `America/New_York` and the exchange's scheduled close.
2. Refresh stale/missing Tiingo daily history incrementally through that expected session.
3. Refresh Cboe VIX history (and only use Yahoo when its fallback is explicitly enabled).
4. Refresh official Treasury par-yield data.
5. Reconcile the configured official economic calendars.
6. Retain the recent catalyst evidence cache. That cache is refreshed only by the existing explicitly requested web-enabled DeepSeek analysis.
7. On the following Streamlit render, rebuild deterministic indicators and the canonical snapshot from SQLite. The latest canonical snapshot is stored only when its underlying-data fingerprint changes.

No AI request is part of the data stages. **Run DeepSeek Analysis After Refresh** is a separate explicit choice. If price sources remain behind the expected session or a primary source fails, the app labels the gap and requires an additional confirmation before the optional analysis runs. Existing reports and successful cached data survive source errors.

## Freshness rules

`exchange-calendars` supplies the XNYS session schedule, including holidays and early closes. A standard-library fallback covers current US market holidays and common early closes if the package has not yet been installed. The application compares the newest stored observation date with the latest completed exchange session; `retrieved_at` is request provenance only. For example, Friday 2026-10-30 history is stale on Tuesday 2026-11-03 after Monday 2026-11-02's session has closed, even if it was re-requested on November 3.

Tiingo `startDate` and `endDate` are treated as inclusive, and the adapter filters returned bars to that inclusive range. Incremental requests overlap the prior week to repair provider corrections; SQLite's `(symbol, trading_date, provider, adjustment_basis)` key makes writes idempotent. An empty response or provider error never deletes existing bars. If the provider has not published the expected bar, the final state is **Provider publication pending** and an explicit later refresh retries it regardless of the last request timestamp.

Per-symbol diagnostics retain prior cached date, expected session, request attempt/status, latest returned date, row upsert count, new cached date, freshness, and safe error category. API credentials and request URLs are excluded from diagnostics.

## Known limitation

The configured Tiingo credential was present during the 2026-11-03 investigation, but the execution environment could not reach Tiingo (`requests.ConnectionError`). Thus the local cache was confirmed through 2026-10-30 for SPY, RSP, QQQ, DIA and MCD, but availability or persistence of 2026-11-02 prices could not be confirmed in that environment. The refresh workflow will request that session and report either the provider's observation or a publication/network status without inventing bars.

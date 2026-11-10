# Market catalysts and oil proxy

## Recent market developments cache

The hidden `recent_market_catalysts_v1` SQLite preference is populated only from dated DeepSeek Search results during an explicitly requested **Dashboard + Web Verification** full-market run. It keeps at most 24 concise records and filters publication dates to the preceding seven calendar days. Each record retains a source URL/title, publication date, a short provider excerpt, the cache creation/expiry timestamps, and a provenance label. It does not store full articles, infer an event date from publication date, or treat retrieval as independent verification. Company matching and affected-segment tags are conservative hypotheses; numeric values are not extracted unless separately verified in the source. An ordinary page rerun does not refresh the cache.

The calendar snapshot distinguishes `User-confirmed` (accepted for planning because the user approved it), `Source-verified` (matched to an authoritative independent source), and `Unconfirmed`. User confirmation never changes the independent source-verification field. The AI prompt asks the model to analyze the closest material events and explain market implications, and to keep routine provenance caveats in evidence details.

## Oil proxy in Rates & Macro

Rates & Macro uses cached Tiingo adjusted closes for USO, refreshed through the existing manual market-history controls. USO is a futures-based oil ETF proxy, not WTI spot crude: futures rolls, fund expenses and tracking effects can cause its return to differ from spot prices. It is compared using normalized/percentage returns, not on the Treasury-yield price axis.

For questions about actual WTI spot prices, Dashboard + Web Verification Ask AI must retrieve dated external evidence. Dashboard Analysis can discuss general mechanisms but cannot claim current spot observations. Previously cached EIA records remain untouched for compatibility, but are no longer read by the primary Market UI or snapshot. The unused adapter remains available for compatibility; the `.env.example` no longer asks users to configure an EIA key.


# Investment Command Center

A personal, local-first investment research workspace built with Python, Streamlit, and SQLite. It keeps manually entered holdings, cash, research notes, and cached market data on the local machine. Its investment philosophy is transparent and reviewable: calculations are deterministic Python, AI output is optional analysis rather than verified fact, and no feature places trades or connects to a brokerage.

<img width="1672" height="918" alt="image" src="https://github.com/user-attachments/assets/f845f9f6-ccc2-455f-b0ab-e24316c8b824" />


The four tabs are **Market Home**, **Portfolio**, **Research & Watchlist**, and **Quant Lab**. Market quotes, price history, VIX observations, and event calendars refresh only after explicit user actions.

## Architecture

| Layer | Responsibility |
|---|---|
| `app.py` and `dashboard/pages/` | Streamlit entry point, four-tab navigation, and page UI |
| `dashboard/calculations.py`, `analytics.py`, `portfolio_risk.py`, `setup_calculations.py`, `market_regime.py` | Deterministic portfolio, price-history, risk, RRR, and regime calculations |
| `dashboard/data_access.py` and `database.py` | SQLite persistence and additive schema initialization |
| `market_data.py`, `history_providers.py`, `vix_sources.py`, `treasury_yields.py`, `event_calendar.py` | Alpha Vantage, Tiingo, Cboe/Yahoo/FRED VIX sources, Treasury par yields and calendar adapters; network calls are explicit refresh operations |
| `ai_providers.py`, `ai_market_analyst.py`, `ai_research.py`, `deepseek_market_research.py` | Shared DeepSeek/OpenAI features plus explicit-button DeepSeek Search and Responses research |
| `tests/` and `test_market_data.py` | Offline tests for calculations, persistence, providers, and navigation |

The local SQLite database is created under `data/`. Do not add it, exports, or credentials to a public repository. A private GitHub repository is the safer default for a personal investment tool.

## Installation and local setup

From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Streamlit prints a local URL, usually `http://localhost:8501`.
The included Streamlit configuration binds the app to `127.0.0.1`, so it is available only from this Mac.

The app's Streamlit config binds to `127.0.0.1`. If you change that setting to expose the app on a network, add appropriate authentication and network controls first.

## Environment configuration

Copy the placeholder file and add only credentials for services you choose to use:

```bash
cp .env.example .env
```

| Variable | Used for | Required? |
|---|---|---|
| `ALPHA_VANTAGE_API_KEY` | Manual spot quote refreshes | Optional |
| `TIINGO_API_KEY` | Manual adjusted daily history refreshes | Optional |
| `FRED_API_KEY` | Optional historical FRED series diagnostics (VIXCLS, policy-rate and credit histories) | Optional |
| `AI_PROVIDER` | Default AI provider: `deepseek` or `openai` | Optional; defaults to DeepSeek |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` | Existing AI features use configured DeepSeek model | Optional |
| `DEEPSEEK_CONNECT_TIMEOUT_SECONDS`, `DEEPSEEK_READ_TIMEOUT_SECONDS` | Separate bounded connect/read timeouts for explicit Market Intelligence requests (defaults 10/180 seconds) | Optional |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | OpenAI Chat Completions/Responses features | Optional; model defaults to the value in `.env.example` |
| `RESEARCH_STRATEGIST_PROMPT` | Optional replacement for the built-in skeptical research-assistant instructions | Optional; leave blank for the built-in prompt |

No calendar API key is required. The calendar adapters use public BLS, BEA, Federal Reserve, and FEC pages/feeds. `OPENAI_API_KEY` is only needed when OpenAI is selected. There is no automatic provider fallback. Keep real values only in local `.env`; `.env.example` must contain placeholders only.

## Market-data and AI providers

Spot quotes use Alpha Vantage; adjusted daily OHLCV uses Tiingo through a provider interface; VIX prefers Cboe published history, with an explicitly enabled yfinance fallback and FRED retained for historical reference only. Treasury par yields use the official Treasury XML feed. Rates & Macro uses Tiingo adjusted USO history as a futures-based oil ETF proxy; it is not a WTI spot-price series. AI uses the OpenAI Python SDK with DeepSeek as the connected Market provider. No API call occurs merely because the app reruns.

### Alpha Vantage quotes

Store the key in the project-root `.env` file:

```text
ALPHA_VANTAGE_API_KEY=your_key_here
```

The app reads this key locally. It is not hardcoded, displayed, or included in API error messages. `.env` is ignored by Git. The app does not request quotes on page load; use the refresh button on Market Home or Portfolio. Successful quotes are cached in SQLite. The app spaces requests and tracks its own daily request budget; requests made by other tools with the same key are outside its counter.

### Optional AI features

Copy `.env.example` to `.env`. The default provider is DeepSeek; add `DEEPSEEK_API_KEY` to enable it. DeepSeek uses the official OpenAI Python SDK against `https://api.deepseek.com` with the Chat Completions endpoint and `deepseek-chat` by default. The AI settings expander lets you switch explicitly to OpenAI; add `OPENAI_API_KEY` for that provider. There is no automatic switch after a failure. Provider/model defaults can also be configured with `AI_PROVIDER`, `DEEPSEEK_MODEL`, and `OPENAI_MODEL`.

The **Generate / Refresh AI Briefing**, Research **Compare with saved strategy**, and submitted chat messages are explicit AI call points. Normal Streamlit reruns do not call an AI API. Market briefings use locally cached SQLite market metrics and remain saved with their snapshot, provider/model, timestamp, and available source metadata. Optional web search is disabled unless you select OpenAI and enable it. Portfolio context is off by default; when explicitly enabled, the Research Strategist may receive the limited portfolio context. It excludes shares, cash, market value, and cost basis.

Each research ticker has its own SQLite chat history. Cached quotes, Tiingo adjusted history, locally calculated technical indicators, verified sector classification, saved strategy, and related earnings events form its local research context. No current filings, fundamentals, valuation feed, or recent-news provider is integrated yet; the app labels these gaps and does not treat model recollection as verification. Full strategy creation uses the ChatGPT JSON import workflow; the in-dashboard AI remains available for optional discussion and saved strategy comparison. Approved short-, medium-, and long-term scenarios use SQLite revision history. RRR and risk-budget sizing are calculated in Python. `RESEARCH_STRATEGIST_PROMPT` can replace the default system prompt for local experimentation; treat a custom prompt as trusted configuration.

You can prepare a strategy in ChatGPT and import it without configuring an AI provider: open a ticker workspace, choose **Import Strategy**, paste a version 3 JSON export, review the normalized fields and independent short-, medium-, and long-term scenarios, then select **Import & Save** to append approved revisions. Versions 1 and 2 remain accepted. Version 2 added structured investment logic; version 3 adds optional, explicitly unverified probability-estimate metadata. **Copy JSON Template** and **Copy ChatGPT Export Instructions** are generated from the schema-aligned example; the complete importable example is in [`docs/strategy-import-template.json`](docs/strategy-import-template.json). The workspace presents compact scenario cards and locally calculated RRR, while full imported research and citations remain available in collapsed sections. The normalizer supports unambiguous string/object/list and alias variations while retaining fields it cannot map for explicit review. Invalid JSON, unsupported schema versions, malformed dates/prices, invalid URLs, or ticker mismatches are rejected before saving with field-path corrections. Preview does not modify approved strategy data. Imported source URLs and claims are retained as unverified references. Missing prices stay null; price ranges remain ranges. Each approved scenario is written to existing revision history. This import path makes no AI or web requests.

The Research workspace also has a **Position Plan** for each approved horizon. Plan revisions are separate from strategy revisions but link back to the approved strategy version and ticker/horizon. Trade setup, position sizing, and staged execution are compact sections prefilled from approved strategy, actual portfolio/cash, and saved policy. Portfolio sizing is recalculated from current cached valuation, shares, cash, approved plan levels, and policy defaults; it never changes actual holdings or cash. Allocation percentages are fractions internally (`0.08` = 8%), defined against total portfolio equity including cash. The UI sizes whole shares; fractional-share planning is not enabled. Multiple entry stages draw from one shared cash, allocation, and stop-risk capacity; later stage triggers start blank instead of repeating the first entry. Exit stages are simulated sequentially and capped at remaining shares. Stops are planning triggers, not guaranteed fills; gap loss and slippage can exceed modeled stop risk.

An optional binary Kelly illustration is available only after the user deliberately enters a probability and source. No probability is inferred from AI text or from SPY correction models. It reports fraction of invested capital separately from risk capital at the modeled stop, shows break-even probability and optional sensitivity, and caps unlevered allocation at 100%; saved portfolio policy constraints still determine the permitted capacity. Quarter Kelly is an illustration, not a recommendation or forecast, and a changed probability never silently changes an approved allocation. Imported probability metadata remains unverified and is not used automatically. Stale quote and unknown price-basis states are separate; stale provider prices are blocked for actionable sizing unless the user enters, dates, and explicitly confirms a manual reference on the same price basis. Confirmed fill details can be recorded separately as an audit entry, but they do not reconcile or modify holdings/cash. If the strategy revision changes after a plan is approved, Portfolio marks it for review rather than carrying it forward silently. This is a planning/monitoring feature only; no brokerage orders are placed.

See [`docs/position-planner-methodology.md`](docs/position-planner-methodology.md) for the Kelly equations, pricing-basis rules, and sizing assumptions.

Legacy Market Assessment records retain their original snapshots, provider metadata, and reports in the collapsed archive. The current Market tab uses a single public-data-only DeepSeek briefing described below; it excludes portfolio records and runs only after the user requests analysis. Deterministic Python regime indicators remain visible independently of AI.

Research strategy import validates a versioned JSON export locally, previews each scenario, calculates RRR in Python, and only saves after **Import & Save**. Missing price levels may remain null and ranges are preserved; unavailable or invalid calculations are labeled. Earlier strategy and scenario revisions remain accessible. No AI feature places orders. Never put credentials in source files; `.env` is ignored by Git. The provider layer uses the official [OpenAI Python SDK](https://developers.openai.com/api/reference/python) and [Chat Completions interface](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create); DeepSeek is configured as an OpenAI-compatible endpoint.

Alpha Vantage's free `GLOBAL_QUOTE` data is end-of-day by default. Each view identifies missing and stale quotes and shows the provider quote date and retrieval timestamp. No missing quote is replaced with a made-up price.

### Historical prices and analytics

The spot-quote integration remains Alpha Vantage. Historical OHLCV uses a separate provider interface and currently has a Tiingo EOD adapter. Configure its optional token as `TIINGO_API_KEY` in the same local `.env` file; the two credentials and services are kept separate. The app never falls back between providers automatically.

Provider review (checked October 2026): Alpha Vantage documents that free `TIME_SERIES_DAILY` returns only the latest 100 observations, while the full series and adjusted-daily endpoint are premium. Tiingo documents a free individual plan with 1,000 API requests/day, 50/hour, 500 unique symbols/month, and five years of history. Its EOD endpoint supplies raw and adjusted OHLCV, including split and dividend adjusted fields. The adapter uses only the adjusted fields and stores them as `split_and_dividend_adjusted`. Tiingo's free license is for the account holder's personal/internal use; do not publish or share this dashboard's data. Review the provider's current plan and terms before use.

Historical requests happen only after an explicit refresh action on Market Home or in Portfolio's historical-risk coverage expander. The initial request asks for two years. Later refreshes skip complete, recent histories, and otherwise check missing historical prefixes, internal gaps, and a short overlap through today. Requests, provider, adjustment basis, retrieval timestamp, and daily OHLCV are persisted in SQLite. Analytics only compare rows with the same provider and adjustment basis. Missing bars are left missing rather than filled.

Returns and momentum use adjusted close-to-close values. Five- and 20-day momentum compare the latest close with the close five or 20 trading observations earlier and require a contiguous window. SMA20/50/200 use adjusted closes. RSI14 uses Wilder smoothing. Realized volatility is the sample standard deviation of the latest 20 available daily returns annualized with 252 sessions. Beta and correlation use matching daily-return dates against SPY. Maximum drawdown uses daily adjusted closes, not intraday lows. This adjusted basis includes dividend adjustments; it is useful for total-return-like comparisons but differs from raw chart prices.

### VIX volatility and market regime

The Market tab uses **Cboe's published daily historical CSV** as the preferred VIX source. Refresh is manual. The official Cboe historical-data page provides downloadable daily closes ([Cboe VIX historical data](https://www.cboe.com/tradable-products/vix/vix-historical-data)); the local adapter caches the source and observation dates in SQLite. When that source does not cover the latest completed cached SPY session, the dashboard reports **Current VIX unavailable** unless the user explicitly opts into a manual yfinance `^VIX` fallback. yfinance is an unofficial interface and intended for personal use ([yfinance project](https://github.com/Symbyos/yfinance/blob/main/README.md)); automated access to Yahoo data is restricted by [Yahoo's terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html), so the fallback is disabled by default. No provider silently replaces a fresher selected observation with an older one. FRED `VIXCLS` is retained only as a historical reference and never supplies the current VIX card.

Current display requires an observation matching the latest completed cached SPY session and no more than four calendar days old (to allow weekends and ordinary market holidays). VIX is an option-implied expected-volatility measure, not a tradable security or market-direction forecast. Point/percent daily change and 5-session point change use the selected provider's own VIX closes; historical charts are labeled with source and last observation. VIX3M is unavailable unless a suitable verified source is added. Cboe historical data use and redistribution remain subject to Cboe terms; this dashboard is for local personal research.

Regime features are deterministic descriptions, not trained probabilities: SPY versus SMA50/200, close drawdown from a trailing 252-session high, 20-session QQQ/SPY and RSP/SPY adjusted-price ratios, fixed-universe sector return dispersion and defensive/cyclical comparison, plus a broad ETF participation proxy. The proxy counts positive returns among SPY, RSP, QQQ, DIA, IWM and SMH; it is not constituent advance/decline breadth. Classification conventions are risk-off if SPY is below both moving averages, drawdown is at or below -15%, or VIX is at least 30; risk-on if above both averages, drawdown is better than -10% (or unavailable), and VIX is below 25; otherwise mixed. Below 20, VIX is described as normal; 20–30 as elevated; 30 or above as stressed. These cutoffs are conventions, not empirically validated regime boundaries.

Historical percentile ranks use prior observations when computing SPY drawdown and sector-dispersion context; insufficient samples remain unavailable. The optional VIX historical analog view compares each past VIX percentile with similar prior readings and reports SPY forward 20-session outcomes only where those outcomes are already observed by the current data date. This is an unvalidated historical association, not a forecast; sample size and caveat are stored with the assessment snapshot. Feature windows never read later-dated observations, but Tiingo does not provide archived data vintages; later corporate-action adjustments may restate historical adjusted closes, so strict point-in-time reconstruction is unavailable. Price histories use available observations only and missing values are not filled.

### Upcoming Market Catalysts

The **Upcoming Market Catalysts** timeline is shown near the top of Market Intelligence. Refreshing it is manual; normal Streamlit reruns read only SQLite. The first adapters use the official [BLS iCalendar feed](https://www.bls.gov/schedule/news_release/bls.ics) for CPI, PPI, Employment Situation, JOLTS, and ECI releases, and the official [BEA release calendar iCalendar feed](https://www.bea.gov/news/schedule/icalendar) for GDP and Personal Income and Outlays (which includes PCE/Core PCE in one release). BLS says its calendar is usually updated Fridays and may be updated as additional releases are scheduled. BEA also publishes a machine-readable JSON calendar; this version uses its official ICS feed.

The Fed adapter reads the official [FOMC meeting calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) and future policy/economy-related entries from the Fed's annual speeches listing. FOMC decisions/press conferences are placed at 2:00 p.m. Eastern on meeting day two. The calendar says future FOMC dates are tentative until confirmation at the preceding meeting. On October 25, 2026, the Fed's next listed meeting is October 27–28; the September meeting has concluded, so that October date is labeled Confirmed. The following December 8–9 meeting remains Tentative until the October meeting concludes. Official BLS, BEA, and Fed speech dates without a tentative designation are labeled Confirmed. The Fed calendar does not publish future minutes dates until scheduled, so no dates are inferred from the “three weeks” convention. A speech is listed as all-day/time unspecified when the official page does not provide a time. The next Jackson Hole symposium date is not currently published in the accessible official calendar, so the app does not estimate its recurring August date. The [FEC election page](https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/) supplies the next federal general election date and says future election dates are tentative and subject to change. No single consolidated official source for policy deadlines was identified; add those manually with an official source URL.

Earnings dates are user-maintained, with configured defaults NVDA, MSFT, AAPL, GOOGL, AMZN, META, and TSLA plus portfolio and research-workspace tickers. There is no universal free official future earnings calendar used here. Enter a date only after checking company investor relations or another source; include the source URL and choose Confirmed, Estimated, or Unverified. Corrections are audited. No earnings date is automatically estimated.

The collapsed catalyst import panel generates a concise prompt for ChatGPT with Robinhood MCP enabled there; the local app cannot call that connector. It asks for compact event lines such as `AAPL 11/02 AMC`, `FOMC 12/08-12/09`, or `CPI 11/10 08:30 ET`, not JSON. Date-only events remain untimed, earnings timing remains BMO/AMC/TBD, and omitted years are inferred only when exactly one date falls inside the selected window. Parse / Preview flags invalid lines and conflicts, lets you edit names, dates, timings, and source URLs, and requires **Approve Import**. All pasted events remain Unverified. Official-source and manually approved records cannot be overwritten by this importer. Existing JSON-import audit records remain compatible in SQLite, and old batches remain available for rollback.

The Volatility Risk Monitor shows configurable absolute regimes (defaults: below 15 subdued, 15–<20 normal, 20–<30 elevated, ≥30 extreme), a one-year historical percentile, and 5/10/15/20-session candlesticks only for complete provider-supplied OHLC rows. Missing OHLC is omitted rather than estimated from closes. Peak surge, peak surge unwound/retained, and net close change are measured against the prior close; no-upward-spike sessions have no spike percentages, and reversals below the prior close can exceed 100% unwound. A separate five-session range expansion requires five complete OHLC records. Cycle labels use explicit rules for Calm, Stress building, Panic/disorder, Turbulent stabilization, and Normalizing; ambiguous conditions remain Mixed signals. Candle colors describe rising/falling VIX, not equity direction; a VIX reversal is not a market-bottom signal.

The Treasury display uses a compact two-column recent monitor; the 20-session view is default, with 5D/60D/120D available. Yield values are percentage points (`4.56` = 4.56%); yield and curve changes are percentage-point differences multiplied by 100 once to report basis points. The curve is based on matched 2Y/10Y observation dates. Event studies are descriptive daily associations and do not establish that a Fed/CPI event caused yield changes.

Events retain provider, source UID, source timezone, UTC time, status, verification time, related tickers, and notes in additive `market_events` tables. Reschedules and cancellations append an audit revision; a canceled event stays in the database but leaves the upcoming view. When a successfully refreshed source omits a previously cached upcoming event, it is retained as Unverified rather than deleted. Failed refreshes preserve cached entries and show source status/staleness. Event details provide a source link and deterministic monitoring context, not a market-reaction prediction. The timeline always displays U.S. Eastern time using the IANA zone `America/New_York`; daylight-saving conversion uses `zoneinfo`, nonexistent spring-forward local times are rejected, and ambiguous fall-back times use the first occurrence. All-day events remain date-only. Event data enters an AI briefing snapshot only when you request generation. Earnings/watchlist symbols are excluded unless **Include Portfolio Context** is enabled.

## Phase 1 features

- Four horizontal tabs: Market Home, Portfolio, Research & Watchlist, and Quant Lab.
- Market symbols begin with SPY, RSP, QQQ, DIA, IWM, and SMH. The list and order are saved locally.
- Portfolio positions, cash, saved quotes, horizon, and thesis status persist in SQLite.
- Each position and watchlist item links to a ticker research workspace.
- Workspaces can be created before opening a position and remain after a position is removed.
- Strategy records include thesis, horizon, status, catalysts, risks, entry range, target, stop or invalidation conditions, position sizing notes, review date, and retained legacy notes.
- Changed strategy records append a dated snapshot; earlier revisions remain available.
- Long-equity setup RRR is calculated deterministically from entry, target, and a numeric stop/invalidation price. Entry ranges are evaluated at both ends; the current RRR is shown separately using a cached quote.
- RRR is a planned scenario, not a guaranteed maximum loss. A stop trigger may execute at a different price. Missing or invalid inputs are shown as unavailable or invalid.
- Buy-zone conditions are checked against an already saved quote when a workspace is viewed. They do not place orders or fetch prices automatically.
- Quant Lab contains placeholders only. It does not load or modify an ML notebook or display invented model results.
- Existing manual market-risk review rules remain available from Market Home.

Market Home charts use locally cached adjusted prices only. The six broad-market ETFs are breadth proxies, not constituent-level advances/declines. Sector rotation is a return-based relative-price map, not a standard RRG or a forecast. A hidden seven-day recent-developments cache is populated only from dated results during an explicitly requested web-enabled full-market analysis; it stores short excerpts and source provenance, not full articles. Portfolio historical risk metrics use aligned Tiingo adjusted-close returns, cached quote weights when available, a dated adjusted-close fallback when not, and cash at zero return. Partial estimates exclude holdings without aligned history rather than treating them as cash. Sector classifications begin with a small local map, remain marked unverified until reviewed, and do not imply holdings look-through for diversified ETFs.

## SQLite and safe migrations

The local database is `data/investment_dashboard.db`. Startup migrations use `CREATE TABLE IF NOT EXISTS` and additive `ALTER TABLE` operations. Existing holdings and quote-cache rows are kept. Existing research notes remain in their original table and are copied into the structured strategy workspace, including the old valuation and entry-zone text. Strategy revision snapshots are append-only.

Keep the database private and back it up securely. `.gitignore` excludes `.env`, `.venv/`, Python caches, logs, Streamlit secrets, `data/`, SQLite files and sidecars, IDE state, and common private export locations/patterns. Ignore rules do not remove files already committed to Git; inspect both `git status` and tracked files before publishing.

## Project structure

```text
app.py                              App setup and four horizontal tabs
dashboard/database.py               Additive SQLite schema migrations
dashboard/data_access.py            SQLite persistence and workspace operations
dashboard/calculations.py           Portfolio valuation and P/L calculations
dashboard/portfolio_risk.py         Aligned historical beta, volatility, exposure, and RRR math
dashboard/sector_classification.py Local ticker mappings and unverified sector labels
dashboard/setup_calculations.py     Long-equity planned/current RRR calculations
dashboard/market_data.py             Alpha Vantage client and response parsing
dashboard/quote_service.py           Manual quote refresh, pacing, and cache use
dashboard/history_providers.py       Historical provider interface and Tiingo adapter
dashboard/market_history.py          Historical cache and refresh orchestration
dashboard/analytics.py               Dependency-free daily-price analytics
dashboard/cboe_volatility.py         Official Cboe daily VIX/VIX9D CSV client and volatility calculations
dashboard/vix_sources.py             Cboe/yfinance source precedence and completed-session validation
dashboard/treasury_yields.py          Official Treasury daily par-yield XML adapter
dashboard/market_explorer.py          ETF pair and configurable basket calculations
dashboard/gpt_market_prompts.py       Copyable research prompts; no AI calls
dashboard/fred_volatility.py         Incremental FRED VIXCLS adapter and persistent caching
dashboard/fred_macro.py              Optional historical FRED policy-rate and credit observations
dashboard/cross_market.py            Cached-date alignment, relative performance, breadth, and correlation calculations
dashboard/market_monitor_ui.py       Phase-one regime overview and cached cross-market explorer
dashboard/volatility_cycle.py        Deterministic VIX regime, daily momentum, and volatility-cycle rules
dashboard/event_calendar.py          Official calendar adapters, timezone conversion, refresh orchestration
dashboard/event_calendar_ui.py       Scrollable event timeline and compact catalyst importer
dashboard/event_import.py            Backward-compatible calendar import audit and reversible batch storage
dashboard/compact_event_import.py    Offline compact event parser, validation, and discovery prompt
dashboard/market_regime.py           Deterministic regime features and historical analog statistics
dashboard/navigation.py              Tab and workspace navigation state
dashboard/theme.py                   Shared typography, spacing, and Plotly defaults
dashboard/pages/home.py               Market visualizations, status, and manual AI briefing
dashboard/market_visuals.py            Pure chart transformations from cached prices
dashboard/ai_providers.py              Shared DeepSeek/OpenAI client and safe error translation
dashboard/ai_market_analyst.py         Cached snapshot builder and provider-neutral briefing flow
dashboard/ai_research.py               Strategy draft and workspace chat prompts
dashboard/ai_settings.py               Compact provider/model/token/context controls
dashboard/pages/portfolio.py          Portfolio health, exposure, position monitor, and risk attribution
dashboard/pages/research.py           Watchlist and ticker strategy workspace
dashboard/pages/quant_lab.py          Future model placeholders
tests/                               Persistence, history, navigation, and math tests
test_market_data.py                  Offline quote parser tests
```

## Tests and manual check

Run the offline tests (provider requests are mocked; no API keys are needed):

```bash
python -m unittest discover -s tests
python -m unittest test_market_data
```

## Refreshing data

- Use the quote refresh action on Market Home or Portfolio for Alpha Vantage quotes.
- Use **Refresh Market Data** at the top of Market for the daily workflow. Freshness is based on the most recent completed XNYS session in `America/New_York`, including holidays, early closes and DST; a recent request timestamp does not make an older observation current. Per-symbol Tiingo outcomes appear in the collapsed diagnostics table. Individual Tiingo, Cboe VIX, Treasury, calendar and optional FRED operations remain under **Data Sources & Diagnostics**. Page loads and chart changes only read SQLite. DeepSeek is not called unless **Run DeepSeek Analysis After Refresh** is selected or an AI action is explicitly clicked; identical successful snapshots reuse the cached report.
- Use **Refresh** in Upcoming Market Catalysts for BLS, BEA, Fed, and FEC schedules. The calendar UI displays all event times in Eastern Time, including daylight-saving changes.
- Generate AI briefings and optional Research discussions only with their explicit actions. Pasted strategy imports require no provider call. No provider is called on normal reruns.

Refreshes can fail because of provider quotas, credentials, network access, symbol coverage, or source schedule updates. Existing local cache data is retained and its dates/status should be checked before relying on it.

### Market Regime & Capital Rotation Monitor

The Market tab is organized as a seven-section research workstation: catalysts, market regime, combined sector heatmap and four-quadrant rotation, relative strength with defensive/cyclical rotation, rates and macro, VIX risk, and one integrated briefing. It retains the configurable pair explorer and basket settings, uses Tiingo split-and-dividend-adjusted ETF closes, and stores official Treasury observations separately in `treasury_yield_history`. TLT and GLD are ETF price proxies; USO is a futures-based oil ETF proxy and may diverge from spot WTI due to rolls, fees and tracking effects. These measures do not directly establish policy expectations, physical commodity demand, or capital flows.

Use **Refresh Treasury** to retrieve the U.S. Treasury daily par-yield feed and **Refresh VIX** for Cboe. The optional Yahoo VIX fallback remains unchecked by default. The collapsed **Data health** panel shows source coverage and retains optional FRED diagnostics. FRED credentials are read only for explicit refresh actions. Failed requests preserve cached observations. Ordinary reruns and chart interactions do not fetch data or call AI APIs; the explicit AI Market Research button does.

Treasury yields use the U.S. Treasury's documented [daily interest-rate XML feed](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed), requesting the daily par-yield curve and parsing the `BC_2YEAR` and `BC_10YEAR` fields. The same-date 10Y–2Y spread is calculated in Python. Yield levels remain in percent; changes and the curve spread are shown in basis points. Publication dates can lag the latest equity close, so the interface displays the Treasury observation date and freshness. These are Treasury par yields, not the effective federal funds rate or market-implied future Fed policy. The source is manually refreshed; cache rows are upserted without deleting prior observations.

The Treasury monitor defaults to 20 trading sessions and also offers 5D, 60D, and 120D. Stored yield levels use percentage-point values (`4.56` means 4.56%); a yield change in bp is `(latest - prior) * 100`. The curve is `(10Y - 2Y) * 100` bp, and its change is the latest same-date curve minus the matched earlier same-date curve. This avoids multiplying percentage-point differences by 10,000. Optional event studies compare the last observation before an event with the next observation after it; daily data cannot establish event timing or causation. Only official-source events marked confirmed are presented as sourced events; user-entered dates are labeled unverified.

Market Regime includes the persistent broad-index comparison chart for SPY/RSP/QQQ/DIA/IWM, with 5/20/60/120-session normalized return or excess-versus-SPY modes. The custom Relative Strength Explorer supports broad-market and standard sector ETFs, 5/20/60/120/252-session windows, cumulative excess return, normalized relative-price ratio, and two-line normalized performance. It aligns exact common adjusted-close dates and requires N+1 observations for N-session returns. Pair favorites persist locally. The cyclical/defensive basket is independently configurable from standard sector ETFs, with equal weights by default. Cross-market comparisons continue to use date-aligned cached observations. Rolling correlations, scatterplots and exploratory lead/lag are historical associations, not evidence of causation or validated trading signals. Optional FRED observations are aligned to a later SPY session where used to avoid assuming same-day release availability.

The **AI Market Briefing** is user-triggered. **Fast Briefing** uses the saved model selection (DeepSeek Flash by default). **Deep Research** explicitly requests `deepseek-v4-pro`; it stops with an account-access error rather than falling back if the API account does not list that model. **Dashboard Analysis** makes one synthesis request over the versioned local Market Intelligence snapshot and no web search. **Dashboard + Web Verification** uses DeepSeek Search followed by synthesis; successful sources are reused for synthesis-only retries. Snapshot v3 returns one highlight for each of six analytical sections, saved insights for the fixed 20-session comparison library, and one integrated briefing with up to three evidence-led hypotheses, all in a single model response. Market Regime includes SPY/RSP/QQQ/DIA/IWM matched-date comparisons across 5/20/60/120 sessions. Reports are cached by stable market-data fingerprint, analysis mode, model/reasoning settings, automatic output policy, and prompt version; changing a chart pair does not stale the full report. If a pair/window has no saved AI insight, its summary is explicitly labeled as a local calculation. The UI displays requested and API-reported model IDs, search status, source count, completion status, token usage, and estimated cost. Per-request/daily caps are checked before a request; estimates are not guaranteed charges. No private portfolio context is sent and no report is sent to Robinhood. See [Market Research controls and cost details](docs/deepseek-market-research.md) and [Market Intelligence snapshot methodology](docs/market-intelligence-workstation.md).

DeepSeek's current official [model-list API](https://api-docs.deepseek.com/api/list-models/) and [pricing page](https://api-docs.deepseek.com/quick_start/pricing/) advertise up to 393,216 output tokens and a 1M context window for the listed models. This app deliberately caps Market Research at 32,000 tokens. The shared AI settings sidebar's 8,192 cap is a separate local restriction for generic Chat Completions features; it is not the provider's maximum and does not control Market Research. On an explicit request, the app checks the account's `/models` metadata and rejects a request above the advertised per-model output/context limits. See [Market Research controls and cost details](docs/deepseek-market-research.md).

The VIX monitor shows one current close headline, separate latest-session and five-completed-session metrics, a recent OHLC candlestick chart, and collapsed descriptive statistics. The five-session peak-surge unwind uses the prior session's close as its reference and is separate from the count of individual-session spike reversals. Other context includes trailing z-score, log-return variability, recent momentum, and same-date SPY/sector observations. Signals may conflict; no risk score or market-bottom probability is produced. [VIX statistical signals and the future validation specification](docs/vix-statistical-research-spec.md) documents definitions, data limitations, and optional volatility data sources.

The fixed market-wide sector baseline is the standard 11 sector ETFs, independent of visible/custom symbols. Sector participation counts available sector ETFs above same-date 50D/200D averages (not constituent breadth); other metrics include 5D/20D/60D relative performance, return dispersion, and a top-three share of positive sector excess-return magnitude. Defensive/cyclical baskets are equal-weight: XLV/XLP/XLU versus XLY/XLI/XLB/XLE/XLF/XLK/XLC/XLRE. RSP/SPY, IWM/SPY, SMH/SPY, SMH/QQQ, and XLY/XLP are price-ratio return proxies. ETF price performance is not fund flows; HYG/LQD and HYG/IEF are credit-sensitive price proxies, not credit spreads.

Optional FRED series can be requested in the collapsed diagnostics. The FRED API requires `FRED_API_KEY`; FRED's [API terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html) and third-party series restrictions still apply. ICE BofA OAS observations remain optional historical diagnostics and are not included as primary Market-tab cards. Macro releases can be revised and provider vintage coverage may be incomplete, so strict point-in-time reconstruction is limited.

Phase 1 does not add a composite risk score, narrative hypotheses, company capex/financing metrics, or portfolio exposure narratives. Those require separate sourced evidence and are left for a later phase. No market-data request is made on ordinary Streamlit reruns, timeframe changes, or chart interactions.

## Known limitations and roadmap

The app is single-user and local-first; it has no login, brokerage connection, order execution, automatic earnings-date feed, or guaranteed real-time quotes. Historical and calendar coverage depends on their named providers. Sector labels are not verified look-through holdings data unless explicitly reviewed. ETF performance is not fund-flow data. Historical analogs and risk/regime conventions are descriptive and have not been validated as forecasts. Provider terms may restrict redistribution, so do not publish cached data or screenshots containing personal information.

Potential future work (not implemented): richer verified breadth sources, an optional documented earnings-calendar adapter, credit-spread context, and formal out-of-sample evaluation of regime and historical-analog features.

To verify the UI, launch the app and open each of the four tabs. Portfolio reads cached quotes and Tiingo adjusted closes only; it does not fetch data on reruns. Historical beta and volatility default to 252 aligned daily returns, use current quote weights where cached and a dated Tiingo close as a price fallback, and treat cash as a zero-return holding. Partial estimates identify included positions and do not assume missing holdings have zero returns. In the Portfolio tab, expand **Historical risk data coverage and refresh** to inspect per-symbol close counts and dates; use its button to fetch only deficient symbols or check alignment gaps. Use **Position asset and sector classifications** for multi-position edits. Local maps are marked unverified until reviewed; diversified ETF exposure remains separate unless you enter verified look-through classifications. Use **⋯ → Edit Strategy** on a holding to edit target and stop levels in the same revisioned record used by Research. These levels are planning inputs, not broker orders. The Portfolio simulator remains hypothetical and never saves position or strategy changes. Confirm ordinary reruns make no network request.

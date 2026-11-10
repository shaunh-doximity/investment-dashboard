# Market Intelligence workstation

The visible Market tab is organized into seven sections: Upcoming Market Catalysts; Market Regime Summary; Sector Rotation & Breadth; Relative Strength & Defensive Rotation; Rates & Macro; Volatility Risk Monitor; and one Integrated AI Market Briefing. The fixed standard sector universe remains the 11 S&P sector ETFs. The heatmap shows absolute return; the quadrant uses sector/SPY ratio return and the change in that ratio return. Neither calculation measures fund flows.

## Snapshot v3

`market-intelligence-snapshot-v3` is the canonical input for a full Market analysis. Its section IDs are `catalysts`, `regime`, `sector_rotation`, `relative_strength`, `rates_macro`, and `vix`. Values carry units, observation dates, source, window, universe, and calculation definition when available. Missing observations remain explicit gaps.

The regime section contains a compact matched-date matrix for SPY, RSP, QQQ, DIA and IWM versus SPY over 5/20/60/120 sessions, including cumulative returns, excess return, prior-window change, dates and coverage. DIA is in the existing manual historical refresh universe. User visibility and display-window choices are persisted as view preferences and omitted from the global data fingerprint.

The relative-strength section contains compact matched-date results for 5/20/60/120/252 sessions. The fixed comparison library covers each of the 11 sector ETFs against SPY, RSP/SPY, IWM/SPY, SMH/SPY, SMH/QQQ, and XLY/XLP. A saved comparison highlight is produced for each standard pair's 20-session window in the same model response as the section highlights and integrated briefing. Other pair/window selections use local calculations and are labeled as non-AI until a user submits an explicit section chat question.

The global fingerprint is based on measured section values and stable analysis configuration. Chart selections, selected pair, and chart windows are separate `view_state`; changing them does not stale or miss the full-market analysis cache. This means a different chart view may have no matching saved comparison highlight, even while the rest of the report remains current.

## AI contract and limits

The full analysis is one explicit DeepSeek request. Its v3 response contains one highlight per visible section, a comparison-highlight array covering the fixed 20-session relative-strength library, and one briefing with at most three evidence-led hypotheses. Each hypothesis includes supporting evidence, a competing explanation, a confirmation/invalidation condition, sector implications, and exact local metric references. Validation rejects unknown section/comparison IDs and references to metrics absent from the snapshot.

Highlights should explain what a measured relationship could mean, why it matters for risk or rotation, and what to monitor. They should not repeat every displayed number or turn price divergence into a claim about capital flows or causation. For TLT, GLD, and USO, price movements are market proxies only; several interpretations may fit the same move. Catalyst highlights prioritize the nearest market-wide events and describe which hypotheses they could test, without predicting outcomes.

Legacy reports remain stored unchanged. Older section highlights are remapped for display where meanings are compatible; merged/renamed section chats stay retained under their original section IDs and are available in the section's collapsed earlier-conversation history. Removed technology-capital-cycle content is retained in the database but has no current visible section.

No AI call occurs on navigation, selector changes, chart interaction, or ordinary reruns. Full analysis, section chat, and optional web verification require explicit user action. AI interpretations are not forecasts, portfolio instructions, or trade recommendations.

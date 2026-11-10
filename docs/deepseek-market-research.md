# DeepSeek Market Research

Market research is explicit-action only. Ordinary Streamlit reruns, mode changes, and saved-report viewing do not call DeepSeek. The app excludes holdings, account details, cash, and other private portfolio data from the market research snapshot.

## Market tab controls

| Mode | Requested model | Reasoning | Output allowance |
| --- | --- | --- | --- |
| Fast Briefing | Saved Fast Briefing model selection (`deepseek-flash` by default; `deepseek-v4-pro` if explicitly selected) | `none` | Automatic 12,000-token allowance |
| Deep Research | `deepseek-v4-pro` | `high` | Automatic 48,000-token allowance |
| One explicit synthesis retry | Same model as the original run | Same mode | Fast: 24,000; Deep: 64,000 tokens, only when prior output was incomplete and cached sources exist |

Deep Research streams progress without displaying or saving hidden reasoning. The requested model and the model ID returned in the API response are recorded separately. If the API does not report an actual model ID, the report says “not reported”; it does not infer a model from account billing records.

The Advanced Settings model selector applies to Fast Briefing and section chats. Full-market Deep Research explicitly requests `deepseek-v4-pro` regardless of that selection; it does not silently fall back. Before a request, the API account's `/models` list is checked. If V4 Pro is unavailable to the account, the run stops before synthesis and reports that exact access limitation. In Dashboard + Web Verification, the app first calls DeepSeek's dedicated Search API and then synthesizes retrieved results with V4 Pro; Dashboard Analysis uses V4 Pro synthesis without web search. Section chats continue to use the saved model selection. The collapsed **AI Usage & Diagnostics** panel reads a local request-audit table containing purpose, requested/returned model, endpoint path, reasoning mode, output allowance, token counts, search count, estimated cost, provider-reported cost when supplied, completion status, and error category. It does not retain prompts, secrets, or portfolio data. Cost estimates are not invoices; DeepSeek may not return a cost field.

The Search API accepts `query` and `ori_question` values of 1–128 characters. The app now normalizes whitespace and bounds both fields before sending; an overlong section question previously caused HTTP 400. A valid HTTP 200 search response is recorded separately from the subsequent model synthesis, so a successful search does not imply V4 Pro was used unless that requested/returned model appears in diagnostics.

Ask AI is instructed to answer conceptual questions directly when a plausible general mechanism is available, compare competing hypotheses, connect them to dated dashboard observations, and identify measurements that would distinguish them. Dashboard Analysis can explain general mechanisms while marking event-specific facts unverified. Web mode retrieves sources only after the user submits a question, cites dates and URLs, and does not treat temporal association as causality. Conversation history is collapsed by default in each section highlight.

The Market tab offers two research modes. **Dashboard Analysis** sends one Responses API synthesis request using the local canonical snapshot and makes no search request. **Dashboard + Web Verification** retrieves a small set of sources with DeepSeek Search, then synthesizes them with the same snapshot. A failed or truncated synthesis preserves successful source results. **Retry synthesis** reuses those results rather than repeating the search. Completed briefings are cached by snapshot fingerprint, requested model, analysis mode, reasoning mode, output allowance, and prompt version. Chart selectors are `view_state`, so changing them does not alter the global fingerprint or full-market cache key.

One response must contain six current section highlights, 20-session highlights for every standard comparison, and the integrated briefing with up to three evidence-led hypotheses. It is validated against `market-analyst-response-v3`. The prompt supplies both the full finite metric-ID registry and per-section registries to reduce guessed references; validation reports exact invalid paths/IDs and logs sanitized diagnostics. Visible malformed output is retained only in a failed report's diagnostic usage record, never as a completed briefing. If an attempt fails after a prior success, it is saved under a separate failed-attempt key so the earlier completed report stays intact. External citations must map to retrieved source IDs. Legacy report/chat records remain retained; the current Market tab presents one authoritative briefing. Deterministic local indicators remain visible when no valid completed briefing matches the current snapshot.

`market-intelligence-snapshot-v3` contains upcoming events with separate user-confirmation and source-verification fields, a fixed public benchmark earnings subset, and up to 24 dated recent developments sourced only during an explicit web-enabled full-market analysis; rule-based regime indicators; the broad index matrix; all-11-sector returns, breadth and quadrant metrics; configurable basket results; standard relative comparisons; Treasury yields, TLT/GLD/USO adjusted ETF proxies, official-event reactions, and Cboe VIX measures/statistics. USO is a futures-based oil ETF proxy, not WTI spot. Private portfolio records and credentials are excluded. The stable fingerprint includes market observations and any active catalyst-cache records, but not chart selectors. The separate view fingerprint is for selected charts/pairs only.

Before an explicit request, the app estimates the full output allowance at peak documented rates and includes a conservative input/search reserve. The existing per-request and daily caps block requests whose estimated ceiling does not fit. Estimates are not guaranteed charges; actual token and search usage is displayed when supplied by the API. A larger token ceiling may still be insufficient if reasoning consumes most of it; incomplete/reasoning-only responses are never shown as completed briefings.

## Why another AI control says 8,192

The shared AI settings sidebar has a local maximum of 8,192 tokens in `dashboard/ai_settings.py`, and `dashboard/ai_providers.py` enforces the same bound for its separate Chat Completions generation path. That is an application-level limit for those generic AI features; it is not DeepSeek's documented model maximum and it is not the Market Research Responses allowance. This milestone leaves that shared control unchanged to avoid altering Research and other tabs. The Market tab has a 64,000-token local ceiling and automatically chooses 12,000 for Fast Briefing, 48,000 for Deep Research, and at most one larger explicit synthesis-only retry.

DeepSeek's current official model list exposes a per-model `max_output_tokens` field. Its published model metadata and pricing page currently list a maximum of 393,216 output tokens and a 1M context window for `deepseek-flash` and `deepseek-v4-pro`. The application deliberately caps Market Research at 64,000 tokens for bounded output and cost control, although the published endpoint maximum is higher. The account-specific `/models` metadata is checked on each explicit analysis request before search or synthesis. On an explicitly requested run, it checks `/models` and rejects an allowance above the account/model's advertised output limit or estimated context capacity before starting a search.

DeepSeek documents `max_output_tokens` for Responses and `max_tokens` for Chat Completions. In either mode, reasoning consumes the generation allowance; Responses usage breaks output usage out into `reasoning_tokens`. The app uses Responses for Market Research, not the generic Chat Completions path.

## Cost reference

The peak rates used for a conservative estimate are currently Flash input $0.30 / million cache-miss tokens and output $1.20 / million tokens. DeepSeek Search and Fetch are currently documented at $0.75 per 1,000 successful operations. As an illustration, 64,000 output tokens alone would be estimated at $0.0768 at the peak output rate, before input tokens and search. Thus a $0.10 per-request cap can admit some 64,000-token requests but may block others depending on the estimated input reserve; the cap is never bypassed. Prices and billing rules can change, so the API's pricing page is authoritative.

## Model extensibility

This feature currently uses DeepSeek only. Its explicit `run_market_research` boundary and public snapshot interface can later be wrapped by separate provider adapters, but OpenAI and Anthropic/Claude Market Research adapters are not implemented here.

## Official documentation checked 2026-11-02

- [DeepSeek Responses API guide](https://api-docs.deepseek.com/guides/responses_api/) documents supported models, `reasoning.effort`, streaming completion events, usage breakdown, and the fact that Responses `web_search` is supported with V4 Pro but ignored with Flash.
- [DeepSeek Responses API reference](https://api-docs.deepseek.com/api/create-response/) documents `max_output_tokens`, response status, and separate reasoning-token usage.
- [DeepSeek model list](https://api-docs.deepseek.com/api/list-models/) documents per-model context and output limits, including `max_output_tokens` metadata.
- [DeepSeek Chat Completions reference](https://api-docs.deepseek.com/api/create-chat-completion/) documents `max_tokens`, context limits, `finish_reason="length"`, and reasoning-token usage.
- [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing/) lists current model versions, maximum output, token pricing, and Search/Fetch charges.

Prices/model routes can change. The app reports the API response's model ID rather than relying on a usage-dashboard label.


## Automatic token policy

The former 2,500-token UI override truncated the response contract: Fast Briefing must return six section highlights, 20-session highlights for the full standard comparison set, and one integrated briefing. Fast now requests 12,000 output tokens with reasoning disabled. Deep Research requests 48,000 tokens with high reasoning; because reasoning and visible text share `max_output_tokens`, the budget is intentionally much larger. A single user-triggered retry may raise Fast to 24,000 or Deep to 64,000 tokens and reuses stored search sources. It rechecks per-request and remaining daily budget and `/models` output/context metadata before synthesis. No automatic retry runs.
